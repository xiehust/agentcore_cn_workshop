#!/usr/bin/env bash
# 发布 workshop 资产到分发通道（全球区 / 中国区二选一）。
# 发布后，任何账号只用 cloudlab-template-china.yaml 一个文件即可建栈：
# 栈内的 AssetFetch 自定义资源会从 CDN 拉 Lambda 包，IDE 实例引导
# 直接 curl 代码总包。
#
# 上传清单:
#   workshop-assets.tar.gz            代码总包（IDE 实例引导 curl 下载）
#   lambda/{writer,db,analytics,memory,tasksapi,runner}-lambda.zip
#                                     6 个数据面 Lambda（栈内 AssetFetch 拉取中转）
#   cloudlab-template-china.yaml      模板本体（供分发：curl -O 后即可部署）
#
# 注意：中国区通道还依赖三个大文件工具资产（tools/awscli-exe-linux-x86_64.zip、
# tools/code-server-<ver>-amd64.rpm、tools/keycloak-26.3-arm64.tar.gz），
# 体积大且不常变，不在本脚本发布范围——由维护者一次性上传（国内网络下大文件
# 建议在 jumphost 下载后用预签名 URL 直传桶）。本脚本 cn 通道会校验其存在。
#
# 用法:
#   bash publish-assets.sh
#   可选环境变量:
#     PUBLISH_TARGET   发布通道 global|cn（缺省按 AWS_REGION 自动判断：cn-* 走 cn）
#     PUBLISH_PROFILE  分发账号的 aws profile（global 通道默认 global；cn 通道默认 cn2）
#     PUBLISH_BUCKET   目标桶（global 默认 private-cf-demo-888577020934；
#                      cn 默认 s3-chenxqdu-nx——维护者私有环境值，换环境请覆盖）
#     PUBLISH_PREFIX   桶内前缀（cn 默认 agentcore-workshop/，global 默认空）
#     CDN_BASE_URL     CDN 基址（global 默认 https://dbr8mrv63fpuw.cloudfront.net；
#                      cn 默认 http://cf.chenxqdu.people.a2z.org.cn/agentcore-workshop，
#                      中国区 CloudFront 默认域名不可直接服务，必须走备案别名域）
#     CDN_DIST_ID      CloudFront 分配 ID，用于失效缓存（global 默认 E1PJYVU6MI701Y；
#                      cn 默认 E2W00KG9BC3OYO）
#
# 前置：本机 python3 + zip；与 agentcore_cn_workshop、agentcore_cn_hands_on
#       同级目录结构（脚本自动定位）。
set -euo pipefail

TARGET="${PUBLISH_TARGET:-}"
if [ -z "$TARGET" ]; then
  case "${AWS_REGION:-}" in cn-*) TARGET=cn ;; *) TARGET=global ;; esac
fi
if [ "$TARGET" = "cn" ]; then
  # cn 通道默认值指向维护者私有环境，换环境用 PUBLISH_* / CDN_* 覆盖
  PROFILE="${PUBLISH_PROFILE:-cn2}"
  BUCKET="${PUBLISH_BUCKET:-s3-chenxqdu-nx}"
  PREFIX="${PUBLISH_PREFIX:-agentcore-workshop/}"
  CDN="${CDN_BASE_URL:-http://cf.chenxqdu.people.a2z.org.cn/agentcore-workshop}"
  DIST_ID="${CDN_DIST_ID:-E2W00KG9BC3OYO}"
  AWS_REGION_ARGS=(--region cn-northwest-1)
else
  PROFILE="${PUBLISH_PROFILE:-global}"
  BUCKET="${PUBLISH_BUCKET:-private-cf-demo-888577020934}"
  PREFIX="${PUBLISH_PREFIX:-}"
  CDN="${CDN_BASE_URL:-https://dbr8mrv63fpuw.cloudfront.net}"
  DIST_ID="${CDN_DIST_ID:-E1PJYVU6MI701Y}"
  AWS_REGION_ARGS=()
fi
echo "==> 发布通道: $TARGET（bucket=$BUCKET prefix=${PREFIX:-<无>} profile=$PROFILE）"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDS_ON_DIR="$(dirname "$SCRIPT_DIR")"                      # agentcore_cn_hands_on
ROOT_DIR="$(dirname "$HANDS_ON_DIR")"                        # 上级：包含两个文件夹
WORKSHOP_DIR="$ROOT_DIR/agentcore_cn_workshop"
TEMPLATE="$HANDS_ON_DIR/cloudformation/cloudlab-template-china.yaml"

[ -f "$TEMPLATE" ] || { echo "❌ 未找到 $TEMPLATE"; exit 1; }

# 两种运行布局：
#   外层平铺布局：<父目录>/{agentcore_cn_workshop, agentcore_cn_hands_on}
#   仓库内直跑布局：本脚本位于 workshop 仓库自带的 agentcore_cn_hands_on/ 下
#     （此时 ROOT_DIR 本身就是 workshop 仓库，无平铺的兄弟目录）——
#     用 rsync 搭出打包用的平铺结构，保证总包内容始终来自仓库当前状态
PACK_ROOT="$ROOT_DIR"
if [ ! -d "$WORKSHOP_DIR" ]; then
  if [ -d "$ROOT_DIR/part1_data_ingestion" ]; then
    STAGE="$(mktemp -d)"
    rsync -a --exclude '.venv' --exclude 'node_modules' --exclude '.git' \
      --exclude '.deploy' --exclude '__pycache__' --exclude '.DS_Store' \
      --exclude '._*' --exclude '*.bedrock_agentcore.yaml' --exclude '.env.local' \
      "$ROOT_DIR/" "$STAGE/agentcore_cn_workshop/"
    rsync -a --exclude '.DS_Store' --exclude '._*' \
      "$HANDS_ON_DIR/" "$STAGE/agentcore_cn_hands_on/"
    PACK_ROOT="$STAGE"
    WORKSHOP_DIR="$STAGE/agentcore_cn_workshop"
    echo "==> 仓库内直跑布局：已 rsync 出打包结构（$STAGE）"
  else
    echo "❌ 未找到 $WORKSHOP_DIR"; exit 1
  fi
else
  # 外层平铺布局：外层 hands_on 快照容易与仓库内版本漂移（发生过），告警提示
  NESTED="$WORKSHOP_DIR/agentcore_cn_hands_on"
  if [ -d "$NESTED" ] && ! diff -rq -x '.DS_Store' -x '._*' "$NESTED" "$HANDS_ON_DIR" >/dev/null 2>&1; then
    echo "⚠️  外层 agentcore_cn_hands_on 与仓库内 $NESTED 内容不一致——"
    echo "    总包将打包外层版本，请确认外层快照是最新的（或改到仓库内直跑本脚本）"
  fi
fi
aws sts get-caller-identity --profile "$PROFILE" ${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"} >/dev/null \
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

echo "==> [3/4] 上传到 s3://${BUCKET}/${PREFIX}（profile: ${PROFILE}）..."
aws s3 cp "$BUILD/workshop-assets.tar.gz" "s3://$BUCKET/${PREFIX}workshop-assets.tar.gz" \
  --profile "$PROFILE" ${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"} --only-show-errors
for z in writer db analytics memory tasksapi runner; do
  aws s3 cp "$BUILD/$z-lambda.zip" "s3://$BUCKET/${PREFIX}lambda/$z-lambda.zip" \
    --profile "$PROFILE" ${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"} --only-show-errors
done
aws s3 cp "$TEMPLATE" "s3://$BUCKET/${PREFIX}cloudlab-template-china.yaml" \
  --profile "$PROFILE" ${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"} --only-show-errors --content-type text/yaml
echo "    已上传 8 个对象"

echo "==> [4/4] CloudFront 缓存失效并验证下载链路..."
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/${PREFIX}workshop-assets.tar.gz" "/${PREFIX}lambda/*" "/${PREFIX}cloudlab-template-china.yaml" \
  --profile "$PROFILE" ${AWS_REGION_ARGS[@]+"${AWS_REGION_ARGS[@]}"} --query 'Invalidation.Id' --output text
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
# cn 通道：校验大文件工具资产（bootstrap 硬依赖前两个；keycloak 预载缺失仅降级）
if [ "$TARGET" = "cn" ]; then
  for path in tools/awscli-exe-linux-x86_64.zip tools/code-server-4.91.1-amd64.rpm; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" -m 30 -r 0-0 "$CDN/$path" || echo 000)
    if [ "$code" = "206" ] || [ "$code" = "200" ]; then
      echo "    ✅ $CDN/$path"
    else
      echo "    ❌ $CDN/$path (HTTP $code) —— 中国区 bootstrap 硬依赖，请先上传（见脚本头注）"; FAIL=1
    fi
  done
  code=$(curl -sS -o /dev/null -w "%{http_code}" -m 30 -r 0-0 "$CDN/tools/keycloak-26.3-arm64.tar.gz" || echo 000)
  [ "$code" = "206" ] || [ "$code" = "200" ] \
    && echo "    ✅ $CDN/tools/keycloak-26.3-arm64.tar.gz" \
    || echo "    ⚠️  tools/keycloak-26.3-arm64.tar.gz 缺失（非阻塞：场景 2 将直接拉 quay.io，国内可能极慢）"
fi
[ "$FAIL" = "0" ] || { echo "❌ 部分文件经 CDN 不可下载，检查桶策略/分配状态"; exit 1; }

echo ""
echo "✅ 发布完成！参与者账号一键部署（二选一）:"
echo ""
echo "   方式 A（控制台）: 下载模板后在 CloudFormation 控制台「上传模板文件」创建栈"
echo "     curl -O $CDN/cloudlab-template-china.yaml"
echo ""
if [ "$TARGET" = "cn" ]; then
  echo "   方式 B（CLI，模板超 51KB 需任一 S3 桶做中转；中国区需显式传 AssetBaseUrl）:"
  echo "     aws cloudformation deploy --template-file cloudlab-template-china.yaml \\"
  echo "       --stack-name agentcore-workshop-base --capabilities CAPABILITY_NAMED_IAM \\"
  echo "       --s3-bucket <你账号的任意桶> --region cn-northwest-1 \\"
  echo "       --parameter-overrides AssetBaseUrl=$CDN"
  echo ""
  echo "   （推荐直接用 deploy-ide.sh：AWS_REGION=cn-northwest-1 bash deploy-ide.sh，"
  echo "     会自动带上 AssetBaseUrl 与 code-server 镜像参数）"
else
  echo "   方式 B（CLI，模板超 51KB 需任一 S3 桶做中转）:"
  echo "     aws cloudformation deploy --template-file cloudlab-template-china.yaml \\"
  echo "       --stack-name agentcore-workshop-base --capabilities CAPABILITY_NAMED_IAM \\"
  echo "       --s3-bucket <你账号的任意桶> --region us-west-2"
fi
