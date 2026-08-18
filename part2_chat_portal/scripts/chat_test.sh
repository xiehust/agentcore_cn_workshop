#!/usr/bin/env bash
# 命令行冒烟测试：模拟前端调用 —— 用 Keycloak 用户 token 直接调
# AgentCore Runtime 的 HTTP 端点，流式打印 Agent 回复。
# 用法: ./chat_test.sh '看下最近30天的选品漏斗' [session_id]
set -euo pipefail

PROMPT="${1:?用法: ./chat_test.sh '<分析请求>' [session_id]}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART2_DIR="$(dirname "$SCRIPT_DIR")"
REGION=$(grep '^VITE_AWS_REGION=' "$PART2_DIR/web/.env.local" | cut -d= -f2)
RUNTIME_ARN=$(grep '^VITE_RUNTIME_ARN=' "$PART2_DIR/web/.env.local" | cut -d= -f2)
# session id 要求至少 33 个字符；不传则新起一个会话
SESSION_ID="${2:-smoke-$(uuidgen)}"

TOKEN=$("$SCRIPT_DIR/get_token.sh")
ESCAPED_ARN=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RUNTIME_ARN")
PAYLOAD=$(python3 -c "import json, sys; print(json.dumps({'prompt': sys.argv[1]}))" "$PROMPT")
# 域名后缀按分区推导（中国区 .amazonaws.com.cn）
case "$REGION" in cn-*) AWS_DOMAIN="amazonaws.com.cn" ;; *) AWS_DOMAIN="amazonaws.com" ;; esac

echo "session: $SESSION_ID"
# -N 关闭缓冲，SSE 增量实时打印
curl -sN -X POST \
  "https://bedrock-agentcore.$REGION.$AWS_DOMAIN/runtimes/$ESCAPED_ARN/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $SESSION_ID" \
  -d "$PAYLOAD" \
  | python3 -u -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if line.startswith('data: '):
        chunk = json.loads(line[6:])
        print(chunk if isinstance(chunk, str) else json.dumps(chunk, ensure_ascii=False), end='', flush=True)
print()
"
