#!/usr/bin/env bash
# 步骤 1/3（可选）—— 基础环境自检/修复（不需要模型 key）
#
# 说明：基础环境已全部随 CloudLab 单栈预建完成（VPC + Aurora + Secrets 端点 +
# 写库/查询两个 Lambda + 7 张表 + venv/agentcore CLI），**新栈无需执行本脚本，
# 可直接跑 02**。本脚本保留用于：环境自检、workshop 源码更新后刷新 Lambda 代码、
# 或异常状态的幂等修复：
#   a. 工具自检 + Python venv + agentcore CLI（缺则装）
#   b. 重新打包并更新写库 Lambda（agentcore-workshop-writer）
#   c. 建表（invoke setup_tables，幂等 DDL）
#   d. 重新打包并更新通用数据面 Lambda（agentcore-workshop-db）
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/00_env.sh"

PART1_DIR="$WORKSHOP_DIR/part1_data_ingestion"
DEPLOY_DIR="$PART1_DIR/.deploy"
mkdir -p "$DEPLOY_DIR"

echo "==> [a] 工具自检..."
for t in aws git zip python3 jq; do
  command -v "$t" >/dev/null || { echo "❌ 缺少工具: $t"; exit 1; }
done
aws sts get-caller-identity --query Account --output text >/dev/null \
  || { echo "❌ AWS 凭证不可用"; exit 1; }
echo "    区域: ${REGION}（分区 ${PARTITION}）"
require_base_stack
ensure_venv
echo "    agentcore CLI: $(command -v agentcore)"

_stack_output() { stack_output "$BASE_STACK" "$1"; }
CLUSTER_ENDPOINT=$(_stack_output ClusterEndpoint)
SECRET_ARN=$(_stack_output SecretArn)
WRITER_ROLE_ARN=$(_stack_output WriterLambdaRoleArn)
PRIVATE_SUBNET_IDS=$(_stack_output PrivateSubnetIds)
LAMBDA_SG_ID=$(_stack_output LambdaClientSecurityGroupId)
echo "    Aurora 端点: $CLUSTER_ENDPOINT"

echo "==> [b] 打包并部署写库 Lambda（VPC 内 pg8000 直连 Aurora）..."
WRITER_BUILD_DIR="$DEPLOY_DIR/writer-build"
rm -rf "$WRITER_BUILD_DIR" && mkdir -p "$WRITER_BUILD_DIR"
cp "$PART1_DIR/infra/writer-lambda/handler.py" "$WRITER_BUILD_DIR/"
cp "$PART1_DIR/infra/setup_tables.sql" "$WRITER_BUILD_DIR/"
# 锁定依赖版本：scramp 1.5.0（2025-10-25 发布）删除了 pg8000 1.31.5 建 SSL 连接时
# 调用的 make_channel_binding，不锁会让 pip 拉到 1.5.0，每次连库抛 AttributeError
# 被 handler 吞掉重试、表现为无日志的 300s 超时。python-dateutil 同锁以防未来破坏性升级。
pip install -q $PGLIBS_SPEC -t "$WRITER_BUILD_DIR" >/dev/null
rm -f "$DEPLOY_DIR/writer-lambda.zip"
(cd "$WRITER_BUILD_DIR" && zip -qr "$DEPLOY_DIR/writer-lambda.zip" .)

WRITER_ENV="Variables={DB_HOST=$CLUSTER_ENDPOINT,DB_NAME=$DATABASE,DB_SECRET_ARN=$SECRET_ARN}"
if aws lambda get-function --function-name "$WRITER_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$WRITER_NAME" --region "$REGION" \
    --zip-file "fileb://$DEPLOY_DIR/writer-lambda.zip" >/dev/null
  aws lambda wait function-updated --function-name "$WRITER_NAME" --region "$REGION"
  aws lambda update-function-configuration --function-name "$WRITER_NAME" --region "$REGION" \
    --environment "$WRITER_ENV" --timeout 300 --memory-size 256 \
    --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" >/dev/null
  aws lambda wait function-updated --function-name "$WRITER_NAME" --region "$REGION"
  echo "    已更新 Lambda: $WRITER_NAME"
else
  # CloudLab 栈会预建全部数据面函数，正常不会走到这个分支
  echo "    ⚠️ $WRITER_NAME 不存在（栈应已预建）。缺失通常意味着栈不完整或被清理过；"
  echo "       受管学员账号的 SCP 一般会拒绝 lambda:CreateFunction——若下方创建失败，请重建 CloudLab 栈"
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

echo "==> [c] 创建数据表（invoke 写库 Lambda 的 setup_tables，幂等）..."
SETUP_OUT="$DEPLOY_DIR/setup_tables_out.json"
for attempt in $(seq 1 5); do
  if aws lambda invoke --function-name "$WRITER_NAME" --region "$REGION" \
      --cli-binary-format raw-in-base64-out --cli-read-timeout 310 \
      --payload '{"op": "setup_tables"}' "$SETUP_OUT" >/dev/null 2>/tmp/invoke_writer_err \
    && python3 -c "
import json, sys
try:
    r = json.load(open('$SETUP_OUT'))
    assert isinstance(r, dict)
except Exception as e:
    print('    响应无法解析:', e); sys.exit(1)
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

echo "==> [d] 打包并部署通用数据面 Lambda（${DB_QUERY_FN}，查询/验证数据用）..."
# 复用 part2 的通用 db handler（op=execute）；执行角色复用写库角色（同为
# VPC 访问 + 读数据库 Secret）。场景 2 部署时 deploy.sh 会走"已存在则更新"
# 分支原地接管，两边兼容。
DB_BUILD_DIR="$DEPLOY_DIR/db-build"
rm -rf "$DB_BUILD_DIR" && mkdir -p "$DB_BUILD_DIR"
cp "$WORKSHOP_DIR/part2_chat_portal/infra/db-lambda/handler.py" "$DB_BUILD_DIR/"
# 同写库 Lambda：锁 scramp==1.4.17 规避 1.5.0 删除 make_channel_binding 导致的连库超时
pip install -q $PGLIBS_SPEC -t "$DB_BUILD_DIR" >/dev/null
rm -f "$DEPLOY_DIR/db-lambda.zip"
(cd "$DB_BUILD_DIR" && zip -qr "$DEPLOY_DIR/db-lambda.zip" .)
if aws lambda get-function --function-name "$DB_QUERY_FN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$DB_QUERY_FN" --region "$REGION" \
    --zip-file "fileb://$DEPLOY_DIR/db-lambda.zip" >/dev/null
  aws lambda wait function-updated --function-name "$DB_QUERY_FN" --region "$REGION"
  echo "    已更新 Lambda: $DB_QUERY_FN"
else
  # CloudLab 栈会预建全部数据面函数，正常不会走到这个分支（同上，SCP 账号创建会被拒）
  echo "    ⚠️ $DB_QUERY_FN 不存在（栈应已预建），缺失请优先重建 CloudLab 栈"
  for attempt in $(seq 1 10); do
    if aws lambda create-function --function-name "$DB_QUERY_FN" --region "$REGION" \
      --runtime python3.12 --handler handler.lambda_handler \
      --role "$WRITER_ROLE_ARN" \
      --zip-file "fileb://$DEPLOY_DIR/db-lambda.zip" \
      --timeout 300 --memory-size 256 \
      --vpc-config "SubnetIds=$PRIVATE_SUBNET_IDS,SecurityGroupIds=$LAMBDA_SG_ID" \
      --environment "$WRITER_ENV" >/dev/null 2>/tmp/create_db_err; then
      break
    fi
    [ "$attempt" -eq 10 ] && { echo "创建通用数据面 Lambda 失败:"; cat /tmp/create_db_err; exit 1; }
    echo "    等待 IAM 角色生效后重试（$attempt/10）..."
    sleep 10
  done
  aws lambda wait function-active-v2 --function-name "$DB_QUERY_FN" --region "$REGION"
  echo "    已创建 Lambda: $DB_QUERY_FN"
fi

echo ""
echo "✅ 基础环境自检/修复完成！"
echo "   - CloudLab 栈:  ${BASE_STACK}（IDE + VPC + Aurora Serverless v2 PostgreSQL）"
echo "   - 写库 Lambda:  ${WRITER_NAME}（建表已完成，共 7 张表）"
echo "   - 查询 Lambda:  ${DB_QUERY_FN}（数据验证通道）"
echo ""
echo "   下一步（需要模型 key，见运行手册第 3 章）:"
echo "   bash $SCRIPTS_DIR/02_run_scenario1.sh"
