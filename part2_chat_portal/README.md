# Part 2：Web Chat 分析门户

通过 Web 聊天界面，用 Agent 分析 Part 1 入库的选品数据。支持**流式输出**、
**固定报表**（选品漏斗、类目/赛道分布）和**开放式即席分析**（自然语言 → 只读 SQL）。

## 架构

```
用户浏览器（React SPA，本地 dev server）
   │  ① OIDC 登录（Authorization Code + PKCE）
   ▼
Keycloak（自部署 IdP：自定义镜像 → ECR → App Runner，自带 HTTPS 域名）
   │  ② access token（audience=analysis-runtime）
   ▼
AgentCore Runtime（分析 Agent，Strands SDK，SSE 流式）
   │    入站认证：JWT authorizer 校验 Keycloak 签发的用户 token
   │
   ├──▶ AgentCore Identity ── ③ OAuth2 credential provider（M2M / client
   │         credentials）为 Agent 换取访问 Gateway 的 token（audience=analysis-gateway）
   │
   ├──▶ AgentCore Gateway（MCP，JWT 入站认证）
   │         │  ④ 调用工具（选品漏斗 / 类目分布 / 即席 SQL）
   │         ▼
   │    数据查询 Lambda（挂进 Part 1 的 Aurora VPC）
   │         │  ⑤ pg8000 直连 5432（同 VPC；凭证走 Secrets Manager VPC 端点）
   │         ▼
   │    Aurora Serverless v2（PostgreSQL，Part 1 创建）
   │
   └──▶ Code Interpreter：统计分析、衍生计算
```

**本部分要验证的关键设计**：Gateway 侧**不使用 VPC egress**——私网访问全部由挂在
Aurora 同 VPC 内的 Lambda 承担（`Gateway → Lambda(VPC) → Aurora(同 VPC)`）。

## 用到的 AgentCore 组件

| 组件 | 用途 |
|------|------|
| **Runtime** | 托管分析 Agent；`--authorizer-config` 配置 JWT 入站认证；SSE 流式响应；按 session 隔离（会话内多轮对话） |
| **Identity** | 出站认证：OAuth2 credential provider 对接 Keycloak（client credentials / M2M），token 缓存在 token vault |
| **Gateway** | 把数据查询 Lambda 转成 3 个 MCP 工具；入站同样用 Keycloak JWT 认证（audience 隔离：Runtime 与 Gateway 各收各的 token） |
| **Code Interpreter** | 对查询结果做统计分析 |
| **Memory** | 本期不启用（部署 `--disable-memory`）。预留：后续接入 AWS 自部署的 **mem0** 记住用户分析偏好，接入点见 `agent/main.py` 标注 |

## 目录结构

```
part2_chat_portal/
├── agent/                        # 分析 Agent（部署到 AgentCore Runtime）
│   ├── main.py                   # 流式入口 + Identity M2M + Gateway MCP 工具
│   └── requirements.txt
├── infra/
│   ├── keycloak/                 # 自部署 Keycloak（Dockerfile + realm 模板）
│   ├── analytics-lambda/         # 数据查询 Lambda（pg8000 直连 Aurora）
│   ├── part2-analytics.yaml      # CloudFormation：Lambda/Gateway 角色 + Secrets VPC 端点
│   └── tool-schema.json          # Gateway Lambda target 的工具 schema
├── scripts/
│   ├── deploy.sh                 # 一键部署（Keycloak + 基础设施 + Gateway + Runtime）
│   ├── get_token.sh              # 测试辅助：demo 账号换取用户 token
│   └── chat_test.sh              # 命令行冒烟测试（流式打印 Agent 回复）
└── web/                          # 前端 SPA（Vite + React + TS，本地 dev server）
```

## 部署

前置：Part 1 已部署（依赖其 Aurora 栈）；本机有 Docker；模型认证同 Part 1。

```bash
export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>   # 或依赖 IAM 自动铸 token
./scripts/deploy.sh
```

脚本幂等，可重复执行。首次部署约 15 分钟（App Runner 建服务最慢）。部署机密
（Keycloak admin 密码、demo 账号密码、M2M client secret）生成后存放在
`.deploy/secrets.env`（已 gitignore）。

## 使用

```bash
# 启动前端（deploy.sh 已生成 web/.env.local）
cd web && npm install && npm run dev
# 浏览器打开 http://localhost:5173，用 analyst 账号登录（密码见部署输出）

# 或命令行冒烟测试（模拟前端调用，流式输出）
./scripts/chat_test.sh '看下最近30天的选品漏斗'
./scripts/chat_test.sh '候选池里产品点最高的5个商品' <上一次输出的session id>  # 多轮
```

## 技术要点

- **流式**：`BedrockAgentCoreApp` 入口用 async generator `yield` 文本增量，
  Runtime 自动转成 SSE；前端 fetch + ReadableStream 解析 `data:` 事件逐字渲染。
- **浏览器 → Runtime**：AgentCore 数据面端点无 CORS 头，浏览器不能直接跨域调用；
  由 Vite dev server 代理（`/api/invoke` → `/runtimes/<ARN>/invocations`），
  用户的 Bearer token 原样透传。
- **audience 隔离**：Keycloak 两个 client 各配 audience mapper——前端用户 token
  带 `analysis-runtime`，Agent 的 M2M token 带 `analysis-gateway`；Runtime 和
  Gateway 的 JWT authorizer 各自校验（AgentCore 的 `allowedClients` 校验的是
  `client_id` claim，Keycloak 默认不带，所以用 `allowedAudience`）。
- **Keycloak on App Runner**：start-dev + `--import-realm`（realm 配置烧进镜像，
  重启自动恢复）；App Runner 自带 HTTPS 域名，若容器收不到 `X-Forwarded-Proto`
  导致 issuer 变 http，deploy.sh 会自动追加 `KC_HOSTNAME` 强制 https。
- **Aurora 冷恢复**：集群 MinCapacity=0 空闲自动暂停，Lambda 首次连接会触发恢复
  （15 秒~1 分钟），连接层带重试；首个问题偶尔偏慢属正常现象。

## 常见问题

- **登录后调用返回 403**：确认前端拿到的 token audience 是 `analysis-runtime`
  （jwt.io 解码看 `aud`），以及 Runtime 的 authorizer discovery URL 与 Keycloak
  地址一致（Keycloak 重建后需重跑 deploy.sh 同步）。
- **工具调用报数据库超时**：多为 Aurora 冷恢复中，重问一次即可。
- **Keycloak 重启后登录态丢失**：dev 模式内嵌 H2 存储，实例重启丢运行期数据
  （用户重新登录即可；realm 配置来自镜像内的导入文件，不会丢）。
