#!/usr/bin/env bash
# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
#
# 中央 Keycloak 容量验证（活动前演练）：按 ~100 人同时在线的峰值形态，五个场景
# 依次压测，每个场景请求总数固定（有界压测，不做无限加压）。
#
#   1. discovery   GET  .well-known/openid-configuration（authorizer / 门户启动）
#   2. jwks        GET  certs（Runtime/Gateway authorizer、门户代理验签拉公钥）
#   3. login-page  GET  auth 端点渲染登录页（浏览器登录第一跳）
#   4. password    POST token password grant（最重：密码哈希校验 + 签发 token）
#   5. m2m         POST token client_credentials（Identity 换 Gateway token）
#
# 用法（压测机需 ApacheBench：sudo dnf install -y httpd-tools）：
#   printf '%s\n%s\n' "$ANALYST_PASSWORD" "$AGENT_CLIENT_SECRET" | \
#     KC_URL=https://<api-id>.execute-api.<region>.amazonaws.com.cn bash kc_loadtest.sh
#   可选环境变量：
#     CONCURRENCY  并发数（默认 100）
#     SCALE        各场景请求数倍率（默认 1：3000/2000/1000/600/600）
#     REALM        realm 名（默认 agentcore-workshop）
#     OUT_DIR      ab 原始输出目录（默认 /tmp/kc-loadtest）
# 凭证从 stdin 读（不进命令行参数）；POST body 文件 600 权限，结束即删。
# 判读：ab 的 Failed requests 含 Length 项（token 长度逐次不同，属误报），以
# Non-2xx 与 Connect/Receive/Exceptions 为准。
set -uo pipefail

KC_URL="${KC_URL:?需要 KC_URL（中央 Keycloak 地址）}"
KC_URL="${KC_URL%/}"
REALM="${REALM:-agentcore-workshop}"
C="${CONCURRENCY:-100}"
SCALE="${SCALE:-1}"
OUT_DIR="${OUT_DIR:-/tmp/kc-loadtest}"
BASE="$KC_URL/realms/$REALM"

command -v ab >/dev/null || { echo "❌ 未找到 ab（sudo dnf install -y httpd-tools）"; exit 1; }
IFS= read -r ANALYST_PW || true
IFS= read -r AGENT_SECRET || true
[ -n "${ANALYST_PW:-}" ] && [ -n "${AGENT_SECRET:-}" ] \
  || { echo "❌ stdin 需两行：analyst 密码、analysis-agent client secret"; exit 1; }

umask 077
mkdir -p "$OUT_DIR"
trap 'rm -f "$OUT_DIR"/*.body' EXIT
printf 'grant_type=password&client_id=workshop-web&username=analyst&password=%s' "$ANALYST_PW" \
  > "$OUT_DIR/password.body"
printf 'grant_type=client_credentials&client_id=analysis-agent&client_secret=%s' "$AGENT_SECRET" \
  > "$OUT_DIR/m2m.body"

report() {
  printf '\n### %s\n' "$1"
  grep -E '^(Complete requests|Failed requests|Non-2xx responses|Requests per second|Time taken for tests)' "$2"
  grep -E '^   \(Connect' "$2"
  grep -E '^Time per request:.*\(mean\)$' "$2"
  grep -E '^ +(50|90|95|99|100)%' "$2"
}

run() {  # $1=场景名 $2=请求数 其余=ab 参数
  local name="$1" n="$2"; shift 2
  echo "==> [$name] c=$C n=$n"
  ab -q -s 60 -c "$C" -n "$n" "$@" > "$OUT_DIR/$name.txt" 2>&1
  report "$name" "$OUT_DIR/$name.txt"
}

LOGIN_QS="client_id=workshop-web&response_type=code&scope=openid&redirect_uri=http%3A%2F%2Flocalhost%3A5173%2F"
FORM="application/x-www-form-urlencoded"

echo "START $(date -u +%FT%TZ)  target=$BASE"
run discovery  $((3000 * SCALE)) -k "$BASE/.well-known/openid-configuration"
run jwks       $((2000 * SCALE)) -k "$BASE/protocol/openid-connect/certs"
run login-page $((1000 * SCALE))    "$BASE/protocol/openid-connect/auth?$LOGIN_QS"
run password   $((600 * SCALE))  -p "$OUT_DIR/password.body" -T "$FORM" "$BASE/protocol/openid-connect/token"
run m2m        $((600 * SCALE))  -p "$OUT_DIR/m2m.body" -T "$FORM" "$BASE/protocol/openid-connect/token"
echo ""
echo "END $(date -u +%FT%TZ)  ab 原始输出: $OUT_DIR/*.txt"
