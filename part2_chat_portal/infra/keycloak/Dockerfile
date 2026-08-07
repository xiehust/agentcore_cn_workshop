# 自部署 Keycloak（Part 2 用户体系 / OIDC IdP）
# 部署形态：本 Dockerfile + 生成好的 realm.json 由 deploy.sh 构建后推到 ECR，
# 跑在 ECS Fargate 上（ARM64），HTTPS 入口由 API Gateway HTTP API 提供
#（免域名的 execute-api 端点，满足 AgentCore JWT authorizer 对公网 HTTPS
# discovery URL 的要求）。见 infra/keycloak-fargate.yaml。
#
# 说明：
# - start-dev + 内嵌 H2 存储，realm 通过 --import-realm 随容器启动导入，
#   实例重启后 demo 配置自动恢复（运行期改动不保留，workshop 够用）。
# - --proxy-headers=xforwarded：信任 API Gateway 的 X-Forwarded-* 头；
#   若代理未带 proto 头导致 issuer 变成 http，deploy.sh 会追加 KC_HOSTNAME
#   环境变量强制 https（见脚本第 1 步的两阶段处理）。
FROM quay.io/keycloak/keycloak:26.3

# realm.json 由 deploy.sh 从 realm-template.json 生成（注入随机 client secret）
COPY realm.json /opt/keycloak/data/import/agentcore-workshop-realm.json

ENV KC_HEALTH_ENABLED=true
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start-dev", "--import-realm", "--http-port=8080", "--proxy-headers=xforwarded", "--hostname-strict=false"]
