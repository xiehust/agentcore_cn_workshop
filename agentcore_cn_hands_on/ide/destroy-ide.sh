#!/usr/bin/env bash
# 一键销毁：删除 CloudLab 单栈。栈内的 CleanupOnDelete 钩子会先自动清理
# 步骤脚本在栈外创建的全部 workshop 资源（agentcore-workshop-* Lambda、
# AgentCore Runtime/Browser/Gateway/Identity、part2 的 2 个子栈（Keycloak 基础设施已随 base 栈托管）、ECR 仓库、
# SDK 角色、残留 ENI），随后栈内资源（IDE + VPC + Aurora + 资产中转桶）
# 一并删除。最后清掉 CLI 部署用的模板中转桶。
#
# Lambda 的 VPC ENI 由 AWS 异步回收，偶尔超过清理钩子的 15 分钟预算——
# 此时首轮删除会 DELETE_FAILED，本脚本自动清扫已释放的 ENI 并重试删除。
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
STACK_NAME="${WORKSHOP_STACK_NAME:-agentcore-workshop-base}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
TRANSFER_BUCKET="${TRANSFER_BUCKET:-agentcore-workshop-cfn-$ACCOUNT_ID-$REGION}"

echo "⚠️  即将删除栈 ${STACK_NAME}（${REGION}）及其清理钩子覆盖的全部 workshop 资源（含数据库数据）。"
if [ "${FORCE:-0}" != "1" ]; then
  read -r -p "确认继续？(yes/N) " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

# 提前记下数仓 VPC id：DELETE_FAILED 兜底清扫 ENI 时用（删栈后就查不到了）
WORKSHOP_VPC_ID=$(aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" \
  --region "$REGION" --query "StackResources[?LogicalResourceId=='WorkshopVpc'].PhysicalResourceId" \
  --output text 2>/dev/null || true)

_delete_and_wait() {
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
}

# 清扫数仓 VPC 里已释放（available）的 Lambda ENI；in-use 的等 AWS 回收，
# 最多等 20 分钟（Lambda Hyperplane ENI 的回收上限量级）
_sweep_lambda_enis() {
  [ -n "$WORKSHOP_VPC_ID" ] && [ "$WORKSHOP_VPC_ID" != "None" ] || return 0
  echo "==> 清扫数仓 VPC（${WORKSHOP_VPC_ID}）里的残留 Lambda ENI..."
  for _ in $(seq 1 80); do
    remaining=0
    while read -r eni status; do
      [ -n "$eni" ] || continue
      if [ "$status" = "available" ]; then
        aws ec2 delete-network-interface --network-interface-id "$eni" --region "$REGION" 2>/dev/null \
          && echo "    已删除 ENI: $eni" || remaining=$((remaining+1))
      else
        remaining=$((remaining+1))
      fi
    done < <(aws ec2 describe-network-interfaces --region "$REGION" \
      --filters "Name=vpc-id,Values=$WORKSHOP_VPC_ID" \
      --query "NetworkInterfaces[?starts_with(Description, 'AWS Lambda VPC ENI') || InterfaceType=='lambda'].[NetworkInterfaceId,Status]" \
      --output text)
    [ "$remaining" -eq 0 ] && { echo "    Lambda ENI 已清空"; return 0; }
    echo "    仍有 $remaining 个 Lambda ENI 待 AWS 释放，15 秒后重查..."
    sleep 15
  done
  echo "    ⚠️ 等待超时，仍有 Lambda ENI 未释放（重跑本脚本即可）"
}

echo "==> 删除 CloudLab 栈: ${STACK_NAME}（清理钩子 + Aurora 删除约需 10-25 分钟）..."
if ! _delete_and_wait; then
  STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo GONE)
  if [ "$STATUS" = "DELETE_FAILED" ]; then
    echo "⚠️  首轮删除失败（多为 Lambda ENI 异步回收慢），自动清扫后重试..."
    _sweep_lambda_enis
    echo "==> 重试删除栈 ${STACK_NAME} ..."
    _delete_and_wait
  elif [ "$STATUS" != "GONE" ]; then
    echo "❌ 删除未完成，当前状态: ${STATUS}。查看事件:"
    echo "   aws cloudformation describe-stack-events --stack-name $STACK_NAME --region $REGION"
    exit 1
  fi
fi
echo "    栈已删除"

# 门户自签证书（ENABLE_PUBLIC_PORTAL 导入的），栈删除后已无引用，可删
CERT_ARN=$(aws acm list-certificates --region "$REGION" --includes "keyTypes=RSA_2048" \
  --query "CertificateSummaryList[?DomainName=='agentcore-workshop-portal.local'] | [0].CertificateArn" \
  --output text 2>/dev/null || true)
if [ -n "$CERT_ARN" ] && [ "$CERT_ARN" != "None" ]; then
  aws acm delete-certificate --certificate-arn "$CERT_ARN" --region "$REGION" 2>/dev/null \
    && echo "==> 已删除门户自签证书: $CERT_ARN" || true
fi

if aws s3api head-bucket --bucket "$TRANSFER_BUCKET" --region "$REGION" 2>/dev/null; then
  echo "==> 清空并删除模板中转桶: $TRANSFER_BUCKET ..."
  aws s3 rm "s3://$TRANSFER_BUCKET" --recursive --region "$REGION" --only-show-errors
  aws s3api delete-bucket --bucket "$TRANSFER_BUCKET" --region "$REGION"
  echo "    桶已删除"
fi

echo "✅ 全部清理完成"
