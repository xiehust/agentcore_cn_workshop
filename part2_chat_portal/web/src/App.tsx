// 应用外壳：侧栏 + 视图切换（不引入 router，两个视图靠顶层 state 切）。
// ChatView 始终挂载（切走只隐藏，保住对话历史）；TasksView 只在激活时挂载，
// 离开即卸载，顺带停掉轮询和 Live View 的 DCV 连接。
// 会话 id 也放在这一层：侧栏的历史会话列表要用它做高亮，「新对话」与「点开历史」
// 都是换 sessionId（restored 区分是新开还是从库里恢复）。
import { useCallback, useEffect, useState } from "react";
import {
  BrainIcon,
  ChatCircleTextIcon,
  ListChecksIcon,
  PencilSimpleIcon,
  PlusIcon,
  TrashIcon,
} from "@phosphor-icons/react";
import { newSessionId } from "./api";
import { displayName, logout } from "./auth";
import ChatView, { QUICK_ACTIONS, type ChatRequest } from "./ChatView";
import {
  deleteSession,
  listSessions,
  renameSession,
  type ChatSession,
} from "./chat/api";
import { formatRelative } from "./lib/time";
import TasksView from "./tasks/TasksView";

type View = "chat" | "tasks";

/** 当前会话：restored=true 表示 id 来自历史列表，ChatView 要先拉回消息。 */
interface Session {
  id: string;
  restored: boolean;
}

/** 长期记忆（mem0）部署期开关：deploy.sh 按 MEM0_* 是否配齐写入 .env.local。
 * 只反映部署配置，运行期降级（如端点故障熔断）不在此显示。 */
const mem0Enabled = import.meta.env.VITE_MEM0_ENABLED === "true";

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatRequest, setChatRequest] = useState<ChatRequest | null>(null);
  const [session, setSession] = useState<Session>(() => ({ id: newSessionId(), restored: false }));
  const [sessions, setSessions] = useState<ChatSession[] | null>(null);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);

  const refreshSessions = useCallback(() => {
    listSessions()
      .then((d) => {
        setSessions(d.sessions);
        setSessionsError(null);
      })
      .catch((e: Error) => {
        setSessions([]);
        setSessionsError(e.message);
      });
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  // Runtime 是在 SSE 流结束之后才把这轮消息写库的，收到「会话有动静」时立刻拉一次
  // 可能还看不到最后一条，1.5 秒后补一拍，列表的时间与条数才是最终值。
  const onSessionsChanged = useCallback(() => {
    refreshSessions();
    setTimeout(refreshSessions, 1500);
  }, [refreshSessions]);

  // 侧栏快捷入口：切回对话视图，并把 prompt 交给 ChatView 发送
  function ask(prompt: string) {
    setView("chat");
    setChatRequest({ text: prompt, nonce: Date.now() });
  }

  function newChat() {
    setView("chat");
    setSession({ id: newSessionId(), restored: false });
  }

  function openSession(s: ChatSession) {
    setView("chat");
    // 点的是当前会话就只切视图：不重挂 session，免得把还没落库的最后一轮冲掉
    if (s.session_id === session.id) return;
    setSession({ id: s.session_id, restored: true });
  }

  async function act(sessionId: string, fn: () => Promise<unknown>) {
    setActing(sessionId);
    try {
      await fn();
      refreshSessions();
    } catch (e) {
      setSessionsError((e as Error).message);
    } finally {
      setActing(null);
    }
  }

  function rename(s: ChatSession) {
    const title = window.prompt("会话名称", s.title)?.trim();
    if (!title || title === s.title) return;
    void act(s.session_id, () => renameSession(s.session_id, title));
  }

  function remove(s: ChatSession) {
    if (!window.confirm(`删除会话「${s.title}」？只从列表移除，消息在数仓里保留。`)) return;
    void act(s.session_id, async () => {
      await deleteSession(s.session_id);
      // 删掉的正是当前会话时，顺势开一个新会话（等同「新对话」）
      if (s.session_id === session.id) setSession({ id: newSessionId(), restored: false });
    });
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

        <div className="section-title">工作区</div>
        <nav className="nav">
          <button
            className={view === "chat" ? "active" : ""}
            aria-current={view === "chat"}
            onClick={() => setView("chat")}
          >
            <ChatCircleTextIcon size={16} />
            分析对话
          </button>
          <button
            className={view === "tasks" ? "active" : ""}
            aria-current={view === "tasks"}
            onClick={() => setView("tasks")}
          >
            <ListChecksIcon size={16} />
            任务管理
          </button>
        </nav>

        <div className="section-title">历史会话</div>
        <div className="history">
          {/* 错误提示独立一行：重命名/删除失败时列表通常不空，塞进空态里就看不见了 */}
          {sessionsError && <div className="history-hint">历史会话操作失败：{sessionsError}</div>}
          {sessions === null && !sessionsError && <div className="history-hint">加载中…</div>}
          {sessions !== null && sessions.length === 0 && !sessionsError && (
            <div className="history-hint">发出第一条消息后，会话会出现在这里。</div>
          )}
          {sessions?.map((s) => {
            const current = s.session_id === session.id;
            return (
              <div
                key={s.session_id}
                className={current ? "history-item active" : "history-item"}
              >
                <button
                  className="history-open"
                  title={s.title}
                  aria-current={current}
                  onClick={() => openSession(s)}
                >
                  <span className="history-title">{s.title}</span>
                  <span className="history-time">
                    {formatRelative(s.last_message_at ?? s.updated_at)}
                    {s.message_count > 0 && ` · ${s.message_count} 条`}
                  </span>
                </button>
                <span className="history-actions">
                  <button
                    className="history-act"
                    aria-label="重命名会话"
                    title="重命名"
                    disabled={acting === s.session_id}
                    onClick={() => rename(s)}
                  >
                    <PencilSimpleIcon size={13} />
                  </button>
                  <button
                    className="history-act danger"
                    aria-label="删除会话"
                    title="删除"
                    disabled={acting === s.session_id}
                    onClick={() => remove(s)}
                  >
                    <TrashIcon size={13} />
                  </button>
                </span>
              </div>
            );
          })}
        </div>

        <div className="section-title">报表与分析</div>
        <div className="quick-actions">
          {QUICK_ACTIONS.map((a) => (
            <button key={a.label} disabled={chatBusy} onClick={() => ask(a.prompt)}>
              <span className="qa-icon">{a.icon}</span>
              <span>
                <span className="qa-label">{a.label}</span>
                <span className="qa-desc">{a.prompt}</span>
              </span>
            </button>
          ))}
        </div>

        <div className="sidebar-footer">
          <div
            className={mem0Enabled ? "memory-note on" : "memory-note"}
            title={
              mem0Enabled
                ? "基于 AWS 自部署 mem0：跨会话记住你的分析偏好（新会话首轮自动带入，本轮对话结束后自动沉淀）"
                : "部署时未配置 MEM0_* 环境变量；export 后重跑 deploy.sh 即可启用"
            }
          >
            <BrainIcon size={14} />
            长期记忆 mem0 · {mem0Enabled ? "已启用" : "未启用"}
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

      <ChatView
        hidden={view !== "chat"}
        request={chatRequest}
        sessionId={session.id}
        restored={session.restored}
        onBusyChange={setChatBusy}
        onSessionsChanged={onSessionsChanged}
      />
      {view === "tasks" && <TasksView />}
    </div>
  );
}
