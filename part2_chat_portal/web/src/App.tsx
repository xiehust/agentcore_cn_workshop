// 应用外壳：侧栏 + 视图切换（不引入 router，两个视图靠顶层 state 切）。
// ChatView 始终挂载（切走只隐藏，保住对话历史）；TasksView 只在激活时挂载，
// 离开即卸载，顺带停掉轮询和 Live View 的 DCV 连接。
import { useState } from "react";
import { BrainIcon, ChatCircleTextIcon, ListChecksIcon, PlusIcon } from "@phosphor-icons/react";
import { displayName, logout } from "./auth";
import ChatView, { QUICK_ACTIONS, type ChatRequest } from "./ChatView";
import TasksView from "./tasks/TasksView";

type View = "chat" | "tasks";

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatRequest, setChatRequest] = useState<ChatRequest | null>(null);
  const [resetKey, setResetKey] = useState(0);

  // 侧栏快捷入口：切回对话视图，并把 prompt 交给 ChatView 发送
  function ask(prompt: string) {
    setView("chat");
    setChatRequest({ text: prompt, nonce: Date.now() });
  }

  function newChat() {
    setView("chat");
    setResetKey((k) => k + 1);
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

      <ChatView
        hidden={view !== "chat"}
        request={chatRequest}
        resetKey={resetKey}
        onBusyChange={setChatBusy}
      />
      {view === "tasks" && <TasksView />}
    </div>
  );
}
