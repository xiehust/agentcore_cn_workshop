#!/usr/bin/env bash
# 调用已部署 Agent 的示例集合。
# 前置：已执行 deploy.sh，agentcore CLI 在 agent/ 目录下能找到 .bedrock_agentcore.yaml
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../agent"

# 示例 1：按关键词搜索选品（eBay 英国站，默认站点）
agentcore invoke '{"prompt": "从 eBay 英国站搜索 desk organizer 选品，处理前 2 个非广告商品，按选品 SOP 完成判定并入库"}'

# 示例 2：eBay 美国站（明确写 US / United States / ebay.com）
# agentcore invoke '{"prompt": "从 eBay US（ebay.com）搜索 desk organizer 选品，处理前 2 个非广告一口价商品，按选品 SOP 完成判定并入库"}'

# 示例 3：按类目浏览选品
# agentcore invoke '{"prompt": "在 eBay 英国站 Home, Furniture & DIY 类目下选品，处理前 2 个非广告一口价商品并入库"}'

# 示例 4：指定单个商品 URL 判定
# agentcore invoke '{"prompt": "对这个商品做选品判定并入库: https://www.ebay.co.uk/itm/XXXXXXXXXXXX"}'

# 示例 5：亚马逊站点（需先配置住宅代理，否则会被 503 拦截，见 README 常见问题）
# agentcore invoke '{"prompt": "从亚马逊英国站搜索 desk organizer，对第 1 个非广告商品做选品判定并入库"}'

# 验证入库结果（RDS Data API 查询，同步返回结果）：
# CLUSTER_ARN/SECRET_ARN 从 CloudFormation 输出获取：
#   aws cloudformation describe-stacks --stack-name agentcore-workshop-aurora \
#     --region us-west-2 --query "Stacks[0].Outputs"
# aws rds-data execute-statement \
#   --resource-arn <ClusterArn> --secret-arn <SecretArn> \
#   --database product_hunting \
#   --sql "SELECT verdict, reason, product_title FROM selection_verdicts ORDER BY created_at DESC LIMIT 10" \
#   --region us-west-2
