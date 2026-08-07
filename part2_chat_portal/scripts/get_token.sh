#!/usr/bin/env bash
# 用 demo 账号（analyst）通过密码模式换取用户 access token，
# 供命令行测试 Runtime 调用（浏览器端走的是 Authorization Code + PKCE）。
# 输出：token 到 stdout。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PART2_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck disable=SC1091
source "$PART2_DIR/.deploy/secrets.env"
KC_URL=$(grep '^VITE_KC_URL=' "$PART2_DIR/web/.env.local" | cut -d= -f2)
KC_REALM=$(grep '^VITE_KC_REALM=' "$PART2_DIR/web/.env.local" | cut -d= -f2)

curl -sf "$KC_URL/realms/$KC_REALM/protocol/openid-connect/token" \
  -d grant_type=password \
  -d client_id=workshop-web \
  -d username=analyst \
  -d "password=$ANALYST_PASSWORD" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])"
