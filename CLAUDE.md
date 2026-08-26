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

区域是唯一开关：海外 `AWS_REGION=us-west-2`、中国 `cn-northwest-1`——部署脚本
按区域自动推导分区（`arn:aws-cn`）、域名后缀（`.amazonaws.com.cn`）与 SDK 端点
覆盖。模型统一标准 OpenAI 兼容 Chat Completions（三元组 env）：全球区默认
bedrock-mantle，`export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>`（ABSK 前缀）；
中国区/自定义端点 `export OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID`。key 必填，
deploy.sh 做 fail-fast 校验（未配 BASE_URL 时 key 必须 ABSK；显式配了 BASE_URL
则信任成对 key，但指向 localhost/私网 IP 直接拒绝——注意部署机环境卫生，公网
残留 BASE_URL 仍会被原样采用）。gpt-5.6-terra 经 /chat/completions 用函数工具
必须 `reasoning_effort=none`（端点硬约束），Agent 对默认 mantle 端点自动带上；
自定义端点缺省不发该参数，需要时 `export MODEL_REASONING_EFFORT` 显式指定。

```bash
# Part 1：部署（Aurora + 写库 Lambda + 建表 + Browser/Profile + Runtime），幂等可重跑
cd part1_data_ingestion && bash scripts/deploy.sh
# Part 1：调用（单商品 2~5 分钟）
# 中国区注意：deploy.sh 会自动覆盖 SDK 端点，但 agentcore invoke/status、
# local_test.py 这类交互命令要先 source scripts/cn_env.sh（海外区是空操作）
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

# 查数仓（RDS Data API 已整体退役——中国区没有；统一经 VPC 内通用数据面 Lambda）
aws lambda invoke --function-name agentcore-workshop-db --region "$AWS_REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"op": "execute", "args": {"sql": "SELECT ...", "params": {}}}' /dev/stdout
```

E2E 浏览器验证用本机 Python 版 Playwright（`playwright` CLI 是 pip 装的）；
门户登录账号 `analyst` / `vivian` / `marco`（长期记忆按登录名隔离，后两者由
`scripts/seed_memories.py` 预置人设记忆），密码在 `part2_chat_portal/.deploy/secrets.env`。

## 架构

两个部分共用一套数仓：Part 1 的 CloudFormation 栈 `agentcore-workshop-aurora` 建
VPC + Aurora Serverless v2（PostgreSQL，Min 0.5 ACU warm standby）+ Secrets Manager
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

- **数据面零 RDS Data API**：中国区没有 Data API（已实证），全仓数据访问统一
  「计算留公网，私网访问全交 VPC 内 Lambda」形态（与 Part 2 的 Gateway →
  Lambda(VPC) → Aurora 同一哲学）。写库 Lambda
  （`infra/writer-lambda/handler.py`）实现 `save_verdict` / `save_candidate` /
  `setup_tables` / `check_tables` 四个 op，`aurora_tools.py` 只剩薄壳；建表也是
  deploy.sh `aws lambda invoke op=setup_tables`（`setup_tables.sql` 打进 zip，
  按分号拆句，**不得引入 dollar-quoted 块**）。超时链：handler 重连上限 240s <
  Lambda timeout 300s < Agent boto3 read_timeout 310s（Agent 侧禁用 boto3 重试）。
  Runtime 执行角色只有对写库 Lambda 的 `lambda:InvokeFunction`
  （栈输出 `AgentWriterInvokePolicyArn`）。**RDS Data API 已整体退役**
  （中国区没有）：Part 2 的会话历史/任务表统一走通用数据面 Lambda
  `agentcore-workshop-db`（`infra/db-lambda/handler.py`，op=execute），集群
  `EnableHttpEndpoint` 与 `AgentDataAccessPolicy` 已删除。
- **业务 SOP 用 Skill 插拔**：Agent 本体（`agent/main.py`）只有通用能力；选品规则
  全在 `agent/skills/*/SKILL.md`。新增场景 = 新增 Skill 目录，不改 Agent 代码。
- **模型**：统一 Strands `OpenAIModel`（标准 OpenAI 兼容 Chat Completions），
  三元组 `OPENAI_BASE_URL`/`MODEL_ID`/`OPENAI_API_KEY` env 注入。全球区默认
  bedrock-mantle 端点（默认 `openai.gpt-5.6-terra`，key 用 Bedrock API key）；
  中国区指区内模型服务商端点。IAM 铸 token 路径已删除，key 必填。
  完整移植状态见根 README「中国区移植说明」。
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
  增量落库（SQL 经 VPC 内通用数据面 Lambda `agentcore-workshop-db` 执行）。
  全程 best-effort，DB 异常不阻断 SSE；无 `DB_LAMBDA_NAME` 环境变量时降级为
  纯内存。会话元数据由 tasks-api Lambda 的 `/chat` 路由管（`user_name` 取 JWT 的
  `preferred_username`），Runtime 拿不到用户身份，故只管消息不管归属。
- 部署走 `agentcore configure --disable-memory`（禁的是 AgentCore 托管 Memory）。
- **长期记忆是自建 mem0**：mem0 跑在 Runtime 进程内（`agent/memory_backend.py`，
  LLM 提取与 embedding 走 OpenAI 兼容端点，`MEM0_*` 环境变量注入）；向量读写经
  `lambda.invoke` 走 VPC 内记忆 Lambda（`infra/memory-lambda/`，pg8000 直连
  Aurora pgvector）。会话首轮 recall 一次注入 system prompt 并缓存整会话
  （保 prompt cache 前缀稳定），每轮流结束后 memorize（await，best-effort，
  绝不阻断 SSE）。user_id 用 session_id 反查 `chat_sessions.user_name`。
  契约见 `.trellis/spec/backend/mem0-long-term-memory.md`。

### 文档

`实验手册.md`（部署+上手总览）→ 两个部分的 README（细节与 FAQ）。改部署行为时
同步这三处。
