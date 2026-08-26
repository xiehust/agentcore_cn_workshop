// 分析对话视图（原 App.tsx 的聊天部分整体搬迁，行为不变）。
// 侧栏归 App.tsx 管，快捷入口点击后由 App 把 prompt 通过 request 属性转发进来。
import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUpIcon, ChartDonutIcon, FunnelSimpleIcon, TerminalIcon } from "@phosphor-icons/react";
import { streamChat } from "./api";
import { getToken } from "./auth";
import { getAllMessages, makeTitle, registerSession } from "./chat/api";
import { Chart, tryParseChart } from "./Chart";

interface Message {
  role: "user" | "assistant";
  content: string;
  error?: boolean;
}

export interface QuickAction {
  icon: ReactNode;
  label: string;
  prompt: string;
}

// 固定报表快捷入口 + 即席分析示例（侧栏与空态共用）
export const QUICK_ACTIONS: QuickAction[] = [
  {
    icon: <FunnelSimpleIcon size={16} />,
    label: "选品漏斗",
    prompt: "看下最近30天的选品漏斗报表",
  },
  {
    icon: <ChartDonutIcon size={16} />,
    label: "类目分布",
    prompt: "看下候选商品的类目/赛道分布",
  },
  {
    icon: <TerminalIcon size={16} />,
    label: "即席分析示例",
    prompt: "候选池里产品点最高的 5 个商品是什么？给出价格、月销和预估售价",
  },
];

/** 外部（侧栏快捷入口）转发进来的一次发送请求；nonce 保证同一 prompt 连点也能重发。 */
export interface ChatRequest {
  text: string;
  nonce: number;
}

function MarkdownBlock({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ className, children, ...props }) {
          const source = String(children).trim();
          if (className === "language-chart") {
            const spec = tryParseChart(source);
            if (spec) return <Chart spec={spec} />;
          }
          return (
            <code className={className} {...props}>
              {children}
            </code>
          );
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

interface ChatViewProps {
  /** 切到任务管理视图时不卸载，仅隐藏，保住当前对话 */
  hidden: boolean;
  request: ChatRequest | null;
  /** 当前会话 id，由 App 持有（「新对话」换新 id，点历史项换成那条会话的 id） */
  sessionId: string;
  /** sessionId 是否来自历史会话：true 时进来先把库里的消息拉回来，false 从空态开始 */
  restored: boolean;
  /** 上报忙碌状态，供侧栏禁用快捷入口 */
  onBusyChange: (busy: boolean) => void;
  /** 本会话有新动静（注册成功 / 一轮问答收尾），让 App 刷新侧栏历史列表 */
  onSessionsChanged: () => void;
}

export default function ChatView({
  hidden,
  request,
  sessionId,
  restored,
  onBusyChange,
  onSessionsChanged,
}: ChatViewProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 本会话是否还需要注册进历史列表（新会话首条消息发出时注册一次）
  const needRegisterRef = useRef(!restored);
  // 当前会话 id 的镜像：abort 之后仍可能有一两个增量回调排在队里，流回调按它
  // 判断自己是否还属于当前会话，切走后到达的增量丢掉——否则会追加到新会话
  // （或刚从库里载入的历史）的最后一条消息上
  const liveSessionRef = useRef(sessionId);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // 载入历史期间也算忙：这会儿 send() 是拒绝的，侧栏快捷入口得跟着禁用，
  // 否则点一下 prompt 被静默丢掉（nonce 已消耗，不会补发）
  useEffect(() => {
    onBusyChange(busy || loadingHistory);
  }, [busy, loadingHistory, onBusyChange]);

  async function send(prompt: string) {
    const text = prompt.trim();
    if (!text || busy || loadingHistory) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "" }]);
    // 新会话首条消息：把会话注册到当前用户名下，它才会出现在侧栏历史里。
    // fire-and-forget，注册失败只记日志——对话本身不受影响（消息由 Runtime 落库）。
    if (needRegisterRef.current) {
      needRegisterRef.current = false;
      void registerSession(sessionId, makeTitle(text))
        .then(() => onSessionsChanged())
        .catch((e: Error) => console.warn("会话注册失败，历史列表暂不显示该会话", e));
    }
    const sid = sessionId;
    let receivedDelta = false;
    const appendDelta = (delta: string) => {
      if (delta) receivedDelta = true;
      setMessages((m) => {
        if (liveSessionRef.current !== sid || m.length === 0) return m;
        const next = [...m];
        next[next.length - 1] = {
          ...next[next.length - 1],
          content: next[next.length - 1].content + delta,
        };
        return next;
      });
    };
    try {
      const token = await getToken();
      abortRef.current = new AbortController();
      await streamChat(text, sid, token, appendDelta, abortRef.current.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        console.warn("分析流异常结束", e);
        setMessages((m) => {
          if (liveSessionRef.current !== sid || m.length === 0) return m;
          const next = [...m];
          if (receivedDelta) {
            const warning = "\n\n> ⚠️ 连接在响应结束前中断，以上内容可能不完整。请按需重新发送。";
            next[next.length - 1] = {
              ...next[next.length - 1],
              content: next[next.length - 1].content + warning,
            };
          } else {
            next[next.length - 1] = {
              role: "assistant",
              content: `分析请求失败：${(e as Error).message}`,
              error: true,
            };
          }
          return next;
        });
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
      // 一轮问答收尾：刷新列表，让排序与消息条数跟上
      onSessionsChanged();
    }
  }

  // 侧栏快捷入口转发进来的 prompt：按 nonce 去重，避免重渲染重复发送
  const sentNonce = useRef(0);
  useEffect(() => {
    if (!request || request.nonce === sentNonce.current) return;
    sentNonce.current = request.nonce;
    void send(request.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request]);

  // 会话切换：中断在途流、清空消息；恢复历史会话时再把库里的消息拉回来渲染。
  // 「新对话」在 App 侧表现为换一个新 sessionId（restored=false），走同一条路径。
  useEffect(() => {
    liveSessionRef.current = sessionId;
    abortRef.current?.abort();
    setMessages([]);
    setHistoryError(null);
    needRegisterRef.current = !restored;
    if (!restored) {
      setLoadingHistory(false);
      return;
    }
    let cancelled = false;
    setLoadingHistory(true);
    getAllMessages(sessionId)
      .then((history) => {
        if (cancelled) return;
        setMessages(history.map((m) => ({ role: m.role, content: m.display_text })));
      })
      .catch((e: Error) => {
        if (!cancelled) setHistoryError(`会话历史加载失败：${e.message}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, restored]);

  return (
    <main className="chat" hidden={hidden}>
      <div className="messages" ref={scrollRef}>
        {loadingHistory && (
          <div className="message assistant">
            <div className="bubble">
              <span className="thinking">
                载入会话历史<span className="dots" />
              </span>
            </div>
          </div>
        )}
        {historyError && (
          <div className="message assistant error">
            <div className="bubble">{historyError}</div>
          </div>
        )}
        {messages.length === 0 && !loadingHistory && !historyError && (
          <div className="empty">
            <div className="empty-mark">选</div>
            <h1>问一个关于选品数据的问题</h1>
            <p>固定报表一键生成；即席问题会转成只读 SQL 查询 Part 1 数仓，再做统计分析。</p>
            <div className="empty-chips">
              {QUICK_ACTIONS.map((a) => (
                <button key={a.label} disabled={busy} onClick={() => send(a.prompt)}>
                  {a.icon}
                  {a.label}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`message ${m.role}${m.error ? " error" : ""}`}>
            <div className="bubble">
              {m.role === "assistant" ? (
                m.content ? (
                  <MarkdownBlock content={m.content} />
                ) : (
                  <span className="thinking">
                    分析中<span className="dots" />
                  </span>
                )
              ) : (
                m.content
              )}
              {m.role === "assistant" && busy && i === messages.length - 1 && m.content && (
                <span className="cursor" />
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="composer">
        <div className="composer-box">
          <textarea
            value={input}
            placeholder="输入分析请求，如：本周入库的候选商品里均价最高的赛道是哪个？"
            rows={1}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
          />
          <button
            className="send"
            aria-label="发送"
            disabled={busy || loadingHistory || !input.trim()}
            onClick={() => send(input)}
          >
            <ArrowUpIcon size={17} weight="bold" />
          </button>
        </div>
        <div className="composer-hint">Enter 发送 · Shift+Enter 换行 · 查询均为只读</div>
      </div>
    </main>
  );
}
