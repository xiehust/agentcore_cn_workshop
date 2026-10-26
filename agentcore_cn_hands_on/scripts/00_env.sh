#!/usr/bin/env bash
# 公共环境库：被 01/02/03/99 脚本 source，不单独执行。
# 职责：区域/分区推导、workshop 目录定位、Python venv 与 agentcore CLI 保障、
#       模型三元组校验（与 workshop 原 deploy.sh 的 fail-fast 逻辑一致）。

# ---- 依赖版本锁（单一事实源，见仓库根 versions.sh）----
# 两种布局：仓库内（本文件上两级 = 仓库根）；学员 IDE 平铺
# （environment/{agentcore_cn_workshop, agentcore_cn_hands_on}，versions.sh
# 在 agentcore_cn_workshop/ 里）。两个位置依次探测。
_VER_BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for _VER_FILE in "$_VER_BASE/versions.sh" "$_VER_BASE/agentcore_cn_workshop/versions.sh"; do
  # shellcheck disable=SC1090
  if [ -f "$_VER_FILE" ]; then . "$_VER_FILE"; break; fi
done

# ---- 区域/分区推导：区域代码是唯一开关（us-west-2 / cn-northwest-1 ...） ----
REGION="${AWS_REGION:-us-west-2}"
export AWS_REGION="$REGION"
case "$REGION" in
  cn-*) PARTITION="aws-cn"; AWS_DOMAIN="amazonaws.com.cn"
        # SDK/starter-toolkit 库内硬编码 .amazonaws.com 端点，中国区用官方环境变量覆盖
        export BEDROCK_AGENTCORE_CP_ENDPOINT="https://bedrock-agentcore-control.$REGION.$AWS_DOMAIN"
        export BEDROCK_AGENTCORE_DP_ENDPOINT="https://bedrock-agentcore.$REGION.$AWS_DOMAIN"
        ;;
  *)    PARTITION="aws"; AWS_DOMAIN="amazonaws.com" ;;
esac
# 接口端点服务名前缀：cn 分区按服务不一致（execute-api/lambda 带 cn. 前缀），
# 本仓库只用它探测 secretsmanager——该服务两个 cn 区实测均为 com.amazonaws 前缀
VPCE_PREFIX="com.amazonaws"
export PARTITION AWS_DOMAIN VPCE_PREFIX

# ---- 目录定位 ----
# IDE 上的布局：本脚本目录就在 workshop 仓库内（~/environment/
# agentcore_cn_workshop/scripts/，IDE 文件树可见、可直接运行）。
# 兼容旧布局（hands_on 同级 / ~/environment/ 下）按候选路径依次探测。
_ENV_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$_ENV_LIB_DIR"
HANDS_ON_DIR="$(dirname "$_ENV_LIB_DIR")"
if [ -z "${WORKSHOP_DIR:-}" ]; then
  for cand in "$(dirname "$_ENV_LIB_DIR")" \
              "$(dirname "$HANDS_ON_DIR")/agentcore_cn_workshop" \
              "$HOME/environment/agentcore_cn_workshop"; do
    if [ -d "$cand/part1_data_ingestion" ]; then WORKSHOP_DIR="$cand"; break; fi
  done
fi
if [ ! -d "${WORKSHOP_DIR:-}/part1_data_ingestion" ]; then
  echo "❌ 未找到 workshop 代码目录（已探测脚本上级目录、hands_on 同级目录与 ~/environment/）"
  echo "   （可用 export WORKSHOP_DIR=/path/to/agentcore_cn_workshop 显式指定）"
  exit 1
fi

# ---- 模型配置文件（$WORKSHOP_DIR/.env，模板见 .env.example）----
# 学员在文件里填一次模型三元组 / MEM0 配置即可，免去逐 Terminal export。
# 语义：终端已 export 的同名变量优先（本文件只兜底）；含 <占位符> 的
# 未填写行自动跳过——漏填仍会触发 require_model_key 的 fail-fast 提示
if [ -f "$WORKSHOP_DIR/.env" ]; then
  while IFS= read -r _line; do
    _line="${_line#export }"
    case "$_line" in ''|\#*) continue ;; esac
    case "$_line" in *=*) ;; *) continue ;; esac
    _key="${_line%%=*}"; _val="${_line#*=}"
    _val="${_val%\"}"; _val="${_val#\"}"; _val="${_val%\'}"; _val="${_val#\'}"
    case "$_val" in *'<'*'>'*) continue ;; esac
    case "$_key" in *[!A-Za-z0-9_]*) continue ;; esac
    [ -n "${!_key:-}" ] || export "$_key=$_val"
  done < "$WORKSHOP_DIR/.env"
  unset _line _key _val
fi
export WORKSHOP_DIR

# starter-toolkit 0.3.12 起每条 agentcore 命令前会打「Starter Toolkit CLI is
# no longer supported / not recommended」迁移横幅（推荐 npm 版 CLI）。本 workshop
# 的部署链路就是按 Python 版 toolkit 设计的（含 npm 同名命令防遮蔽逻辑），
# 用官方开关抑制该提示。
export AGENTCORE_SUPPRESS_RECOMMENDATION=1

# ---- 常量 ----
# BASE_STACK：单一 CloudLab 栈（IDE + 数仓基础环境），由 cloudformation/
# cloudlab-template-china.yaml 建出。IDE 引导会在 /etc/profile.d 里注入实际栈名。
# 数仓相关输出键与 workshop 原 aurora 栈一致，故 AURORA_STACK 直接指向本栈
#（part2 deploy.sh 已支持用该环境变量覆盖默认栈名）。
BASE_STACK="${BASE_STACK:-agentcore-workshop-base}"
export AURORA_STACK="$BASE_STACK"
DATABASE="product_hunting"
WRITER_NAME="agentcore-workshop-writer"

stack_output() {  # stack_output <stack> <OutputKey>
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

# 基础栈探测：失败时回显真实错误，区分「栈不存在」与「无权限」。
# 吞 stderr 的写法会把 AccessDenied（受管账号 SCP 拦运行期调用，实测）
# 误报成「未找到栈」，排障方向直接带偏
require_base_stack() {
  local err
  if err=$(aws cloudformation describe-stacks --stack-name "$BASE_STACK" \
      --region "$REGION" 2>&1 >/dev/null); then
    return 0
  fi
  echo "❌ 基础栈 ${BASE_STACK} 探测失败："
  echo "   $(echo "$err" | head -2)"
  case "$err" in
    *"does not exist"*)
      echo "   → 栈不存在：先在操作员机器执行 ide/deploy-ide.sh；栈名非默认时 export BASE_STACK=<实际栈名>" ;;
    *AccessDenied*|*"not authorized"*|*"explicit deny"*)
      echo "   → 当前身份（IDE 实例角色）无权 cloudformation:DescribeStacks。"
      echo "     受管账号常见原因：SCP 只豁免了建栈者，未覆盖运行期角色——"
      echo "     请管理员按前缀豁免 arn:${PARTITION}:iam::*:role/agentcore-workshop-*" ;;
  esac
  exit 1
}

# ---- 数仓查询助手（经 VPC 内通用数据面 Lambda，Data API 的替代） ----
DB_QUERY_FN="agentcore-workshop-db"

db_exec_sql() {  # db_exec_sql '<sql>' -> 响应 JSON 打到 stdout
  local payload
  payload=$(python3 -c 'import json,sys; print(json.dumps({"op":"execute","args":{"sql":sys.argv[1],"params":{}}}))' "$1")
  aws lambda invoke --function-name "$DB_QUERY_FN" --region "$REGION" \
    --cli-binary-format raw-in-base64-out --cli-read-timeout 310 \
    --payload "$payload" /tmp/.db_exec_out.json >/dev/null 2>&1 || true
  cat /tmp/.db_exec_out.json 2>/dev/null
}

db_now() {  # 数据库自己的时钟（UTC 字符串），做增量水位线用
  db_exec_sql "SELECT to_char(now(), 'YYYY-MM-DD HH24:MI:SS') AS t" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['rows'][0]['t'])" 2>/dev/null || true
}

show_rows_since() {  # show_rows_since '<UTC时间串>' 展示两张业务表自该时刻起的新增
  local since="$1" t
  for t in selection_verdicts product_candidates; do
    echo "── ${t}（created_at >= ${since} UTC）──"
    db_exec_sql "SELECT * FROM $t WHERE created_at >= '$since' ORDER BY created_at DESC LIMIT 20" \
      | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('  （查询响应解析失败）'); raise SystemExit
if d.get('status') != 'ok':
    print('  查询失败:', d.get('error', d)); raise SystemExit
rows = d.get('rows', [])
if not rows:
    print('  （无新增记录）')
for i, r in enumerate(rows, 1):
    print(f'  ▸ 记录 {i}')
    for k, v in r.items():
        v = '' if v is None else str(v)
        print(f'      {k}: {v[:160]}')
"
  done
}

# ---- Python venv 与 agentcore CLI ----
# venv 建在 workshop 仓库根（$WORKSHOP_DIR/.venv）：workshop 原 deploy.sh 的
# agentcore 防遮蔽逻辑会自动回落到这个位置，两边天然兼容。
ensure_venv() {
  local py=python3
  if command -v python3.11 >/dev/null 2>&1; then py=python3.11; fi
  if ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    echo "❌ 需要 Python 3.10+（当前 $($py --version 2>&1)）。IDE 引导默认安装 python3.11。"
    exit 1
  fi
  if [ ! -x "$WORKSHOP_DIR/.venv/bin/pip" ]; then
    echo "==> 创建 Python venv: $WORKSHOP_DIR/.venv（${py}）..."
    "$py" -m venv "$WORKSHOP_DIR/.venv"
  fi
  # shellcheck disable=SC1091
  source "$WORKSHOP_DIR/.venv/bin/activate"
  # 版本锁定：统一取自仓库根 versions.sh（本文件顶部已 source）。versions.sh
  # 缺失时兜底到实测版本，保证单机独立运行也不崩。改版本请改 versions.sh。
  TOOLKIT_VERSION="${TOOLKIT_VERSION:-0.3.14}"
  UV_VERSION="${UV_VERSION:-0.12.23}"
  if ! "$WORKSHOP_DIR/.venv/bin/pip" show bedrock-agentcore-starter-toolkit >/dev/null 2>&1; then
    echo "==> 安装 bedrock-agentcore-starter-toolkit==${TOOLKIT_VERSION}（agentcore CLI）..."
    pip install -q --upgrade pip
    pip install -q "bedrock-agentcore-starter-toolkit==${TOOLKIT_VERSION}" "uv==${UV_VERSION}"
  fi
  # uv：direct_code_deploy 模式打包依赖的硬前置（旧环境可能只装了 toolkit）
  command -v uv >/dev/null 2>&1 || pip install -q "uv==${UV_VERSION}"
  # agentcore CLI 可用性自检（npm 有同名命令，识别依据：Python 版帮助里有 configure）
  if ! { agentcore --help >/tmp/.agentcore_help 2>/dev/null && grep -qE '\bconfigure\b' /tmp/.agentcore_help; }; then
    echo "❌ agentcore CLI 异常（PATH 里的可能是 npm 同名命令）。venv: $WORKSHOP_DIR/.venv"
    exit 1
  fi
  echo "    toolkit $(pip show bedrock-agentcore-starter-toolkit 2>/dev/null | awk '/^Version/{print $2}') / uv $(uv --version 2>/dev/null | awk '{print $2}')"
}

# ---- 模型三元组校验（fail-fast，与 workshop deploy.sh 完全一致的规则） ----
# 显式配了 OPENAI_BASE_URL：视为自定义端点，信任成对的 key（中国区形态）。
# 未配 BASE_URL：走默认 bedrock-mantle，key 必须是 Bedrock API key（ABSK 前缀）。
require_model_key() {
  local model_key="${OPENAI_API_KEY:-${AWS_BEARER_TOKEN_BEDROCK:-}}"
  if [ -z "$model_key" ]; then
    echo "❌ 缺少模型 API key。二选一："
    echo "   1) 全球区默认端点: export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key（ABSK 前缀）>"
    echo "      （Bedrock 控制台开通 OpenAI GPT-5.6 模型访问后，在 API keys 页生成）"
    echo "   2) 自定义/中国区端点: export OPENAI_BASE_URL=<https://.../v1> OPENAI_API_KEY=<key> MODEL_ID=<模型名>"
    exit 1
  fi
  if [ -z "${OPENAI_BASE_URL:-}" ] && [ "$PARTITION" = "aws-cn" ]; then
    echo "❌ 中国区无默认模型端点（bedrock-mantle 仅全球区可用）："
    echo "   请 export OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID 指向区内 OpenAI 兼容端点。"
    exit 1
  fi
  if [ -z "${OPENAI_BASE_URL:-}" ] && [[ "$model_key" != ABSK* ]]; then
    echo "❌ 未配置 OPENAI_BASE_URL 时默认走 bedrock-mantle，key 必须是 Bedrock API key（ABSK 前缀）。"
    exit 1
  fi
  if [ -n "${OPENAI_BASE_URL:-}" ] \
      && printf '%s' "$OPENAI_BASE_URL" | grep -qE '://(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'; then
    echo "❌ OPENAI_BASE_URL 指向私网/本机地址（${OPENAI_BASE_URL}）——公网 Runtime 永远无法访问。"
    exit 1
  fi
  if [ -n "${OPENAI_BASE_URL:-}" ]; then
    echo "ℹ️  模型端点: ${OPENAI_BASE_URL}（MODEL_ID=${MODEL_ID:-<内置默认>}）"
  else
    echo "ℹ️  模型端点: bedrock-mantle 默认端点（Bedrock API key 认证）"
  fi
}
