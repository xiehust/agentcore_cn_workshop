#!/usr/bin/env bash
# Part 1 一键部署脚本
#
# 步骤：
#   1. 部署 Aurora Serverless v2 + VPC（CloudFormation）
#   2. 打包并部署写库 Lambda（挂 Aurora VPC，pg8000 直连 5432）
#   3. 建表（invoke 写库 Lambda 的 setup_tables 操作）
#   4. 创建自定义 Browser（开启 Web Bot Auth）与 Browser Profile
#   5. 部署 Agent 到 AgentCore Runtime（agentcore CLI）
#   6. 把写库/浏览器访问策略附加到 Runtime 执行角色
#
# 数据面全程不用 RDS Data API（中国区没有该服务）：Agent 与本脚本写数仓都经
# 写库 Lambda。集群的 Data API 仍开着，供 Part 2 与 CLI 查数仓使用。
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
WRITER_NAME="agentcore-workshop-writer"
BROWSER_NAME="product_hunting_browser"
BROWSER_PROFILE_NAME="product_hunting_profile"
BROWSER_ROLE_NAME="agentcore-workshop-browser-role"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART1_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOY_DIR="$PART1_DIR/.deploy"
mkdir -p "$DEPLOY_DIR"

# agentcore CLI 防遮蔽：npm 也有个同名命令，环境里若被它压住，自动改用仓库根
# .venv 里的 Python 版（bedrock-agentcore-starter-toolkit）。识别依据：只有
# Python 版的帮助里列出 configure 子命令（npm 版对任意 --help 都返回 0，
# 不能拿退出码判断）。
_agentcore_ok() { "$1" --help 2>/dev/null | grep -qE '\bconfigure\b'; }
if ! _agentcore_ok agentcore; then
  REPO_VENV_BIN="$(dirname "$PART1_DIR")/.venv/bin"
  if [ -x "$REPO_VENV_BIN/agentcore" ] && _agentcore_ok "$REPO_VENV_BIN/agentcore"; then
    export PATH="$REPO_VENV_BIN:$PATH"
    echo "ℹ️  agentcore 被同名命令遮蔽或未在 PATH，已改用 $REPO_VENV_BIN/agentcore"
  else
    echo "❌ 找不到可用的 agentcore CLI（当前 PATH 里的没有 configure 子命令，可能是 npm 的同名命令）。"
    echo "   请 pip install bedrock-agentcore-starter-toolkit，或激活装了它的 venv 后重跑。"
    exit 1
  fi
fi

_stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# ---- 预检：Secrets Manager 接口端点的归属 ----
# 端点现在归本栈（VPC 拥有者）。老环境里它属于 Part 2 的 analytics 栈，而同一
# VPC 对同一服务只允许一个 Private DNS 端点，必须先让 Part 2 删掉旧的。
# 与其等 CFN CREATE_FAILED 回滚，不如开跑前就报清楚。
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
  EXISTING_VPC_ID=$(_stack_output VpcId)
  if [ -n "$EXISTING_VPC_ID" ] && [ "$EXISTING_VPC_ID" != "None" ]; then
    FOREIGN_ENDPOINTS=$(aws ec2 describe-vpc-endpoints --region "$REGION" \
      --filters "Name=vpc-id,Values=$EXISTING_VPC_ID" \
                "Name=service-name,Values=com.amazonaws.$REGION.secretsmanager" \
      --query "VpcEndpoints[?!(Tags[?Key=='aws:cloudformation:stack-name' && Value=='$STACK_NAME'])].VpcEndpointId" \
      --output text)
    if [ -n "$FOREIGN_ENDPOINTS" ] && [ "$FOREIGN_ENDPOINTS" != "None" ]; then
      echo "❌ VPC $EXISTING_VPC_ID 里已存在不属于本栈的 Secrets Manager 接口端点: $FOREIGN_ENDPOINTS"
      echo "   该端点已挪到本栈管理，同一 VPC 同一服务只允许一个 Private DNS 端点。"
      echo "   请先运行 part2_chat_portal/scripts/deploy.sh 更新 Part 2 栈移除旧端点，再重跑本脚本。"
      exit 1
    fi
  fi
fi

echo "==> [1/6] 部署 Aurora Serverless v2 + VPC（首次约需 10 分钟）..."
aws cloudformation deploy \
  --template-file "$PART1_DIR/infra/aurora-serverless.yaml" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --parameter-overrides "WriterFunctionName=$WRITER_NAME"

POLICY_ARN=$(_stack_output AgentWriterInvokePolicyArn)
DATA_POLICY_ARN=$(_stack_output AgentDataAccessPolicyArn)
CLUSTER_ARN=$(_stack_output ClusterArn)
CLUSTER_ENDPOINT=$(_stack_output ClusterEndpoint)
SECRET_ARN=$(_stack_output SecretArn)
WRITER_ROLE_ARN=$(_stack_output WriterLambdaRoleArn)
PRIVATE_SUBNET_IDS=$(_stack_output PrivateSubnetIds)
LAMBDA_SG_ID=$(_stack_output LambdaClientSecurityGroupId)
echo "    写库 Lambda 调用策略: $POLICY_ARN"
echo "    Aurora 集群: $CLUSTER_ARN"

echo "==> [2/6] 打包并部署写库 Lambda（VPC 内 pg8000 直连 Aurora）..."
WRITER_BUILD_DIR="$DEPLOY_DIR/writer-build"
rm -rf "$WRITER_BUILD_DIR" && mkdir -p "$WRITER_BUILD_DIR"
cp "$PART1_DIR/infra/writer-lambda/handler.py" "$WRITER_BUILD_DIR/"
# setup_tables.sql 一起打包：建表也由这个函数执行（步骤 3）
cp "$PART1_DIR/infra/setup_tables.sql" "$WRITER_BUILD_DIR/"
pip install -q pg8000 -t "$WRITER_BUILD_DIR" >/dev/null
rm -f "$DEPLOY_DIR/writer-lambda.zip"
(cd "$WRITER_BUILD_DIR" && zip -qr "$DEPLOY_DIR/writer-lambda.zip" .)

# timeout 300 秒：覆盖 handler 内最长 240 秒的冷恢复重连 + 语句执行
WRITER_ENV="Variables={DB_HOST=$CLUSTER_ENDPOINT,DB_NAME=$DATABASE,DB_SECRET_ARN=$SECRET_ARN}"
if aws lambda get-function --function-name "$WRITER_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$WRITER_NAME" --region "$REGION" \
    --zip-file "fileb://$DEPLOY_DIR/writer-lambda.zip" >/dev/null
  aws lambda wait function-updated --function-name "$WRITER_NAME" --region "$REGION"
  # vpc-config 一起更新：Aurora 栈重建过（VPC/子网/SG 换了）时让函数收敛回来
  aws lambda update-function-configuration --function-name "$WRITER_NAME" --region "$REGION" \
    --environment "$WRITER_ENV" --timeout 300 --memory-size 256 \
    --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" >/dev/null
  aws lambda wait function-updated --function-name "$WRITER_NAME" --region "$REGION"
  echo "    已更新 Lambda: $WRITER_NAME"
else
  # 新建 IAM 角色到 Lambda 可见有延迟，带重试
  for attempt in $(seq 1 10); do
    if aws lambda create-function --function-name "$WRITER_NAME" --region "$REGION" \
      --runtime python3.12 --handler handler.lambda_handler \
      --role "$WRITER_ROLE_ARN" \
      --zip-file "fileb://$DEPLOY_DIR/writer-lambda.zip" \
      --timeout 300 --memory-size 256 \
      --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" \
      --environment "$WRITER_ENV" >/dev/null 2>/tmp/create_writer_err; then
      break
    fi
    [ "$attempt" -eq 10 ] && { echo "创建写库 Lambda 失败:"; cat /tmp/create_writer_err; exit 1; }
    echo "    等待 IAM 角色生效后重试（$attempt/10）..."
    sleep 10
  done
  aws lambda wait function-active-v2 --function-name "$WRITER_NAME" --region "$REGION"
  echo "    已创建 Lambda: $WRITER_NAME"
fi

echo "==> [3/6] 创建数据表（invoke 写库 Lambda 的 setup_tables）..."
# DDL 全是 IF NOT EXISTS 系语句，幂等可重跑。新建函数首次调用可能撞 ENI 就绪
# 延迟，集群空闲暂停时还要等冷恢复（handler 内最长等 240 秒），所以带重试。
SETUP_OUT="$DEPLOY_DIR/setup_tables_out.json"
for attempt in $(seq 1 5); do
  if aws lambda invoke --function-name "$WRITER_NAME" --region "$REGION" \
      --cli-binary-format raw-in-base64-out \
      --payload '{"op": "setup_tables"}' "$SETUP_OUT" >/dev/null 2>/tmp/invoke_writer_err \
    && python3 -c "
import json, sys
try:
    r = json.load(open('$SETUP_OUT'))
    assert isinstance(r, dict)
except Exception as e:
    print('    响应无法解析:', e); sys.exit(1)
# errorMessage 是 Lambda 未捕获异常（含超时）的错误信封，与 handler 的 error 区分
print('   ', r.get('detail') or r.get('error') or r.get('errorMessage') or r)
sys.exit(0 if r.get('status') == 'ok' else 1)
"; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    echo "建表失败:"; cat /tmp/invoke_writer_err 2>/dev/null; cat "$SETUP_OUT" 2>/dev/null; exit 1
  fi
  echo "    等待 Lambda 网络/集群就绪后重试（$attempt/5）..."
  sleep 15
done
echo "    建表完成（selection_verdicts / product_candidates + Part 2 的五张表）"

echo "==> [4/6] 创建自定义 Browser（Web Bot Auth）与 Browser Profile..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 4a. 自定义 browser 的执行角色：Web Bot Auth 只要求信任策略，无需附加权限策略
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

# 4b. 自定义 browser（开启请求签名 Web Bot Auth，预览功能）；按名字幂等
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

# 4c. browser profile（跨会话保留 cookies/localStorage）；按名字幂等
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

echo "==> [5/6] 部署 Agent 到 AgentCore Runtime..."
cd "$PART1_DIR/agent"
# configure：生成 .bedrock_agentcore.yaml（自动创建执行角色 / ECR 仓库）
# --disable-memory：不自动创建 AgentCore Memory（本 demo 不用自带记忆，
# Agent 每次调用无状态，历史数据都在 Aurora）
agentcore configure --entrypoint main.py --name "$AGENT_NAME" --region "$REGION" \
  --requirements-file requirements.txt --non-interactive --disable-memory
# launch：默认 direct_code_deploy 模式，直接把代码部署到 Runtime（本机无需 Docker）
#（MODEL_ID / OPENAI_BASE_URL 不设则用 main.py 内置默认值）
agentcore launch \
  --env WRITER_LAMBDA_NAME="$WRITER_NAME" \
  ${OPENAI_API_KEY:+--env OPENAI_API_KEY="$OPENAI_API_KEY"} \
  ${AWS_BEARER_TOKEN_BEDROCK:+--env AWS_BEARER_TOKEN_BEDROCK="$AWS_BEARER_TOKEN_BEDROCK"} \
  --env BROWSER_IDENTIFIER="$BROWSER_ID" \
  --env BROWSER_PROFILE_ID="$PROFILE_ID" \
  ${MODEL_ID:+--env MODEL_ID="$MODEL_ID"} \
  ${OPENAI_BASE_URL:+--env OPENAI_BASE_URL="$OPENAI_BASE_URL"}

echo "==> [6/6] 附加写库/浏览器访问策略到 Runtime 执行角色..."
# launch 完成后 .bedrock_agentcore.yaml 中会写入自动创建的执行角色 ARN
ROLE_ARN=$(grep -oE 'arn:aws:iam::[0-9]+:role/[A-Za-z0-9+=,.@_/-]+' .bedrock_agentcore.yaml | head -1 || true)
ROLE_NAME="${ROLE_ARN##*/}"
if [ -n "$ROLE_NAME" ]; then
  # 写数仓只需要「能调写库 Lambda」，不再需要 rds-data 与数据库凭证
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
  # 老环境上挂过的 Data API 策略在此摘掉（该策略如今只服务 Part 2）；
  # 没挂过时 detach 会报 NoSuchEntity，忽略即可
  if [ -n "$DATA_POLICY_ARN" ] && [ "$DATA_POLICY_ARN" != "None" ]; then
    aws iam detach-role-policy --role-name "$ROLE_NAME" \
      --policy-arn "$DATA_POLICY_ARN" >/dev/null 2>&1 || true
  fi
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
echo "✅ 部署完成！"
echo "   写库 Lambda: $WRITER_NAME（Agent 与建表都经它写数仓）"
echo ""
echo "   测试调用:"
echo "   agentcore invoke '{\"prompt\": \"从 eBay 英国站搜索 desk organizer 选品，处理前 2 个非广告商品\"}'"
