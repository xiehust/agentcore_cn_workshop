#!/usr/bin/env bash
# 用 demo 账号通过密码模式换取用户 access token，
# 供命令行测试 Runtime 调用（浏览器端走的是 Authorization Code + PKCE）。
# 账号用 PORTAL_USER 环境变量选择（缺省 analyst；密码从 secrets.env 里
# <大写用户名>_PASSWORD 取）：PORTAL_USER=vivian ./get_token.sh
# 输出：token 到 stdout。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART2_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck disable=SC1091
source "$PART2_DIR/.deploy/secrets.env"
KC_URL=$(grep '^VITE_KC_URL=' "$PART2_DIR/web/.env.local" | cut -d= -f2)
KC_REALM=$(grep '^VITE_KC_REALM=' "$PART2_DIR/web/.env.local" | cut -d= -f2)

PORTAL_USER="${PORTAL_USER:-analyst}"
# 密码变量名 = 大写用户名 + _PASSWORD（bash 3.2 兼容的间接展开）
PASSWORD_VAR="$(printf '%s' "$PORTAL_USER" | tr '[:lower:]' '[:upper:]')_PASSWORD"
PASSWORD="${!PASSWORD_VAR:-}"
if [ -z "$PASSWORD" ]; then
  echo "❌ secrets.env 里没有 $PASSWORD_VAR（未知账号 $PORTAL_USER？先重跑 deploy.sh 生成）" >&2
  exit 1
fi

curl -sf "$KC_URL/realms/$KC_REALM/protocol/openid-connect/token" \
  -d grant_type=password \
  -d client_id=workshop-web \
  -d "username=$PORTAL_USER" \
  -d "password=$PASSWORD" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])"
