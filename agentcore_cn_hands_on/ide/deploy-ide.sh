#!/usr/bin/env bash
# 在操作员机器上运行：打包 workshop 代码 -> 上传 S3 -> 部署 CloudLab 单栈
#（VS Code IDE Server + workshop 数仓基础环境，一个栈全包，删栈一键销毁）。
#
# 用法:
#   AWS_REGION=us-west-2 bash deploy-ide.sh                 # 全球区测试
#   AWS_REGION=cn-northwest-1 bash deploy-ide.sh            # 中国区
#   可选环境变量:
#     WORKSHOP_STACK_NAME 栈名（默认 agentcore-workshop-base；须以 agentcore-workshop 开头，
#                         IDE 角色的最小权限按该前缀收敛）
#     ALLOWED_CIDR        允许访问 IDE 的来源 CIDR（默认 0.0.0.0/0，建议收敛）
#     IDE_INSTANCE_TYPE   实例类型（默认 t3.large）
#     IDE_ALB_PORT        IDE 入口端口（默认 8889，中国区免备案必须非标准端口；
#                         全球区可传 80 兼容只放行 80/443 出站的办公网络）
#     ASSET_BASE_URL      资产 CDN 基址（透传模板 AssetBaseUrl）。中国区默认
#                         http://cf.chenxqdu.people.a2z.org.cn/agentcore-workshop
#                         （维护者私有分发环境，换环境请覆盖）；全球区留空用模板默认
#     CODE_SERVER_RPM_URL 区内可达的 code-server rpm 镜像地址（中国区默认从
#                         ASSET_BASE_URL/tools/ 派生）
#     ENABLE_PUBLIC_PORTAL=1 公网化场景 2 的 Web 门户（HTTPS）：自动生成自签证书导入
#                         ACM，ALB 加 8443 HTTPS 监听器转发实例 5173（浏览器一次
#                         证书告警；8443 非标准端口，中国区免备案）
#     ENABLE_HTTP_PORTAL  公网化场景 2 的 Web 门户（纯 HTTP，NLB TCP:5173 直通），
#                         默认开（=1）：跑完 03 即可用公网 URL 访问（03 打印
#                         HttpPortalUrl）。免备案、免证书告警；前端自动退回无 PKCE
#                         授权码流程，token 明文传输，仅限 workshop 演示。实例安全组
#                         仍零 0.0.0.0/0（公网面只收在 NLB 安全组）。设 =0 关闭
#
# 前置：本机 aws CLI 已配置凭证；与 agentcore_cn_workshop、agentcore_cn_hands_on
#       同级目录结构（脚本自动定位）。
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
STACK_NAME="${WORKSHOP_STACK_NAME:-agentcore-workshop-base}"
ALLOWED_CIDR="${ALLOWED_CIDR:-0.0.0.0/0}"
IDE_INSTANCE_TYPE="${IDE_INSTANCE_TYPE:-t3.large}"
CODE_SERVER_RPM_URL="${CODE_SERVER_RPM_URL:-}"
ASSET_BASE_URL="${ASSET_BASE_URL:-}"
CODE_SERVER_VERSION="${CODE_SERVER_VERSION:-4.91.1}"

# 中国区：资产必须走区内 CDN（全球区 CloudFront 跨境慢且易致建栈超时回滚）。
# 默认指向维护者私有分发环境，换环境用 ASSET_BASE_URL 覆盖
case "$REGION" in cn-*)
  ASSET_BASE_URL="${ASSET_BASE_URL:-http://cf.chenxqdu.people.a2z.org.cn/agentcore-workshop}"
  [ -n "$CODE_SERVER_RPM_URL" ] || \
    CODE_SERVER_RPM_URL="$ASSET_BASE_URL/tools/code-server-$CODE_SERVER_VERSION-amd64.rpm"
;; esac

case "$STACK_NAME" in
  agentcore-workshop*) ;;
  *) echo "❌ 栈名必须以 agentcore-workshop 开头（IDE 最小权限按该前缀收敛）: $STACK_NAME"; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDS_ON_DIR="$(dirname "$SCRIPT_DIR")"                      # agentcore_cn_hands_on
ROOT_DIR="$(dirname "$HANDS_ON_DIR")"                        # 上级：包含两个文件夹
WORKSHOP_DIR="$ROOT_DIR/agentcore_cn_workshop"
TEMPLATE="$HANDS_ON_DIR/cloudformation/cloudlab-template-china.yaml"

if [ ! -d "$WORKSHOP_DIR" ]; then
  if [ -d "$ROOT_DIR/part1_data_ingestion" ]; then
    # 仓库内直跑布局：本脚本位于 workshop 仓库自带的 agentcore_cn_hands_on/ 下
    WORKSHOP_DIR="$ROOT_DIR"
  else
    echo "❌ 未找到 workshop 代码目录: $WORKSHOP_DIR"
    echo "   支持两种布局：外层平铺（agentcore_cn_workshop 与 agentcore_cn_hands_on"
    echo "   同一父目录）或在 workshop 仓库内直跑本脚本。"
    exit 1
  fi
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
# CLI 部署超 51KB 模板必经 S3 中转；只放模板副本，可随时删
TRANSFER_BUCKET="${TRANSFER_BUCKET:-agentcore-workshop-cfn-$ACCOUNT_ID-$REGION}"

# 资产（代码总包 + Lambda zip）统一从分发账号的 CloudFront 下发（模板参数
# AssetBaseUrl 有默认值）。本地代码有改动时先发布：
echo "==> [1/4] 发布资产到 CloudFront 分发桶（SKIP_PUBLISH=1 跳过）..."
if [ "${SKIP_PUBLISH:-0}" = "1" ]; then
  echo "    已跳过（使用 CDN 上现有资产）"
else
  bash "$SCRIPT_DIR/publish-assets.sh"
fi

echo "==> [2/4] 准备模板中转桶: $TRANSFER_BUCKET ..."
if ! aws s3api head-bucket --bucket "$TRANSFER_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$TRANSFER_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$TRANSFER_BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$TRANSFER_BUCKET" --region "$REGION" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  echo "    已创建中转桶: $TRANSFER_BUCKET"
fi

echo "==> [3/4] 部署 CloudLab 栈（首次约 15-20 分钟，含 Aurora 建库与实例引导）..."
# 参数全部显式声明：本次运行没给的开关一律回落默认值（CloudFormation 的
# deploy 对未传参数会沿用栈上旧值，不显式传会出现"关不掉"的漂移）
EXTRA_PARAMS=()
[ -n "$CODE_SERVER_RPM_URL" ] && EXTRA_PARAMS+=("CodeServerRpmUrl=$CODE_SERVER_RPM_URL")
[ -n "$ASSET_BASE_URL" ] && EXTRA_PARAMS+=("AssetBaseUrl=$ASSET_BASE_URL")
EXTRA_PARAMS+=("IdeAlbPort=${IDE_ALB_PORT:-8889}")
# 中央 Keycloak 活动形态：ENABLE_LOCAL_KC=0 时本栈不跑本地 Keycloak
#（身份系统集中在已备案账号，学员 .env 配 KC_URL_OVERRIDE）
if [ "${ENABLE_LOCAL_KC:-1}" = "0" ]; then
  EXTRA_PARAMS+=("EnableLocalKeycloak=false")
else
  EXTRA_PARAMS+=("EnableLocalKeycloak=true")
fi

# 可选：公网门户证书（自签导入 ACM，按域名幂等复用）
PORTAL_CERT_DOMAIN="agentcore-workshop-portal.local"
if [ "${ENABLE_PUBLIC_PORTAL:-0}" = "1" ]; then
  CERT_ARN=$(aws acm list-certificates --region "$REGION" --includes "keyTypes=RSA_2048" \
    --query "CertificateSummaryList[?DomainName=='$PORTAL_CERT_DOMAIN'] | [0].CertificateArn" \
    --output text 2>/dev/null || true)
  if [ -z "$CERT_ARN" ] || [ "$CERT_ARN" = "None" ]; then
    echo "    生成自签证书并导入 ACM（${PORTAL_CERT_DOMAIN}，有效期 3 年）..."
    CERT_DIR=$(mktemp -d)
    openssl req -x509 -newkey rsa:2048 -sha256 -days 1095 -nodes \
      -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
      -subj "/CN=$PORTAL_CERT_DOMAIN" >/dev/null 2>&1
    CERT_ARN=$(aws acm import-certificate --region "$REGION" \
      --certificate "fileb://$CERT_DIR/cert.pem" \
      --private-key "fileb://$CERT_DIR/key.pem" \
      --tags "Key=Name,Value=agentcore-workshop-portal" \
      --query CertificateArn --output text)
    rm -rf "$CERT_DIR"
    echo "    已导入: $CERT_ARN"
  else
    echo "    复用已有门户证书: $CERT_ARN"
  fi
  EXTRA_PARAMS+=("PortalCertificateArn=$CERT_ARN")
else
  EXTRA_PARAMS+=("PortalCertificateArn=")
fi
# 公网 HTTP 门户默认开（中国区免备案形态）：跑完 03 即可用公网 URL 访问门户。
# 实例安全组仍零 0.0.0.0/0（公网面只收在 NLB 安全组）。ENABLE_HTTP_PORTAL=0 可关闭
if [ "${ENABLE_HTTP_PORTAL:-1}" = "0" ]; then
  EXTRA_PARAMS+=("EnableHttpPortal=false")
else
  EXTRA_PARAMS+=("EnableHttpPortal=true")
fi
# 模板超过 51,200 字节，需经 S3 中转（专用中转桶，仅存模板副本）
aws cloudformation deploy \
  --template-file "$TEMPLATE" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --s3-bucket "$TRANSFER_BUCKET" \
  --s3-prefix cfn \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "AllowedCidr=$ALLOWED_CIDR" \
    "IdeInstanceType=$IDE_INSTANCE_TYPE" \
    ${EXTRA_PARAMS[@]+"${EXTRA_PARAMS[@]}"}

echo "==> [4/4] 读取输出..."
_out() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
IDE_URL=$(_out IdeUrl)
PWD_CMD=$(_out IdePasswordCommand)
INSTANCE_ID=$(_out IdeInstanceId)
PORTAL_URL=$(_out PortalUrl)
HTTP_PORTAL_URL=$(_out HttpPortalUrl)

echo ""
echo "✅ CloudLab 栈部署完成（IDE + 数仓基础环境，单栈）！"
echo ""
echo "   IDE 地址:   $IDE_URL"
echo "   登录密码:   $PWD_CMD"
echo "               （输出 JSON 中的 password 字段）"
echo "   实例 ID:    $INSTANCE_ID"
echo ""
echo "   登录后打开 Terminal（菜单 ≡ -> Terminal -> New Terminal），依次执行:"
echo "     bash ~/environment/agentcore_cn_workshop/scripts/01_deploy_base.sh"
echo "     bash ~/environment/agentcore_cn_workshop/scripts/02_run_scenario1.sh"
echo "     bash ~/environment/agentcore_cn_workshop/scripts/03_run_scenario2.sh"
if [ -n "$PORTAL_URL" ] && [ "$PORTAL_URL" != "None" ]; then
  echo ""
  echo "   场景 2 公网门户(HTTPS): $PORTAL_URL"
  echo "     （需先跑完 03 并在 IDE Terminal 启动前端 npm run dev；"
  echo "       自签证书首次访问需在浏览器告警页点「继续前往」）"
fi
if [ -n "$HTTP_PORTAL_URL" ] && [ "$HTTP_PORTAL_URL" != "None" ]; then
  echo ""
  echo "   场景 2 公网门户(HTTP, 免备案免告警): $HTTP_PORTAL_URL"
  echo "     （需先跑完 03 并在 IDE Terminal 启动前端 npm run dev；"
  echo "       ⚠️ 明文传输 token，仅限 workshop 演示）"
fi
echo ""
echo "   一键销毁（含步骤脚本创建的全部 workshop 资源）: bash $SCRIPT_DIR/destroy-ide.sh"
