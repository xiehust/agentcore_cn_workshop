import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowUpIcon,
  BrainIcon,
  ChartDonutIcon,
  FunnelSimpleIcon,
  PlusIcon,
  TerminalIcon,
} from "@phosphor-icons/react";
import { streamChat, newSessionId } from "./api";
import { getToken, displayName, logout } from "./auth";
import { Chart, tryParseChart } from "./Chart";

interface Message {
  role: "user" | "assistant";
  content: string;
  error?: boolean;
}

// 固定报表快捷入口 + 即席分析示例
const QUICK_ACTIONS: { icon: ReactNode; label: string; prompt: string }[] = [
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

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState(newSessionId);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

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

  function newChat() {
    abortRef.current?.abort();
    setSessionId(newSessionId());
    setMessages([]);
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">选</span>
          <div>
            <div className="brand-name">选品分析台</div>
            <div className="brand-sub">AGENTCORE WORKSHOP</div>
          </div>
        </div>

        <button className="new-chat" onClick={newChat}>
          <PlusIcon size={15} weight="bold" />
          新对话
        </button>

        <div className="section-title">报表与分析</div>
        <div className="quick-actions">
          {QUICK_ACTIONS.map((a) => (
            <button key={a.label} disabled={busy} onClick={() => send(a.prompt)}>
              <span className="qa-icon">{a.icon}</span>
              <span>
                <span className="qa-label">{a.label}</span>
                <span className="qa-desc">{a.prompt}</span>
              </span>
            </button>
          ))}
        </div>

        <div className="sidebar-footer">
          <div className="memory-note" title="预留：后续接入 AWS 自部署的 mem0 记住分析偏好">
            <BrainIcon size={14} />
            长期记忆 mem0 · 预留未启用
          </div>
          <div className="user-row">
            <span className="avatar">{displayName().slice(0, 1)}</span>
            <span className="user-name">{displayName()}</span>
            <button className="logout" onClick={logout}>
              退出
            </button>
          </div>
        </div>
      </aside>

      <main className="chat">
        <div className="messages" ref={scrollRef}>
          {messages.length === 0 && (
            <div className="empty">
              <div className="empty-mark">选</div>
              <h1>问一个关于选品数据的问题</h1>
              <p>
                固定报表一键生成；即席问题会转成只读 SQL 查询 Part 1 数仓，再做统计分析。
              </p>
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
    </div>
  );
}
