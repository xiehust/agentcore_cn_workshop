// 数据摄入任务管理视图：任务列表 → 任务详情（运行历史）→ 运行详情。
// 不用 router，靠本组件内的 route state 切子视图。
import { useCallback, useEffect, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import {
  ArrowLeftIcon,
  ArrowsClockwiseIcon,
  CaretDownIcon,
  CheckIcon,
  ListChecksIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  TimerIcon,
  TrashIcon,
  XIcon,
} from "@phosphor-icons/react";
import {
  createTask,
  deleteTask,
  describeSchedule,
  formatTs,
  getTask,
  isRunActive,
  listTasks,
  pauseTask,
  resumeTask,
  runTask,
  type Run,
  type Task,
  type TaskMode,
  type TaskSummary,
} from "./api";
import { RunBadge, TaskBadge } from "./StatusBadge";
import RunDetail from "./RunDetail";

type Route =
  | { kind: "list" }
  | { kind: "task"; taskId: string }
  | { kind: "run"; runId: string; back: Route };

export default function TasksView() {
  const [route, setRoute] = useState<Route>({ kind: "list" });

  return (
    <main className="tasks">
      {route.kind === "list" && (
        <TaskList
          onOpenTask={(taskId) => setRoute({ kind: "task", taskId })}
          onOpenRun={(runId) => setRoute({ kind: "run", runId, back: { kind: "list" } })}
        />
      )}
      {route.kind === "task" && (
        <TaskDetail
          taskId={route.taskId}
          onBack={() => setRoute({ kind: "list" })}
          onOpenRun={(runId) =>
            setRoute({ kind: "run", runId, back: { kind: "task", taskId: route.taskId } })
          }
        />
      )}
      {route.kind === "run" && (
        <RunDetail runId={route.runId} onBack={() => setRoute(route.back)} />
      )}
    </main>
  );
}

/**
 * 列表/详情共用的轮询节奏：有运行在跑时 3 秒，否则 10 秒。
 * active 翻转会重建定时器并立刻拉一次，属预期（状态刚变时正需要及时刷新）。
 */
function usePolling(refresh: () => void, active: boolean) {
  useEffect(() => {
    refresh();
    const id = setInterval(refresh, active ? 3000 : 10000);
    return () => clearInterval(id);
  }, [refresh, active]);
}

/* ================= 任务列表 ================= */

function TaskList({
  onOpenTask,
  onOpenRun,
}: {
  onOpenTask: (taskId: string) => void;
  onOpenRun: (runId: string) => void;
}) {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [acting, setActing] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listTasks()
      .then((d) => {
        setTasks(d.tasks);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const hasActive = !!tasks?.some((t) => isRunActive(t.last_run?.status));
  usePolling(refresh, hasActive);

  async function act(taskId: string, fn: () => Promise<unknown>) {
    setActing(taskId);
    try {
      await fn();
      refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActing(null);
    }
  }

  function remove(t: TaskSummary) {
    if (!window.confirm(`删除任务「${t.name}」？历史运行记录会保留，定期调度会一并撤销。`)) return;
    void act(t.task_id, () => deleteTask(t.task_id));
  }

  return (
    <>
      <header className="tasks-head">
        <div>
          <h1>
            <ListChecksIcon size={19} />
            数据摄入任务
          </h1>
          <p>在门户里创建、调度和复盘 Part 1 的选品摄入任务，不必回命令行。</p>
        </div>
        <div className="head-actions">
          <button className="ghost-btn" onClick={refresh} title="立即刷新">
            <ArrowsClockwiseIcon size={15} />
          </button>
          <button className="accent-btn" onClick={() => setCreating(true)}>
            <PlusIcon size={15} weight="bold" />
            新建任务
          </button>
        </div>
      </header>

      {error && <div className="alert">{error}</div>}

      <div className="tasks-body">
        {tasks === null && !error && (
          <div className="thinking">
            加载任务列表<span className="dots" />
          </div>
        )}
        {tasks !== null && tasks.length === 0 && (
          <div className="task-empty">
            <div className="empty-mark">摄</div>
            <h2>还没有摄入任务</h2>
            <p>新建一个一次性任务立即抓一批候选商品，或者配成定期任务每天自动跑。</p>
            <button className="accent-btn" onClick={() => setCreating(true)}>
              <PlusIcon size={15} weight="bold" />
              新建任务
            </button>
          </div>
        )}
        {tasks !== null && tasks.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th>任务</th>
                  <th>调度</th>
                  <th>状态</th>
                  <th>最近一次运行</th>
                  <th className="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.task_id}>
                    <td>
                      <button className="link-cell" onClick={() => onOpenTask(t.task_id)}>
                        {t.name}
                      </button>
                      <span className="cell-sub">
                        <span className="country-tag">{t.country}</span>
                        {t.prompt}
                      </span>
                    </td>
                    <td>
                      {t.mode === "one_time" ? "一次性" : "定期"}
                      {t.mode === "recurring" && (
                        <span className="cell-sub mono">{describeSchedule(t.schedule_expr)}</span>
                      )}
                    </td>
                    <td>
                      <TaskBadge status={t.status} />
                    </td>
                    <td>
                      {t.last_run ? (
                        <button className="link-cell" onClick={() => onOpenRun(t.last_run!.run_id)}>
                          <RunBadge status={t.last_run.status} />
                          <span className="cell-sub">{formatTs(t.last_run.started_at)}</span>
                        </button>
                      ) : (
                        <span className="cell-muted">未运行</span>
                      )}
                    </td>
                    <td className="col-actions">
                      <button
                        className="icon-btn"
                        title="立即运行一次"
                        disabled={acting === t.task_id}
                        onClick={() => void act(t.task_id, () => runTask(t.task_id))}
                      >
                        <PlayIcon size={15} />
                      </button>
                      {t.mode === "recurring" && (
                        <button
                          className="icon-btn"
                          title={t.status === "paused" ? "恢复调度" : "暂停调度"}
                          disabled={acting === t.task_id}
                          onClick={() =>
                            void act(t.task_id, () =>
                              t.status === "paused" ? resumeTask(t.task_id) : pauseTask(t.task_id),
                            )
                          }
                        >
                          {/* 恢复用定时器图标，跟左边「立即运行」的三角区分开 */}
                          {t.status === "paused" ? <TimerIcon size={15} /> : <PauseIcon size={15} />}
                        </button>
                      )}
                      <button
                        className="icon-btn danger"
                        title="删除任务"
                        disabled={acting === t.task_id}
                        onClick={() => remove(t)}
                      >
                        <TrashIcon size={15} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {creating && (
        <NewTaskPanel
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            refresh();
          }}
        />
      )}
    </>
  );
}

/* ================= 新建任务面板 ================= */

type SchedulePreset = "hours" | "daily" | "custom";
const COUNTRY_OPTIONS = ["UK", "US"] as const;
const COUNTRY_LISTBOX_ID = "task-country-options";

/** 把周期预设翻成 EventBridge Scheduler 表达式。 */
function buildScheduleExpr(
  preset: SchedulePreset,
  hours: number,
  time: string,
  custom: string,
): string {
  if (preset === "hours") {
    const n = Math.max(1, Math.floor(hours));
    return `rate(${n} ${n === 1 ? "hour" : "hours"})`;
  }
  if (preset === "daily") {
    const [h, m] = time.split(":");
    return `cron(${Number(m) || 0} ${Number(h) || 0} * * ? *)`;
  }
  return custom.trim();
}

function NewTaskPanel({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [country, setCountry] = useState("UK");
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<TaskMode>("one_time");
  const [preset, setPreset] = useState<SchedulePreset>("daily");
  const [hours, setHours] = useState(6);
  const [time, setTime] = useState("09:00");
  const [custom, setCustom] = useState("");
  const [tz, setTz] = useState("Asia/Shanghai");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [countryMenuOpen, setCountryMenuOpen] = useState(false);
  const [countryActiveIndex, setCountryActiveIndex] = useState(-1);
  const countryInputRef = useRef<HTMLInputElement>(null);

  const expr = buildScheduleExpr(preset, hours, time, custom);

  function openCountryMenu() {
    setCountryMenuOpen(true);
    setCountryActiveIndex(COUNTRY_OPTIONS.findIndex((option) => option === country));
  }

  function closeCountryMenu() {
    setCountryMenuOpen(false);
    setCountryActiveIndex(-1);
  }

  function selectCountry(option: (typeof COUNTRY_OPTIONS)[number]) {
    setCountry(option);
    closeCountryMenu();
    countryInputRef.current?.focus();
  }

  function handleCountryKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setCountryMenuOpen(true);
      setCountryActiveIndex((current) => {
        if (current < 0) return direction > 0 ? 0 : COUNTRY_OPTIONS.length - 1;
        return (current + direction + COUNTRY_OPTIONS.length) % COUNTRY_OPTIONS.length;
      });
      return;
    }

    if (event.key === "Enter" && countryMenuOpen && countryActiveIndex >= 0) {
      event.preventDefault();
      selectCountry(COUNTRY_OPTIONS[countryActiveIndex]);
      return;
    }

    if (event.key === "Escape" && countryMenuOpen) {
      event.preventDefault();
      closeCountryMenu();
    }
  }

  function handleCountryBlur(event: FocusEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      closeCountryMenu();
    }
  }

  async function submit() {
    const normalizedCountry = country.trim();
    if (!name.trim() || !normalizedCountry || !prompt.trim()) {
      setError("任务名称、国家/站点和摄入指令都要填。");
      return;
    }
    if (mode === "recurring" && !expr) {
      setError("请填写调度表达式。");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await createTask({
        name: name.trim(),
        country: normalizedCountry,
        prompt: prompt.trim(),
        mode,
        // rate(...) 不看时区，只有 cron / 自定义才带上
        ...(mode === "recurring"
          ? { schedule_expr: expr, ...(preset === "hours" ? {} : { schedule_tz: tz }) }
          : {}),
      });
      onCreated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="drawer-mask" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h2>新建摄入任务</h2>
          <button className="icon-btn" aria-label="关闭" onClick={onClose}>
            <XIcon size={16} />
          </button>
        </header>

        <div className="drawer-body">
          <label className="field">
            <span className="field-label">任务名称</span>
            <input
              value={name}
              placeholder="如：家居收纳选品扫描"
              onChange={(e) => setName(e.target.value)}
            />
          </label>

          <div className="field">
            <label className="field-label" htmlFor="task-country-input">
              国家 / 站点
            </label>
            <div className="country-combobox" onBlur={handleCountryBlur}>
              <div className="country-combobox-input">
                <input
                  ref={countryInputRef}
                  id="task-country-input"
                  role="combobox"
                  value={country}
                  maxLength={64}
                  autoComplete="off"
                  aria-autocomplete="list"
                  aria-expanded={countryMenuOpen}
                  aria-controls={COUNTRY_LISTBOX_ID}
                  aria-activedescendant={
                    countryMenuOpen && countryActiveIndex >= 0
                      ? `${COUNTRY_LISTBOX_ID}-${countryActiveIndex}`
                      : undefined
                  }
                  aria-describedby="task-country-hint"
                  onFocus={openCountryMenu}
                  onClick={openCountryMenu}
                  onChange={(e) => {
                    const value = e.target.value;
                    setCountry(value);
                    setCountryMenuOpen(true);
                    setCountryActiveIndex(COUNTRY_OPTIONS.findIndex((option) => option === value));
                  }}
                  onKeyDown={handleCountryKeyDown}
                />
                <button
                  type="button"
                  className={countryMenuOpen ? "country-combobox-toggle open" : "country-combobox-toggle"}
                  aria-label="展开国家 / 站点选项"
                  aria-controls={COUNTRY_LISTBOX_ID}
                  aria-expanded={countryMenuOpen}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    openCountryMenu();
                    countryInputRef.current?.focus();
                  }}
                >
                  <CaretDownIcon size={16} />
                </button>
              </div>
              {countryMenuOpen && (
                <div id={COUNTRY_LISTBOX_ID} className="country-combobox-menu" role="listbox">
                  {COUNTRY_OPTIONS.map((option, index) => {
                    const selected = option === country;
                    return (
                      <button
                        key={option}
                        id={`${COUNTRY_LISTBOX_ID}-${index}`}
                        type="button"
                        role="option"
                        tabIndex={-1}
                        aria-selected={selected}
                        className={[
                          "country-combobox-option",
                          index === countryActiveIndex ? "active" : "",
                          selected ? "selected" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        onMouseDown={(e) => e.preventDefault()}
                        onMouseEnter={() => setCountryActiveIndex(index)}
                        onClick={() => selectCountry(option)}
                      >
                        <span>{option}</span>
                        <span className="country-combobox-check" aria-hidden="true">
                          {selected && <CheckIcon size={15} weight="bold" />}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            <span id="task-country-hint" className="field-hint">
              可输入其他国家；运行前需有支持该值的 Part 1 Skill。
            </span>
          </div>

          <label className="field">
            <span className="field-label">摄入指令</span>
            <textarea
              value={prompt}
              rows={5}
              placeholder="给 Agent 的自然语言指令，如：在 eBay 上找便携咖啡机相关的候选商品，按选品 SOP 判定并入库"
              onChange={(e) => setPrompt(e.target.value)}
            />
            <span className="field-hint">单次运行约 2~5 分钟，会调 Part 1 的摄入 Agent。</span>
          </label>

          <div className="field">
            <span className="field-label">调度模式</span>
            <div className="radio-row">
              <label className={mode === "one_time" ? "radio active" : "radio"}>
                <input
                  type="radio"
                  name="mode"
                  checked={mode === "one_time"}
                  onChange={() => setMode("one_time")}
                />
                一次性 · 创建后立即执行
              </label>
              <label className={mode === "recurring" ? "radio active" : "radio"}>
                <input
                  type="radio"
                  name="mode"
                  checked={mode === "recurring"}
                  onChange={() => setMode("recurring")}
                />
                定期 · 按周期自动重复
              </label>
            </div>
          </div>

          {mode === "recurring" && (
            <div className="field schedule-box">
              <span className="field-label">执行周期</span>
              <div className="seg">
                <button
                  className={preset === "hours" ? "active" : ""}
                  onClick={() => setPreset("hours")}
                >
                  每 N 小时
                </button>
                <button
                  className={preset === "daily" ? "active" : ""}
                  onClick={() => setPreset("daily")}
                >
                  每天定时
                </button>
                <button
                  className={preset === "custom" ? "active" : ""}
                  onClick={() => setPreset("custom")}
                >
                  自定义
                </button>
              </div>

              {preset === "hours" && (
                <div className="inline-field">
                  每
                  <input
                    type="number"
                    min={1}
                    max={720}
                    value={hours}
                    onChange={(e) => setHours(Number(e.target.value))}
                  />
                  小时执行一次
                </div>
              )}
              {preset === "daily" && (
                <div className="inline-field">
                  每天
                  <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
                  <input
                    className="tz"
                    value={tz}
                    onChange={(e) => setTz(e.target.value)}
                    aria-label="时区"
                  />
                </div>
              )}
              {preset === "custom" && (
                <div className="inline-field stack">
                  <input
                    className="mono"
                    value={custom}
                    placeholder="rate(30 minutes) 或 cron(0 9 * * ? *)"
                    onChange={(e) => setCustom(e.target.value)}
                  />
                  <input
                    className="tz"
                    value={tz}
                    onChange={(e) => setTz(e.target.value)}
                    aria-label="时区"
                  />
                </div>
              )}
              <span className="field-hint mono">生成表达式：{expr || "—"}</span>
            </div>
          )}

          {error && <div className="alert">{error}</div>}
        </div>

        <footer className="drawer-foot">
          <button className="ghost-btn" onClick={onClose}>
            取消
          </button>
          <button className="accent-btn" disabled={submitting} onClick={() => void submit()}>
            {submitting ? "提交中…" : mode === "one_time" ? "创建并立即执行" : "创建定期任务"}
          </button>
        </footer>
      </aside>
    </div>
  );
}

/* ================= 任务详情（运行历史） ================= */

function TaskDetail({
  taskId,
  onBack,
  onOpenRun,
}: {
  taskId: string;
  onBack: () => void;
  onOpenRun: (runId: string) => void;
}) {
  const [data, setData] = useState<{ task: Task; runs: Run[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    getTask(taskId)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [taskId]);

  const hasActive = !!data?.runs.some((r) => isRunActive(r.status));
  usePolling(refresh, hasActive);

  async function triggerRun() {
    setBusy(true);
    try {
      const { run_id } = await runTask(taskId);
      refresh();
      onOpenRun(run_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <header className="tasks-head">
        <div>
          <button className="back-btn" onClick={onBack}>
            <ArrowLeftIcon size={14} />
            任务列表
          </button>
          <h1>
            {data?.task.name ?? "任务详情"}
            {data && <span className="country-tag detail-country">{data.task.country}</span>}
          </h1>
          {data && (
            <p className="mono">
              {data.task.mode === "one_time"
                ? "一次性任务"
                : `定期任务 · ${describeSchedule(data.task.schedule_expr)}`}
              {" · "}
              {data.task.prompt}
            </p>
          )}
        </div>
        <div className="head-actions">
          {data && <TaskBadge status={data.task.status} />}
          <button className="accent-btn" disabled={busy} onClick={() => void triggerRun()}>
            <PlayIcon size={15} weight="bold" />
            立即运行
          </button>
        </div>
      </header>

      {error && <div className="alert">{error}</div>}

      <div className="tasks-body">
        {data === null && !error && (
          <div className="thinking">
            加载中<span className="dots" />
          </div>
        )}
        {data && data.runs.length === 0 && <div className="cell-muted">这个任务还没有运行记录。</div>}
        {data && data.runs.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th>运行</th>
                  <th>状态</th>
                  <th>开始</th>
                  <th>结束</th>
                  <th>候选 / 判定</th>
                </tr>
              </thead>
              <tbody>
                {data.runs.map((r) => (
                  <tr key={r.run_id}>
                    <td>
                      <button className="link-cell mono" onClick={() => onOpenRun(r.run_id)}>
                        {r.run_id.slice(0, 8)}
                      </button>
                    </td>
                    <td>
                      <RunBadge status={r.status} />
                    </td>
                    <td>{formatTs(r.started_at)}</td>
                    <td>{formatTs(r.finished_at)}</td>
                    <td>
                      {r.stats
                        ? `${r.stats.candidates ?? 0} / ${Object.values(r.stats.verdicts ?? {}).reduce(
                            (a, b) => a + b,
                            0,
                          )}`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
