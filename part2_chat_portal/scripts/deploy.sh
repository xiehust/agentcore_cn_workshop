#!/usr/bin/env bash
# Part 2 一键部署脚本
#
# 步骤：
#   1. 部署 Keycloak（自定义镜像 -> ECR -> ECS Fargate + API Gateway HTTPS 入口）
#   2. 部署分析链路基础设施（CloudFormation：Lambda/Gateway 角色；
#      取凭证用的 Secrets Manager VPC 端点归 Part 1 的 Aurora 栈）
#   3. 打包并部署数据查询 Lambda（挂进 Aurora VPC，pg8000 直连 5432）
#   4. 打包并部署记忆向量 Lambda（mem0 pgvector 数据面）与通用数据面 db Lambda
#     （会话历史/任务表读写，Data API 的替代——中国区没有 RDS Data API），
#      并初始化 pgvector 扩展与记忆表
#   5. 创建 Gateway（JWT 认证）+ Lambda target（MCP 工具）
#   6. 创建 Identity OAuth2 credential provider（Agent 访问 Gateway 的 M2M）
#   7. 部署分析 Agent 到 Runtime（JWT 入站认证 + 流式）
#   8. 附加 Runtime 执行角色所需策略
#   9. 生成前端 web/.env.local
#  10. 部署数据摄入任务管理模块（Tasks HTTP API + tasks-api / runner Lambda +
#      EventBridge Scheduler 调度组），并把 API 地址补进 web/.env.local
#
# 前置：Part 1 已部署（依赖其 Aurora 栈输出）；本机有 docker；
#      模型认证同 Part 1：默认 bedrock-mantle（export AWS_BEARER_TOKEN_BEDROCK=
#      <Bedrock API key>），或 export OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID
#      指向任意 OpenAI 兼容 Chat Completions 端点（中国区形态）
#      长期记忆（mem0）需要 OpenAI 兼容端点，部署前 export（缺省则记忆功能禁用，
#      对话不受影响）：
#        MEM0_LLM_MODEL / MEM0_LLM_BASE_URL / MEM0_LLM_API_KEY          事实提取 LLM
#        MEM0_EMBEDDER_MODEL / MEM0_EMBEDDER_BASE_URL / MEM0_EMBEDDER_API_KEY
#        MEM0_EMBEDDER_DIMS（默认 1024；embedding 维度决定 pgvector 表结构，
#        建表后更换需换 MEM0_COLLECTION 重建）
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
# ---- 分区/域名推导：区域代码是唯一开关（us-west-2 / cn-northwest-1 ...） ----
case "$REGION" in
  cn-*) PARTITION="aws-cn"; AWS_DOMAIN="amazonaws.com.cn"
        # SDK/starter-toolkit 库内硬编码 .amazonaws.com 端点，中国区用官方
        # 环境变量覆盖（agentcore CLI 与 Runtime 内 SDK 都认这两个变量）
        export BEDROCK_AGENTCORE_CP_ENDPOINT="https://bedrock-agentcore-control.$REGION.$AWS_DOMAIN"
        export BEDROCK_AGENTCORE_DP_ENDPOINT="https://bedrock-agentcore.$REGION.$AWS_DOMAIN"
        ;;
  *)    PARTITION="aws"; AWS_DOMAIN="amazonaws.com" ;;
esac

# ---- 模型三元组校验（fail-fast，不再静默丢弃） ----
# 显式配了 OPENAI_BASE_URL：视为自定义端点，信任成对的 key（任意前缀——中国区
# 服务商的 key 不是 ABSK）。注意部署机环境卫生：残留的 BASE_URL 会被原样采用。
# 未配 BASE_URL：走默认 bedrock-mantle，key 必须是 Bedrock API key（ABSK 前缀）。
MODEL_KEY="${OPENAI_API_KEY:-${AWS_BEARER_TOKEN_BEDROCK:-}}"
if [ -z "$MODEL_KEY" ]; then
  echo "❌ 缺少模型 API key：export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>（默认 mantle 端点），"
  echo "   或 export OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID 指向自定义 OpenAI 兼容端点。"
  exit 1
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ "$PARTITION" = "aws-cn" ]; then
  echo "❌ 中国区无默认模型端点（bedrock-mantle 仅全球区可用）："
  echo "   请 export OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID 指向区内 OpenAI 兼容端点。"
  exit 1
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [[ "$MODEL_KEY" != ABSK* ]]; then
  echo "❌ 未配置 OPENAI_BASE_URL 时默认走 bedrock-mantle，key 必须是 Bedrock API key（ABSK 前缀）。"
  echo "   当前 key 非 ABSK：要么换 Bedrock API key，要么补 export 配套的 OPENAI_BASE_URL（+MODEL_ID）。"
  exit 1
fi
if [ -n "${OPENAI_BASE_URL:-}" ] \
    && printf '%s' "$OPENAI_BASE_URL" | grep -qE '://(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'; then
  echo "❌ OPENAI_BASE_URL 指向私网/本机地址（$OPENAI_BASE_URL）——公网 Runtime 永远无法访问。"
  echo "   多半是部署机残留的环境变量：unset OPENAI_BASE_URL OPENAI_API_KEY 后重跑，"
  echo "   或改指公网可达的 OpenAI 兼容端点。"
  exit 1
fi
if [ -n "${OPENAI_BASE_URL:-}" ] && [ -z "${MODEL_ID:-}" ]; then
  echo "⚠️  已配置 OPENAI_BASE_URL 但未 export MODEL_ID，Runtime 将用内置默认模型"
  echo "   openai.gpt-5.6-terra——若自定义端点不提供该模型，首次调用才会失败。"
fi
if [ -n "${OPENAI_BASE_URL:-}" ]; then
  echo "ℹ️  模型端点: $OPENAI_BASE_URL（自定义 OpenAI 兼容端点，MODEL_ID=${MODEL_ID:-<内置默认>}）"
else
  echo "ℹ️  模型端点: bedrock-mantle 默认端点（Bedrock API key 认证）"
fi
AURORA_STACK="agentcore-workshop-aurora"
PART2_STACK="agentcore-workshop-part2"
KC_STACK="agentcore-workshop-keycloak"
KC_ECR_REPO="agentcore-workshop/keycloak"
KC_REALM="agentcore-workshop"
LAMBDA_NAME="agentcore-workshop-analytics"
# 长期记忆（mem0）：向量 Lambda 与 pgvector collection（表名）；embedding 维度
# 与 embedding 模型绑定，换模型必须同时换 collection（表按维度建，不能就地改）
MEMORY_LAMBDA_NAME="agentcore-workshop-memory"
# 通用数据面 Lambda：会话历史/任务表的全部 SQL 经它进 Aurora（Data API 替代）
DB_LAMBDA_NAME="agentcore-workshop-db"
MEM0_COLLECTION="${MEM0_COLLECTION:-mem0_ltm}"
MEM0_EMBEDDER_DIMS="${MEM0_EMBEDDER_DIMS:-1024}"
GATEWAY_NAME="agentcore-workshop-analytics-gw"
GATEWAY_TARGET_NAME="analytics"
IDENTITY_PROVIDER_NAME="agentcore-workshop-keycloak"
AGENT_NAME="analysis_chat_agent"
# 任务管理模块（步骤 10）
TASKS_STACK="agentcore-workshop-part2-tasks"
TASKS_API_NAME="agentcore-workshop-tasks-api"
RUNNER_NAME="agentcore-workshop-ingestion-runner"
# 允许跨域访问 Tasks API 的前端来源（Vite dev server；多个用逗号分隔）
# 5273 是 5173 被本机其他项目占用时的备用端口（npm run dev -- --port 5273 --strictPort）
TASKS_ALLOWED_ORIGINS="${TASKS_ALLOWED_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173,http://localhost:5273,http://127.0.0.1:5273}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART2_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOY_DIR="$PART2_DIR/.deploy"

# agentcore CLI 防遮蔽：npm 也有个同名命令，环境里若被它压住，自动改用仓库根
# .venv 里的 Python 版（bedrock-agentcore-starter-toolkit）。识别依据：只有
# Python 版的帮助里列出 configure 子命令（npm 版对任意 --help 都返回 0，
# 不能拿退出码判断）。
# 注意不能用 grep -q：-q 命中即退出会给还在写帮助文本的 agentcore 发 SIGPIPE，
# 在 set -o pipefail 下整个管道被判失败（时序相关，间歇性发作）。先落盘再 grep。
_agentcore_ok() { "$1" --help >/tmp/.agentcore_help 2>/dev/null && grep -qE '\bconfigure\b' /tmp/.agentcore_help; }
if ! _agentcore_ok agentcore; then
  REPO_VENV_BIN="$(dirname "$PART2_DIR")/.venv/bin"
  if [ -x "$REPO_VENV_BIN/agentcore" ] && _agentcore_ok "$REPO_VENV_BIN/agentcore"; then
    export PATH="$REPO_VENV_BIN:$PATH"
    echo "ℹ️  agentcore 被同名命令遮蔽或未在 PATH，已改用 $REPO_VENV_BIN/agentcore"
  else
    echo "❌ 找不到可用的 agentcore CLI（当前 PATH 里的没有 configure 子命令，可能是 npm 的同名命令）。"
    echo "   请 pip install bedrock-agentcore-starter-toolkit，或激活装了它的 venv 后重跑。"
    exit 1
  fi
fi
mkdir -p "$DEPLOY_DIR"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# ---- 部署机密（首次生成后固定，存 .deploy/，已 gitignore） ----
SECRETS_FILE="$DEPLOY_DIR/secrets.env"
if [ ! -f "$SECRETS_FILE" ]; then
  cat > "$SECRETS_FILE" <<EOF
KC_ADMIN_PASSWORD=$(openssl rand -hex 12)
AGENT_CLIENT_SECRET=$(openssl rand -hex 24)
ANALYST_PASSWORD=$(openssl rand -hex 8)
EOF
  echo "ℹ️  已生成部署机密: $SECRETS_FILE"
fi
# 增量迁移：老部署的 secrets.env 里没有后加的 key，缺哪个补哪个
# （set -u 下缺 key 会让后面的 sed 注入直接炸掉）
for demo_user in VIVIAN MARCO; do
  grep -q "^${demo_user}_PASSWORD=" "$SECRETS_FILE" \
    || echo "${demo_user}_PASSWORD=$(openssl rand -hex 8)" >> "$SECRETS_FILE"
done
# shellcheck disable=SC1090
source "$SECRETS_FILE"

_aurora_output() {
  aws cloudformation describe-stacks --stack-name "$AURORA_STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
CLUSTER_ENDPOINT=$(_aurora_output ClusterEndpoint)
DB_SECRET_ARN=$(_aurora_output SecretArn)
DATABASE=$(_aurora_output DatabaseName)
# 集群 ARN：任务管理栈参数记录用（数据访问已统一走 VPC 内 db Lambda，
# Data API 时代的托管策略 AgentDataAccessPolicy 已随之退役）
AURORA_CLUSTER_ARN=$(_aurora_output ClusterArn)
VPC_ID=$(_aurora_output VpcId)
PRIVATE_SUBNET_IDS=$(_aurora_output PrivateSubnetIds)
LAMBDA_SG_ID=$(_aurora_output LambdaClientSecurityGroupId)
if [ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ]; then
  echo "❌ 未找到 Part 1 的 Aurora 栈（$AURORA_STACK），请先部署 Part 1"; exit 1
fi

# 预检会话历史两表（建表归 Part 1 deploy.sh 管）：缺表时 /chat 接口会 500、
# Runtime 落库每轮报错，与其部署完再排查，不如现在快速失败。
# 中国区没有 RDS Data API，预检经 Part 1 写库 Lambda 的只读 check_tables op
#（此刻 Part 2 自己的 db Lambda 还没部署）；--cli-read-timeout 310 对齐其
# 内部 240s 冷恢复重连。
echo "==> 预检数仓会话历史表（chat_sessions / chat_messages）..."
CHECK_PAYLOAD='{"op": "check_tables", "args": {"tables": ["chat_sessions", "chat_messages"]}}'
if ! aws lambda invoke --function-name agentcore-workshop-writer --region "$REGION" \
    --cli-binary-format raw-in-base64-out --cli-read-timeout 310 \
    --payload "$CHECK_PAYLOAD" /tmp/check_tables_out.json >/dev/null 2>&1 \
    || ! grep -q '"missing": *\[\]' /tmp/check_tables_out.json; then
  echo "❌ 数仓缺少会话历史表（chat_sessions / chat_messages），或 Part 1 写库 Lambda"
  echo "   不可用/版本过旧（不识别 check_tables op）："
  cat /tmp/check_tables_out.json 2>/dev/null || true
  echo "   请先重跑 Part 1 部署脚本：cd part1_data_ingestion && bash scripts/deploy.sh"
  echo "  （若上方输出是「未知操作: check_tables」，说明写库 Lambda 代码过旧，"
  echo "   重跑 Part 1 即可更新，再回来重跑本脚本）"
  exit 1
fi

echo "==> [1/10] 部署 Keycloak（ECR + ECS Fargate + API Gateway，首次约需 5-8 分钟）..."
# 1a. 生成 realm.json（注入机密）并准备构建上下文
KC_BUILD_DIR="$DEPLOY_DIR/kc-build"
mkdir -p "$KC_BUILD_DIR"
cp "$PART2_DIR/infra/keycloak/Dockerfile" "$KC_BUILD_DIR/"
sed -e "s/__AGENT_CLIENT_SECRET__/$AGENT_CLIENT_SECRET/" \
    -e "s/__ANALYST_PASSWORD__/$ANALYST_PASSWORD/" \
    -e "s/__VIVIAN_PASSWORD__/$VIVIAN_PASSWORD/" \
    -e "s/__MARCO_PASSWORD__/$MARCO_PASSWORD/" \
    "$PART2_DIR/infra/keycloak/realm-template.json" > "$KC_BUILD_DIR/realm.json"

# 1b. ECR 仓库 + 构建推送
aws ecr describe-repositories --repository-names "$KC_ECR_REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$KC_ECR_REPO" --region "$REGION" >/dev/null
ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.$AWS_DOMAIN/$KC_ECR_REPO"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin \
  "$ACCOUNT_ID.dkr.ecr.$REGION.$AWS_DOMAIN" >/dev/null
# Fargate 任务定义按 ARM64 运行（便宜且本机可原生构建）；平台显式声明，
# 在 x86 机器上部署也能交叉构建（本 Dockerfile 无 RUN 步骤）
docker build -q --platform linux/arm64 -t "$ECR_URI:latest" "$KC_BUILD_DIR"
docker push -q "$ECR_URI:latest"
echo "    镜像已推送: $ECR_URI:latest"

# 1c. 部署 Keycloak 栈（ECS Fargate + Cloud Map + API Gateway HTTP API + VPC Link）
#     App Runner 自 2026-04-30 起不接受新客户，改用 Fargate；HTTPS 入口由
#     API Gateway 提供（免域名的 execute-api 端点，无需 ALB）
aws cloudformation deploy \
  --template-file "$PART2_DIR/infra/keycloak-fargate.yaml" \
  --stack-name "$KC_STACK" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --parameter-overrides \
    "ImageUri=$ECR_URI:latest" \
    "KcAdminPassword=$KC_ADMIN_PASSWORD" \
  --no-fail-on-empty-changeset

_kc_stack_output() {
  aws cloudformation describe-stacks --stack-name "$KC_STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
KC_URL=$(_kc_stack_output KeycloakUrl)
ECS_CLUSTER=$(_kc_stack_output ClusterName)
ECS_SERVICE=$(_kc_stack_output ServiceName)
DISCOVERY_URL="$KC_URL/realms/$KC_REALM/.well-known/openid-configuration"

# 镜像 tag 固定为 latest，改过 realm-template 后需强制换新任务拉取新镜像
if [ "${KC_REDEPLOY:-0}" = "1" ]; then
  echo "    KC_REDEPLOY=1，强制服务重新部署以拉取最新镜像..."
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --force-new-deployment --region "$REGION" >/dev/null
  aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --region "$REGION"
fi

# 1d. 等 Keycloak 完成启动（realm 导入后才开始监听），并确保 issuer 是 https。
#     若 API Gateway 未把 X-Forwarded-Proto 传给容器导致 issuer 是 http，
#     用 KcHostname 参数更新栈强制 https（两阶段处理）。
echo "    Keycloak 地址: $KC_URL，等待启动完成..."
for i in $(seq 1 40); do
  if ISSUER=$(curl -sf "$DISCOVERY_URL" | python3 -c "import sys,json;print(json.load(sys.stdin)['issuer'])" 2>/dev/null); then
    break
  fi
  [ "$i" -eq 40 ] && { echo "❌ 等待 Keycloak 启动超时: $DISCOVERY_URL"; exit 1; }
  sleep 15
done
if [[ "$ISSUER" != https://* ]]; then
  echo "    issuer 为 $ISSUER，设置 KC_HOSTNAME 强制 https 并更新栈..."
  aws cloudformation deploy \
    --template-file "$PART2_DIR/infra/keycloak-fargate.yaml" \
    --stack-name "$KC_STACK" \
    --capabilities CAPABILITY_IAM \
    --region "$REGION" \
    --parameter-overrides \
      "ImageUri=$ECR_URI:latest" \
      "KcAdminPassword=$KC_ADMIN_PASSWORD" \
      "KcHostname=$KC_URL" \
    --no-fail-on-empty-changeset
  aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --region "$REGION"
  for i in $(seq 1 40); do
    ISSUER=$(curl -sf "$DISCOVERY_URL" | python3 -c "import sys,json;print(json.load(sys.stdin)['issuer'])" 2>/dev/null || true)
    [[ "$ISSUER" == https://* ]] && break
    [ "$i" -eq 40 ] && { echo "❌ Keycloak issuer 仍非 https: $ISSUER"; exit 1; }
    sleep 15
  done
fi
echo "    Keycloak 就绪，issuer: $ISSUER"

echo "==> [2/10] 部署分析链路基础设施（CloudFormation）..."
aws cloudformation deploy \
  --template-file "$PART2_DIR/infra/part2-analytics.yaml" \
  --stack-name "$PART2_STACK" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --parameter-overrides \
    "VpcId=$VPC_ID" \
    "PrivateSubnetIds=$PRIVATE_SUBNET_IDS" \
    "LambdaClientSecurityGroupId=$LAMBDA_SG_ID" \
    "DbSecretArn=$DB_SECRET_ARN" \
    "AnalyticsFunctionName=$LAMBDA_NAME" \
    "MemoryFunctionName=$MEMORY_LAMBDA_NAME" \
    "DbFunctionName=$DB_LAMBDA_NAME"

_part2_output() {
  aws cloudformation describe-stacks --stack-name "$PART2_STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
LAMBDA_ROLE_ARN=$(_part2_output AnalyticsLambdaRoleArn)
MEMORY_LAMBDA_ROLE_ARN=$(_part2_output MemoryLambdaRoleArn)
DB_LAMBDA_ROLE_ARN=$(_part2_output DbLambdaRoleArn)
GATEWAY_ROLE_ARN=$(_part2_output GatewayRoleArn)

echo "==> [3/10] 打包并部署数据查询 Lambda（VPC 内直连 Aurora）..."
LAMBDA_BUILD_DIR="$DEPLOY_DIR/lambda-build"
rm -rf "$LAMBDA_BUILD_DIR" && mkdir -p "$LAMBDA_BUILD_DIR"
cp "$PART2_DIR/infra/analytics-lambda/handler.py" "$LAMBDA_BUILD_DIR/"
pip install -q pg8000 -t "$LAMBDA_BUILD_DIR" >/dev/null
(cd "$LAMBDA_BUILD_DIR" && zip -qr "$DEPLOY_DIR/analytics-lambda.zip" .)

LAMBDA_ENV="Variables={DB_HOST=$CLUSTER_ENDPOINT,DB_NAME=$DATABASE,DB_SECRET_ARN=$DB_SECRET_ARN}"
if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$LAMBDA_NAME" --region "$REGION" \
    --zip-file "fileb://$DEPLOY_DIR/analytics-lambda.zip" >/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"
  # vpc-config 一起更新：Aurora 栈重建（VPC/子网/SG 换号）后让函数收敛回来，
  # 否则会僵在旧网络配置上（与 Part 1 写库 Lambda 的 update 分支同款处理）
  aws lambda update-function-configuration --function-name "$LAMBDA_NAME" --region "$REGION" \
    --environment "$LAMBDA_ENV" --timeout 60 --memory-size 256 \
    --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" >/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$REGION"
  echo "    已更新 Lambda: $LAMBDA_NAME"
else
  # 新建 IAM 角色到 Lambda 可见有延迟，带重试
  for attempt in $(seq 1 10); do
    if aws lambda create-function --function-name "$LAMBDA_NAME" --region "$REGION" \
      --runtime python3.12 --handler handler.lambda_handler \
      --role "$LAMBDA_ROLE_ARN" \
      --zip-file "fileb://$DEPLOY_DIR/analytics-lambda.zip" \
      --timeout 60 --memory-size 256 \
      --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" \
      --environment "$LAMBDA_ENV" >/dev/null 2>/tmp/create_lambda_err; then
      break
    fi
    [ "$attempt" -eq 10 ] && { echo "创建 Lambda 失败:"; cat /tmp/create_lambda_err; exit 1; }
    echo "    等待 IAM 角色生效后重试（$attempt/10）..."
    sleep 10
  done
  aws lambda wait function-active-v2 --function-name "$LAMBDA_NAME" --region "$REGION"
  echo "    已创建 Lambda: $LAMBDA_NAME"
fi
LAMBDA_ARN=$(aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" \
  --query Configuration.FunctionArn --output text)

echo "==> [4/10] 打包并部署记忆向量 Lambda（mem0 pgvector 数据面）..."
MEMORY_BUILD_DIR="$DEPLOY_DIR/memory-lambda-build"
rm -rf "$MEMORY_BUILD_DIR" && mkdir -p "$MEMORY_BUILD_DIR"
cp "$PART2_DIR/infra/memory-lambda/handler.py" "$MEMORY_BUILD_DIR/"
pip install -q pg8000 -t "$MEMORY_BUILD_DIR" >/dev/null
(cd "$MEMORY_BUILD_DIR" && zip -qr "$DEPLOY_DIR/memory-lambda.zip" .)

MEMORY_LAMBDA_ENV="Variables={DB_HOST=$CLUSTER_ENDPOINT,DB_NAME=$DATABASE,DB_SECRET_ARN=$DB_SECRET_ARN}"
if aws lambda get-function --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION" \
    --zip-file "fileb://$DEPLOY_DIR/memory-lambda.zip" >/dev/null
  aws lambda wait function-updated --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION"
  # vpc-config 一起更新：Aurora 栈重建后让函数收敛回来（与数据查询 Lambda 同款）
  aws lambda update-function-configuration --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION" \
    --environment "$MEMORY_LAMBDA_ENV" --timeout 300 --memory-size 512 \
    --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" >/dev/null
  aws lambda wait function-updated --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION"
  echo "    已更新 Lambda: $MEMORY_LAMBDA_NAME"
else
  # 新建 IAM 角色到 Lambda 可见有延迟，带重试
  for attempt in $(seq 1 10); do
    if aws lambda create-function --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION" \
      --runtime python3.12 --handler handler.lambda_handler \
      --role "$MEMORY_LAMBDA_ROLE_ARN" \
      --zip-file "fileb://$DEPLOY_DIR/memory-lambda.zip" \
      --timeout 300 --memory-size 512 \
      --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" \
      --environment "$MEMORY_LAMBDA_ENV" >/dev/null 2>/tmp/create_memory_lambda_err; then
      break
    fi
    [ "$attempt" -eq 10 ] && { echo "创建 Lambda 失败:"; cat /tmp/create_memory_lambda_err; exit 1; }
    echo "    等待 IAM 角色生效后重试（$attempt/10）..."
    sleep 10
  done
  aws lambda wait function-active-v2 --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION"
  echo "    已创建 Lambda: $MEMORY_LAMBDA_NAME"
fi

# 初始化 pgvector 扩展并预建记忆表（集群冷恢复最长约 4 分钟，读超时给足；
# 记忆是增强功能，初始化失败只警告不阻断——create_col 会在首次使用时兜底建表）
echo "    初始化 pgvector 扩展与记忆表（$MEM0_COLLECTION, vector($MEM0_EMBEDDER_DIMS)）..."
SETUP_PAYLOAD="{\"op\": \"setup\", \"args\": {\"collection\": \"$MEM0_COLLECTION\", \"dims\": $MEM0_EMBEDDER_DIMS}}"
if aws lambda invoke --function-name "$MEMORY_LAMBDA_NAME" --region "$REGION" \
    --cli-binary-format raw-in-base64-out --cli-read-timeout 310 \
    --payload "$SETUP_PAYLOAD" /tmp/memory_setup_out.json >/dev/null 2>&1 \
    && grep -q '"status": *"ok"' /tmp/memory_setup_out.json; then
  echo "    记忆表就绪: $(cat /tmp/memory_setup_out.json)"
else
  echo "    ⚠️ 记忆表初始化未成功（$(cat /tmp/memory_setup_out.json 2>/dev/null || echo '无响应')），"
  echo "       不阻断部署；Agent 首次使用记忆时会自动建表，或稍后手动重试 op=setup"
fi

# 通用数据面 db Lambda：会话历史（Runtime）与任务表（tasks-api/runner）的全部
# SQL 经它进 Aurora——RDS Data API 的中国区可移植替代（打包模式与上面两个一致）
echo "    打包并部署通用数据面 Lambda（$DB_LAMBDA_NAME）..."
DB_BUILD_DIR="$DEPLOY_DIR/db-lambda-build"
rm -rf "$DB_BUILD_DIR" && mkdir -p "$DB_BUILD_DIR"
cp "$PART2_DIR/infra/db-lambda/handler.py" "$DB_BUILD_DIR/"
pip install -q pg8000 -t "$DB_BUILD_DIR" >/dev/null
(cd "$DB_BUILD_DIR" && zip -qr "$DEPLOY_DIR/db-lambda.zip" .)

DB_LAMBDA_ENV="Variables={DB_HOST=$CLUSTER_ENDPOINT,DB_NAME=$DATABASE,DB_SECRET_ARN=$DB_SECRET_ARN}"
if aws lambda get-function --function-name "$DB_LAMBDA_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$DB_LAMBDA_NAME" --region "$REGION" \
    --zip-file "fileb://$DEPLOY_DIR/db-lambda.zip" >/dev/null
  aws lambda wait function-updated --function-name "$DB_LAMBDA_NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$DB_LAMBDA_NAME" --region "$REGION" \
    --environment "$DB_LAMBDA_ENV" --timeout 300 --memory-size 256 \
    --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" >/dev/null
  aws lambda wait function-updated --function-name "$DB_LAMBDA_NAME" --region "$REGION"
  echo "    已更新 Lambda: $DB_LAMBDA_NAME"
else
  # 新建 IAM 角色到 Lambda 可见有延迟，带重试
  for attempt in $(seq 1 10); do
    if aws lambda create-function --function-name "$DB_LAMBDA_NAME" --region "$REGION" \
      --runtime python3.12 --handler handler.lambda_handler \
      --role "$DB_LAMBDA_ROLE_ARN" \
      --zip-file "fileb://$DEPLOY_DIR/db-lambda.zip" \
      --timeout 300 --memory-size 256 \
      --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" \
      --environment "$DB_LAMBDA_ENV" >/dev/null 2>/tmp/create_db_lambda_err; then
      break
    fi
    [ "$attempt" -eq 10 ] && { echo "创建 Lambda 失败:"; cat /tmp/create_db_lambda_err; exit 1; }
    echo "    等待 IAM 角色生效后重试（$attempt/10）..."
    sleep 10
  done
  aws lambda wait function-active-v2 --function-name "$DB_LAMBDA_NAME" --region "$REGION"
  echo "    已创建 Lambda: $DB_LAMBDA_NAME"
fi

echo "==> [5/10] 创建 Gateway + Lambda target（MCP 工具）..."
GW_AUTH_CONFIG="{\"customJWTAuthorizer\": {\"discoveryUrl\": \"$DISCOVERY_URL\", \"allowedAudience\": [\"analysis-gateway\"]}}"
# 注意：结果多于一页时 CLI 会按页应用 --query、逐页输出（可能出现多行
# "None"），需过滤后取第一个真实值
GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query "items[?name=='$GATEWAY_NAME'].gatewayId" --output text \
  | tr '[:space:]' '\n' | grep -v '^None$' | head -1 || true)
if [ -z "$GATEWAY_ID" ] || [ "$GATEWAY_ID" = "None" ]; then
  GATEWAY_ID=$(aws bedrock-agentcore-control create-gateway --region "$REGION" \
    --name "$GATEWAY_NAME" \
    --description "Workshop Part2 analytics gateway" \
    --protocol-type MCP \
    --role-arn "$GATEWAY_ROLE_ARN" \
    --authorizer-type CUSTOM_JWT \
    --authorizer-configuration "$GW_AUTH_CONFIG" \
    --query gatewayId --output text)
  echo "    已创建 Gateway: $GATEWAY_ID"
else
  # Keycloak 重建后 discovery URL 可能变化，保持 authorizer 与之同步
  aws bedrock-agentcore-control update-gateway --region "$REGION" \
    --gateway-identifier "$GATEWAY_ID" \
    --name "$GATEWAY_NAME" \
    --protocol-type MCP \
    --role-arn "$GATEWAY_ROLE_ARN" \
    --authorizer-type CUSTOM_JWT \
    --authorizer-configuration "$GW_AUTH_CONFIG" >/dev/null
  echo "    复用已有 Gateway: $GATEWAY_ID（已同步 authorizer 配置）"
fi
# 等 Gateway 就绪
for _ in $(seq 1 20); do
  GW_STATUS=$(aws bedrock-agentcore-control get-gateway --region "$REGION" \
    --gateway-identifier "$GATEWAY_ID" --query status --output text)
  [ "$GW_STATUS" = "READY" ] && break
  sleep 10
done
GATEWAY_URL=$(aws bedrock-agentcore-control get-gateway --region "$REGION" \
  --gateway-identifier "$GATEWAY_ID" --query gatewayUrl --output text)

TOOL_SCHEMA=$(cat "$PART2_DIR/infra/tool-schema.json")
TARGET_CONFIG="{\"mcp\": {\"lambda\": {\"lambdaArn\": \"$LAMBDA_ARN\", \"toolSchema\": {\"inlinePayload\": $TOOL_SCHEMA}}}}"
CRED_CONFIG='[{"credentialProviderType": "GATEWAY_IAM_ROLE"}]'
TARGET_ID=$(aws bedrock-agentcore-control list-gateway-targets --region "$REGION" \
  --gateway-identifier "$GATEWAY_ID" \
  --query "items[?name=='$GATEWAY_TARGET_NAME'].targetId" --output text \
  | tr '[:space:]' '\n' | grep -v '^None$' | head -1 || true)
if [ -z "$TARGET_ID" ] || [ "$TARGET_ID" = "None" ]; then
  TARGET_ID=$(aws bedrock-agentcore-control create-gateway-target --region "$REGION" \
    --gateway-identifier "$GATEWAY_ID" \
    --name "$GATEWAY_TARGET_NAME" \
    --target-configuration "$TARGET_CONFIG" \
    --credential-provider-configurations "$CRED_CONFIG" \
    --query targetId --output text)
  echo "    已创建 Gateway target: $TARGET_ID"
else
  aws bedrock-agentcore-control update-gateway-target --region "$REGION" \
    --gateway-identifier "$GATEWAY_ID" --target-id "$TARGET_ID" \
    --name "$GATEWAY_TARGET_NAME" \
    --target-configuration "$TARGET_CONFIG" \
    --credential-provider-configurations "$CRED_CONFIG" >/dev/null
  echo "    复用已有 target: $TARGET_ID（已同步工具 schema）"
fi

echo "==> [6/10] 创建 Identity OAuth2 credential provider（M2M）..."
PROVIDER_CONFIG="{\"customOauth2ProviderConfig\": {\"oauthDiscovery\": {\"discoveryUrl\": \"$DISCOVERY_URL\"}, \"clientId\": \"analysis-agent\", \"clientSecret\": \"$AGENT_CLIENT_SECRET\"}}"
if aws bedrock-agentcore-control get-oauth2-credential-provider --region "$REGION" \
    --name "$IDENTITY_PROVIDER_NAME" >/dev/null 2>&1; then
  aws bedrock-agentcore-control update-oauth2-credential-provider --region "$REGION" \
    --name "$IDENTITY_PROVIDER_NAME" \
    --credential-provider-vendor CustomOauth2 \
    --oauth2-provider-config-input "$PROVIDER_CONFIG" >/dev/null
  echo "    复用已有 provider: $IDENTITY_PROVIDER_NAME（已同步配置）"
else
  aws bedrock-agentcore-control create-oauth2-credential-provider --region "$REGION" \
    --name "$IDENTITY_PROVIDER_NAME" \
    --credential-provider-vendor CustomOauth2 \
    --oauth2-provider-config-input "$PROVIDER_CONFIG" >/dev/null
  echo "    已创建 provider: $IDENTITY_PROVIDER_NAME"
fi

echo "==> [7/10] 部署分析 Agent 到 Runtime（JWT 入站认证 + 流式）..."
# 长期记忆（mem0）配置提示：LLM 与 embedding 走 OpenAI 兼容端点，四个必填
# 变量缺一记忆功能整体禁用（Agent 侧优雅降级，对话正常）
MEM0_CONFIGURED="true"
if [ -z "${MEM0_LLM_BASE_URL:-}" ] || [ -z "${MEM0_LLM_API_KEY:-}" ] \
    || [ -z "${MEM0_LLM_MODEL:-}" ] || [ -z "${MEM0_EMBEDDER_MODEL:-}" ]; then
  MEM0_CONFIGURED="false"
  echo "    ⚠️ 未配置 MEM0_LLM_MODEL / MEM0_LLM_BASE_URL / MEM0_LLM_API_KEY /"
  echo "       MEM0_EMBEDDER_MODEL（四者均必填），长期记忆（mem0）将处于禁用状态；"
  echo "       需要时 export 后重跑本脚本"
fi
RUNTIME_AUTH_CONFIG="{\"customJWTAuthorizer\": {\"discoveryUrl\": \"$DISCOVERY_URL\", \"allowedAudience\": [\"analysis-runtime\"]}}"
cd "$PART2_DIR/agent"
# --disable-memory 禁用的是 AgentCore 托管 Memory；本 workshop 的长期记忆是
# 自建 mem0（memory_backend.py + 记忆向量 Lambda），两者无关
agentcore configure --entrypoint main.py --name "$AGENT_NAME" --region "$REGION" \
  --requirements-file requirements.txt --non-interactive --disable-memory \
  --authorizer-config "$RUNTIME_AUTH_CONFIG"
agentcore launch \
  --env GATEWAY_URL="$GATEWAY_URL" \
  --env IDENTITY_PROVIDER_NAME="$IDENTITY_PROVIDER_NAME" \
  --env DB_LAMBDA_NAME="$DB_LAMBDA_NAME" \
  --env MEMORY_LAMBDA_NAME="$MEMORY_LAMBDA_NAME" \
  ${BEDROCK_AGENTCORE_DP_ENDPOINT:+--env BEDROCK_AGENTCORE_DP_ENDPOINT="$BEDROCK_AGENTCORE_DP_ENDPOINT"} \
  --env MEM0_COLLECTION="$MEM0_COLLECTION" \
  --env MEM0_EMBEDDER_DIMS="$MEM0_EMBEDDER_DIMS" \
  ${MEM0_LLM_MODEL:+--env MEM0_LLM_MODEL="$MEM0_LLM_MODEL"} \
  ${MEM0_LLM_BASE_URL:+--env MEM0_LLM_BASE_URL="$MEM0_LLM_BASE_URL"} \
  ${MEM0_LLM_API_KEY:+--env MEM0_LLM_API_KEY="$MEM0_LLM_API_KEY"} \
  ${MEM0_EMBEDDER_MODEL:+--env MEM0_EMBEDDER_MODEL="$MEM0_EMBEDDER_MODEL"} \
  ${MEM0_EMBEDDER_BASE_URL:+--env MEM0_EMBEDDER_BASE_URL="$MEM0_EMBEDDER_BASE_URL"} \
  ${MEM0_EMBEDDER_API_KEY:+--env MEM0_EMBEDDER_API_KEY="$MEM0_EMBEDDER_API_KEY"} \
  ${MEM0_EMBEDDER_PASS_DIMS:+--env MEM0_EMBEDDER_PASS_DIMS="$MEM0_EMBEDDER_PASS_DIMS"} \
  ${MEM0_SEARCH_TIMEOUT:+--env MEM0_SEARCH_TIMEOUT="$MEM0_SEARCH_TIMEOUT"} \
  ${MEM0_ADD_TIMEOUT:+--env MEM0_ADD_TIMEOUT="$MEM0_ADD_TIMEOUT"} \
  ${OPENAI_API_KEY:+--env OPENAI_API_KEY="$OPENAI_API_KEY"} \
  ${AWS_BEARER_TOKEN_BEDROCK:+--env AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK"} \
  ${MODEL_ID:+--env MODEL_ID="$MODEL_ID"} \
  ${OPENAI_BASE_URL:+--env OPENAI_BASE_URL="$OPENAI_BASE_URL"}

echo "==> [8/10] 附加 Runtime 执行角色所需策略..."
#（grep 正则做分区通配：aws-cn 分区的 ARN 是 arn:aws-cn:...）
ROLE_ARN=$(grep -oE 'arn:aws[a-z-]*:iam::[0-9]+:role/[A-Za-z0-9+=,.@_/-]+' .bedrock_agentcore.yaml | head -1 || true)
ROLE_NAME="${ROLE_ARN##*/}"
if [ -n "$ROLE_NAME" ]; then
  # 会话历史读写 = 调 db Lambda；模型不再需要 IAM 权限（OpenAI 兼容端点 + key）
  aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name part2-agent-access \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"IdentityOutboundAuth\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"bedrock-agentcore:GetWorkloadAccessToken\",
          \"bedrock-agentcore:GetWorkloadAccessTokenForJWT\",
          \"bedrock-agentcore:GetWorkloadAccessTokenForUserId\",
          \"bedrock-agentcore:GetResourceOauth2Token\"
        ],
        \"Resource\": \"arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:*\"
      }, {
        \"Sid\": \"CodeInterpreter\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"bedrock-agentcore:StartCodeInterpreterSession\",
          \"bedrock-agentcore:InvokeCodeInterpreter\",
          \"bedrock-agentcore:GetCodeInterpreterSession\",
          \"bedrock-agentcore:ListCodeInterpreterSessions\",
          \"bedrock-agentcore:StopCodeInterpreterSession\"
        ],
        \"Resource\": [
          \"arn:$PARTITION:bedrock-agentcore:$REGION:aws:code-interpreter/*\",
          \"arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:*\"
        ]
      }, {
        \"Sid\": \"DataPlaneLambdas\",
        \"Effect\": \"Allow\",
        \"Action\": [\"lambda:InvokeFunction\"],
        \"Resource\": [
          \"arn:$PARTITION:lambda:$REGION:$ACCOUNT_ID:function:$MEMORY_LAMBDA_NAME\",
          \"arn:$PARTITION:lambda:$REGION:$ACCOUNT_ID:function:$DB_LAMBDA_NAME\"
        ]
      }]
    }"
  echo "    已附加到角色: $ROLE_NAME"
else
  echo "    ⚠️ 未能自动解析执行角色，请手动附加 Identity / Code Interpreter / 数据面 Lambda 权限"
fi
AGENT_ARN=$(grep -oE 'arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:[0-9]+:runtime/[A-Za-z0-9_-]+' \
  .bedrock_agentcore.yaml | head -1 || true)

echo "==> [9/10] 生成前端配置 web/.env.local ..."
cat > "$PART2_DIR/web/.env.local" <<EOF
VITE_KC_URL=$KC_URL
VITE_KC_REALM=$KC_REALM
VITE_KC_CLIENT_ID=workshop-web
VITE_RUNTIME_ARN=$AGENT_ARN
VITE_AWS_REGION=$REGION
VITE_MEM0_ENABLED=$MEM0_CONFIGURED
EOF

echo "==> [10/10] 部署数据摄入任务管理模块（Tasks API + 两个 Lambda + 调度组）..."
# 依赖 Part 1 的 Runtime（runner 要调它）与 Aurora 栈输出；Part 1 未部署时
# 只警告跳过，不让整个 Part 2 部署失败
PART1_AGENT_CONFIG="$(dirname "$PART2_DIR")/part1_data_ingestion/agent/.bedrock_agentcore.yaml"
PART1_RUNTIME_ARN=""
if [ -f "$PART1_AGENT_CONFIG" ]; then
  PART1_RUNTIME_ARN=$(grep -oE 'arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:[0-9]+:runtime/[A-Za-z0-9_-]+' \
    "$PART1_AGENT_CONFIG" | head -1 || true)
fi

if [ -z "$PART1_RUNTIME_ARN" ]; then
  echo "    ⚠️ 未从 $PART1_AGENT_CONFIG 解析到 Part 1 Runtime ARN，跳过任务管理模块"
  echo "       （先在 part1_data_ingestion 下跑 scripts/deploy.sh，再重跑本脚本）"
  TASKS_API_URL=""
else
  # 首轮部署时两个函数还不存在，而它们的执行角色由本栈创建（鸡生蛋）：
  # 先用 ApiEnabled=false 只建角色/调度组，建完函数再补建 HTTP API
  TASKS_API_ENABLED="true"
  aws lambda get-function --function-name "$TASKS_API_NAME" --region "$REGION" >/dev/null 2>&1 \
    || TASKS_API_ENABLED="false"
  aws lambda get-function --function-name "$RUNNER_NAME" --region "$REGION" >/dev/null 2>&1 \
    || TASKS_API_ENABLED="false"

  _deploy_tasks_stack() {
    aws cloudformation deploy \
      --template-file "$PART2_DIR/infra/part2-tasks.yaml" \
      --stack-name "$TASKS_STACK" \
      --capabilities CAPABILITY_NAMED_IAM \
      --region "$REGION" \
      --parameter-overrides \
        "KeycloakIssuerUrl=$ISSUER" \
        "Part1RuntimeArn=$PART1_RUNTIME_ARN" \
        "AuroraClusterArn=$AURORA_CLUSTER_ARN" \
        "AuroraSecretArn=$DB_SECRET_ARN" \
        "DbFunctionName=$DB_LAMBDA_NAME" \
        "TasksApiFunctionName=$TASKS_API_NAME" \
        "RunnerFunctionName=$RUNNER_NAME" \
        "MemoryFunctionName=$MEMORY_LAMBDA_NAME" \
        "AllowedOrigins=$TASKS_ALLOWED_ORIGINS" \
        "ApiEnabled=$1" \
      --no-fail-on-empty-changeset
  }
  _tasks_output() {
    aws cloudformation describe-stacks --stack-name "$TASKS_STACK" --region "$REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
  }
  _deploy_tasks_stack "$TASKS_API_ENABLED"

  TASKS_API_ROLE_ARN=$(_tasks_output TasksApiRoleArn)
  RUNNER_ROLE_ARN=$(_tasks_output RunnerRoleArn)
  SCHEDULE_GROUP=$(_tasks_output ScheduleGroupName)
  SCHEDULER_ROLE_ARN=$(_tasks_output SchedulerInvokeRoleArn)
  # runner 函数 ARN：Scheduler 的调度目标——按 $PARTITION 拼接（分区随区域推导；
  # 此处函数尚未创建，无法回读真实 ARN）
  RUNNER_ARN="arn:$PARTITION:lambda:$REGION:$ACCOUNT_ID:function:$RUNNER_NAME"

  # 两个函数都只依赖 runtime 自带的 boto3，直接 zip 单文件即可
  _deploy_tasks_lambda() {
    local name="$1" src="$2" role="$3" timeout="$4" memory="$5" env="$6"
    local zip="$DEPLOY_DIR/$name.zip"
    rm -f "$zip"
    (cd "$(dirname "$src")" && zip -q "$zip" "$(basename "$src")")
    if aws lambda get-function --function-name "$name" --region "$REGION" >/dev/null 2>&1; then
      aws lambda update-function-code --function-name "$name" --region "$REGION" \
        --zip-file "fileb://$zip" >/dev/null
      aws lambda wait function-updated --function-name "$name" --region "$REGION"
      aws lambda update-function-configuration --function-name "$name" --region "$REGION" \
        --environment "$env" --timeout "$timeout" --memory-size "$memory" >/dev/null
      aws lambda wait function-updated --function-name "$name" --region "$REGION"
      echo "    已更新 Lambda: $name"
    else
      # 新建 IAM 角色到 Lambda 可见有延迟，带重试
      for attempt in $(seq 1 10); do
        if aws lambda create-function --function-name "$name" --region "$REGION" \
          --runtime python3.12 --handler handler.lambda_handler \
          --role "$role" --zip-file "fileb://$zip" \
          --timeout "$timeout" --memory-size "$memory" \
          --environment "$env" >/dev/null 2>/tmp/create_tasks_lambda_err; then
          break
        fi
        [ "$attempt" -eq 10 ] && { echo "创建 Lambda 失败($name):"; cat /tmp/create_tasks_lambda_err; exit 1; }
        echo "    等待 IAM 角色生效后重试（$attempt/10）..."
        sleep 10
      done
      aws lambda wait function-active-v2 --function-name "$name" --region "$REGION"
      echo "    已创建 Lambda: $name"
    fi
  }

  # 数仓读写统一经 VPC 内 db Lambda（Data API 已退役）
  DB_ENV="DB_LAMBDA_NAME=$DB_LAMBDA_NAME"
  # runner 同步消费 SSE，单次运行 2~5 分钟，超时给到 15 分钟上限
  _deploy_tasks_lambda "$RUNNER_NAME" "$PART2_DIR/infra/runner-lambda/handler.py" \
    "$RUNNER_ROLE_ARN" 900 512 \
    "Variables={$DB_ENV,PART1_RUNTIME_ARN=$PART1_RUNTIME_ARN}"
  _deploy_tasks_lambda "$TASKS_API_NAME" "$PART2_DIR/infra/tasks-api-lambda/handler.py" \
    "$TASKS_API_ROLE_ARN" 60 256 \
    "Variables={$DB_ENV,RUNNER_FUNCTION_NAME=$RUNNER_NAME,RUNNER_FUNCTION_ARN=$RUNNER_ARN,SCHEDULE_GROUP=$SCHEDULE_GROUP,SCHEDULER_ROLE_ARN=$SCHEDULER_ROLE_ARN,MEMORY_LAMBDA_NAME=$MEMORY_LAMBDA_NAME,MEM0_COLLECTION=$MEM0_COLLECTION,MEM0_EMBEDDER_DIMS=$MEM0_EMBEDDER_DIMS}"

  # 首轮：函数就绪后补建 HTTP API（路由 + JWT authorizer + 调用许可）
  if [ "$TASKS_API_ENABLED" != "true" ]; then
    echo "    函数已就绪，补建 Tasks HTTP API ..."
    _deploy_tasks_stack "true"
  fi
  TASKS_API_URL=$(_tasks_output ApiEndpoint)
  echo "    Tasks API: $TASKS_API_URL"
  echo "VITE_TASKS_API_URL=$TASKS_API_URL" >> "$PART2_DIR/web/.env.local"
fi

echo ""
echo "✅ Part 2 部署完成！"
echo ""
echo "   Keycloak:      $KC_URL（管理台 admin / 密码见 $SECRETS_FILE）"
echo "   门户测试账号:   analyst / $ANALYST_PASSWORD（基线，无预置记忆）"
echo "                  vivian  / $VIVIAN_PASSWORD（资深买手，跑 seed_memories.py 预置偏好）"
echo "                  marco   / $MARCO_PASSWORD（风控合规，跑 seed_memories.py 预置偏好）"
echo "   Gateway:       $GATEWAY_URL"
echo "   Runtime Agent: $AGENT_ARN"
if [ -n "${TASKS_API_URL:-}" ]; then
  echo "   Tasks API:     $TASKS_API_URL（任务管理模块，需 Keycloak token）"
fi
echo ""
echo "   启动前端: cd $PART2_DIR/web && npm install && npm run dev"
echo "   然后浏览器打开 http://localhost:5173（用上面的测试账号登录）"
echo "   命令行冒烟测试: $SCRIPT_DIR/chat_test.sh '看下最近30天的选品漏斗'"
