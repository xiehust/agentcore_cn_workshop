// Tasks HTTP API 的请求封装（任务管理 tasks/ 与分析会话 chat/ 两个域共用）。
// 后端是 API Gateway HTTP API + Keycloak JWT authorizer，自带 CORS，浏览器直连即可，
// 不像 Runtime 数据面那样需要 vite dev proxy。每个请求都带 Keycloak 用户 token
// （aud=analysis-runtime）。

import { getToken } from "../auth";

const BASE = (import.meta.env.VITE_TASKS_API_URL || "").replace(/\/+$/, "");

/** 带 HTTP 状态码的异常：Live View 需要区分 409（还没起浏览器）/410（会话已结束）。 */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
