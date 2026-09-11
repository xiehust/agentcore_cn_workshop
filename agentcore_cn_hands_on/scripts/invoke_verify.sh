#!/usr/bin/env bash
# 调用场景 1 的摄入 Agent，结束后自动展示【本次运行新增】的入库记录。
#
# 用法（IDE Terminal 内）:
#   bash ~/environment/agentcore_cn_workshop/scripts/invoke_verify.sh '从 eBay US（ebay.com）搜索 desk organizer 选品，处理前 2 个非广告一口价商品，按选品 SOP 完成判定并入库'
#
#   只看最近 N 分钟的入库记录（不调用 Agent）:
#   bash ~/environment/agentcore_cn_workshop/scripts/invoke_verify.sh --recent 60
#
# 查询通道：agentcore-workshop-db（01_deploy_base.sh 已部署）。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/00_env.sh"

PART1_DIR="$WORKSHOP_DIR/part1_data_ingestion"

[ $# -ge 1 ] || { echo "用法: bash $0 '<给 Agent 的中文指令>'   或   bash $0 --recent <分钟数>"; exit 1; }

# ---- --recent 模式：只查不调 ----
if [ "$1" = "--recent" ]; then
  MIN="${2:-60}"
  echo "查询通道: ${DB_QUERY_FN}（最近 ${MIN} 分钟）"
  SINCE=$(db_exec_sql "SELECT to_char(now() - interval '$MIN minutes', 'YYYY-MM-DD HH24:MI:SS') AS t" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['rows'][0]['t'])")
  show_rows_since "$SINCE"
  exit 0
fi

# ---- 正常模式：invoke + 展示新增 ----
PROMPT="$1"
ensure_venv

# 用数据库自己的时钟做水位线，避免本机/DB 时区偏差
START_TS=$(db_now)

PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1]}))' "$PROMPT")
echo "════ 调用 Agent（单商品约 2-5 分钟，多商品按数量累加）════"
cd "$PART1_DIR/agent"
agentcore invoke "$PAYLOAD"

echo ""
echo "════ 本次运行新增的入库记录 ════"
if [ -n "$START_TS" ]; then
  show_rows_since "$START_TS"
else
  echo "（起始时间未记录，展示最近 30 分钟）"
  show_rows_since "$(db_exec_sql "SELECT to_char(now() - interval '30 minutes', 'YYYY-MM-DD HH24:MI:SS') AS t" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['rows'][0]['t'])")"
fi
