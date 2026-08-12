# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目约定

- 全部沟通、文档、commit message 用中文。
- 文档中不得出现真实的 AWS 端点/账号信息（API Gateway ID、账号 ID 等），一律用
  `<api-id>`、`<region>` 占位符；密码只指向 `.deploy/secrets.env`（已 gitignore）。
- 前端动样式前先走 design skill 定美学方向。Part 2 门户已定型「量化工作台」暗色风格：
  冷石墨底 + 单一青柠 accent（#c9f158）、IBM Plex Sans/Mono（@fontsource 自托管）、
  Phosphor 图标（用 `*Icon` 后缀导出，裸名已 deprecated）。不要回到暖纸/朱砂/衬线的
  Claude 系暖色调。
- AGENTS.md 由 Trellis 托管，块内内容不要手改。

## 常用命令

区域固定 `us-west-2`。模型认证：`export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>`
（ABSK 前缀）。注意 `OPENAI_API_KEY` 优先级更高，环境里残留 OpenAI 官网 `sk-` key
会导致模型 401（deploy.sh 会检测并忽略非 ABSK 前缀的值）。

```bash
# Part 1：部署（Aurora + 写库 Lambda + 建表 + Browser/Profile + Runtime），幂等可重跑
cd part1_data_ingestion && bash scripts/deploy.sh
# Part 1：调用（单商品 2~5 分钟）
cd part1_data_ingestion/agent && agentcore invoke '{"prompt": "..."}'
# Part 1：本地跑 Agent（Browser/CI/Aurora 仍在云端）
python scripts/local_test.py "..."

# Part 2：部署（依赖 Part 1 的 Aurora 栈；本机需 Docker）
cd part2_chat_portal && ./scripts/deploy.sh
# 改了 Keycloak realm/镜像后强制重建
KC_REDEPLOY=1 ./scripts/deploy.sh

# Part 2：前端（deploy.sh 已生成 web/.env.local）
cd part2_chat_portal/web && npm install && npm run dev   # http://localhost:5173
npm run build                                            # tsc -b && vite build（类型检查）

# Part 2：命令行冒烟（token → 流式调用 Runtime；第二个参数传 session id 做多轮）
./scripts/chat_test.sh '看下最近30天的选品漏斗' [session-id]

# 查数仓（Data API 只剩「查」和 Part 2 在用；ARN 从 agentcore-workshop-aurora 栈输出取）
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database product_hunting --region us-west-2 --sql "SELECT ..."
```

E2E 浏览器验证用本机 Python 版 Playwright（`playwright` CLI 是 pip 装的）；
门户登录账号 `analyst`，密码在 `part2_chat_portal/.deploy/secrets.env`。

## 架构

两个部分共用一套数仓：Part 1 的 CloudFormation 栈 `agentcore-workshop-aurora` 建
VPC + Aurora Serverless v2（PostgreSQL，Min 0 ACU 空闲自动暂停）+ Secrets Manager
接口端点（端点随 VPC 归本栈，Part 1 写库 Lambda 与 Part 2 分析 Lambda 共用；同一
VPC 同一服务只能有一个 Private DNS 端点，已有环境升级要先跑 Part 2 deploy 删旧
端点、再跑 Part 1 deploy 建新的，Part 1 脚本有预检）。表全部由
Part 1 的 `infra/setup_tables.sql` 幂等建出：业务两表 `selection_verdicts`
（判定记录）、`product_candidates`（候选档案），任务管理三表
`ingestion_tasks`/`ingestion_runs`/`ingestion_run_events`，会话历史两表
`chat_sessions`/`chat_messages`（后五张服务 Part 2 门户）。

### Part 1：数据摄入（part1_data_ingestion/）

调用方 → Runtime（Strands Agent）→ Browser 爬 eBay / Code Interpreter 测算 →
`lambda.invoke` 写库 Lambda（挂 Aurora 同 VPC，pg8000 直连）入库（Agent 不挂 VPC）。

- **写库不走 RDS Data API**：中国区没有 Data API，本 workshop 后续要整体移植，
  数据面提前换成两边都可用的形态（与 Part 2 的 Gateway → Lambda(VPC) → Aurora
  同一哲学：计算留公网，私网访问全交 VPC 内 Lambda）。写库 Lambda
  （`infra/writer-lambda/handler.py`）实现 `save_verdict` / `save_candidate` /
  `setup_tables` 三个 op，`aurora_tools.py` 只剩薄壳；建表也是 deploy.sh
  `aws lambda invoke op=setup_tables`（`setup_tables.sql` 打进 zip，按分号拆句，
  **不得引入 dollar-quoted 块**）。超时链：handler 重连上限 240s < Lambda
  timeout 300s < Agent boto3 read_timeout 310s（Agent 侧禁用 boto3 重试）。
  Runtime 执行角色只有对写库 Lambda 的 `lambda:InvokeFunction`
  （栈输出 `AgentWriterInvokePolicyArn`）；集群 `EnableHttpEndpoint` 保留，
  `AgentDataAccessPolicy`（rds-data）如今只服务 Part 2 的 Runtime 与两个
  tasks Lambda，Part 2 迁完才能删。
- **业务 SOP 用 Skill 插拔**：Agent 本体（`agent/main.py`）只有通用能力；选品规则
  全在 `agent/skills/*/SKILL.md`。新增场景 = 新增 Skill 目录，不改 Agent 代码。
- **模型**：`OpenAIResponsesModel` 走 bedrock-mantle 的 OpenAI 兼容端点
  （默认 `openai.gpt-5.6-terra`，端点在 us-east-1）。换 Converse 系模型需改回
  `BedrockModel`。
- **Browser**：自定义 browser 开 Web Bot Auth（请求签名过 Cloudflare/Akamai 反爬），
  Profile 跨会话保留 cookies。亚马逊对数据中心 IP 硬封锁，默认站点是 eBay。

### Part 2：分析门户（part2_chat_portal/）

认证链是理解 Part 2 的关键，两段 token 用 audience 隔离：

```
SPA（PKCE 登录）→ Keycloak → 用户 token（aud=analysis-runtime）→ Runtime（JWT 入站）
Runtime → Identity（M2M client credentials）→ token（aud=analysis-gateway）→ Gateway（MCP）
Gateway → Lambda（挂 Aurora 同 VPC，pg8000 直连）→ Aurora
```

- JWT authorizer 用 `allowedAudience` 而非 `allowedClients`——后者校验 `client_id`
  claim，Keycloak 默认不发；两个 client 的 audience 在
  `infra/keycloak/realm-template.json` 里用 audience mapper 配置。
- **Gateway 零 VPC egress**：私网访问全由 Lambda 承担，Gateway 只做 Lambda → MCP
  工具转换（工具 schema 在 `infra/tool-schema.json`，Lambda 收到的工具名带
  `目标名___` 前缀，handler 里剥掉）。
- **Keycloak 自部署**：镜像烧入 realm（H2 存储，重启回基线）→ ECR → ECS Fargate
  （ARM64，本机是 aarch64，构建时显式 `--platform linux/arm64`）；HTTPS 入口是
  API Gateway HTTP API + VPC Link + Cloud Map（**必须 SRV 记录**，A 记录无端口会
  500）。App Runner 已停止接新客户，不要用。
- **Keycloak 地址被烧进** Runtime authorizer 与 Identity provider 的 discovery URL：
  Keycloak 栈重建后必须重跑 deploy.sh 同步，否则 403。
- **流式**：`agent/main.py` 入口是 async generator，Runtime 自动转 SSE；Runtime
  数据面无 CORS 头，浏览器经 Vite dev server 代理（`vite.config.ts` 的
  `/api/invoke` 改写）。会话隔离靠 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`
  （≥33 字符），会话内多轮历史存 Runtime microVM 的模块级 dict。
- **会话持久化**：microVM 内存命中就零 DB 读；miss（回收后 / 换实例）时
  `agent/main.py` 从 `chat_messages` 回灌完整 Strands 消息（含工具轮），流结束后
  增量落库。全程 best-effort，DB 异常不阻断 SSE；无 `AURORA_*` 环境变量时降级为
  纯内存。会话元数据由 tasks-api Lambda 的 `/chat` 路由管（`user_name` 取 JWT 的
  `preferred_username`），Runtime 拿不到用户身份，故只管消息不管归属。
- 部署走 `agentcore configure --disable-memory`（Memory 本期不用，预留 mem0）。

### 文档

`实验手册.md`（部署+上手总览）→ 两个部分的 README（细节与 FAQ）。改部署行为时
同步这三处。
