#!/usr/bin/env bash
# 步骤 3/3 —— 运行场景 2：Web Chat 分析门户（需要模型 key + Docker）
#
# 内容：
#   a. 前置检查（场景 1 完成度 / Docker / 模型 key）
#   b. 调用 workshop 原版 part2_chat_portal/scripts/deploy.sh（10 步全链路，幂等：
#      Keycloak(ECS Fargate) -> 分析链路 IAM -> 3 个数据面 Lambda -> Gateway(MCP)
#      -> Identity(M2M) -> 分析 Agent(Runtime, JWT) -> 前端配置 -> 任务管理模块）
#   c. 预装前端依赖并打印门户公网地址
# 不做冒烟调用；需要验证时手动执行 chat_test.sh（命令行流式对话）
#
# 可选（长期记忆 mem0，缺省禁用不影响对话）：部署前 export
#   MEM0_LLM_MODEL / MEM0_LLM_BASE_URL / MEM0_LLM_API_KEY / MEM0_EMBEDDER_MODEL
#   （及 MEM0_EMBEDDER_BASE_URL / MEM0_EMBEDDER_API_KEY / MEM0_EMBEDDER_DIMS）
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/00_env.sh"

PART1_DIR="$WORKSHOP_DIR/part1_data_ingestion"
PART2_DIR="$WORKSHOP_DIR/part2_chat_portal"

ensure_venv
require_model_key

echo "==> [a] 前置检查..."
# AURORA_STACK 已由 00_env.sh 指向 CloudLab 单栈（part2 deploy.sh 支持该覆盖）
require_base_stack
if [ ! -f "$PART1_DIR/agent/.bedrock_agentcore.yaml" ]; then
  echo "⚠️  未检测到场景 1 的 Agent 部署记录——任务管理模块（步骤 10）会被跳过。"
  echo "   建议先执行 02_run_scenario1.sh；也可继续（门户对话功能不受影响）。"
fi
if ! docker info >/dev/null 2>&1; then
  echo "❌ Docker 不可用（Keycloak 镜像构建需要）。IDE 实例上排查："
  echo "   sudo systemctl start docker && sudo usermod -aG docker \$USER，然后重开 Terminal"
  exit 1
fi
command -v npm >/dev/null || echo "⚠️  未找到 npm，步骤 [d] 前端依赖预装将跳过"

# 公网门户已启用时（CloudLab 栈输出 PortalUrl / HttpPortalUrl），把它们加进
# Tasks API 的 CORS 白名单——否则门户里的"任务/会话列表"接口会被浏览器跨域拦截。
# 显式 TASKS_ALLOWED_ORIGINS 始终优先。
PORTAL_URL=$(stack_output "$BASE_STACK" PortalUrl 2>/dev/null || true)
HTTP_PORTAL_URL=$(stack_output "$BASE_STACK" HttpPortalUrl 2>/dev/null || true)
PORTAL_ORIGINS=""
[ -n "$PORTAL_URL" ] && [ "$PORTAL_URL" != "None" ] && PORTAL_ORIGINS="$PORTAL_URL"
if [ -n "$HTTP_PORTAL_URL" ] && [ "$HTTP_PORTAL_URL" != "None" ]; then
  PORTAL_ORIGINS="${PORTAL_ORIGINS:+$PORTAL_ORIGINS,}$HTTP_PORTAL_URL"
fi
if [ -n "$PORTAL_ORIGINS" ] && [ -z "${TASKS_ALLOWED_ORIGINS:-}" ]; then
  export TASKS_ALLOWED_ORIGINS="$PORTAL_ORIGINS,http://localhost:5173,http://127.0.0.1:5173,http://localhost:5273,http://127.0.0.1:5273"
  echo "    公网门户已启用，Tasks API CORS 白名单: $TASKS_ALLOWED_ORIGINS"
fi

echo "==> [b] 执行 workshop Part 2 一键部署（首次约 10-15 分钟）..."
bash "$PART2_DIR/scripts/deploy.sh"

# ==> [c] 预灌兜底演示数据（反爬保险）
# 现场人多并发时 eBay 反爬更严，场景 1 的真实爬取可能颗粒无收，导致数仓没数据、
# 门户演示翻车。这一步一次性预灌一批真实感的候选/判定数据，保证门户永远有数据可查。
# 与爬虫完全解耦：现场爬成功是锦上添花，爬不动也不影响演示。
#   · 幂等——按 asin_or_url 去重，重跑场景 2 不会灌重复行
#   · best-effort——失败只告警不中断（门户对话功能不依赖这批数据）
#   · 跳过——export SKIP_PRODUCT_SEED=1（现场确认真实爬取已有充足数据时）
# 依赖此刻均已就绪：写库 Lambda（$WRITER_NAME，场景 0/1 建）、通用数据面 Lambda
# （$DB_QUERY_FN，上面 deploy.sh 步骤 4 建）；region/凭证由 00_env.sh 备好。
if [ "${SKIP_PRODUCT_SEED:-0}" = "1" ]; then
  echo "==> [c] 预灌兜底数据：SKIP_PRODUCT_SEED=1，跳过"
else
  echo "==> [c] 预灌兜底演示数据（反爬保险，幂等）..."
  python3 "$PART1_DIR/scripts/seed_products.py" \
    || echo "⚠️  预灌兜底数据未成功——不阻断部署；门户对话不受影响，可稍后手动重试：
       python3 $PART1_DIR/scripts/seed_products.py"
fi

echo ""
echo "==> [d] 预装前端依赖..."
if command -v npm >/dev/null; then
  (cd "$PART2_DIR/web" && npm ci --no-audit --no-fund) || echo "⚠️  npm ci 失败，可稍后手动重试"
fi

# 把公网门户地址写进前端 env：npm run dev 启动时 vite.config.ts 直接打印该地址
#（优先免告警的 HTTP 入口；HTTPS 自签入口次之）
PUBLIC_PORTAL_URL=""
[ -n "$HTTP_PORTAL_URL" ] && [ "$HTTP_PORTAL_URL" != "None" ] && PUBLIC_PORTAL_URL="$HTTP_PORTAL_URL"
if [ -z "$PUBLIC_PORTAL_URL" ] && [ -n "$PORTAL_URL" ] && [ "$PORTAL_URL" != "None" ]; then
  PUBLIC_PORTAL_URL="$PORTAL_URL"
fi
if [ -n "$PUBLIC_PORTAL_URL" ] && [ -f "$PART2_DIR/web/.env.local" ]; then
  grep -v '^VITE_PUBLIC_PORTAL_URL=' "$PART2_DIR/web/.env.local" > "$PART2_DIR/web/.env.local.tmp" || true
  echo "VITE_PUBLIC_PORTAL_URL=$PUBLIC_PORTAL_URL" >> "$PART2_DIR/web/.env.local.tmp"
  mv "$PART2_DIR/web/.env.local.tmp" "$PART2_DIR/web/.env.local"
fi

echo ""
echo "✅ 场景 2 部署完成！"
echo ""
echo "   门户账号（密码见 $PART2_DIR/.deploy/secrets.env）:"
echo "     analyst（基线）/ vivian（资深买手）/ marco（风控合规）"
echo ""
echo "   ── 打开 Web 门户 ──"
echo "   1) 在本 IDE Terminal 启动前端:"
echo "      cd $PART2_DIR/web && npm run dev"
if [ -n "$HTTP_PORTAL_URL" ] && [ "$HTTP_PORTAL_URL" != "None" ]; then
  echo "   2) 浏览器直接打开 $HTTP_PORTAL_URL"
elif [ -n "$PORTAL_URL" ] && [ "$PORTAL_URL" != "None" ]; then
  echo "   2) 浏览器直接打开 $PORTAL_URL"
  echo "      （自签证书首次访问需在告警页点「高级 -> 继续前往」）"
fi
