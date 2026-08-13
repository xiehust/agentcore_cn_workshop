#!/usr/bin/env bash
# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
#
# 长期记忆（mem0）冒烟测试，两部分：
#   1. 向量 Lambda 直连冒烟：用独立的 mem0_smoke collection 走一遍
#      setup → insert → search（带 user_id filter）→ get → delete → delete_col
#      全生命周期，验证 VPC 内 pgvector 数据面可用（不依赖 Runtime / LLM）。
#   2. 端到端跨会话回忆：注册两个属于同一用户（analyst）的门户会话，
#      会话 A 告诉 Agent 一个分析偏好 → 会话 B 询问偏好，人工核对回复里
#      是否带出会话 A 沉淀的记忆。
#
# 用法: ./memory_test.sh            # 只跑第 1 部分（快，无 LLM 成本）
#      ./memory_test.sh --e2e      # 两部分都跑（会产生模型调用费用）
#
# 前置：Part 2 已部署（deploy.sh），本机 aws credentials 可调 Lambda 与 rds-data
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART2_DIR="$(dirname "$SCRIPT_DIR")"
REGION="${AWS_REGION:-us-west-2}"
MEMORY_LAMBDA_NAME="${MEMORY_LAMBDA_NAME:-agentcore-workshop-memory}"
DIMS="${MEM0_EMBEDDER_DIMS:-1024}"
MEM0_COLLECTION="${MEM0_COLLECTION:-mem0_ltm}"
SMOKE_COLLECTION="mem0_smoke"

# 若本机 shell 里配了 embedding 端点，先做一次真实维度断言：embedding 实际
# 输出维度 ≠ DIMS 是"记忆静默失效"的头号配置错误，冒烟阶段就把它显形
EMB_BASE="${MEM0_EMBEDDER_BASE_URL:-${MEM0_LLM_BASE_URL:-}}"
EMB_KEY="${MEM0_EMBEDDER_API_KEY:-${MEM0_LLM_API_KEY:-}}"
if [ -n "$EMB_BASE" ] && [ -n "$EMB_KEY" ] && [ -n "${MEM0_EMBEDDER_MODEL:-}" ]; then
  echo "==> [0] embedding 维度探测（$MEM0_EMBEDDER_MODEL @ $EMB_BASE）..."
  ACTUAL_DIMS=$(curl -sf -m 30 "$EMB_BASE/embeddings" \
    -H "Authorization: Bearer $EMB_KEY" -H "Content-Type: application/json" \
    -d "{\"model\": \"$MEM0_EMBEDDER_MODEL\", \"input\": \"dims probe\"}" \
    | python3 -c "import json,sys; print(len(json.load(sys.stdin)['data'][0]['embedding']))" \
    || echo "")
  if [ -z "$ACTUAL_DIMS" ]; then
    echo "⚠️  embedding 端点探测失败（不阻断冒烟）；Runtime 侧初始化时会再探一次"
  elif [ "$ACTUAL_DIMS" != "$DIMS" ]; then
    echo "❌ embedding 实际输出 $ACTUAL_DIMS 维 ≠ MEM0_EMBEDDER_DIMS=$DIMS（表结构维度）。"
    echo "   改 MEM0_EMBEDDER_DIMS=$ACTUAL_DIMS 并换 MEM0_COLLECTION 重建表后重新部署，"
    echo "   或对 matryoshka 系模型设 MEM0_EMBEDDER_PASS_DIMS=true。"
    exit 1
  else
    echo "✅ embedding 维度一致（$ACTUAL_DIMS）"
  fi
fi

_invoke() {
  # $1 = JSON payload；stdout 输出响应体
  local out
  out=$(mktemp)
  aws lambda invoke --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION" \
    --cli-binary-format raw-in-base64-out --cli-read-timeout 310 \
    --payload "$1" "$out" >/dev/null
  cat "$out"; rm -f "$out"
}

_expect_ok() {
  # $1 = 响应体, $2 = 步骤名
  if ! grep -q '"status": *"ok"' <<<"$1"; then
    echo "❌ $2 失败: $1"; exit 1
  fi
  echo "✅ $2"
}

echo "==> [1/2] 向量 Lambda 直连冒烟（collection=$SMOKE_COLLECTION, dims=$DIMS）..."

# 随机单位向量与近邻向量由 python 生成，直接拼成完整 payload
PAYLOADS=$(python3 - "$SMOKE_COLLECTION" "$DIMS" <<'EOF'
import json, random, sys, uuid
collection, dims = sys.argv[1], int(sys.argv[2])
random.seed(42)
vec = [round(random.uniform(-1, 1), 6) for _ in range(dims)]
near = [round(v + 0.01, 6) for v in vec]
mem_id = str(uuid.uuid4())
base = {"collection": collection, "dims": dims}
print(json.dumps({"op": "create_col", "args": base}))
print(json.dumps({"op": "insert", "args": {**base, "items": [
    {"id": mem_id, "vector": vec,
     "payload": {"data": "用户只关注英国站", "user_id": "smoke-user",
                 "text_lemmatized": "用户只关注英国站"}}]}}))
print(json.dumps({"op": "search", "args": {**base, "vector": near, "top_k": 3,
    "filters": {"user_id": "smoke-user"}}}))
print(json.dumps({"op": "search", "args": {**base, "vector": near, "top_k": 3,
    "filters": {"user_id": "other-user"}}}))
print(json.dumps({"op": "get", "args": {**base, "id": mem_id}}))
print(json.dumps({"op": "delete", "args": {**base, "id": mem_id}}))
print(json.dumps({"op": "delete_col", "args": base}))
EOF
)
# bash 3.2（macOS 自带）没有 mapfile，用 while-read 装数组
P=()
while IFS= read -r _line; do P+=("$_line"); done <<<"$PAYLOADS"

_expect_ok "$(_invoke "${P[0]}")" "create_col（含 pgvector 扩展，冷恢复可能需约 1 分钟）"
_expect_ok "$(_invoke "${P[1]}")" "insert（1 条 $DIMS 维向量）"

R_SEARCH=$(_invoke "${P[2]}")
_expect_ok "$R_SEARCH" "search（user_id=smoke-user）"
grep -q '英国站' <<<"$R_SEARCH" || { echo "❌ search 未命中插入的记忆: $R_SEARCH"; exit 1; }
echo "   命中: $(python3 -c "import json,sys; r=json.loads(sys.argv[1])['result'][0]; print(f\"score={r['score']:.4f} data={r['payload']['data']}\")" "$R_SEARCH")"

R_MISS=$(_invoke "${P[3]}")
_expect_ok "$R_MISS" "search（user_id=other-user，应隔离为空）"
python3 -c "import json,sys; assert json.loads(sys.argv[1])['result']==[], '用户隔离失败'" "$R_MISS"
echo "   用户隔离 OK（other-user 检索为空）"

_expect_ok "$(_invoke "${P[4]}")" "get"
_expect_ok "$(_invoke "${P[5]}")" "delete"
_expect_ok "$(_invoke "${P[6]}")" "delete_col（清理冒烟表）"
echo "✅ 向量 Lambda 数据面全部通过"

[ "${1:-}" = "--e2e" ] || { echo; echo "（跑端到端跨会话回忆：./memory_test.sh --e2e）"; exit 0; }

echo ""
echo "==> [2/2] 端到端跨会话回忆（同一用户 analyst 的两个新会话）..."
CLUSTER_ARN=$(aws cloudformation describe-stacks --stack-name agentcore-workshop-aurora \
  --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='ClusterArn'].OutputValue" --output text)
SECRET_ARN=$(aws cloudformation describe-stacks --stack-name agentcore-workshop-aurora \
  --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='SecretArn'].OutputValue" --output text)
DATABASE=$(aws cloudformation describe-stacks --stack-name agentcore-workshop-aurora \
  --region "$REGION" --query "Stacks[0].Outputs[?OutputKey=='DatabaseName'].OutputValue" --output text)

SESSION_A="web-$(uuidgen | tr '[:upper:]' '[:lower:]')"
SESSION_B="web-$(uuidgen | tr '[:upper:]' '[:lower:]')"
# Runtime 按 chat_sessions.user_name 归属记忆——直接落两行（等价于门户注册会话）
for S in "$SESSION_A" "$SESSION_B"; do
  aws rds-data execute-statement --region "$REGION" \
    --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" --database "$DATABASE" \
    --sql "INSERT INTO chat_sessions (session_id, user_name, title) VALUES (:sid, 'analyst', 'memory e2e') ON CONFLICT (session_id) DO NOTHING" \
    --parameters "[{\"name\": \"sid\", \"value\": {\"stringValue\": \"$S\"}}]" >/dev/null
done
echo "   会话已注册: A=$SESSION_A  B=$SESSION_B（user=analyst）"

echo ""
echo "--- 会话 A：陈述偏好（结束后等待记忆提取写回）---"
"$SCRIPT_DIR/chat_test.sh" '记住我的分析偏好：金额一律换算成人民币展算，我只关注英国站的数据' "$SESSION_A"
echo ""
echo "   等待 20 秒（mem0 事实提取在流结束后写回）..."
sleep 20

echo ""
echo "--- 会话 B：询问偏好（预期回复带出会话 A 沉淀的记忆）---"
"$SCRIPT_DIR/chat_test.sh" '我之前说过什么分析偏好？' "$SESSION_B"
echo ""
echo "==> 人工核对：会话 B 的回复应提到「人民币」与「英国站」。"
echo "    直查记忆库: aws rds-data execute-statement --resource-arn \"$CLUSTER_ARN\" \\"
echo "      --secret-arn \"$SECRET_ARN\" --database \"$DATABASE\" --region $REGION \\"
echo "      --sql \"SELECT payload->>'data' AS memory, payload->>'user_id' AS user FROM $MEM0_COLLECTION\""
