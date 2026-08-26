# 数据摄入任务管理模块（后端契约）

> 2026-08-08 随任务 `08-08-ingestion-task-manager` 落地。跨层契约（SPA ↔ HTTP API
> ↔ 两个 Lambda ↔ Part 1 Runtime ↔ Aurora），改动任何一环前先读这份契约。

## Scenario: 门户编排 Part 1 摄入任务

### 1. Scope / Trigger

- 触发：涉及 REST 契约、SSE 事件契约、任务三表 schema、EventBridge Scheduler、
  Live View presign 任一环的改动。
- 代码位置：`part2_chat_portal/infra/{tasks-api-lambda,runner-lambda}/handler.py`、
  `infra/part2-tasks.yaml`、`part1_data_ingestion/agent/main.py`、
  `infra/setup_tables.sql`、前端 `web/src/tasks/api.ts`（TS 类型是契约镜像）。

### 2. Signatures

- 表：`ingestion_tasks(task_id PK, name, prompt, country free-form string,
  mode one_time|recurring,
  schedule_expr, status active|paused|deleted, created_at, updated_at)`；
  `ingestion_runs(run_id PK, task_id, status queued|running|succeeded|failed,
  runtime_session_id, browser_id, browser_session_id, stats JSONB, error,
  started_at, finished_at, created_at)`；
  `ingestion_run_events(event_id BIGSERIAL PK, run_id, seq, kind, message,
  payload JSONB, created_at)`。业务两表有可空 `run_id VARCHAR(36)` 列。
- Part 1 payload：`{"prompt": str, "run_id": str?}`。
- runner 入参：`{"task_id": str, "run_id": str?}` —— **run_id 有无用来区分来源**：
  tasks-api（门户）触发必带 run_id（先 INSERT queued 记录）；Scheduler 只带 task_id。

### 3. Contracts

- REST 全部路由挂 Keycloak JWT authorizer（issuer=Keycloak realm，
  aud=`analysis-runtime`），响应形状见 `web/src/tasks/api.ts` 的类型定义
  （那是契约的唯一镜像，改后端必须同步改它）。
- `country` 是最长 64 字符的自由文本，不是枚举。tasks-api 压缩首尾及重复空白，
  省略或空值兼容为 `UK`；后端不得维护国家白名单或国家到域名/币种/`site` 的映射。
- runner 把数据库中保存的 `country` 原值置于原始 prompt 前，要求 Part 1 选择明确
  支持该值的 Skill。任务字段优先于 prompt 内冲突描述；域名、币种、筛选和入库
  `site` 由 Skill 定义；没有匹配 Skill 时必须报错，不得回退其他国家。
- 新国家的扩展边界是新增识别该保存值的 Part 1 Skill，不修改 tasks-api、runner、
  schema 或前端国家目录。前端 `UK` / `US` 只是可编辑输入框的建议项。
- SSE 结构化事件只有两种 dict（其余为裸文本增量）：
  `{"event":"tool_use","name","tool_use_id"}`、
  `{"event":"browser_session","browser_id","session_id"}`。
  **只在 payload 带 run_id 时发**，保证 CLI 输出不变。
- 事件表 `kind` 词表（三处必须一致：setup_tables.sql 注释 / runner emit /
  前端 RunEventKind）：`phase|tool|log|browser|error|result`。
- env：两个 Lambda 共用 `AURORA_CLUSTER_ARN/AURORA_SECRET_ARN/AURORA_DATABASE`；
  tasks-api 另有 `RUNNER_FUNCTION_NAME/RUNNER_FUNCTION_ARN/SCHEDULE_GROUP/
  SCHEDULER_ROLE_ARN`；runner 另有 `PART1_RUNTIME_ARN`。

### 4. Validation & Error Matrix

| 条件 | 行为 |
|---|---|
| 无/无效 JWT | API Gateway 层 401（不进 Lambda） |
| run 已结束（succeeded/failed）请求 live-view | 410（前端关闭入口） |
| run 未就绪（queued，或 running 无 browser_session_id） | 409（前端可重试） |
| GetBrowserSession 非 READY / NotFound | 410 |
| 一次性任务 pause/resume | 400 |
| recurring 的 schedule_expr 不以 `rate(`/`cron(` 开头 | 400 |
| country 省略、仅空白 | 归一化为 `UK` |
| country 超过 64 字符 | 400，不写任务 |
| Scheduler 触发但任务非 active | 丢弃（保证「暂停后不再触发」） |
| 门户手动 run 暂停中的任务 | **放行**（暂停语义只拦自动调度）；任务已删则把预建 queued 记录收尾成 failed |
| runner 任何异常/Lambda 将超时（剩余 <40s） | run 必须收尾成 failed，**绝不留 running/queued 悬挂** |
| 500 响应 | 只回异常类名，原文只进 CloudWatch（防 SQL/ARN 泄漏） |

### 5. Good/Base/Bad Cases

- Good：门户建 one_time 任务 → 202 带 run_id → 事件流 phase→tool→browser→result
  → succeeded + stats `{candidates,verdicts:{中文词表},duration_s}`。
- Base：run 没用到浏览器（纯测算 prompt）→ 无 browser 事件、无实况入口，属正常。
- Bad：把 `ANY` 路由挂 JWT authorizer → CORS 预检 OPTIONS 被吞成 401，浏览器
  全部跨域失败。必须显式方法路由，OPTIONS 交给 CorsConfiguration。

### 6. Tests Required

- 桩件级：tasks-api 全路由状态码+形状；runner 五场景（Scheduler+暂停丢弃 /
  门户+暂停放行 / 门户+已删收尾 failed / 任务不存在丢弃 / deadline 超时收尾）。
- country 扩展：省略/空白默认 UK、重复空白归一化、自定义值原样入库、超长 400；
  runner prompt 保留原指令并包含通用 Skill 路由约束，不得出现国家到域名的映射。
- 云上冒烟：无 token 401；建任务→轮询到 succeeded；live-view 409/410/200 三分支；
  `aws scheduler get-schedule` 验证 pause=DISABLED。

### 7. Wrong vs Correct

#### Wrong
```python
if task["status"] != "active":
    return  # Scheduler 与门户触发一视同仁地丢弃
```
门户触发前已 INSERT queued 记录，直接 return 会让它永久卡「排队中」。

#### Correct
```python
if task["status"] != "active" and not portal_run_id:
    return  # 只丢弃 Scheduler 的在途触发；门户手动触发放行/收尾预建记录
```

## 既有约定（本模块沿用，新代码也应沿用）

- **本模块的两个 Lambda 走 RDS Data API**（不进 VPC）；analytics Lambda 是
  read-only 会话（`default_transaction_read_only=on`），不要借它写任务表。
  新增数仓访问点看 [数据库访问约定](./database-guidelines.md) 选路径——Part 1
  已改走 VPC 内写库 Lambda（中国区没有 Data API），本模块的迁移是后续任务。
- **Aurora Min 0.5 ACU warm standby**：默认不自动暂停；任务链路仍保留连接
  超时与失败重试，覆盖维护和瞬时故障。
- **Runtime session id ≥33 字符**：`"ingest-run-" + uuid4` = 47 字符。
- **Live View presign 只在服务端做**（botocore `SigV4QueryAuth`，service
  `bedrock-agentcore`，`GET {data-plane}/browser-streams/{id}/sessions/{sid}/live-view`，
  上限 300 秒），SigV4 凭证绝不下发浏览器。
- **CFN 栈首轮两阶段**：`AWS::Lambda::Permission` 不能指向未创建的函数；
  deploy.sh 先 `ApiEnabled=false` 建角色/调度组，函数就绪后再补建 API，重跑幂等。
- **判定词表是中文域**：`PASS/排除/补采/采集`（PASS=未过 R1 硬门槛已丢弃，
  「采集」才是通过）。后端原样透传，不做映射。
