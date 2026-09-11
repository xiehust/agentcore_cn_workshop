#!/usr/bin/env bash
# 发布 workshop 资产到分发账号（global profile）的 CloudFront 私有桶。
# 发布后，任何账号只用 cloudlab-template-china.yaml 一个文件即可建栈：
# 栈内的 AssetFetch 自定义资源会从 CloudFront 拉 Lambda 包，IDE 实例引导
# 直接 curl 代码总包。
#
# 上传清单:
#   workshop-assets.tar.gz            代码总包（IDE 实例引导 curl 下载）
#   lambda/{writer,db,analytics,memory,tasksapi,runner}-lambda.zip
#                                     6 个数据面 Lambda（栈内 AssetFetch 拉取中转）
#   cloudlab-template-china.yaml      模板本体（供分发：curl -O 后即可部署）
#
# 用法:
#   bash publish-assets.sh
#   可选环境变量:
#     PUBLISH_PROFILE  分发账号的 aws profile（默认 global）
#     PUBLISH_BUCKET   目标桶（默认 private-cf-demo-888577020934）
#     CDN_BASE_URL     CloudFront 域名（默认 https://dbr8mrv63fpuw.cloudfront.net）
#     CDN_DIST_ID      CloudFront 分配 ID，用于失效缓存（默认 E1PJYVU6MI701Y）
#
# 前置：本机 python3 + zip；与 agentcore_cn_workshop、agentcore_cn_hands_on
#       同级目录结构（脚本自动定位）。
set -euo pipefail

PROFILE="${PUBLISH_PROFILE:-global}"
BUCKET="${PUBLISH_BUCKET:-private-cf-demo-888577020934}"
CDN="${CDN_BASE_URL:-https://dbr8mrv63fpuw.cloudfront.net}"
DIST_ID="${CDN_DIST_ID:-E1PJYVU6MI701Y}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDS_ON_DIR="$(dirname "$SCRIPT_DIR")"                      # agentcore_cn_hands_on
ROOT_DIR="$(dirname "$HANDS_ON_DIR")"                        # 上级：包含两个文件夹
WORKSHOP_DIR="$ROOT_DIR/agentcore_cn_workshop"
TEMPLATE="$HANDS_ON_DIR/cloudformation/cloudlab-template-china.yaml"

[ -d "$WORKSHOP_DIR" ] || { echo "❌ 未找到 $WORKSHOP_DIR"; exit 1; }
[ -f "$TEMPLATE" ] || { echo "❌ 未找到 $TEMPLATE"; exit 1; }
aws sts get-caller-identity --profile "$PROFILE" >/dev/null \
  || { echo "❌ profile '$PROFILE' 凭证不可用"; exit 1; }

echo "==> [1/4] 打包 workshop 代码总包（排除 .venv/.deploy/node_modules/.git 等本地产物）..."
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
# COPYFILE_DISABLE=1：禁止 macOS tar 写入 AppleDouble 元数据（._xxx 文件）
COPYFILE_DISABLE=1 tar czf "$BUILD/workshop-assets.tar.gz" -C "$ROOT_DIR" \
  --exclude='._*' \
  --exclude='.venv' \
  --exclude='.deploy' \
  --exclude='node_modules' \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='*.bedrock_agentcore.yaml' \
  --exclude='.env.local' \
  agentcore_cn_workshop agentcore_cn_hands_on
echo "    $(du -h "$BUILD/workshop-assets.tar.gz" | cut -f1) workshop-assets.tar.gz"

echo "==> [2/4] 构建 6 个数据面 Lambda 包（pg8000 打包）..."
python3 -m venv "$BUILD/venv" && "$BUILD/venv/bin/pip" -q install pg8000 -t "$BUILD/deps"
# 写库 Lambda：handler + 建表 SQL + pg8000
mkdir -p "$BUILD/writer" && cp -R "$BUILD/deps/." "$BUILD/writer/"
cp "$WORKSHOP_DIR/part1_data_ingestion/infra/writer-lambda/handler.py" \
   "$WORKSHOP_DIR/part1_data_ingestion/infra/setup_tables.sql" "$BUILD/writer/"
(cd "$BUILD/writer" && zip -qr "$BUILD/writer-lambda.zip" .)
# 通用数据面 / 分析 / 记忆 Lambda：handler + pg8000
for pair in "db:part2_chat_portal/infra/db-lambda/handler.py" \
            "analytics:part2_chat_portal/infra/analytics-lambda/handler.py" \
            "memory:part2_chat_portal/infra/memory-lambda/handler.py"; do
  name="${pair%%:*}"; src="${pair#*:}"
  mkdir -p "$BUILD/$name" && cp -R "$BUILD/deps/." "$BUILD/$name/"
  cp "$WORKSHOP_DIR/$src" "$BUILD/$name/"
  (cd "$BUILD/$name" && zip -qr "$BUILD/$name-lambda.zip" .)
done
# tasks-api / runner：单文件（只依赖 runtime 自带 boto3）
for pair in "tasksapi:part2_chat_portal/infra/tasks-api-lambda/handler.py" \
            "runner:part2_chat_portal/infra/runner-lambda/handler.py"; do
  name="${pair%%:*}"; src="${pair#*:}"
  mkdir -p "$BUILD/$name" && cp "$WORKSHOP_DIR/$src" "$BUILD/$name/"
  (cd "$BUILD/$name" && zip -qr "$BUILD/$name-lambda.zip" .)
done

echo "==> [3/4] 上传到 s3://${BUCKET}（profile: ${PROFILE}）..."
aws s3 cp "$BUILD/workshop-assets.tar.gz" "s3://$BUCKET/workshop-assets.tar.gz" \
  --profile "$PROFILE" --only-show-errors
for z in writer db analytics memory tasksapi runner; do
  aws s3 cp "$BUILD/$z-lambda.zip" "s3://$BUCKET/lambda/$z-lambda.zip" \
    --profile "$PROFILE" --only-show-errors
done
aws s3 cp "$TEMPLATE" "s3://$BUCKET/cloudlab-template-china.yaml" \
  --profile "$PROFILE" --only-show-errors --content-type text/yaml
echo "    已上传 8 个对象"

echo "==> [4/4] CloudFront 缓存失效并验证下载链路..."
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/workshop-assets.tar.gz" "/lambda/*" "/cloudlab-template-china.yaml" \
  --profile "$PROFILE" --query 'Invalidation.Id' --output text
FAIL=0
for path in workshop-assets.tar.gz lambda/writer-lambda.zip lambda/db-lambda.zip \
            lambda/analytics-lambda.zip lambda/memory-lambda.zip \
            lambda/tasksapi-lambda.zip lambda/runner-lambda.zip \
            cloudlab-template-china.yaml; do
  code=$(curl -sS -o /dev/null -w "%{http_code}" -m 30 -r 0-0 "$CDN/$path" || echo 000)
  # Range 请求命中返回 206，未开 Range 支持时 200 也算通
  if [ "$code" = "206" ] || [ "$code" = "200" ]; then
    echo "    ✅ $CDN/$path"
  else
    echo "    ❌ $CDN/$path (HTTP $code)"; FAIL=1
  fi
done
[ "$FAIL" = "0" ] || { echo "❌ 部分文件经 CDN 不可下载，检查桶策略/分配状态"; exit 1; }

echo ""
echo "✅ 发布完成！参与者账号一键部署（模板已含默认资产地址，二选一）:"
echo ""
echo "   方式 A（控制台）: 下载模板后在 CloudFormation 控制台「上传模板文件」创建栈"
echo "     curl -O $CDN/cloudlab-template-china.yaml"
echo ""
echo "   方式 B（CLI，模板超 51KB 需任一 S3 桶做中转）:"
echo "     aws cloudformation deploy --template-file cloudlab-template-china.yaml \\"
echo "       --stack-name agentcore-workshop-base --capabilities CAPABILITY_NAMED_IAM \\"
echo "       --s3-bucket <你账号的任意桶> --region us-west-2"
