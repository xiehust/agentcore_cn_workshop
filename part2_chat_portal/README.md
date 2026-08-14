# Part 2：Web Chat 分析门户

通过 Web 聊天界面，用 Agent 分析 Part 1 入库的选品数据。支持**流式输出**、
**固定报表**（选品漏斗、类目/赛道分布）和**开放式即席分析**（自然语言 → 只读 SQL）。
对话[**落库持久化**](#分析会话与历史)，侧栏可回看、恢复、重命名、删除历史会话。
门户还带一个[**数据摄入任务管理**](#数据摄入任务管理模块)模块：在页面上创建、调度、
观察 Part 1 的摄入任务（含云端浏览器实况），不必回命令行 `agentcore invoke`。

## 架构

```
用户浏览器（React SPA，本地 dev server）
   │  ① OIDC 登录（Authorization Code + PKCE）
   ▼
Keycloak（自部署 IdP：自定义镜像 → ECR → ECS Fargate，
         HTTPS 入口 = API Gateway HTTP API + VPC Link + Cloud Map）
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
   │         │  ⑤ pg8000 直连 5432（同 VPC；凭证走 Secrets Manager VPC 端点，
   │         │     端点建在 Part 1 的 Aurora 栈里，与它的写库 Lambda 共用）
   │         ▼
   │    Aurora Serverless v2（PostgreSQL，Part 1 创建）
   │
   ├──▶ Code Interpreter：统计分析、衍生计算
   │
   └──▶ RDS Data API ──▶ chat_messages（对话逐轮落库；沙箱回收后回灌恢复上下文）
```

**本部分要验证的关键设计**：Gateway 侧**不使用 VPC egress**——私网访问全部由挂在
Aurora 同 VPC 内的 Lambda 承担（`Gateway → Lambda(VPC) → Aurora(同 VPC)`）。

任务管理模块走的是另一条链路（不经过 Gateway 和 Identity），见
[数据摄入任务管理模块](#数据摄入任务管理模块)。

## 用到的 AgentCore 组件

| 组件 | 用途 |
|------|------|
| **Runtime** | 托管分析 Agent；`--authorizer-config` 配置 JWT 入站认证；SSE 流式响应；按 session 隔离（会话内多轮对话），历史双层——沙箱内存 + 数仓回灌 |
| **Identity** | 出站认证：OAuth2 credential provider 对接 Keycloak（client credentials / M2M），token 缓存在 token vault |
| **Gateway** | 把数据查询 Lambda 转成 3 个 MCP 工具；入站同样用 Keycloak JWT 认证（audience 隔离：Runtime 与 Gateway 各收各的 token） |
| **Code Interpreter** | 对查询结果做统计分析 |
| **Browser** | 任务管理模块里观看 Part 1 抓取时的云端浏览器实况（Live View，DCV 直播流；presigned URL 由服务端签发） |
| **Memory** | 托管 Memory 不启用（部署 `--disable-memory`）；长期记忆用 **AWS 自部署 mem0** 实现，跨会话记住用户分析偏好，见[长期记忆（mem0 自部署）](#长期记忆mem0-自部署)。[会话历史](#分析会话与历史)是自建的两张 Aurora 表，与两者都无关 |

## 目录结构

```
part2_chat_portal/
├── agent/                        # 分析 Agent（部署到 AgentCore Runtime）
│   ├── main.py                   # 流式入口 + Identity M2M + Gateway MCP 工具
│   │                             #   + 会话历史落库/回灌（RDS Data API）
│   ├── memory_backend.py         # 长期记忆：mem0 封装 + 自定义向量后端
│   │                             #   （向量操作 RPC 到 VPC 内记忆 Lambda）
│   └── requirements.txt
├── infra/
│   ├── keycloak/                 # 自部署 Keycloak（Dockerfile + realm 模板）
│   ├── analytics-lambda/         # 数据查询 Lambda（pg8000 直连 Aurora）
│   ├── memory-lambda/            # 记忆向量 Lambda（mem0 pgvector 数据面）
│   ├── part2-analytics.yaml      # CloudFormation：Lambda/Gateway 角色
│   │                             #   （Secrets VPC 端点归 Part 1 的 Aurora 栈）
│   ├── tool-schema.json          # Gateway Lambda target 的工具 schema
│   ├── part2-tasks.yaml          # CloudFormation：Tasks HTTP API（/tasks、/runs、/chat 路由）
│   │                             #   + JWT authorizer + 两个 Lambda 角色 + 调度组
│   ├── tasks-api-lambda/         # 任务 CRUD / 调度管理 / Live View 签名 / 会话元数据（/chat）
│   └── runner-lambda/            # 执行一次运行：SigV4 调 Part 1 Runtime，消费 SSE 落事件
├── scripts/
│   ├── deploy.sh                 # 一键部署（Keycloak + 基础设施 + Gateway + Runtime + 任务管理模块）
│   ├── get_token.sh              # 测试辅助：demo 账号换取用户 token
│   ├── chat_test.sh              # 命令行冒烟测试（流式打印 Agent 回复）
│   └── memory_test.sh            # 长期记忆冒烟（向量 Lambda 生命周期 + 跨会话回忆）
└── web/                          # 前端 SPA（Vite + React + TS，本地 dev server）
    └── src/
        ├── chat/                 # 会话历史接口（列表 / 注册 / 消息 / 重命名 / 删除）
        ├── lib/                  # 共用小工具：http.ts（请求封装）、time.ts
        └── tasks/                # 任务管理页：列表 + 新建抽屉 + 运行详情 + Live View
```

## 部署

前置：Part 1 已部署（依赖其 Aurora 栈）；本机有 Docker；模型认证同 Part 1。

```bash
export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>   # 或依赖 IAM 自动铸 token
./scripts/deploy.sh
```

脚本幂等，可重复执行。首次部署约 10 分钟，共 10 步：Keycloak（1）→ 分析链路
基础设施与数据查询 Lambda（2-3）→ 记忆向量 Lambda 与 pgvector 初始化（4）→
Gateway 与工具注册（5）→ Identity credential provider（6）→ 分析 Agent 到
Runtime（7-8）→ 前端配置（9）→ 数据摄入任务管理模块（10）。部署机密
（Keycloak admin 密码、demo 账号密码、M2M client secret）生成后存放在
`.deploy/secrets.env`（已 gitignore）。

长期记忆（mem0）需要一个 OpenAI 兼容端点做事实提取与 embedding，部署前
export（不 export 也能部署，记忆功能整体禁用、对话不受影响）：

```bash
export MEM0_LLM_MODEL=<提取模型名>                  # 需稳定输出 JSON
export MEM0_LLM_BASE_URL=<OpenAI 兼容端点>/v1
export MEM0_LLM_API_KEY=<key>
export MEM0_EMBEDDER_MODEL=<embedding 模型名>
export MEM0_EMBEDDER_DIMS=1024                      # 维度决定 pgvector 表结构
```

> **已有环境升级注意**：数据查询 Lambda 取凭证用的 Secrets Manager 接口端点已
> 从本部分的 analytics 栈挪到 Part 1 的 Aurora 栈（端点随 VPC 归属，且同一 VPC
> 对同一服务只允许一个 Private DNS 端点）。因此升级顺序是**先跑本部分的
> `deploy.sh`**（栈更新删掉旧端点），**再跑 Part 1 的 `deploy.sh`**（建新端点）；
> 顺序反了 Part 1 的脚本会预检报错。两次之间的窗口期里本部分的 Lambda 冷启动
> 取不到凭证，跑完 Part 1 即恢复。代码与运行时配置零改动。

第 9 步额外依赖 Part 1 的 Runtime（runner 要调它，ARN 从
`part1_data_ingestion/agent/.bedrock_agentcore.yaml` 解析）和 Part 1 建的任务表；
解析不到 ARN 时脚本只打警告跳过该步，其余部署照常完成，补跑 Part 1 的 deploy.sh
后重跑即可。

会话历史相关的三处部署动作：

- 脚本开头**预检** `chat_sessions` / `chat_messages` 两表（查询带冷恢复重试）。
  缺表就地失败并提示回 Part 1 重跑 `deploy.sh` 建表——与其部署完再排查
  `/chat` 接口 500、Runtime 每轮落库报错，不如先失败。
- 第 6 步 `agentcore launch` 给 Runtime 注入
  `AURORA_CLUSTER_ARN` / `AURORA_SECRET_ARN` / `AURORA_DATABASE`（值取自 Part 1
  的 Aurora 栈输出）。
- 第 7 步给 Runtime 执行角色 attach Part 1 栈输出的数据访问托管策略
  （`AgentDataAccessPolicyArn`，与任务管理模块的两个 Lambda 同一份权限）。

后两项由脚本无条件执行、天然幂等；本地裸跑 Agent（没有这些环境变量）时对话
功能不受影响，只是历史退化为纯内存（等同旧行为）。

## 使用

### 两个登录入口

| 入口 | 地址 | 账号 | 说明 |
|------|------|------|------|
| 选品分析门户 | http://localhost:5173 | 三个分析师：`analyst`（基线，无预置记忆）、`vivian`（资深买手）、`marco`（风控合规）；密码见 `.deploy/secrets.env` 的 `<大写用户名>_PASSWORD` | 属于 `agentcore-workshop` realm，只认门户用户；长期记忆按登录名隔离，多账号用于[记忆对比演示](#长期记忆mem0-自部署) |
| Keycloak 管理台 | `https://<api-id>.execute-api.<region>.amazonaws.com/admin/`（deploy.sh 输出的 Keycloak 地址 + `/admin/`） | `admin`（密码见 `KC_ADMIN_PASSWORD`） | master realm 管理员，与门户账号不通用；登录后左上角可切到 `agentcore-workshop` realm 管理用户/client |

> 注意：两个入口的账号体系是分开的，用 admin 登门户或用 analyst 登管理台
> 都会报 "Invalid username or password"。另外 Keycloak 是内嵌 H2 存储
> （demo 配置），任务重启后管理台里的运行期改动会丢，realm 基线随镜像恢复；
> 要持久加用户请改 `infra/keycloak/realm-template.json` 后 `KC_REDEPLOY=1` 重跑。

```bash
# 启动前端（deploy.sh 已生成 web/.env.local）
cd web && npm install && npm run dev
# 浏览器打开 http://localhost:5173，用 analyst 账号登录（密码见部署输出）

# 或命令行冒烟测试（模拟前端调用，流式输出）
./scripts/chat_test.sh '看下最近30天的选品漏斗'
./scripts/chat_test.sh '候选池里产品点最高的5个商品' <上一次输出的session id>  # 多轮
```

侧栏有两个视图：默认的聊天分析，以及「任务管理」（数据摄入任务，见下节）；
中间是[「历史会话」列表](#分析会话与历史)。

## 分析会话与历史

对话不再是一次性的：每轮消息落 Aurora，页面刷新、换标签、Runtime 沙箱被回收
之后都能回看并接着聊。写和读分两条路——**消息内容归 Runtime，会话元数据归
tasks-api Lambda**：

```
写入：SPA ──▶ Runtime（invoke，链路与前面完全一致）
                 │ ① 沙箱内存里没有该会话时，按 seq 回灌 chat_messages（含工具轮）
                 │ ② 流结束后把本轮新增的消息增量写回
                 ▼
             RDS Data API（Runtime 不挂 VPC）──▶ Aurora

读取 / 管理：SPA ──▶ Tasks HTTP API 的 /chat/* ──▶ tasks-api Lambda ──▶ Aurora
             （与任务管理模块同一个 API、同一个 Lambda、同一个 JWT authorizer）
```

几个设计点：

- **归属靠前端注册**。Runtime 侧拿不到用户身份，所以会话在前端发第一条消息时
  调 `POST /chat/sessions` 注册，`user_name` 由 Lambda 从 JWT claims 的
  `preferred_username` 取（不信前端传值）。`/chat` 的每条查询都按 `user_name`
  过滤，取不到该 claim 直接 403。
- **存全量 Strands message**。`chat_messages.content` 是消息原样 JSON（含
  `toolUse` / `toolResult`），回灌保真；前端回看用的是另一列 `display_text`
  （text block 拼接），纯工具轮该列为 NULL，读接口过滤掉。
- **落库绝不阻断聊天**。任何 DB 异常只打日志，SSE 流照常返回；没有 `AURORA_*`
  环境变量时（例如本地裸跑）整体降级为纯内存历史，行为等同这个功能上线之前。
- **回灌会修尾**。库里若存着不完整的轮次（例如上次落库中途失败，工具调用没有
  对应结果），回灌时把这段尾巴丢掉并顺手从表里删除——留着它们会占住后续消息的
  `seq`，被唯一索引静默挡掉。这样交给模型的消息序列始终合法。
- **CLI 会话不进列表**。`chat_test.sh` 没有注册步骤，消息照落库但 `chat_sessions`
  里没有行，门户列表自然看不到——这是有意为之，不是遗漏。

### 数据表

两张表同样建在 Part 1 的 `infra/setup_tables.sql`（表 6、表 7）：
`chat_sessions`（`session_id` 主键、归属用户、标题、`active`/`deleted` 软删状态）、
`chat_messages`（`(session_id, seq)` 唯一索引、`content` 全量 JSONB、`display_text`）。
会话 id 直接复用 Runtime 的会话隔离 id（前端生成 `web-<uuid>`，满足 ≥33 字符），
不另建一套映射；服务端按 `^web-[0-9a-f-]{36}$` 校验，别的字符串注册不进来。

### API（/chat 路由组）

与 `/tasks`、`/runs` 共用一个 HTTP API 和同一个 JWT authorizer，请求同样要带
`Authorization: Bearer <Keycloak token>`。

| 方法 + 路径 | 用途 |
|------------|------|
| `POST /chat/sessions` | 注册会话，幂等：已存在则 touch `updated_at` 并把软删状态恢复 `active`（删掉当前会话后又继续聊，应重新可见）。body：`session_id`、`title`；id 已属于别的用户时 409 |
| `GET /chat/sessions` | 当前用户的会话列表，按最后活动倒序（最多 200 条）；带 `last_message_at`（会话还没消息时为空）和 `message_count`（不含纯工具轮） |
| `GET /chat/sessions/{id}/messages?after_seq=N` | 消息列表，按 `seq` 升序，单页 200 条 + `has_more`（Data API 单次结果 1MB 上限，长会话靠游标续拉，不做静默截断） |
| `PATCH /chat/sessions/{id}` | 重命名。body：`title` |
| `DELETE /chat/sessions/{id}` | 软删会话（列表消失），消息保留 |
| `GET /chat/memories` | 当前用户的 mem0 长期记忆条目（按沉淀时间倒序）；记忆服务不可达时降级返回空列表 + `degraded` 标记 |

### 页面上的使用

侧栏「历史会话」按最后活动时间倒序列出自己的会话，当前会话高亮。点一条即载入
完整对话回看（Markdown 与图表照常渲染），可以直接接着追问——沙箱还活着就用内存
历史，被回收了就回灌，前端无感知。鼠标悬停出现重命名和删除按钮；删除的正是当前
会话时会自动切到新对话。「新对话」语义不变，新会话在第一条消息发出后才进列表，
标题默认取首条输入截断，之后可以改。

## 长期记忆（mem0 自部署）

会话历史解决"同一个会话接着聊"；长期记忆解决"**换个会话还记得你**"——用户在
任何会话里表达过的分析偏好（"金额换算成人民币"、"我只看英国站"），下次新开会话
时 Agent 自动带着这些偏好工作。用 [mem0](https://github.com/mem0ai/mem0) OSS
自部署实现，不用 AgentCore 托管 Memory（部署仍是 `--disable-memory`，两者无关），
全链路无区域锁定、中国区可原样移植。

### 链路

```
Runtime（分析 Agent + mem0，公网）                VPC 内
┌──────────────────────────────────┐             ┌─────────────────────┐
│ 会话首轮: recall ──────────────────┼─────────────▶ 记忆向量 Lambda      │
│   → 记忆注入 system_prompt（缓存整会话）          │  pg8000 → Aurora    │
│ 每轮结束: memorize（LLM 事实提取） ─┼─────────────▶  pgvector           │
│   LLM/embedding → OpenAI 兼容端点  │  lambda.invoke └─────────────────────┘
└──────────────────────────────────┘
```

- **计算面留公网**：mem0 的 LLM 事实提取与 embedding 都走 OpenAI 兼容端点
  （`MEM0_*` 环境变量注入，端点与 key 不落仓库）；
- **数据面下沉 VPC**：向量读写经 `lambda.invoke` 走记忆向量 Lambda
  （`infra/memory-lambda/`，pg8000 直连 Aurora pgvector）——Aurora VPC 无
  IGW/NAT、Runtime 又不挂 VPC，这是两侧网络约束下的自然拆法，且与数据查询
  Lambda 同构；
- **记忆归属**：按 `chat_sessions.user_name` 隔离（session 反查，身份来自
  已验证的 Keycloak JWT，不信前端）；CLI 冒烟会话（`smoke-*`）未注册归属，
  自动跳过记忆。

### 行为约定

- **会话首轮检索一次，整会话缓存**：记忆注入 system prompt 后本会话内不再
  变化——保证主模型 prompt cache 前缀稳定（每轮检索会把缓存全部打穿）。
  本会话新学到的偏好下个新会话生效。
- **每轮结束自动沉淀**：流结束后本轮问答交 mem0 做 LLM 事实提取（与既有
  记忆去重/合并），SSE 流关闭因此变晚——典型几秒，最坏 `MEM0_ADD_TIMEOUT`
  （默认 20s，正文已送达，仅输入框解锁变晚；连续失败自动熔断）。
- **永不阻断对话**：mem0 未配置/Lambda 不可达/集群冷恢复/提取失败，只降级为
  "本轮或本进程无记忆"，日志（CloudWatch，前缀 `[mem0]`）可查原因。

完整契约（RPC 协议、表结构、降级矩阵、mem0 版本锁定原因）见
`.trellis/spec/backend/mem0-long-term-memory.md`。

### 多分析师记忆对比（演示形态）

记忆按登录名隔离，workshop 预置三个分析师账号演示"不同人、不同经验"：

| 账号 | 人设 | 预置记忆 |
|------|------|---------|
| `analyst` | 基线 | 无——现场演示"说一句偏好 → 新会话自动生效"的沉淀链路 |
| `vivian` | 资深买手 | 人民币展示、只看美国站（库里主要是英国站数据，她会如实提示口径缺口——本身也是记忆生效的演示点）、月销≥50 才纳入、结论先行带表格、高毛利优先 |
| `marco` | 风控合规 | 英镑原币注汇率、必查认证与材质、评分<3 排除、结论附数据来源、保守表述 |

预置记忆由种子脚本写入（幂等，重复跑不产生重复条目；`--wipe <user>` 可清空重来）：

```bash
# 环境变量与部署时的 MEM0_* 同一套
python3 scripts/seed_memories.py
```

演示：vivian 与 marco 分别登录问同一个问题，回答口径截然不同；点侧栏底部
「长期记忆 mem0」徽标展开**「我的长期记忆」面板**（`GET /chat/memories`，
tasks-api 按 JWT 用户过滤后从记忆 Lambda 读取），直接看到当前账号的记忆条目。
命令行对比：`PORTAL_USER=vivian ./scripts/chat_test.sh '...'`。

### 验证

```bash
./scripts/memory_test.sh          # 向量 Lambda 数据面全生命周期冒烟（无 LLM 成本）
./scripts/memory_test.sh --e2e    # 同一用户两个新会话，验证跨会话回忆
```

或者直接在门户里体验：会话 A 告诉它"以后金额都换算成人民币，我只关注英国站"，
新建会话 B 问"看下最近 30 天的选品漏斗"——回答应默认按人民币与英国站口径。
直查记忆库：

```bash
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database product_hunting --region us-west-2 \
  --sql "SELECT payload->>'data' AS memory, payload->>'user_id' AS user_name FROM mem0_ltm"
```

## 数据摄入任务管理模块

把 Part 1 的摄入任务从命令行搬进门户：创建（一次性 / 定期）、并行执行、进度与
关键日志、云端浏览器实况、按运行复盘入库结果。Part 1 的 Runtime **认证方式不变**
（仍是 IAM），`agentcore invoke` 与 `local_test.py` 照旧可用。

### 链路

```
门户任务页（React，src/tasks/）
   │  ① 复用聊天侧同一个 Keycloak 用户 token（audience=analysis-runtime）
   ▼
Tasks HTTP API（API Gateway HTTP API + JWT authorizer + CORS）
   │  ② 校验 Keycloak issuer 与 audience，未带有效 token 直接 401
   ▼
tasks-api Lambda（python3.12，无 VPC）
   │  · 任务/运行/事件/结果 CRUD：RDS Data API 读写（同 Part 1 的入库方式）
   │  · 定期任务：EventBridge Scheduler 建/改/删 schedule
   │  · Live View：服务端 SigV4 预签 URL（AWS 凭证不下发浏览器）
   │
   ├─ ③a 一次性任务 / 手动重跑：异步 Invoke ─┐
   └─ ③b 定期任务：Scheduler 按表达式触发 ──┤
                                            ▼
                              runner Lambda（python3.12，无 VPC，超时 900s）
                                 │ ④ SigV4 InvokeAgentRuntime（独立 session id）
                                 ▼
                              Part 1 的摄入 Runtime（IAM 认证）
                                 │ ⑤ 消费 SSE 流：结构化事件 + 文本增量
                                 ▼
                              ingestion_run_events（进度/日志）+ 运行状态与统计
```

几个设计点：

- **前端只有 Keycloak token**。调 Part 1 Runtime 需要的 SigV4 由 runner Lambda
  用自身执行角色代签，所以 Part 1 不必挂 JWT authorizer，命令行用法得以保留。
- **两个 Lambda 都不进 VPC**，走 RDS Data API。既避开分析侧 Lambda 的只读会话
  限制（那个 Lambda 显式设了 `default_transaction_read_only=on`），也免去 VPC
  出口配置。
- **并发靠 session 隔离**。每次运行分配独立的 Runtime session id
  （`ingest-run-<run_id>`，满足 ≥33 字符要求），对应各自的 microVM，多个任务
  同时跑不会互相污染历史或浏览器会话。
- **进度用轮询而非推送**。前端每 3 秒带 `after_seq` 游标增量拉事件；单次运行
  只有 2~5 分钟，比在 HTTP API + Lambda 上做 SSE 简单得多。

### 数据表

三张表建在 Part 1 的 `infra/setup_tables.sql` 里（随 Part 1 的 deploy.sh 幂等执行）：
`ingestion_tasks`（任务定义：prompt、模式、调度表达式、启停状态）、
`ingestion_runs`（每次运行：状态、session id、浏览器会话、统计 JSONB、起止时间）、
`ingestion_run_events`（事件流：`seq` 自增游标 + `kind` 分类 phase/tool/browser/log/result/error）。
两张业务表新增可空 `run_id` 列，明细表就是按它过滤出「本次运行写入的记录」。

### 部署产物

| 资源 | 名称 / 规格 |
|------|------------|
| CloudFormation 栈 | `agentcore-workshop-part2-tasks`（HTTP API + JWT authorizer + 两个 Lambda 的执行角色 + 调度组 + Scheduler 调用角色） |
| tasks-api Lambda | `agentcore-workshop-tasks-api`（python3.12，60 秒 / 256MB） |
| runner Lambda | `agentcore-workshop-ingestion-runner`（python3.12，900 秒 / 512MB） |
| 调度组 | `agentcore-workshop-ingestion`（每个定期任务一个 schedule，名字就是 `task_id`） |
| 前端配置 | `web/.env.local` 追加 `VITE_TASKS_API_URL=https://<api-id>.execute-api.<region>.amazonaws.com` |

首轮部署会看到该栈被部署两次：两个 Lambda 的执行角色由栈创建，而 HTTP API 的
Lambda 调用许可要等函数存在才能建，脚本因此先用 `ApiEnabled=false` 建角色和调度组，
建完函数再补建 API。这是预期行为，重跑时函数已存在，一次部署到位。

允许跨域的前端来源默认是 `http://localhost:5173,http://127.0.0.1:5173`，
换端口或换机器访问时用 `TASKS_ALLOWED_ORIGINS=... ./scripts/deploy.sh` 覆盖。

### API

全部路由都挂 JWT authorizer，请求需带 `Authorization: Bearer <Keycloak token>`。

| 方法 + 路径 | 用途 |
|------------|------|
| `POST /tasks` | 建任务。body：`name`、`prompt`、`mode`（`one_time` / `recurring`），定期任务另带 `schedule_expr`（`rate(...)` 或 `cron(...)`）与可选 `schedule_tz`。一次性任务立即建 run 并异步触发 runner |
| `GET /tasks` | 任务列表，每条附最近一次运行的摘要 |
| `GET /tasks/{task_id}` | 任务详情 + 运行历史 |
| `POST /tasks/{task_id}/run` | 手动再跑一次（与在跑的运行并行） |
| `POST /tasks/{task_id}/pause`、`.../resume` | 定期任务停用 / 启用（同步改 schedule 的 State 和表内状态）。暂停只拦自动调度，暂停期间仍可用「立即运行一次」手动跑 |
| `DELETE /tasks/{task_id}` | 软删任务并删掉 schedule；历史运行与结果保留 |
| `GET /runs/{run_id}` | 运行状态、起止时间、统计 |
| `GET /runs/{run_id}/events?after_seq=N` | 增量拉事件（单次最多 500 条，前端 3 秒轮询） |
| `GET /runs/{run_id}/results?table=candidates\|verdicts&page=1&page_size=20` | 按 `run_id` 分页查两张业务表的明细（列写死在 handler 里，不接受前端传列名） |
| `GET /runs/{run_id}/live-view` | 返回 Live View presigned URL（300 秒）与画布尺寸；浏览器会话已结束返回 410 |

### 页面上的使用

侧栏「任务管理」→「新建任务」：任务名 + 指令（就是 `agentcore invoke` 的 prompt）
+ 模式。定期任务的周期有两个预设——「每 N 小时」（转成 `rate(N hours)`）和
「每天定时」（转成 `cron(m H * * ? *)`，带时区，透传给 EventBridge Scheduler），
也可以选「自定义」直接写表达式。任务卡片上的操作：「立即运行一次」（与在跑的运行
并行）、「暂停调度」／「恢复调度」、「删除任务」。验证调度是否生效时用
`rate(5 minutes)` 比等每天 09:00 快。

运行详情页：进度时间线（工具调用、浏览器会话建立等）、关键日志、统计卡
（候选商品数 / 判定分布 / 耗时）、明细表（「候选商品」「选品判定」两个 tab 分页）。
判定值按数仓原文显示 `PASS / 排除 / 补采 / 采集`，其中 `PASS` 的语义是
**未过 R1 硬门槛已丢弃**，真正入库成候选档案的是「采集」——鼠标悬停有说明。

运行中可点「打开实况」看云端 Chromium 的 DCV 直播画面（`bedrock-agentcore` npm 包
里的 `BrowserLiveView`，懒加载）。URL 300 秒过期，页面提供「刷新重连」再签一次；
运行结束浏览器会话销毁，入口自动关闭；若这次运行没用到浏览器则没有实况入口。

实测耗时与命令行一致：单商品约 1.5~2 分钟，3 个商品约 3~5 分钟。

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
- **Keycloak on Fargate**：App Runner 自 2026-04-30 起不再接受新客户，改用
  最接近的组合——ECS Fargate（ARM64）跑容器，HTTPS 入口用 API Gateway HTTP API
  + VPC Link + Cloud Map 服务发现（免域名的 `execute-api` 端点，无需 ALB）；
  start-dev + `--import-realm`（realm 配置烧进镜像，重启自动恢复）；若容器
  收不到 `X-Forwarded-Proto` 导致 issuer 变 http，deploy.sh 会自动设置
  `KC_HOSTNAME` 强制 https。
- **Aurora 冷恢复**：集群 MinCapacity=0 空闲自动暂停，Lambda 首次连接会触发恢复
  （15 秒~1 分钟），连接层带重试；首个问题偶尔偏慢属正常现象。两个任务管理
  Lambda 走 RDS Data API，同样带 `DatabaseResumingException` 退避重试。Runtime
  侧的会话历史读写把重试上限收紧到 20 秒——回灌等不起一分钟，超时就按空历史继续，
  宁可丢上下文也不挂住请求。
- **超大消息裁剪**：单条消息 JSON 超过 300KB（Data API 单参数 512KB 留余量）时
  降级为只存文本的裁剪版，极端场景丢的是工具明细，回灌链路不会断。
- **Live View 的前端集成**：`bedrock-agentcore` 包里的 `BrowserLiveView` 写的是
  `import dcv from "dcv"` / `dcv-ui`，这两个裸 specifier 没有对应 npm 包，指的是
  包内 vendored 的 Amazon DCV Web SDK——`vite.config.ts` 用 `resolve.alias` 指过去，
  再用 `vite-plugin-static-copy` 把解码器 worker/WASM 拷进产物（DCV 运行时按固定
  路径 fetch 它们，不经打包器）。`.npmrc` 的 `legacy-peer-deps` 是为了不把该包的
  playwright/strands 等 peer 依赖拉进前端。

## 常见问题

- **登录页报 `Invalid parameter: redirect_uri`**：realm 已放宽为接受任意
  redirect URI（demo 配置），如仍出现说明跑的还是旧镜像，`KC_REDEPLOY=1`
  重跑 deploy.sh。注意浏览器端 PKCE 依赖 Web Crypto，只在**安全上下文**
  可用——请通过 `localhost` 端口转发或 HTTPS 隧道访问门户，不要用
  `http://<IP>:5173` 直连（页面会卡在空白）。
- **登录后调用返回 403**：确认前端拿到的 token audience 是 `analysis-runtime`
  （jwt.io 解码看 `aud`），以及 Runtime 的 authorizer discovery URL 与 Keycloak
  地址一致（Keycloak 重建后需重跑 deploy.sh 同步）。
- **工具调用报数据库超时**：多为 Aurora 冷恢复中，重问一次即可。
- **长期记忆不生效（换会话后不记得偏好）**：按顺序查 Runtime 的 CloudWatch
  日志里 `[mem0]` 前缀的行——`长期记忆禁用` 说明 `MEM0_LLM_MODEL` /
  `MEM0_LLM_BASE_URL` / `MEM0_LLM_API_KEY` / `MEM0_EMBEDDER_MODEL` 没配齐
  （export 后重跑 deploy.sh）；`recall 超时` 多为 Aurora 冷恢复（0 ACU 唤醒
  要几十秒），该会话检索被放弃且不重试，新开会话即可；`memorize 失败` 看
  具体报错——embedding 维度与表不符（`MEM0_EMBEDDER_DIMS` 和建表时不一致，
  换 `MEM0_COLLECTION` 重建）、提取模型输出非法 JSON（换个模型）都在此列。
  记忆链路任何故障都不影响正常对话。
- **每轮回答完输入框要多等几秒才解锁**：正常——流结束后 mem0 在做本轮的
  记忆提取（LLM 调用），必须等它写完 Runtime 才能安全冻结沙箱。典型几秒、
  最坏 `MEM0_ADD_TIMEOUT`（默认 20s）；提取连续失败 3 次会自动熔断停付。
- **想清空某用户的长期记忆**：直接删行即可
  `DELETE FROM mem0_ltm WHERE payload->>'user_id' = '<用户名>'`
  （rds-data execute-statement，库表见上文「长期记忆」节）。
- **侧栏「历史会话」不显示，或回看旧会话后追问不认前文**：这两个症状对应链路的
  两端。列表空说明读侧有问题——`GET /chat/sessions` 是不是 401/403（token 过期，
  或 token 里没有 `preferred_username`），以及会话是否真的注册过（CLI 会话不注册）。
  追问丢上下文说明写侧没落库，按顺序查：Runtime 有没有
  `AURORA_CLUSTER_ARN` / `AURORA_SECRET_ARN` 环境变量（`agentcore status` 或控制台
  看运行时配置）、执行角色有没有挂 Part 1 的数据访问策略、数仓里两张表是否存在
  （`SELECT COUNT(*) FROM pg_tables WHERE tablename IN ('chat_sessions','chat_messages')`）。
  落库失败只打日志不影响回答，所以界面上不会报错，日志在 Runtime 的 CloudWatch
  日志组里。重跑 Part 1 的 deploy.sh 建表、再重跑本脚本可一并修好后两项。
- **怎么确认回灌真的生效**：不用等沙箱空闲 15 分钟——强制停掉当前的 Runtime
  会话再用同一个会话追问一句依赖前文的话（如「刚才那个漏斗」）。注意本 Runtime
  配的是 JWT 入站认证，`agentcore stop-session` 和 `aws bedrock-agentcore
  stop-runtime-session` 走 SigV4 会报 `Authorization method mismatch`，要带用户
  token 直调数据面（或者干脆重新部署一次把沙箱顶掉）：

  ```bash
  TOKEN=$(./scripts/get_token.sh)
  ARN_ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('<Runtime ARN>', safe=''))")
  curl -X POST "https://bedrock-agentcore.<region>.amazonaws.com/runtimes/$ARN_ENC/stopruntimesession" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <session id>" -d '{}'
  ```

  回答仍接得上说明历史是从 Aurora 回灌的，此路径已实测通过；Runtime 日志里能
  看到 `[history] 已回灌 N 条历史消息`。
- **Keycloak 重启后登录态丢失**：dev 模式内嵌 H2 存储，实例重启丢运行期数据
  （用户重新登录即可；realm 配置来自镜像内的导入文件，不会丢）。
- **deploy.sh 第 4 步报 `unknown command 'configure'`**：本机装了 npm 版
  AgentCore CLI（`@aws/agentcore`），它在 `PATH` 里遮蔽了脚本要用的 Python 版
  `agentcore`（两者子命令不同）。把 Python 版前置即可：
  `PATH="<仓库路径>/.venv/bin:$PATH" ./scripts/deploy.sh`。
- **任务页报「未配置 VITE_TASKS_API_URL」**：deploy.sh 第 9 步被跳过（多为 Part 1
  未部署，读不到 Runtime ARN）。补跑 Part 1 的 deploy.sh 后重跑本脚本，然后重启
  Vite dev server 让新的 `.env.local` 生效。
- **任务管理 API 返回 401 / 403**：401 是没带或带了过期的 Keycloak token；403 多为
  token 的 `aud` 不是 `analysis-runtime`，或 Keycloak 重建后没重跑 deploy.sh
  同步 authorizer 的 issuer（与聊天侧同一个原因）。
- **看不到「打开实况」或点开报 410**：Live View 要求该次运行正在进行且浏览器会话
  存活。运行结束会话即销毁，入口自动关闭；有些指令（例如只做测算）根本不开浏览器，
  自然也没有实况。
- **headless 浏览器里 Live View 黑屏**：DCV 解码绘制依赖 WebGL。无 GPU 的
  headless Chromium（CI / Playwright 自动化）需加
  `--use-angle=swiftshader --enable-unsafe-swiftshader` 走软件渲染，否则画框
  一直黑屏，容易误判成连接失败；真人用桌面浏览器不受影响。
- **不起前端时怎么冒烟测试任务管理**：
  ```bash
  TOKEN=$(./scripts/get_token.sh)
  TASKS_API=$(grep VITE_TASKS_API_URL web/.env.local | cut -d= -f2)
  # 建一个一次性任务（返回 task 与 run_id）
  curl -s -X POST "$TASKS_API/tasks" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"name":"smoke","prompt":"从 eBay 英国站搜索 desk organizer 选品，处理第 1 个非广告商品","mode":"one_time"}'
  # 轮询运行状态直到 succeeded / failed（单商品约 1.5~2 分钟）
  curl -s "$TASKS_API/runs/<run_id>" -H "Authorization: Bearer $TOKEN"
  # 进度事件与结果明细
  curl -s "$TASKS_API/runs/<run_id>/events?after_seq=0" -H "Authorization: Bearer $TOKEN"
  curl -s "$TASKS_API/runs/<run_id>/results?table=verdicts" -H "Authorization: Bearer $TOKEN"
  ```
  不带 `Authorization` 头再打一次，应当返回 401。
