#!/usr/bin/env bash
# Part 2 一键部署脚本
#
# 步骤：
#   1. 部署 Keycloak（自定义镜像 -> ECR -> ECS Fargate + API Gateway HTTPS 入口）
#   2. 部署分析链路基础设施（CloudFormation：Lambda/Gateway 角色 + Secrets 端点）
#   3. 打包并部署数据查询 Lambda（挂进 Aurora VPC，pg8000 直连 5432）
#   4. 创建 Gateway（JWT 认证）+ Lambda target（MCP 工具）
#   5. 创建 Identity OAuth2 credential provider（Agent 访问 Gateway 的 M2M）
#   6. 部署分析 Agent 到 Runtime（JWT 入站认证 + 流式）
#   7. 附加 Runtime 执行角色所需策略
#   8. 生成前端 web/.env.local 并输出使用说明
#
# 前置：Part 1 已部署（依赖其 Aurora 栈输出）；本机有 docker；
#      模型认证同 Part 1（export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>）
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
# Bedrock API key 以 ABSK 开头；环境里若残留 OpenAI 官网的 sk- key，
# 透传进 Runtime 会压过 AWS_BEARER_TOKEN_BEDROCK 导致模型 401，这里直接忽略
if [ -n "${OPENAI_API_KEY:-}" ] && [[ "${OPENAI_API_KEY}" != ABSK* ]]; then
  echo "⚠️  OPENAI_API_KEY 不是 Bedrock API key（非 ABSK 前缀），忽略；改用 AWS_BEARER_TOKEN_BEDROCK / IAM"
  unset OPENAI_API_KEY
fi
AURORA_STACK="agentcore-workshop-aurora"
PART2_STACK="agentcore-workshop-part2"
KC_STACK="agentcore-workshop-keycloak"
KC_ECR_REPO="agentcore-workshop/keycloak"
KC_REALM="agentcore-workshop"
LAMBDA_NAME="agentcore-workshop-analytics"
GATEWAY_NAME="agentcore-workshop-analytics-gw"
GATEWAY_TARGET_NAME="analytics"
IDENTITY_PROVIDER_NAME="agentcore-workshop-keycloak"
AGENT_NAME="analysis_chat_agent"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART2_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOY_DIR="$PART2_DIR/.deploy"
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
# shellcheck disable=SC1090
source "$SECRETS_FILE"

_aurora_output() {
  aws cloudformation describe-stacks --stack-name "$AURORA_STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
CLUSTER_ENDPOINT=$(_aurora_output ClusterEndpoint)
DB_SECRET_ARN=$(_aurora_output SecretArn)
DATABASE=$(_aurora_output DatabaseName)
VPC_ID=$(_aurora_output VpcId)
PRIVATE_SUBNET_IDS=$(_aurora_output PrivateSubnetIds)
LAMBDA_SG_ID=$(_aurora_output LambdaClientSecurityGroupId)
if [ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ]; then
  echo "❌ 未找到 Part 1 的 Aurora 栈（$AURORA_STACK），请先部署 Part 1"; exit 1
fi

echo "==> [1/8] 部署 Keycloak（ECR + ECS Fargate + API Gateway，首次约需 5-8 分钟）..."
# 1a. 生成 realm.json（注入机密）并准备构建上下文
KC_BUILD_DIR="$DEPLOY_DIR/kc-build"
mkdir -p "$KC_BUILD_DIR"
cp "$PART2_DIR/infra/keycloak/Dockerfile" "$KC_BUILD_DIR/"
sed -e "s/__AGENT_CLIENT_SECRET__/$AGENT_CLIENT_SECRET/" \
    -e "s/__ANALYST_PASSWORD__/$ANALYST_PASSWORD/" \
    "$PART2_DIR/infra/keycloak/realm-template.json" > "$KC_BUILD_DIR/realm.json"

# 1b. ECR 仓库 + 构建推送
aws ecr describe-repositories --repository-names "$KC_ECR_REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$KC_ECR_REPO" --region "$REGION" >/dev/null
ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$KC_ECR_REPO"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin \
  "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com" >/dev/null
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

echo "==> [2/8] 部署分析链路基础设施（CloudFormation）..."
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
    "AnalyticsFunctionName=$LAMBDA_NAME"

_part2_output() {
  aws cloudformation describe-stacks --stack-name "$PART2_STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
LAMBDA_ROLE_ARN=$(_part2_output AnalyticsLambdaRoleArn)
GATEWAY_ROLE_ARN=$(_part2_output GatewayRoleArn)

echo "==> [3/8] 打包并部署数据查询 Lambda（VPC 内直连 Aurora）..."
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
  aws lambda update-function-configuration --function-name "$LAMBDA_NAME" --region "$REGION" \
    --environment "$LAMBDA_ENV" --timeout 60 --memory-size 256 >/dev/null
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

echo "==> [4/8] 创建 Gateway + Lambda target（MCP 工具）..."
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

echo "==> [5/8] 创建 Identity OAuth2 credential provider（M2M）..."
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

echo "==> [6/8] 部署分析 Agent 到 Runtime（JWT 入站认证 + 流式）..."
RUNTIME_AUTH_CONFIG="{\"customJWTAuthorizer\": {\"discoveryUrl\": \"$DISCOVERY_URL\", \"allowedAudience\": [\"analysis-runtime\"]}}"
cd "$PART2_DIR/agent"
agentcore configure --entrypoint main.py --name "$AGENT_NAME" --region "$REGION" \
  --requirements-file requirements.txt --non-interactive --disable-memory \
  --authorizer-config "$RUNTIME_AUTH_CONFIG"
agentcore launch \
  --env GATEWAY_URL="$GATEWAY_URL" \
  --env IDENTITY_PROVIDER_NAME="$IDENTITY_PROVIDER_NAME" \
  ${OPENAI_API_KEY:+--env OPENAI_API_KEY="$OPENAI_API_KEY"} \
  ${AWS_BEARER_TOKEN_BEDROCK:+--env AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK"} \
  ${MODEL_ID:+--env MODEL_ID="$MODEL_ID"} \
  ${OPENAI_BASE_URL:+--env OPENAI_BASE_URL="$OPENAI_BASE_URL"}

echo "==> [7/8] 附加 Runtime 执行角色所需策略..."
ROLE_ARN=$(grep -oE 'arn:aws:iam::[0-9]+:role/[A-Za-z0-9+=,.@_/-]+' .bedrock_agentcore.yaml | head -1 || true)
ROLE_NAME="${ROLE_ARN##*/}"
if [ -n "$ROLE_NAME" ]; then
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
        \"Resource\": \"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:*\"
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
          \"arn:aws:bedrock-agentcore:$REGION:aws:code-interpreter/*\",
          \"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:*\"
        ]
      }, {
        \"Sid\": \"MantleModelInvocation\",
        \"Effect\": \"Allow\",
        \"Action\": [\"bedrock-mantle:CreateInference\"],
        \"Resource\": \"arn:aws:bedrock-mantle:*:$ACCOUNT_ID:project/*\"
      }]
    }"
  echo "    已附加到角色: $ROLE_NAME"
else
  echo "    ⚠️ 未能自动解析执行角色，请手动附加 Identity / Code Interpreter / mantle 权限"
fi
AGENT_ARN=$(grep -oE 'arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]+:runtime/[A-Za-z0-9_-]+' \
  .bedrock_agentcore.yaml | head -1 || true)

echo "==> [8/8] 生成前端配置 web/.env.local ..."
cat > "$PART2_DIR/web/.env.local" <<EOF
VITE_KC_URL=$KC_URL
VITE_KC_REALM=$KC_REALM
VITE_KC_CLIENT_ID=workshop-web
VITE_RUNTIME_ARN=$AGENT_ARN
VITE_AWS_REGION=$REGION
EOF

echo ""
echo "✅ Part 2 部署完成！"
echo ""
echo "   Keycloak:      $KC_URL（管理台 admin / 密码见 $SECRETS_FILE）"
echo "   门户测试账号:   analyst / $ANALYST_PASSWORD"
echo "   Gateway:       $GATEWAY_URL"
echo "   Runtime Agent: $AGENT_ARN"
echo ""
echo "   启动前端: cd $PART2_DIR/web && npm install && npm run dev"
echo "   然后浏览器打开 http://localhost:5173（用上面的测试账号登录）"
echo "   命令行冒烟测试: $SCRIPT_DIR/chat_test.sh '看下最近30天的选品漏斗'"
