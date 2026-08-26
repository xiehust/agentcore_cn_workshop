# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
#
# 中国区环境变量助手 —— source 使用（两个 Part 的 scripts/ 各放一份，内容相同）：
#     export AWS_REGION=cn-northwest-1 && source scripts/cn_env.sh
#
# 作用：bedrock-agentcore SDK 与 starter-toolkit 库内硬编码 .amazonaws.com 端点，
# 中国区需用官方环境变量覆盖。deploy.sh 已内置同款推导；本文件服务**交互路径**
# （agentcore invoke / agentcore status / python scripts/local_test.py 等直接
# 调 CLI/SDK 的场景）。海外区 source 本文件是空操作。
case "${AWS_REGION:-}" in
  cn-*)
    export BEDROCK_AGENTCORE_CP_ENDPOINT="https://bedrock-agentcore-control.${AWS_REGION}.amazonaws.com.cn"
    export BEDROCK_AGENTCORE_DP_ENDPOINT="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com.cn"
    echo "ℹ️  已设置中国区 AgentCore 端点覆盖（CP/DP → ${AWS_REGION}.amazonaws.com.cn）"
    ;;
esac
