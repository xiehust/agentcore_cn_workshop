# Part 2：Web Chat 分析门户

通过 Web 聊天界面，用 Agent 分析 Part 1 入库的选品数据。支持**流式输出**、
**固定报表**（选品漏斗、类目/赛道分布）和**开放式即席分析**（自然语言 → 只读 SQL）。
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
   │         │  ⑤ pg8000 直连 5432（同 VPC；凭证走 Secrets Manager VPC 端点）
   │         ▼
   │    Aurora Serverless v2（PostgreSQL，Part 1 创建）
   │
   └──▶ Code Interpreter：统计分析、衍生计算
```

**本部分要验证的关键设计**：Gateway 侧**不使用 VPC egress**——私网访问全部由挂在
Aurora 同 VPC 内的 Lambda 承担（`Gateway → Lambda(VPC) → Aurora(同 VPC)`）。

任务管理模块走的是另一条链路（不经过 Gateway 和 Identity），见
[数据摄入任务管理模块](#数据摄入任务管理模块)。

## 用到的 AgentCore 组件

| 组件 | 用途 |
|------|------|
| **Runtime** | 托管分析 Agent；`--authorizer-config` 配置 JWT 入站认证；SSE 流式响应；按 session 隔离（会话内多轮对话） |
| **Identity** | 出站认证：OAuth2 credential provider 对接 Keycloak（client credentials / M2M），token 缓存在 token vault |
| **Gateway** | 把数据查询 Lambda 转成 3 个 MCP 工具；入站同样用 Keycloak JWT 认证（audience 隔离：Runtime 与 Gateway 各收各的 token） |
| **Code Interpreter** | 对查询结果做统计分析 |
| **Browser** | 任务管理模块里观看 Part 1 抓取时的云端浏览器实况（Live View，DCV 直播流；presigned URL 由服务端签发） |
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
│   ├── tool-schema.json          # Gateway Lambda target 的工具 schema
│   ├── part2-tasks.yaml          # CloudFormation：Tasks HTTP API + JWT authorizer + 两个 Lambda 角色 + 调度组
│   ├── tasks-api-lambda/         # 任务 CRUD / 调度管理 / Live View 签名
│   └── runner-lambda/            # 执行一次运行：SigV4 调 Part 1 Runtime，消费 SSE 落事件
├── scripts/
│   ├── deploy.sh                 # 一键部署（Keycloak + 基础设施 + Gateway + Runtime + 任务管理模块）
│   ├── get_token.sh              # 测试辅助：demo 账号换取用户 token
│   └── chat_test.sh              # 命令行冒烟测试（流式打印 Agent 回复）
└── web/                          # 前端 SPA（Vite + React + TS，本地 dev server）
    └── src/tasks/                # 任务管理页：列表 + 新建抽屉 + 运行详情 + Live View
```

## 部署

前置：Part 1 已部署（依赖其 Aurora 栈）；本机有 Docker；模型认证同 Part 1。

```bash
export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>   # 或依赖 IAM 自动铸 token
./scripts/deploy.sh
```

脚本幂等，可重复执行。首次部署约 10 分钟，共 9 步：Keycloak（1）→ 分析链路基础
设施与数据查询 Lambda（2-3）→ Gateway 与工具注册（4）→ Identity credential
provider（5）→ 分析 Agent 到 Runtime（6-7）→ 前端配置（8）→ 数据摄入任务管理
模块（9）。部署机密（Keycloak admin 密码、demo 账号密码、M2M client secret）
生成后存放在 `.deploy/secrets.env`（已 gitignore）。

第 9 步额外依赖 Part 1 的 Runtime（runner 要调它，ARN 从
`part1_data_ingestion/agent/.bedrock_agentcore.yaml` 解析）和 Part 1 建的任务表；
解析不到 ARN 时脚本只打警告跳过该步，其余部署照常完成，补跑 Part 1 的 deploy.sh
后重跑即可。

## 使用

### 两个登录入口

| 入口 | 地址 | 账号 | 说明 |
|------|------|------|------|
| 选品分析门户 | http://localhost:5173 | `analyst`（密码见 `.deploy/secrets.env` 的 `ANALYST_PASSWORD`） | 属于 `agentcore-workshop` realm，只认门户用户 |
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

侧栏有两个视图：默认的聊天分析，以及「任务管理」（数据摄入任务，见下节）。

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
  Lambda 走 RDS Data API，同样带 `DatabaseResumingException` 退避重试。
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
