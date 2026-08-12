# 技术设计：门户 Data Ingestion 任务管理模块

## 1. 总体架构

在现有两段式架构上新增一条「任务管理」链路，不动既有聊天分析链路：

```
SPA（任务管理页，Keycloak 用户 token）
  │  REST + CORS（JWT authorizer 校验 issuer + aud=analysis-runtime）
  ▼
Tasks HTTP API（API Gateway HTTP API，新）
  ▼
tasks-api Lambda（Python，无 VPC，RDS Data API 读写任务表）
  │  ① CRUD 任务/运行/事件/结果    ② 生成 Live View presigned URL
  │  ③ 一次性任务 → 异步 Invoke runner；定期任务 → 管理 EventBridge Scheduler
  ▼
runner Lambda（Python，无 VPC，timeout 900s）
  │  建 run 记录 → SigV4 调 Part 1 Runtime（独立 session id）→ 消费 SSE 流
  │  → 写 ingestion_run_events / 更新 run 状态与统计
  ▼
Part 1 Runtime（product_ingestion_agent，不改认证方式）
  → Browser 抓取 / CI 测算 / RDS Data API 入库（新增 run_id 标记）
```

选型理由（对应摸底发现的三个落差）：

- **触发路径**：Part 1 Runtime 保持 IAM SigV4（不加 JWT authorizer，保住
  `agentcore invoke` CLI 用法，PRD C2）。由 runner Lambda 用自身 IAM 角色调
  `bedrock-agentcore:InvokeAgentRuntime` 代签。SPA 永远只带 Keycloak token。
- **认证复用**：HTTP API 原生 JWT authorizer 直接校验 Keycloak issuer +
  `aud=analysis-runtime`（workshop-web client 已有 audience mapper，realm 零改动）。
- **数据面**：两个新 Lambda 都走 RDS Data API（Part 1 同款），**不进 VPC**——
  避开现有 analytics Lambda 的 read-only 会话限制（handler.py 设了
  `default_transaction_read_only=on`），也免去 VPC 出口问题。
- **前端直连 + CORS**：Tasks HTTP API 开 CORS（allow origin `http://localhost:5173`
  等，allow header `authorization`），SPA 直接 fetch，不再加 Vite 代理条目
  （Runtime 数据面才需要代理，HTTP API 自己能配 CORS）。

## 2. 数据模型（追加到 `part1_data_ingestion/infra/setup_tables.sql`）

deploy.sh 步骤 2 会自动逐条幂等执行，新 DDL 直接追加即可：

```sql
CREATE TABLE IF NOT EXISTS ingestion_tasks (
    task_id       VARCHAR(36)  NOT NULL,
    name          VARCHAR(256) NOT NULL,
    prompt        VARCHAR(4096) NOT NULL,
    mode          VARCHAR(16)  NOT NULL,          -- one_time / recurring
    schedule_expr VARCHAR(128),                   -- recurring：rate(...)/cron(...)
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active/paused/deleted
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id));

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id             VARCHAR(36) NOT NULL,
    task_id            VARCHAR(36) NOT NULL,
    status             VARCHAR(16) NOT NULL DEFAULT 'queued', -- queued/running/succeeded/failed
    runtime_session_id VARCHAR(64),               -- 调 Part 1 用的 session id（≥33 字符）
    browser_id         VARCHAR(128),              -- Live View 用
    browser_session_id VARCHAR(128),
    stats              JSONB,                     -- {candidates, verdicts:{PASS:..}, duration_s}
    error              VARCHAR(2048),
    started_at         TIMESTAMP,
    finished_at        TIMESTAMP,
    created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id));
CREATE INDEX IF NOT EXISTS idx_runs_task ON ingestion_runs (task_id, created_at);

CREATE TABLE IF NOT EXISTS ingestion_run_events (
    event_id  BIGSERIAL,
    run_id    VARCHAR(36) NOT NULL,
    seq       INT NOT NULL,                        -- runner 内自增，前端增量拉取游标
    kind      VARCHAR(24) NOT NULL,                -- phase/tool/log/browser/error/result
    message   VARCHAR(4096),
    payload   JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id));
CREATE INDEX IF NOT EXISTS idx_events_run ON ingestion_run_events (run_id, seq);

-- 结果关联：业务表加可空 run_id（旧数据不受影响）
ALTER TABLE selection_verdicts  ADD COLUMN IF NOT EXISTS run_id VARCHAR(36);
ALTER TABLE product_candidates  ADD COLUMN IF NOT EXISTS run_id VARCHAR(36);
CREATE INDEX IF NOT EXISTS idx_verdicts_run   ON selection_verdicts (run_id);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON product_candidates (run_id);
```

## 3. Part 1 Agent 改造（向后兼容）

`part1_data_ingestion/agent/main.py` + `agent/tools/aurora_tools.py` +
`agent/tools/browser_ext.py`：

1. **payload 契约扩展**：`{"prompt": ..., "run_id": "<uuid>"}`。无 `run_id` 时
   行为与现状完全一致（CLI/local_test 不受影响，PRD C2）。
2. **run_id 贯穿入库**：`aurora_tools.py` 加模块级 contextvar
   `CURRENT_RUN_ID`；`main.py` 在 invoke 开头 set。`save_verdict` /
   `save_candidate` 的 INSERT 追加 `run_id` 列（值可空）。
   每个 run 独立 session id → 独立 microVM，无并发串扰。
3. **结构化进度事件**：仅当 `run_id` 存在时，除文本增量外额外 yield dict 事件
   （Runtime SSE 会把 dict 序列化为 JSON，runner 据此落 events 表）：
   - `{"event":"tool_use","name":...,"tool_use_id":...}` —— 从
     `stream_async` 的 `current_tool_use` 事件去重提取；
   - `{"event":"browser_session","browser_id":...,"session_id":...}` ——
     browser 会话惰性创建，在流式循环里检测
     `ExtendedAgentCoreBrowser._client_dict` 首次出现 session 后 yield 一次；
   - 纯文本增量保持原样 yield（runner 聚合成「关键日志」行）。
4. **viewport 固定**：`browser_ext.py` 的 `start()` 显式传
   `viewport={"width":1600,"height":900}`，与前端 `BrowserLiveView` 的
   `remoteWidth/remoteHeight` 常量对齐（不匹配会裁切/黑边）。

## 4. 新增 Lambda

### 4.1 tasks-api（`part2_chat_portal/infra/tasks-api-lambda/handler.py`）

无 VPC；env：`AURORA_CLUSTER_ARN/AURORA_SECRET_ARN/AURORA_DATABASE`、
`RUNNER_FUNCTION_NAME`、`PART1_RUNTIME_ARN`、`SCHEDULE_GROUP`、
`SCHEDULER_ROLE_ARN`、`AWS_REGION`。路由（HTTP API payload v2）：

| 方法+路径 | 行为 |
|---|---|
| POST `/tasks` | 建任务；`mode=one_time` → 立即 `lambda:Invoke`(Event) runner；`mode=recurring` → `scheduler:CreateSchedule`（组内、目标 runner、payload `{task_id}`、`FlexibleTimeWindow OFF`） |
| GET `/tasks` | 列表（含各任务最近一次 run 摘要，一条 SQL LEFT JOIN LATERAL） |
| GET `/tasks/{id}` | 详情 + runs 列表 |
| POST `/tasks/{id}/run` | 手动再跑一次（并行验证入口） |
| POST `/tasks/{id}/pause` / `resume` | recurring：`UpdateSchedule` State=DISABLED/ENABLED；表内 status 同步 |
| DELETE `/tasks/{id}` | 软删（status=deleted）+ `DeleteSchedule`（容忍 NotFound） |
| GET `/runs/{id}` | run 详情（状态/时间/stats） |
| GET `/runs/{id}/events?after_seq=N` | 增量事件（前端 3s 轮询游标） |
| GET `/runs/{id}/results?table=candidates\|verdicts&page=` | 按 run_id 查两张业务表，分页 |
| GET `/runs/{id}/live-view` | 校验 run=running 且有 browser_session_id → `BrowserClient.generate_live_view_url(expires=300)` → `{url,width,height,expires_in}`；会话已结束返回 410 |

依赖：仅 boto3（runtime 自带）+ `bedrock-agentcore` SDK（打进 zip，用于
generate_live_view_url；如包体过大可退化为手写 SigV4QueryAuth presign，逻辑
已知：`GET {data-plane}/browser-streams/{id}/sessions/{sid}/live-view`）。

### 4.2 runner（`part2_chat_portal/infra/runner-lambda/handler.py`）

无 VPC，timeout 900s（运行 2~5 分钟 < 15 分钟上限）；env 同上（去
SCHEDULER 相关）。入参 `{task_id}`（tasks-api 或 Scheduler 触发）：

1. 读任务（status 必须 active）→ INSERT `ingestion_runs`（status=running，
   `runtime_session_id = "ingest-run-" + run_id`，≥33 字符）。
2. boto3 `bedrock-agentcore` `invoke_agent_runtime`（SigV4，streaming SSE）。
3. 逐事件消费：dict 事件 → 按 kind 落 `ingestion_run_events`（browser_session
   事件同时 UPDATE run 的 browser 字段）；文本增量按行/句聚合成 `log` 事件
   （节流：每 N 秒或每 M 字符一条，避免事件表爆炸）。
4. 流结束：统计 `SELECT count/verdict 分布 WHERE run_id=...` → UPDATE run
   （status=succeeded，stats jsonb，finished_at）；异常 → status=failed + error。

并行性：一次性任务由 tasks-api 异步 Invoke，天然并发；每 run 独立
runtime session id → Part 1 各自 microVM。

## 5. 基础设施（新文件 `part2_chat_portal/infra/part2-tasks.yaml`）

独立 CFN 栈 `agentcore-workshop-part2-tasks`（不动现有 part2-analytics 栈）：

- `AWS::ApiGatewayV2::Api`（HTTP API，CORS：AllowOrigins 参数化含
  `http://localhost:5173`，AllowHeaders `authorization,content-type`）；
- `AWS::ApiGatewayV2::Authorizer`（JWT：Issuer=Keycloak realm URL，
  Audience=`analysis-runtime`）；路由 `ANY /tasks`、`ANY /tasks/{proxy+}`、
  `GET /runs/{proxy+}` 全挂 authorizer；
- 两个 Lambda 的执行角色：
  - 共同：`rds-data:ExecuteStatement/BatchExecuteStatement` +
    `secretsmanager:GetSecretValue`（直接 attach Part 1 栈输出的
    `AgentDataAccessPolicyArn`）；
  - tasks-api 追加：`lambda:InvokeFunction`(runner)、
    `scheduler:CreateSchedule/UpdateSchedule/DeleteSchedule/GetSchedule`、
    `iam:PassRole`(scheduler 角色)、
    `bedrock-agentcore:GetBrowserSession/ConnectBrowserLiveViewStream`；
  - runner 追加：`bedrock-agentcore:InvokeAgentRuntime`（Part 1 runtime ARN
    及其 `runtime-endpoint/*`）；
- `AWS::Scheduler::ScheduleGroup` + Scheduler 调用 runner 的角色
  （信任 `scheduler.amazonaws.com`，`lambda:InvokeFunction`）。

部署编排：`part2_chat_portal/scripts/deploy.sh` 追加步骤（读 Part 1 栈输出与
`.bedrock_agentcore.yaml` 里的 Part 1 runtime ARN → 部署栈 → zip 两个 Lambda
→ 写 `web/.env.local` 追加 `VITE_TASKS_API_URL`）。幂等：栈 update、函数
update-code、schedule 按名 upsert。

## 6. 前端（`part2_chat_portal/web/`）

- **导航**：不引入 router，`App.tsx` 提升出顶层 `view` 状态
  （`chat | tasks`），侧栏加「任务管理」入口（Phosphor `ListChecksIcon`）。
  聊天视图组件整体下沉为 `ChatView.tsx`，零行为变化。
- **新组件**（沿用 styles.css token 与既有表格/卡片样式类）：
  - `tasks/TasksView.tsx`：任务列表（状态徽标、模式、最近一次 run、操作按钮）
    + 「新建任务」抽屉（名称/prompt/模式单选/周期设置）；
  - `tasks/RunDetail.tsx`：run 详情——进度时间线（phase/tool 事件）、关键日志
    （mono 滚动区，复用 `.thinking/.dots` 表示运行中）、统计卡片（复用
    `.chart-card`）、明细表（复用 `.bubble table` 样式，candidates/verdicts
    两个 tab，分页）；
  - `tasks/LiveView.tsx`：懒加载（`React.lazy`）包一层
    `bedrock-agentcore/browser/live-view` 的 `BrowserLiveView`；
    运行中显示「打开实况」按钮 → 拉 `/runs/{id}/live-view` → 渲染；
    URL 300 秒过期 → 到期前提示 + 「刷新重连」重拉。
  - `tasks/api.ts`：fetch 封装（`VITE_TASKS_API_URL` + `getToken()` bearer）；
    run 运行中每 3s 轮询 events（`after_seq` 游标）与 run 状态。
- **构建配置**：`npm i bedrock-agentcore`（peer 依赖 playwright/ai/strands 与
  前端无关，`.npmrc` 加 `legacy-peer-deps=true` 避免拉入）；`vite.config.ts`
  按官方 blog 加 `resolve.alias`（dcv/dcv-ui）、`resolve.dedupe`（react）、
  `vite-plugin-static-copy`（DCV worker/WASM 文件）。
- **视觉**：全部沿用「量化工作台」token（#c9f158 accent、IBM Plex、Phosphor
  `*Icon`），状态色：running=accent 呼吸点、succeeded=accent、failed=--danger、
  paused=--muted。

## 7. 关键权衡与备选

| 决策 | 备选 | 取舍 |
|---|---|---|
| runner Lambda 同步消费 SSE | Step Functions / 任务自轮询 | 单次 ≤5 分钟，15 分钟上限足够；最简单且能实时落事件 |
| EventBridge Scheduler（每任务一个 schedule） | 每分钟 poller 扫表 | Scheduler 免轮询、暂停/恢复即 UpdateSchedule；代价是多一个 IAM PassRole |
| 复用 aud=analysis-runtime | 新增专用 audience | 少改 realm（镜像要重烧）；workshop 单用户场景可接受，文档注明 |
| 事件轮询（3s） | SSE/WebSocket 推送 | HTTP API + Lambda 做 SSE 复杂；3s 轮询对 2~5 分钟任务体验足够 |
| 新 CFN 栈 part2-tasks | 扩展 part2-analytics 栈 | 现有栈稳定不动，回滚边界清晰 |
| Live View URL 由 tasks-api 生成 | 前端拿 AWS 凭证自签 | SigV4 凭证不得下发浏览器（PRD C6） |

## 8. 风险与回滚

- `bedrock-agentcore` npm 包（0.4.3）较新：若 Vite 集成受阻，退路是按 DCV
  Web SDK 直接接 presigned URL（包内 vendored，alias 指过去即可）。
- Live View presigned URL 上限 300s：前端做倒计时刷新，属产品行为非缺陷。
- Aurora Min 0 ACU 冷恢复：runner/tasks-api 沿用 Part 1 的重试模式
  （`DatabaseResumingException` 退避重试）。
- 回滚：删 `agentcore-workshop-part2-tasks` 栈 + 两个 Lambda 即可；表结构
  只加列/加表，不影响既有链路；Part 1 agent 改动向后兼容，可单独回滚重发。
