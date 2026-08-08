// 分析对话视图（原 App.tsx 的聊天部分整体搬迁，行为不变）。
// 侧栏归 App.tsx 管，快捷入口点击后由 App 把 prompt 通过 request 属性转发进来。
import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUpIcon, ChartDonutIcon, FunnelSimpleIcon, TerminalIcon } from "@phosphor-icons/react";
import { streamChat, newSessionId } from "./api";
import { getToken } from "./auth";
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
  /** 切到任务管理视图时不卸载，仅隐藏，保住会话历史 */
  hidden: boolean;
  request: ChatRequest | null;
  /** 「新对话」自增，换 session 并清空消息 */
  resetKey: number;
  /** 上报忙碌状态，供侧栏禁用快捷入口 */
  onBusyChange: (busy: boolean) => void;
}

export default function ChatView({ hidden, request, resetKey, onBusyChange }: ChatViewProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState(newSessionId);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    onBusyChange(busy);
  }, [busy, onBusyChange]);

  async function send(prompt: string) {
    const text = prompt.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "" }]);
    const appendDelta = (delta: string) =>
      setMessages((m) => {
        const next = [...m];
        next[next.length - 1] = {
          ...next[next.length - 1],
          content: next[next.length - 1].content + delta,
        };
        return next;
      });
    try {
      const token = await getToken();
      abortRef.current = new AbortController();
      await streamChat(text, sessionId, token, appendDelta, abortRef.current.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setMessages((m) => {
          const next = [...m];
          next[next.length - 1] = {
            role: "assistant",
            content: `分析请求失败：${(e as Error).message}`,
            error: true,
          };
          return next;
        });
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
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

  // 「新对话」：中断当前流、换 session、清空消息
  useEffect(() => {
    if (resetKey === 0) return;
    abortRef.current?.abort();
    setSessionId(newSessionId());
    setMessages([]);
  }, [resetKey]);

  return (
    <main className="chat" hidden={hidden}>
      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
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
            disabled={busy || !input.trim()}
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
