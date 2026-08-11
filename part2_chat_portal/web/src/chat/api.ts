// 分析会话（历史）的 REST 客户端，与任务管理共用同一个 Tasks HTTP API 和请求封装。
// 分工：会话元数据（标题 / 归属用户）由这里的 /chat 接口写，归属取的是 JWT 里的
// preferred_username；消息内容由 Runtime 侧落库（它才有完整的 Strands messages），
// 前端只读回看。所以「新会话」要等首条消息发出后调 registerSession 才会进列表。
// 类型定义是后端 /chat 响应形状的唯一前端镜像，后端改形状必须同步这里。

import { request } from "../lib/http";

/* ================= 类型 ================= */

export type ChatSessionStatus = "active" | "deleted";

export interface ChatSession {
  /** 与 Runtime 的会话隔离 id 同值（web-<uuid>），不存在第二套映射。 */
  session_id: string;
  title: string;
  status: ChatSessionStatus;
  created_at: string;
  updated_at: string;
  /** 最后一条消息的时间（列表排序键）；刚注册还没落库消息时为 null。 */
  last_message_at: string | null;
  message_count: number;
}

/** 读接口已过滤掉纯工具轮（display_text IS NULL），所以拿到的都有文本。 */
export interface ChatMessage {
  seq: number;
  role: "user" | "assistant";
  display_text: string;
  created_at: string;
}

/* ================= 接口 ================= */

export function listSessions(): Promise<{ sessions: ChatSession[] }> {
  return request("/chat/sessions");
}

/**
 * 注册会话，幂等 upsert：已存在则 touch updated_at，软删过的会重新置回 active
 * （用户正在这个会话里继续对话，让它从列表里消失更费解）。
 * session_id 撞了别人的会话时后端回 409。
 */
export function registerSession(
  sessionId: string,
  title: string,
): Promise<{ session: ChatSession }> {
  return request("/chat/sessions", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId, title }),
  });
}

/** 单页消息（后端一页 200 条，has_more 为真时用最后一条的 seq 作游标续拉）。 */
export function getMessages(
  sessionId: string,
  afterSeq?: number,
): Promise<{ messages: ChatMessage[]; has_more: boolean }> {
  const qs = afterSeq == null ? "" : `?after_seq=${afterSeq}`;
  return request(`/chat/sessions/${encodeURIComponent(sessionId)}/messages${qs}`);
}

/** 回看用：把分页拉完拼成一整段对话（封顶 10 页，防超长会话把页面拖死）。 */
export async function getAllMessages(sessionId: string): Promise<ChatMessage[]> {
  const all: ChatMessage[] = [];
  let cursor: number | undefined;
  for (let page = 0; page < 10; page++) {
    const { messages, has_more } = await getMessages(sessionId, cursor);
    all.push(...messages);
    if (!has_more || messages.length === 0) break;
    cursor = messages[messages.length - 1].seq;
  }
  return all;
}

export function renameSession(
  sessionId: string,
  title: string,
): Promise<{ session: ChatSession }> {
  return request(`/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

/**
 * 软删：会话从列表消失，消息在库里保留。
 * App 里删掉的正是当前会话时会顺势开新会话，免得下一条消息又把它注册回来。
 */
export function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  return request(`/chat/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

/* ================= 展示辅助 ================= */

const TITLE_MAX = 30;

/** 会话默认标题：首条输入压平空白后截断（可在侧栏重命名）。 */
export function makeTitle(prompt: string): string {
  const text = prompt.trim().replace(/\s+/g, " ");
  if (!text) return "新对话";
  return text.length > TITLE_MAX ? text.slice(0, TITLE_MAX) + "…" : text;
}
