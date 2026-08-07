#!/usr/bin/env bash
# Part 1 一键部署脚本
#
# 步骤：
#   1. 部署 Aurora Serverless v2 + VPC（CloudFormation）
#   2. 建表（RDS Data API）
#   3. 创建自定义 Browser（开启 Web Bot Auth）与 Browser Profile
#   4. 部署 Agent 到 AgentCore Runtime（agentcore CLI）
#   5. 把数仓/浏览器访问策略附加到 Runtime 执行角色
#
# 前置：aws CLI 已配置凭证；pip install bedrock-agentcore-starter-toolkit
# 模型认证（二选一）：export OPENAI_API_KEY=<Bedrock API key>；
# 或不设置，Agent 用 Runtime IAM 角色自动铸 token（推荐，免 key 管理）
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
# Bedrock API key 以 ABSK 开头；环境里若残留 OpenAI 官网的 sk- key，
# 透传进 Runtime 会压过 AWS_BEARER_TOKEN_BEDROCK 导致模型 401，这里直接忽略
if [ -n "${OPENAI_API_KEY:-}" ] && [[ "${OPENAI_API_KEY}" != ABSK* ]]; then
  echo "⚠️  OPENAI_API_KEY 不是 Bedrock API key（非 ABSK 前缀），忽略；改用 AWS_BEARER_TOKEN_BEDROCK / IAM"
  unset OPENAI_API_KEY
fi
if [ -n "${OPENAI_API_KEY:-}" ]; then
  echo "ℹ️  使用显式 OPENAI_API_KEY 认证模型（须是 Bedrock API key，不是 OpenAI 官网 sk- key）"
elif [ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]; then
  echo "ℹ️  使用 AWS_BEARER_TOKEN_BEDROCK（Bedrock API key）认证模型"
else
  echo "ℹ️  未设置 API key，模型调用将用 Runtime IAM 角色自动铸 token"
fi
STACK_NAME="agentcore-workshop-aurora"
DATABASE="product_hunting"
AGENT_NAME="product_ingestion_agent"
BROWSER_NAME="product_hunting_browser"
BROWSER_PROFILE_NAME="product_hunting_profile"
BROWSER_ROLE_NAME="agentcore-workshop-browser-role"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART1_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> [1/5] 部署 Aurora Serverless v2 + VPC（首次约需 10 分钟）..."
aws cloudformation deploy \
  --template-file "$PART1_DIR/infra/aurora-serverless.yaml" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION"

_stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
POLICY_ARN=$(_stack_output AgentDataAccessPolicyArn)
CLUSTER_ARN=$(_stack_output ClusterArn)
SECRET_ARN=$(_stack_output SecretArn)
echo "    数仓访问策略: $POLICY_ARN"
echo "    Aurora 集群: $CLUSTER_ARN"

echo "==> [2/5] 创建数据表..."
# rds-data 一次只能执行一条语句：去掉注释后按分号拆分逐条执行。
# 集群空闲自动暂停（MinCapacity=0），首条语句可能触发冷恢复，带重试。
mapfile -t STMTS < <(sed 's/--.*//' "$PART1_DIR/infra/setup_tables.sql" | tr '\n' ' ' | tr ';' '\n' | grep -vE '^\s*$')
for stmt in "${STMTS[@]}"; do
  for attempt in $(seq 1 24); do
    if aws rds-data execute-statement --region "$REGION" \
      --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
      --database "$DATABASE" --sql "$stmt" >/dev/null 2>/tmp/rds_data_err; then
      break
    fi
    if [ "$attempt" -eq 24 ]; then
      echo "建表失败:"; cat /tmp/rds_data_err; exit 1
    fi
    echo "    等待集群恢复后重试（$attempt/24）..."
    sleep 10
  done
done
echo "    建表完成（selection_verdicts / product_candidates）"

echo "==> [3/5] 创建自定义 Browser（Web Bot Auth）与 Browser Profile..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 3a. 自定义 browser 的执行角色：Web Bot Auth 只要求信任策略，无需附加权限策略
if ! aws iam get-role --role-name "$BROWSER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$BROWSER_ROLE_NAME" \
    --assume-role-policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"BedrockAgentCoreBuiltInTools\",
        \"Effect\": \"Allow\",
        \"Principal\": {\"Service\": \"bedrock-agentcore.amazonaws.com\"},
        \"Action\": \"sts:AssumeRole\",
        \"Condition\": {
          \"StringEquals\": {\"aws:SourceAccount\": \"$ACCOUNT_ID\"},
          \"ArnLike\": {\"aws:SourceArn\": \"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:*\"}
        }
      }]
    }" >/dev/null
  echo "    已创建 browser 执行角色: $BROWSER_ROLE_NAME"
fi
BROWSER_ROLE_ARN=$(aws iam get-role --role-name "$BROWSER_ROLE_NAME" --query Role.Arn --output text)

# 3b. 自定义 browser（开启请求签名 Web Bot Auth，预览功能）；按名字幂等
BROWSER_ID=$(aws bedrock-agentcore-control list-browsers --region "$REGION" --type CUSTOM \
  --query "browserSummaries[?name=='$BROWSER_NAME'] | [0].browserId" --output text)
if [ -z "$BROWSER_ID" ] || [ "$BROWSER_ID" = "None" ]; then
  # 新建 IAM 角色到全区域可见有延迟，CreateBrowser 校验信任策略可能暂时失败，重试即可
  for attempt in $(seq 1 10); do
    if BROWSER_ID=$(aws bedrock-agentcore-control create-browser --region "$REGION" \
      --name "$BROWSER_NAME" \
      --description "Workshop browser with Web Bot Auth" \
      --network-configuration '{"networkMode": "PUBLIC"}' \
      --execution-role-arn "$BROWSER_ROLE_ARN" \
      --browser-signing '{"enabled": true}' \
      --query browserId --output text 2>/tmp/create_browser_err); then
      break
    fi
    if [ "$attempt" -eq 10 ]; then
      echo "创建 browser 失败:"; cat /tmp/create_browser_err; exit 1
    fi
    echo "    等待 IAM 角色生效后重试（$attempt/10）..."
    sleep 10
  done
  echo "    已创建自定义 browser: $BROWSER_ID"
else
  echo "    复用已有 browser: $BROWSER_ID"
fi

# 3c. browser profile（跨会话保留 cookies/localStorage）；按名字幂等
PROFILE_ID=$(aws bedrock-agentcore-control list-browser-profiles --region "$REGION" \
  --query "profileSummaries[?name=='$BROWSER_PROFILE_NAME'] | [0].profileId" --output text)
if [ -z "$PROFILE_ID" ] || [ "$PROFILE_ID" = "None" ]; then
  PROFILE_ID=$(aws bedrock-agentcore-control create-browser-profile --region "$REGION" \
    --name "$BROWSER_PROFILE_NAME" \
    --description "Workshop browser profile" \
    --query profileId --output text)
  echo "    已创建 browser profile: $PROFILE_ID"
else
  echo "    复用已有 profile: $PROFILE_ID"
fi

echo "==> [4/5] 部署 Agent 到 AgentCore Runtime..."
cd "$PART1_DIR/agent"
# configure：生成 .bedrock_agentcore.yaml（自动创建执行角色 / ECR 仓库）
# --disable-memory：不自动创建 AgentCore Memory（本 demo 不用自带记忆，
# Agent 每次调用无状态，历史数据都在 Aurora）
agentcore configure --entrypoint main.py --name "$AGENT_NAME" --region "$REGION" \
  --requirements-file requirements.txt --non-interactive --disable-memory
# launch：默认 direct_code_deploy 模式，直接把代码部署到 Runtime（本机无需 Docker）
#（MODEL_ID / OPENAI_BASE_URL 不设则用 main.py 内置默认值）
agentcore launch \
  --env AURORA_CLUSTER_ARN="$CLUSTER_ARN" \
  --env AURORA_SECRET_ARN="$SECRET_ARN" \
  --env AURORA_DATABASE="$DATABASE" \
  ${OPENAI_API_KEY:+--env OPENAI_API_KEY="$OPENAI_API_KEY"} \
  ${AWS_BEARER_TOKEN_BEDROCK:+--env AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK"} \
  --env BROWSER_IDENTIFIER="$BROWSER_ID" \
  --env BROWSER_PROFILE_ID="$PROFILE_ID" \
  ${MODEL_ID:+--env MODEL_ID="$MODEL_ID"} \
  ${OPENAI_BASE_URL:+--env OPENAI_BASE_URL="$OPENAI_BASE_URL"}

echo "==> [5/5] 附加数仓/浏览器访问策略到 Runtime 执行角色..."
# launch 完成后 .bedrock_agentcore.yaml 中会写入自动创建的执行角色 ARN
ROLE_ARN=$(grep -oE 'arn:aws:iam::[0-9]+:role/[A-Za-z0-9+=,.@_/-]+' .bedrock_agentcore.yaml | head -1 || true)
ROLE_NAME="${ROLE_ARN##*/}"
if [ -n "$ROLE_NAME" ]; then
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
  # 自定义 browser 与 profile 的数据面权限（CLI 自动生成的角色只覆盖默认浏览器）
  aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name browser-profile-access \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"BrowserProfileUsage\",
        \"Effect\": \"Allow\",
        \"Action\": [
          \"bedrock-agentcore:StartBrowserSession\",
          \"bedrock-agentcore:StopBrowserSession\",
          \"bedrock-agentcore:GetBrowserSession\",
          \"bedrock-agentcore:ListBrowserSessions\",
          \"bedrock-agentcore:ConnectBrowserAutomationStream\",
          \"bedrock-agentcore:SaveBrowserSessionProfile\"
        ],
        \"Resource\": [
          \"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:browser/*\",
          \"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:browser-custom/*\",
          \"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:browser-profile/*\"
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
  echo "    ⚠️ 未能自动解析执行角色，请手动执行:"
  echo "    aws iam attach-role-policy --role-name <Runtime执行角色> --policy-arn $POLICY_ARN"
fi

echo ""
echo "✅ 部署完成！测试调用:"
echo "   agentcore invoke '{\"prompt\": \"从 eBay 英国站搜索 desk organizer 选品，处理前 2 个非广告商品\"}'"
