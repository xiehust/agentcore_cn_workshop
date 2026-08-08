// 数据摄入任务管理的 REST 客户端。
// 后端是独立的 Tasks HTTP API（API Gateway HTTP API + Keycloak JWT authorizer），
// 自带 CORS，浏览器直连即可，不像 Runtime 数据面那样需要 vite dev proxy。
// 每个请求都带上 Keycloak 用户 token（aud=analysis-runtime）。

import { getToken } from "../auth";

const BASE = (import.meta.env.VITE_TASKS_API_URL || "").replace(/\/+$/, "");

/* ================= 类型 ================= */

export type TaskMode = "one_time" | "recurring";
export type TaskStatus = "active" | "paused" | "deleted";
export type RunStatus = "queued" | "running" | "succeeded" | "failed";
export type RunEventKind = "phase" | "tool" | "log" | "browser" | "error" | "result";
export type ResultTable = "candidates" | "verdicts";

/**
 * 注意没有 schedule_tz：时区只透传给 EventBridge Scheduler，不落库
 * （ingestion_tasks 表没有 tz 列），所以读接口拿不回来，只在创建入参里有。
 */
export interface Task {
  task_id: string;
  name: string;
  prompt: string;
  mode: TaskMode;
  schedule_expr: string | null;
  status: TaskStatus;
  created_at: string;
  updated_at?: string | null;
}

/** 列表接口在任务上附带最近一次运行的摘要。 */
export interface LastRun {
  run_id: string;
  status: RunStatus;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskSummary extends Task {
  last_run: LastRun | null;
}

/**
 * 单次运行的统计（成功收尾时由 runner 按 run_id 聚合写入）。
 * verdicts 的键是 Part 1 的判定值 PASS / 排除 / 补采 / 采集（中文键，不是英文枚举）。
 */
export interface RunStats {
  candidates?: number;
  verdicts?: Record<string, number>;
  duration_s?: number;
  /** 聚合统计本身失败时后端塞的说明，前端提示一下，别把 0 当真值。 */
  stats_error?: string;
}

export interface Run {
  run_id: string;
  task_id: string;
  status: RunStatus;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  stats: RunStats | null;
  browser_id: string | null;
  browser_session_id: string | null;
}

export interface RunEvent {
  seq: number;
  kind: RunEventKind;
  message: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface ResultsPage {
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
}

export interface LiveViewInfo {
  url: string;
  width: number;
  height: number;
  /** presigned URL 有效期（秒），上限 300。 */
  expires_in: number;
}

export interface CreateTaskInput {
  name: string;
  prompt: string;
  mode: TaskMode;
  schedule_expr?: string;
  schedule_tz?: string;
}

/* ================= 请求封装 ================= */

/** 带 HTTP 状态码的异常：Live View 需要区分 409（还没起浏览器）/410（会话已结束）。 */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!BASE) {
    throw new ApiError(0, "未配置 VITE_TASKS_API_URL，请先跑 part2 的 deploy.sh 生成 .env.local");
  }
  const token = await getToken();
  const resp = await fetch(BASE + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  const text = await resp.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // 非 JSON 响应（如网关的 HTML 错误页），留给下面按状态码报错
    }
  }
  if (!resp.ok) {
    const err =
      body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string"
        ? (body as { error: string }).error
        : text || `HTTP ${resp.status}`;
    throw new ApiError(resp.status, err);
  }
  return body as T;
}

/* ================= 任务 ================= */

export function listTasks(): Promise<{ tasks: TaskSummary[] }> {
  return request("/tasks");
}

export function createTask(
  input: CreateTaskInput,
): Promise<{ task: Task; run_id: string | null }> {
  return request("/tasks", { method: "POST", body: JSON.stringify(input) });
}

export function getTask(taskId: string): Promise<{ task: Task; runs: Run[] }> {
  return request(`/tasks/${encodeURIComponent(taskId)}`);
}

/** 手动再跑一次（一次性任务重跑 / 定期任务立即触发；也是并行验证入口）。 */
export function runTask(taskId: string): Promise<{ run_id: string }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/run`, { method: "POST" });
}

export function pauseTask(taskId: string): Promise<{ task: Task }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
}

export function resumeTask(taskId: string): Promise<{ task: Task }> {
  return request(`/tasks/${encodeURIComponent(taskId)}/resume`, { method: "POST" });
}

export function deleteTask(taskId: string): Promise<{ ok: boolean }> {
  return request(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

/* ================= 运行 ================= */

export function getRun(runId: string): Promise<{ run: Run }> {
  return request(`/runs/${encodeURIComponent(runId)}`);
}

/** 增量拉事件：afterSeq 是上次拿到的最大 seq，做轮询游标。 */
export function getRunEvents(runId: string, afterSeq = 0): Promise<{ events: RunEvent[] }> {
  return request(`/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`);
}

export function getRunResults(
  runId: string,
  table: ResultTable,
  page = 1,
  pageSize = 20,
): Promise<ResultsPage> {
  const qs = new URLSearchParams({
    table,
    page: String(page),
    page_size: String(pageSize),
  });
  return request(`/runs/${encodeURIComponent(runId)}/results?${qs}`);
}

/**
 * 取 Browser Live View 的 presigned URL（服务端用 SigV4 签，凭证不下发浏览器）。
 * 409：浏览器会话还没起；410：会话已结束。
 */
export function getLiveView(runId: string): Promise<LiveViewInfo> {
  return request(`/runs/${encodeURIComponent(runId)}/live-view`);
}

/* ================= 展示辅助 ================= */

export const RUN_STATUS_TEXT: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
};

export const TASK_STATUS_TEXT: Record<TaskStatus, string> = {
  active: "启用",
  paused: "已暂停",
  deleted: "已删除",
};

export function isRunActive(status: RunStatus | undefined): boolean {
  return status === "queued" || status === "running";
}

/** 时间戳后端用 to_char 输出带 Z 的 UTC ISO8601；没带时区标记的值一律按 UTC 兜底解析。 */
export function parseTs(ts: string | null | undefined): Date | null {
  if (!ts) return null;
  const normalized = /[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : ts.replace(" ", "T") + "Z";
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatTs(ts: string | null | undefined): string {
  const d = parseTs(ts);
  if (!d) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function formatClock(ts: string | null | undefined): string {
  const d = parseTs(ts);
  if (!d) return "—";
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

/** 秒数转「3分12秒」；用于耗时展示。 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
}

/** 未收尾的运行没有 stats.duration_s，用起止时间现算。 */
export function runDurationSeconds(run: Run): number | null {
  if (run.stats?.duration_s != null) return run.stats.duration_s;
  const start = parseTs(run.started_at);
  if (!start) return null;
  const end = parseTs(run.finished_at) ?? new Date();
  return (end.getTime() - start.getTime()) / 1000;
}

/** 定期任务的调度表达式转中文说明，认不出就原样显示。 */
export function describeSchedule(expr: string | null | undefined): string {
  if (!expr) return "—";
  const rate = expr.match(/^rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)$/);
  if (rate) {
    const unit = { minute: "分钟", minutes: "分钟", hour: "小时", hours: "小时", day: "天", days: "天" };
    return `每 ${rate[1]} ${unit[rate[2] as keyof typeof unit]}`;
  }
  const cron = expr.match(/^cron\((\d+)\s+(\d+)\s+\*\s+\*\s+\?\s+\*\)$/);
  if (cron) {
    return `每天 ${cron[2].padStart(2, "0")}:${cron[1].padStart(2, "0")}`;
  }
  return expr;
}
