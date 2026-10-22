#!/usr/bin/env bash
# 步骤 2/3 —— 运行场景 1：选品数据摄入 Pipeline（需要模型 key）
#
# 内容（抽离自 part1_data_ingestion/scripts/deploy.sh 的第 4-6 步）：
#   a. 创建自定义 Browser（Web Bot Auth）与 Browser Profile
#   b. 部署数据摄入 Agent 到 AgentCore Runtime（agentcore configure + launch）
#   c. 附加写库/浏览器访问策略到 Runtime 执行角色
# 不做冒烟调用；需要验证时手动执行 invoke_verify.sh（调用 + 自动展示入库结果）
#
# 前置：01_deploy_base.sh 已执行；export 模型 key（AWS_BEARER_TOKEN_BEDROCK 或
#       OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID 三元组）
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/00_env.sh"

PART1_DIR="$WORKSHOP_DIR/part1_data_ingestion"
AGENT_NAME="product_ingestion_agent"
BROWSER_NAME="product_hunting_browser"
BROWSER_PROFILE_NAME="product_hunting_profile"
BROWSER_ROLE_NAME="agentcore-workshop-browser-role"

ensure_venv
require_model_key

# ---- 前置检查：基础环境就绪 ----
require_base_stack
POLICY_ARN=$(stack_output "$BASE_STACK" AgentWriterInvokePolicyArn)
aws lambda get-function --function-name "$WRITER_NAME" --region "$REGION" >/dev/null 2>&1 \
  || { echo "❌ 写库 Lambda $WRITER_NAME 不存在，请先执行 01_deploy_base.sh"; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "==> [a] 创建自定义 Browser（Web Bot Auth）与 Browser Profile..."
# a1. 自定义 browser 的执行角色：Web Bot Auth 只要求信任策略，无需附加权限策略。
# IAM 角色名全局唯一，SourceArn 必须区域通配（锁账号即可）——锁区域会让同账号
# 第二个区域复用该角色时 CreateBrowser 校验失败（实测：脚本会一路重试到失败）；
# 角色已存在时同样收敛信任策略，自愈旧版脚本创建的锁区域角色
BROWSER_TRUST_POLICY="{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"BedrockAgentCoreBuiltInTools\",
    \"Effect\": \"Allow\",
    \"Principal\": {\"Service\": \"bedrock-agentcore.amazonaws.com\"},
    \"Action\": \"sts:AssumeRole\",
    \"Condition\": {
      \"StringEquals\": {\"aws:SourceAccount\": \"$ACCOUNT_ID\"},
      \"ArnLike\": {\"aws:SourceArn\": \"arn:$PARTITION:bedrock-agentcore:*:$ACCOUNT_ID:*\"}
    }
  }]
}"
if ! aws iam get-role --role-name "$BROWSER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$BROWSER_ROLE_NAME" \
    --assume-role-policy-document "$BROWSER_TRUST_POLICY" >/dev/null
  echo "    已创建 browser 执行角色: $BROWSER_ROLE_NAME"
else
  aws iam update-assume-role-policy --role-name "$BROWSER_ROLE_NAME" \
    --policy-document "$BROWSER_TRUST_POLICY"
fi
BROWSER_ROLE_ARN=$(aws iam get-role --role-name "$BROWSER_ROLE_NAME" --query Role.Arn --output text)

# a2. 自定义 browser（开启请求签名 Web Bot Auth）；按名字幂等
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

# a3. browser profile（跨会话保留 cookies/localStorage）；按名字幂等
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

echo "==> [b] 部署数据摄入 Agent 到 AgentCore Runtime..."
cd "$PART1_DIR/agent"
# --disable-memory：本场景 Agent 无状态，历史数据都在 Aurora
# 显式 direct_code_deploy：toolkit 0.3.13 起默认 container+CodeBuild，
# 而 aws-cn 分区无 CodeBuild ARM_CONTAINER；直接代码部署也免本机 Docker
agentcore configure --entrypoint main.py --name "$AGENT_NAME" --region "$REGION" \
  --requirements-file requirements.txt --non-interactive --disable-memory \
  --deployment-type direct_code_deploy
agentcore launch --auto-update-on-conflict \
  --env WRITER_LAMBDA_NAME="$WRITER_NAME" \
  ${OPENAI_API_KEY:+--env OPENAI_API_KEY="$OPENAI_API_KEY"} \
  ${AWS_BEARER_TOKEN_BEDROCK:+--env AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK"} \
  --env BROWSER_IDENTIFIER="$BROWSER_ID" \
  --env BROWSER_PROFILE_ID="$PROFILE_ID" \
  ${MODEL_ID:+--env MODEL_ID="$MODEL_ID"} \
  ${MODEL_REASONING_EFFORT:+--env MODEL_REASONING_EFFORT="$MODEL_REASONING_EFFORT"} \
  ${OPENAI_BASE_URL:+--env OPENAI_BASE_URL="$OPENAI_BASE_URL"} \
  ${BEDROCK_AGENTCORE_DP_ENDPOINT:+--env BEDROCK_AGENTCORE_DP_ENDPOINT="$BEDROCK_AGENTCORE_DP_ENDPOINT"}

echo "==> [c] 附加写库/浏览器访问策略到 Runtime 执行角色..."
#（正则做分区通配：aws-cn 分区的 ARN 是 arn:aws-cn:iam::...）
ROLE_ARN=$(grep -oE 'arn:aws[a-z-]*:iam::[0-9]+:role/[A-Za-z0-9+=,.@_/-]+' .bedrock_agentcore.yaml | head -1 || true)
ROLE_NAME="${ROLE_ARN##*/}"
if [ -n "$ROLE_NAME" ]; then
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
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
          \"arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:browser/*\",
          \"arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:browser-custom/*\",
          \"arn:$PARTITION:bedrock-agentcore:$REGION:$ACCOUNT_ID:browser-profile/*\"
        ]
      }]
    }"
  echo "    已附加到角色: $ROLE_NAME"
else
  echo "    ⚠️ 未能自动解析执行角色，请手动执行:"
  echo "    aws iam attach-role-policy --role-name <Runtime执行角色> --policy-arn $POLICY_ARN"
fi

echo ""
echo "✅ 场景 1 部署完成！Agent: $AGENT_NAME"
