#!/usr/bin/env bash
# 清理两个场景的资源（按依赖逆序，best-effort 幂等），保留 CloudLab 基础栈
#（IDE + VPC + Aurora + 写库 Lambda）——适合"重置场景重新练习"。
#
# 一键全部销毁（含 IDE 与数仓）请在操作员机器上执行 ide/destroy-ide.sh：
# 删除 CloudLab 栈时栈内 CleanupOnDelete 钩子会自动做与本脚本等价的清扫。
#
# 顺序：调度 -> Lambda -> AgentCore 资源 -> CloudFormation 子栈 -> ECR -> 日志组
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/00_env.sh"

echo "⚠️  即将删除区域 $REGION 内两个场景的 agentcore-workshop 资源（保留基础栈 $BASE_STACK 与写库 Lambda）。"
if [ "${FORCE:-0}" != "1" ]; then
  read -r -p "确认继续？(yes/N) " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

echo "==> [1/6] 删除 EventBridge Scheduler 调度..."
for s in $(aws scheduler list-schedules --group-name agentcore-workshop-ingestion \
    --region "$REGION" --query 'Schedules[].Name' --output text 2>/dev/null); do
  [ "$s" = "None" ] && continue
  aws scheduler delete-schedule --name "$s" --group-name agentcore-workshop-ingestion \
    --region "$REGION" 2>/dev/null && echo "    已删除调度: $s"
done

echo "==> [2/6] Lambda 函数：全部随 base 栈托管（CFN），重置场景不删除"

echo "==> [3/6] 删除 AgentCore 资源（Runtime / Gateway / Identity / Browser）..."
# Runtime（两个 Agent）
for name in product_ingestion_agent analysis_chat_agent; do
  RT_ID=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='$name'] | [0].agentRuntimeId" --output text 2>/dev/null)
  if [ -n "$RT_ID" ] && [ "$RT_ID" != "None" ]; then
    aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$RT_ID" \
      --region "$REGION" >/dev/null 2>&1 && echo "    已删除 Runtime: $name ($RT_ID)"
  fi
done
# Gateway（先删 target）
GW_ID=$(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query "items[?name=='agentcore-workshop-analytics-gw'].gatewayId" --output text 2>/dev/null \
  | tr '[:space:]' '\n' | grep -v '^None$' | head -1)
if [ -n "$GW_ID" ]; then
  for t in $(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GW_ID" \
      --region "$REGION" --query 'items[].targetId' --output text 2>/dev/null); do
    [ "$t" = "None" ] && continue
    aws bedrock-agentcore-control delete-gateway-target --gateway-identifier "$GW_ID" \
      --target-id "$t" --region "$REGION" >/dev/null 2>&1 && echo "    已删除 Gateway target: $t"
  done
  sleep 5
  aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GW_ID" \
    --region "$REGION" >/dev/null 2>&1 && echo "    已删除 Gateway: $GW_ID"
fi
# Identity provider
aws bedrock-agentcore-control delete-oauth2-credential-provider \
  --name agentcore-workshop-keycloak --region "$REGION" >/dev/null 2>&1 \
  && echo "    已删除 OAuth2 provider: agentcore-workshop-keycloak"
# 自定义 Browser 与 Profile
BR_ID=$(aws bedrock-agentcore-control list-browsers --region "$REGION" --type CUSTOM \
  --query "browserSummaries[?name=='product_hunting_browser'] | [0].browserId" --output text 2>/dev/null)
if [ -n "$BR_ID" ] && [ "$BR_ID" != "None" ]; then
  aws bedrock-agentcore-control delete-browser --browser-id "$BR_ID" --region "$REGION" \
    >/dev/null 2>&1 && echo "    已删除 Browser: $BR_ID"
fi
PF_ID=$(aws bedrock-agentcore-control list-browser-profiles --region "$REGION" \
  --query "profileSummaries[?name=='product_hunting_profile'] | [0].profileId" --output text 2>/dev/null)
if [ -n "$PF_ID" ] && [ "$PF_ID" != "None" ]; then
  aws bedrock-agentcore-control delete-browser-profile --profile-id "$PF_ID" --region "$REGION" \
    >/dev/null 2>&1 && echo "    已删除 Browser profile: $PF_ID"
fi

# Keycloak 基础设施随 base 栈托管：重置场景只需缩容到 0 副本（镜像/栈保留）
KC_CLUSTER=$(stack_output "$BASE_STACK" KeycloakClusterName 2>/dev/null || true)
KC_SERVICE=$(stack_output "$BASE_STACK" KeycloakServiceName 2>/dev/null || true)
if [ -n "$KC_CLUSTER" ] && [ "$KC_CLUSTER" != "None" ]; then
  aws ecs update-service --cluster "$KC_CLUSTER" --service "$KC_SERVICE" \
    --desired-count 0 --region "$REGION" >/dev/null 2>&1 \
    && echo "    Keycloak 服务已缩容到 0（基础设施保留在 $BASE_STACK 栈）"
fi

echo "==> [4/6] 删除场景子栈（按依赖逆序，每个等待完成；基础栈 $BASE_STACK 保留）..."
for stack in agentcore-workshop-part2-tasks agentcore-workshop-part2 \
             agentcore-workshop-keycloak; do
  if aws cloudformation describe-stacks --stack-name "$stack" --region "$REGION" >/dev/null 2>&1; then
    echo "    删除栈: $stack ..."
    aws cloudformation delete-stack --stack-name "$stack" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION" \
      || echo "    ⚠️ $stack 删除超时/失败，请在控制台查看栈事件"
  fi
done

echo "==> [5/6] 删除脚本创建的 IAM 角色与 ECR 仓库..."
aws iam delete-role --role-name agentcore-workshop-browser-role 2>/dev/null \
  && echo "    已删除角色: agentcore-workshop-browser-role" || true
# starter toolkit 自动创建的 Runtime/CodeBuild 角色
for r in $(aws iam list-roles --query "Roles[?starts_with(RoleName, 'AmazonBedrockAgentCoreSDK')].RoleName" --output text 2>/dev/null); do
  [ "$r" = "None" ] && continue
  for p in $(aws iam list-attached-role-policies --role-name "$r" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws iam detach-role-policy --role-name "$r" --policy-arn "$p" 2>/dev/null
  done
  for p in $(aws iam list-role-policies --role-name "$r" --query 'PolicyNames[]' --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name "$r" --policy-name "$p" 2>/dev/null
  done
  aws iam delete-role --role-name "$r" 2>/dev/null && echo "    已删除角色: $r"
done
# keycloak 镜像仓库随 base 栈托管（EmptyOnDelete），重置场景不删，只扫 toolkit 的
for repo in $(aws ecr describe-repositories --region "$REGION" \
    --query "repositories[?starts_with(repositoryName, 'bedrock-agentcore-')].repositoryName" \
    --output text 2>/dev/null); do
  [ "$repo" = "None" ] && continue
  aws ecr delete-repository --repository-name "$repo" --force --region "$REGION" >/dev/null 2>&1 \
    && echo "    已删除 ECR 仓库: $repo"
done

echo "==> [6/6] 删除 CloudWatch 日志组（best-effort；/ecs 的 Keycloak 日志组随 base 栈托管，保留）..."
for prefix in /aws/lambda/agentcore-workshop /aws/bedrock-agentcore; do
  for lg in $(aws logs describe-log-groups --log-group-name-prefix "$prefix" --region "$REGION" \
      --query 'logGroups[].logGroupName' --output text 2>/dev/null); do
    [ "$lg" = "None" ] && continue
    aws logs delete-log-group --log-group-name "$lg" --region "$REGION" 2>/dev/null \
      && echo "    已删除日志组: $lg"
  done
done

# 本地部署产物
rm -f "$WORKSHOP_DIR/part1_data_ingestion/agent/.bedrock_agentcore.yaml" \
      "$WORKSHOP_DIR/part2_chat_portal/agent/.bedrock_agentcore.yaml" 2>/dev/null

echo ""
echo "✅ 场景资源清理完成（基础栈 $BASE_STACK 与写库 Lambda 保留，可直接重跑 02/03）。"
echo "   提示: starter toolkit 可能创建过 bedrock-agentcore-* 前缀的 S3 桶/CodeBuild 项目，"
echo "   如有残留可在控制台确认后删除。"
echo "   一键全部销毁（含 IDE 与数仓）: 在操作员机器上执行 ide/destroy-ide.sh。"
