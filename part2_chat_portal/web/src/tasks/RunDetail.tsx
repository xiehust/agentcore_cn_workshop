// 单次运行的详情：状态头 / 浏览器实况 / 进度时间线 / 关键日志 / 统计卡 / 明细表。
// 运行中每 3 秒轮询 run 与 events（events 用 after_seq 游标增量追加），终态停轮询。
import {
  Component,
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowLeftIcon,
  CaretLeftIcon,
  CaretRightIcon,
  ClockIcon,
  DatabaseIcon,
  FlagIcon,
  GlobeIcon,
  TerminalWindowIcon,
  WarningCircleIcon,
  WrenchIcon,
} from "@phosphor-icons/react";
import {
  formatClock,
  formatDuration,
  formatTs,
  getRun,
  getRunEvents,
  getRunResults,
  isRunActive,
  runDurationSeconds,
  type ResultTable,
  type ResultsPage,
  type Run,
  type RunEvent,
} from "./api";
import { RunBadge } from "./StatusBadge";

// DCV Web SDK 有 2MB+，懒加载别拖累首屏
const LiveView = lazy(() => import("./LiveView"));

// lazy chunk 加载失败（DCV SDK 拉不下来/被网络拦）时 Suspense 只会一直卡在
// fallback 的「加载实况组件…」上，必须用 error boundary 接住并给出出路
class LiveViewBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed) {
      return (
        <div className="live-hint">
          <WarningCircleIcon size={16} />
          实况组件加载失败（DCV 播放器资源未能拉取，常见于网络受限环境）。
          <button className="ghost-btn tiny" onClick={() => this.setState({ failed: false })}>
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const PAGE_SIZE = 20;

// Part 1 的 save_verdict 只收 PASS / 排除 / 补采 / 采集 四种值（见 SKILL.md）。
// 判定值直接按原文展示——它就是 workshop 教的那套词汇，也是数仓里的实际取值；
// 只挂 tooltip 解释语义。特别注意 PASS 是「没过 R1 硬门槛、已丢弃」而不是「通过」，
// 真正通过的是「采集」，别给 PASS 配任何"通过"字样的标签。
const VERDICT_HINT: Record<string, string> = {
  PASS: "未过 R1 硬门槛（价格/月销），已丢弃",
  排除: "踩到硬门槛或负向黑名单，丢弃并记录原因",
  补采: "数据缺失或抓取失败，标记待补，不当真值",
  采集: "过了所有门槛，已产出完整候选档案",
};

export default function RunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);
  const finalizedRef = useRef(false);
  // 上一次轮询没回来就跳过这一拍：游标要等响应到达才前移，并发请求会拉到重复事件
  const inFlightRef = useRef(false);

  const poll = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const [runResp, eventResp] = await Promise.all([
        getRun(runId),
        getRunEvents(runId, seqRef.current),
      ]);
      setRun(runResp.run);
      if (eventResp.events.length > 0) {
        seqRef.current = Math.max(seqRef.current, ...eventResp.events.map((e) => e.seq));
        // 按 seq 去重兜底（后端重发同一条也不会重复渲染）
        setEvents((prev) => {
          const seen = new Set(prev.map((e) => e.seq));
          const fresh = eventResp.events.filter((e) => !seen.has(e.seq));
          return fresh.length > 0 ? [...prev, ...fresh] : prev;
        });
      }
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      inFlightRef.current = false;
    }
  }, [runId]);

  // 换 run 时重置游标与已累积事件
  useEffect(() => {
    seqRef.current = 0;
    finalizedRef.current = false;
    setEvents([]);
    setRun(null);
  }, [runId]);

  const stopped = run !== null && !isRunActive(run.status);

  useEffect(() => {
    if (stopped) return;
    void poll();
    const id = setInterval(() => void poll(), 3000);
    return () => clearInterval(id);
  }, [poll, stopped]);

  // 进终态时再补一次，确保收尾的 result 事件和 stats 都拿到
  useEffect(() => {
    if (!stopped || finalizedRef.current) return;
    finalizedRef.current = true;
    void poll();
  }, [stopped, poll]);

  // 收尾统计 SQL 抛异常（如 Aurora 恰好冷暂停）时 run 仍是 succeeded，但统计数字不可信
  const statsBroken = !!run?.stats?.stats_error;
  const timeline = events.filter((e) => e.kind !== "log");
  const logs = events.filter((e) => e.kind === "log");
  const running = run?.status === "running";
  // 浏览器会话起来之后才有实况可看
  const browserReady = !!run?.browser_session_id || events.some((e) => e.kind === "browser");

  return (
    <>
      <header className="tasks-head">
        <div>
          <button className="back-btn" onClick={onBack}>
            <ArrowLeftIcon size={14} />
            返回
          </button>
          <h1>
            运行 <span className="mono">{runId.slice(0, 8)}</span>
            {run && <RunBadge status={run.status} />}
          </h1>
          {run && (
            <p className="run-meta mono">
              <ClockIcon size={13} />
              {formatTs(run.started_at)} → {run.finished_at ? formatTs(run.finished_at) : "进行中"}
              {" · 耗时 "}
              {formatDuration(runDurationSeconds(run))}
            </p>
          )}
          {run?.status === "running" && (
            <span className="thinking">
              摄入进行中<span className="dots" />
            </span>
          )}
        </div>
      </header>

      {error && <div className="alert">{error}</div>}
      {run?.error && <div className="alert">运行失败：{run.error}</div>}
      {/* 聚合统计失败：下面的统计卡数字不可信，明确说出来 */}
      {run?.stats?.stats_error && (
        <div className="alert">统计聚合失败，下方数字可能不准：{run.stats.stats_error}</div>
      )}

      <div className="tasks-body run-detail">
        {running && browserReady && (
          <section className="panel-card">
            <div className="panel-title">
              <TerminalWindowIcon size={15} />
              云端浏览器实况
            </div>
            <LiveViewBoundary>
              <Suspense
                fallback={
                  <div className="thinking">
                    加载实况组件<span className="dots" />
                  </div>
                }
              >
                <LiveView runId={runId} />
              </Suspense>
            </LiveViewBoundary>
          </section>
        )}

        <div className="run-grid">
          <section className="panel-card">
            <div className="panel-title">
              <FlagIcon size={15} />
              进度时间线
            </div>
            {timeline.length === 0 ? (
              <div className="cell-muted">等待 Agent 上报第一个阶段…</div>
            ) : (
              <ol className="timeline">
                {timeline.map((e) => (
                  <li key={e.seq} className={e.kind === "error" ? "danger" : ""}>
                    <span className="tl-icon">{eventIcon(e.kind)}</span>
                    <span className="tl-text">{eventText(e)}</span>
                    <span className="tl-time mono">{formatClock(e.created_at)}</span>
                  </li>
                ))}
              </ol>
            )}
          </section>

          <LogPane logs={logs} live={running} />
        </div>

        {run?.stats && (
          <section className="stat-row">
            <div className="chart-card stat-card">
              <div className="chart-title">候选商品</div>
              {/* 聚合失败时后端会给 candidates=0 / verdicts={}，那是「没统计到」不是「真的是 0」，
                  显示 — 而不是 0，免得跟一次真的没抓到东西的运行混淆 */}
              <div className="stat-value mono">
                {statsBroken ? "—" : run.stats.candidates ?? 0}
              </div>
              <div className="stat-sub">
                {statsBroken ? "统计未取到，看下方明细" : "本次写入 product_candidates"}
              </div>
            </div>
            <div className="chart-card stat-card">
              <div className="chart-title">判定分布</div>
              {run.stats.verdicts && Object.keys(run.stats.verdicts).length > 0 ? (
                <ul className="verdict-list">
                  {Object.entries(run.stats.verdicts).map(([k, v]) => (
                    <li key={k} title={VERDICT_HINT[k] ?? k}>
                      <span>{k}</span>
                      <span className="mono">{v}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="stat-value mono">{statsBroken ? "—" : 0}</div>
              )}
              <div className="stat-sub">
                {statsBroken ? "统计未取到，看下方明细" : "本次写入 selection_verdicts"}
              </div>
            </div>
            <div className="chart-card stat-card">
              <div className="chart-title">耗时</div>
              <div className="stat-value mono">{formatDuration(run.stats.duration_s)}</div>
              <div className="stat-sub">从触发到收尾</div>
            </div>
          </section>
        )}

        {run && <ResultTabs runId={runId} runStatus={run.status} />}
      </div>
    </>
  );
}

function eventIcon(kind: RunEvent["kind"]) {
  switch (kind) {
    case "tool":
      return <WrenchIcon size={13} />;
    case "browser":
      return <GlobeIcon size={13} />;
    case "result":
      return <DatabaseIcon size={13} />;
    case "error":
      return <WarningCircleIcon size={13} />;
    default:
      return <FlagIcon size={13} />;
  }
}

/** 事件文案：优先用后端给的 message，缺失时从 payload 里凑一个可读描述。 */
function eventText(e: RunEvent): string {
  if (e.message) return e.message;
  const name = e.payload?.name;
  if (typeof name === "string") return `调用工具 ${name}`;
  const sid = e.payload?.session_id;
  if (typeof sid === "string") return `浏览器会话 ${sid.slice(0, 12)}`;
  return e.kind;
}

/* ================= 关键日志 ================= */

function LogPane({ logs, live }: { logs: RunEvent[]; live: boolean }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);

  // 跟随滚底；用户手动上滚后暂停跟随，滚回底部恢复
  useEffect(() => {
    if (!follow) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logs.length, follow]);

  function onScroll() {
    const el = boxRef.current;
    if (!el) return;
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
  }

  return (
    <section className="panel-card">
      <div className="panel-title">
        <TerminalWindowIcon size={15} />
        关键日志
        {!follow && (
          <button className="ghost-btn tiny" onClick={() => setFollow(true)}>
            回到底部
          </button>
        )}
      </div>
      <div className="log-box mono" ref={boxRef} onScroll={onScroll}>
        {logs.length === 0 ? (
          <span className="cell-muted">暂无日志</span>
        ) : (
          logs.map((e) => (
            <div key={e.seq} className="log-line">
              <span className="log-time">{formatClock(e.created_at)}</span>
              <span>{e.message}</span>
            </div>
          ))
        )}
        {live && logs.length > 0 && <span className="cursor" />}
      </div>
    </section>
  );
}

/* ================= 明细表 ================= */

function ResultTabs({ runId, runStatus }: { runId: string; runStatus: Run["status"] }) {
  // table 与 page 放一个 state，切 tab 时同步回第一页，避免多发一次请求
  const [{ table, page }, setView] = useState<{ table: ResultTable; page: number }>({
    table: "candidates",
    page: 1,
  });
  const [data, setData] = useState<ResultsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const setPage = (next: (p: number) => number) =>
    setView((v) => ({ ...v, page: next(v.page) }));

  // runStatus 进入终态时重新拉一次，把收尾写入的记录带出来
  useEffect(() => {
    let alive = true;
    setData(null);
    getRunResults(runId, table, page, PAGE_SIZE)
      .then((d) => {
        if (alive) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [runId, table, page, runStatus]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / (data.page_size || PAGE_SIZE))) : 1;
  const columns = data && data.rows.length > 0 ? Object.keys(data.rows[0]) : [];

  return (
    <section className="panel-card">
      <div className="panel-title">
        <DatabaseIcon size={15} />
        本次入库明细
        <div className="seg tiny">
          <button
            className={table === "candidates" ? "active" : ""}
            onClick={() => setView({ table: "candidates", page: 1 })}
          >
            候选商品
          </button>
          <button
            className={table === "verdicts" ? "active" : ""}
            onClick={() => setView({ table: "verdicts", page: 1 })}
          >
            选品判定
          </button>
        </div>
      </div>

      {error && <div className="alert">{error}</div>}
      {!error && data === null && (
        <div className="thinking">
          加载明细<span className="dots" />
        </div>
      )}
      {data && data.rows.length === 0 && <div className="cell-muted">本次运行没有写入记录。</div>}
      {data && data.rows.length > 0 && (
        <>
          <div className="data-table scroll-x">
            <table>
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row, i) => (
                  <tr key={i}>
                    {columns.map((c) => (
                      <td key={c} title={cellText(row[c])}>
                        {cellText(row[c])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <button
              className="icon-btn"
              disabled={page <= 1}
              aria-label="上一页"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <CaretLeftIcon size={14} />
            </button>
            <span className="mono">
              {page} / {totalPages} · 共 {data.total} 条
            </span>
            <button
              className="icon-btn"
              disabled={page >= totalPages}
              aria-label="下一页"
              onClick={() => setPage((p) => p + 1)}
            >
              <CaretRightIcon size={14} />
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function cellText(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
