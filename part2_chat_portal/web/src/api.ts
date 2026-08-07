// 调用 AgentCore Runtime（经 vite dev proxy 转发）并解析 SSE 流。
// Runtime 端 BedrockAgentCoreApp 把 Agent 的每个文本增量作为一条
// `data: <JSON>` 事件推送。

export async function streamChat(
  prompt: string,
  sessionId: string,
  token: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/invoke", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId,
    },
    body: JSON.stringify({ prompt }),
    signal,
  });
  if (!resp.ok) {
    throw new Error(`调用失败 HTTP ${resp.status}: ${await resp.text()}`);
  }
  if (!resp.body) throw new Error("响应无内容");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 事件以空行分隔；最后一段可能不完整，留到下一轮
    const events = buffer.split("\n\n");
    buffer = events.pop()!;
    for (const event of events) {
      for (const line of event.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try {
          const chunk = JSON.parse(line.slice(6));
          if (typeof chunk === "string") onDelta(chunk);
          else if (chunk && typeof chunk === "object" && "error" in chunk)
            onDelta(`\n\n> ⚠️ ${(chunk as { error: string }).error}\n`);
        } catch {
          // 非 JSON 的心跳/注释行，忽略
        }
      }
    }
  }
}

/** Runtime 要求 session id 至少 33 个字符，uuid（36 位）满足。 */
export function newSessionId(): string {
  return `web-${crypto.randomUUID()}`;
}
