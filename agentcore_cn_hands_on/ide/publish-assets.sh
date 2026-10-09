#!/usr/bin/env bash
# 发布 workshop 资产到分发通道（一次构建，可同时发多个通道）。
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
# 通道:
#   cn      中国区 CDN（备案别名域 + 区内桶）。deploy-ide.sh 所有区域默认消费
#   mirror  cn 通道在全球区账号的全量镜像（CloudFront 默认域名 + 专用私有桶，
#           OAC 回源，目录结构与 cn 一致含 tools/）。备用：deploy-ide.sh 的
#           ASSET_SOURCE=mirror 显式切换过来
#   global  早期全球区通道（另一维护者环境，根前缀、无 tools/），保留兼容
#
# 注意：cn / mirror 通道还依赖三个大文件工具资产（tools/awscli-exe-linux-x86_64.zip、
# tools/code-server-<ver>-amd64.rpm、tools/keycloak-26.3-arm64.tar.gz），
# 体积大且不常变，不在本脚本发布范围——由维护者一次性上传（国内网络下大文件
# 建议在 jumphost 下载后用预签名 URL 直传桶；更新 cn 后记得同步 mirror）。
# 本脚本会校验其存在。
#
# 用法:
#   bash publish-assets.sh
#   可选环境变量:
#     PUBLISH_TARGET   逗号分隔的通道列表，如 cn,mirror（缺省按 AWS_REGION：
#                      cn-* → cn,mirror；其余 → global）
#     PUBLISH_REQUIRE  必需通道（缺省列表第一个）：它失败则整体失败；其余通道
#                      为附带同步，凭证不可用或上传失败只告警不阻断
#     cn / global 通道覆盖（两者互斥，不能同一次发布）:
#       PUBLISH_PROFILE  aws profile（cn 默认 cn2；global 默认 global）
#       PUBLISH_BUCKET   目标桶（cn 默认 s3-chenxqdu-nx；global 默认
#                        private-cf-demo-888577020934——维护者私有环境值，换环境请覆盖）
#       PUBLISH_PREFIX   桶内前缀（cn 默认 agentcore-workshop/，global 默认空）
#       CDN_BASE_URL     CDN 基址（cn 默认 http://cf.chenxqdu.people.a2z.org.cn/agentcore-workshop，
#                        中国区 CloudFront 默认域名不可直接服务，必须走备案别名域；
#                        global 默认 https://dbr8mrv63fpuw.cloudfront.net）
#       CDN_DIST_ID      CloudFront 分配 ID（cn 默认 E2W00KG9BC3OYO；global 默认 E1PJYVU6MI701Y）
#     mirror 通道覆盖:
#       MIRROR_PROFILE / MIRROR_BUCKET / MIRROR_PREFIX / MIRROR_CDN_BASE_URL / MIRROR_DIST_ID
#       （默认 default / agentcore-workshop-assets-956045422469 / agentcore-workshop/ /
#        https://dkbkvrzdiwo2j.cloudfront.net/agentcore-workshop / E2O8H2PE779NQO）
#
# 前置：本机 python3 + zip；与 agentcore_cn_workshop、agentcore_cn_hands_on
#       同级目录结构（脚本自动定位）。
set -euo pipefail

TARGETS="${PUBLISH_TARGET:-}"
if [ -z "$TARGETS" ]; then
  case "${AWS_REGION:-}" in cn-*) TARGETS="cn,mirror" ;; *) TARGETS="global" ;; esac
fi
IFS=',' read -r -a CHANNELS <<< "$TARGETS"
HAS_CN=0; HAS_GLOBAL=0
for ch in "${CHANNELS[@]}"; do
  case "$ch" in
    cn) HAS_CN=1 ;;
    global) HAS_GLOBAL=1 ;;
    mirror) ;;
    *) echo "❌ 未知通道: $ch（可选 cn / mirror / global）"; exit 1 ;;
  esac
done
[ "$HAS_CN$HAS_GLOBAL" != "11" ] \
  || { echo "❌ cn 与 global 通道共用 PUBLISH_* 覆盖变量，不能同一次发布"; exit 1; }
REQUIRED="${PUBLISH_REQUIRE:-${CHANNELS[0]}}"
case ",$TARGETS," in
  *",$REQUIRED,"*) ;;
  *) echo "❌ PUBLISH_REQUIRE=$REQUIRED 不在 PUBLISH_TARGET（$TARGETS）中"; exit 1 ;;
esac

# 通道参数（维护者私有环境默认值，换环境用对应覆盖变量）
channel_config() {
  case "$1" in
    cn)
      CH_PROFILE="${PUBLISH_PROFILE:-cn2}"
      CH_BUCKET="${PUBLISH_BUCKET:-s3-chenxqdu-nx}"
      CH_PREFIX="${PUBLISH_PREFIX:-agentcore-workshop/}"
      CH_CDN="${CDN_BASE_URL:-http://cf.chenxqdu.people.a2z.org.cn/agentcore-workshop}"
      CH_DIST="${CDN_DIST_ID:-E2W00KG9BC3OYO}"
      CH_REGION_ARGS=(--region cn-northwest-1); CH_TOOLS=1 ;;
    mirror)
      CH_PROFILE="${MIRROR_PROFILE:-default}"
      CH_BUCKET="${MIRROR_BUCKET:-agentcore-workshop-assets-956045422469}"
      CH_PREFIX="${MIRROR_PREFIX:-agentcore-workshop/}"
      CH_CDN="${MIRROR_CDN_BASE_URL:-https://dkbkvrzdiwo2j.cloudfront.net/agentcore-workshop}"
      CH_DIST="${MIRROR_DIST_ID:-E2O8H2PE779NQO}"
      CH_REGION_ARGS=(--region us-west-2); CH_TOOLS=1 ;;
    global)
      CH_PROFILE="${PUBLISH_PROFILE:-global}"
      CH_BUCKET="${PUBLISH_BUCKET:-private-cf-demo-888577020934}"
      CH_PREFIX="${PUBLISH_PREFIX:-}"
      CH_CDN="${CDN_BASE_URL:-https://dbr8mrv63fpuw.cloudfront.net}"
      CH_DIST="${CDN_DIST_ID:-E1PJYVU6MI701Y}"
      CH_REGION_ARGS=(); CH_TOOLS=0 ;;
  esac
}
echo "==> 发布通道: ${TARGETS}（必需: ${REQUIRED}）"

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
      --exclude '.env' \
      "$ROOT_DIR/" "$STAGE/agentcore_cn_workshop/"
    rsync -a --exclude '.DS_Store' --exclude '._*' \
      "$HANDS_ON_DIR/" "$STAGE/agentcore_cn_hands_on/"
    PACK_ROOT="$STAGE"
    WORKSHOP_DIR="$STAGE/agentcore_cn_workshop"
    echo "==> 仓库内直跑布局：已 rsync 出打包结构（${STAGE}）"
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
# 凭证预检（打包前）：必需通道不可用直接失败；附带通道不可用则跳过。必需通道排最前发布
ACTIVE=()
for ch in "$REQUIRED" "${CHANNELS[@]}"; do
  case " ${ACTIVE[*]-} " in *" $ch "*) continue ;; esac
  channel_config "$ch"
  if aws sts get-caller-identity --profile "$CH_PROFILE" ${CH_REGION_ARGS[@]+"${CH_REGION_ARGS[@]}"} >/dev/null 2>&1; then
    ACTIVE+=("$ch")
  elif [ "$ch" = "$REQUIRED" ]; then
    echo "❌ [$ch] profile '$CH_PROFILE' 凭证不可用"; exit 1
  else
    echo "⚠️  [$ch] profile '$CH_PROFILE' 凭证不可用，跳过该附带通道（其 CDN 内容将保持旧版）"
  fi
done

echo "==> [1/4] 打包 workshop 代码总包（排除 .venv/.deploy/node_modules/.git 等本地产物）..."
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD" "${STAGE:-}"' EXIT
# COPYFILE_DISABLE=1：禁止 macOS tar 写入 AppleDouble 元数据（._xxx 文件）
COPYFILE_DISABLE=1 tar czf "$BUILD/workshop-assets.tar.gz" -C "$PACK_ROOT" \
  --exclude='._*' \
  --exclude='.venv' \
  --exclude='.deploy' \
  --exclude='node_modules' \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='*.bedrock_agentcore.yaml' \
  --exclude='.env.local' \
  --exclude='.env' \
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

# CDN 可达性校验：Range 请求命中返回 206，未开 Range 支持时 200 也算通
cdn_ok() {
  local code
  code=$(curl -sS -o /dev/null -w "%{http_code}" -m 30 -r 0-0 "$1" || echo 000)
  [ "$code" = "206" ] || [ "$code" = "200" ]
}

# 上传 + 失效 + 验证一个通道；返回非 0 表示该通道失败
publish_channel() {
  local ch="$1" path fail=0
  channel_config "$ch"
  local -a A
  A=(--profile "$CH_PROFILE" ${CH_REGION_ARGS[@]+"${CH_REGION_ARGS[@]}"})
  echo "==> [3/4][$ch] 上传到 s3://${CH_BUCKET}/${CH_PREFIX}（profile: ${CH_PROFILE}）..."
  aws s3 cp "$BUILD/workshop-assets.tar.gz" "s3://$CH_BUCKET/${CH_PREFIX}workshop-assets.tar.gz" \
    "${A[@]}" --only-show-errors || return 1
  for z in writer db analytics memory tasksapi runner; do
    aws s3 cp "$BUILD/$z-lambda.zip" "s3://$CH_BUCKET/${CH_PREFIX}lambda/$z-lambda.zip" \
      "${A[@]}" --only-show-errors || return 1
  done
  aws s3 cp "$TEMPLATE" "s3://$CH_BUCKET/${CH_PREFIX}cloudlab-template-china.yaml" \
    "${A[@]}" --only-show-errors --content-type text/yaml || return 1
  echo "    已上传 8 个对象"

  echo "==> [4/4][$ch] CloudFront 缓存失效并验证下载链路..."
  aws cloudfront create-invalidation --distribution-id "$CH_DIST" \
    --paths "/${CH_PREFIX}workshop-assets.tar.gz" "/${CH_PREFIX}lambda/*" "/${CH_PREFIX}cloudlab-template-china.yaml" \
    "${A[@]}" --query 'Invalidation.Id' --output text || return 1
  for path in workshop-assets.tar.gz lambda/writer-lambda.zip lambda/db-lambda.zip \
              lambda/analytics-lambda.zip lambda/memory-lambda.zip \
              lambda/tasksapi-lambda.zip lambda/runner-lambda.zip \
              cloudlab-template-china.yaml; do
    if cdn_ok "$CH_CDN/$path"; then echo "    ✅ $CH_CDN/$path"
    else echo "    ❌ $CH_CDN/$path"; fail=1; fi
  done
  # 大文件工具资产（中国区 bootstrap 硬依赖前两个；keycloak 预载缺失仅降级）
  if [ "$CH_TOOLS" = "1" ]; then
    for path in tools/awscli-exe-linux-x86_64.zip tools/code-server-4.91.1-amd64.rpm; do
      if cdn_ok "$CH_CDN/$path"; then echo "    ✅ $CH_CDN/$path"
      else echo "    ❌ $CH_CDN/$path —— 中国区 bootstrap 硬依赖，请先上传（见脚本头注）"; fail=1; fi
    done
    if cdn_ok "$CH_CDN/tools/keycloak-26.3-arm64.tar.gz"; then
      echo "    ✅ $CH_CDN/tools/keycloak-26.3-arm64.tar.gz"
    else
      echo "    ⚠️  tools/keycloak-26.3-arm64.tar.gz 缺失（非阻塞：场景 2 将直接拉 quay.io，国内可能极慢）"
    fi
  fi
  [ "$fail" = "0" ]
}

PUBLISHED=()
for ch in "${ACTIVE[@]}"; do
  if publish_channel "$ch"; then
    PUBLISHED+=("$ch")
  elif [ "$ch" = "$REQUIRED" ]; then
    echo "❌ [$ch] 必需通道发布失败（上传出错或经 CDN 不可下载，检查桶策略/分配状态）"; exit 1
  else
    echo "⚠️  [$ch] 附带通道同步失败（不阻断本次发布；该 CDN 内容可能过期，稍后可用 PUBLISH_TARGET=$ch 单独补发）"
  fi
done

channel_config "$REQUIRED"
CDN="$CH_CDN"
echo ""
echo "✅ 发布完成！已发布通道: ${PUBLISHED[*]}。参与者账号一键部署（二选一）:"
echo ""
echo "   方式 A（控制台）: 下载模板后在 CloudFormation 控制台「上传模板文件」创建栈"
echo "     ⚠️ 栈名必须以 agentcore-workshop 开头（IDE 角色权限按该前缀收敛），推荐 agentcore-workshop-base"
echo "     curl -O $CDN/cloudlab-template-china.yaml"
echo ""
if [ "$REQUIRED" != "global" ]; then
  echo "   方式 B（CLI，模板超 51KB 需任一 S3 桶做中转；资产基址与 code-server 地址成对显式传）:"
  echo "     aws cloudformation deploy --template-file cloudlab-template-china.yaml \\"
  echo "       --stack-name agentcore-workshop-base --capabilities CAPABILITY_NAMED_IAM \\"
  echo "       --s3-bucket <你账号的任意桶> --region <region> \\"
  echo "       --parameter-overrides AssetBaseUrl=$CDN \\"
  echo "         CodeServerRpmUrl=$CDN/tools/code-server-4.91.1-amd64.rpm"
  echo ""
  echo "   （推荐直接用 deploy-ide.sh：ASSET_SOURCE=$REQUIRED bash deploy-ide.sh，"
  echo "     会自动带上 AssetBaseUrl 与 code-server 镜像参数）"
else
  echo "   方式 B（CLI，模板超 51KB 需任一 S3 桶做中转）:"
  echo "     aws cloudformation deploy --template-file cloudlab-template-china.yaml \\"
  echo "       --stack-name agentcore-workshop-base --capabilities CAPABILITY_NAMED_IAM \\"
  echo "       --s3-bucket <你账号的任意桶> --region us-west-2"
fi
