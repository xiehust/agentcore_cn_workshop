# 分析会话与历史模块（后端契约）

> 2026-08-11 随任务 `08-11-chat-sessions-history` 落地。跨层契约（SPA ↔ Tasks
> HTTP API 的 `/chat` 路由 ↔ tasks-api Lambda ↔ Aurora ↔ Part 2 Runtime），
> 改动任何一环前先读这份契约。

## Scenario: 门户对话持久化与历史恢复

### 1. Scope / Trigger

- 触发：涉及 `/chat` REST 契约、chat 两表 schema、Runtime 回灌/落库逻辑、
  会话归属模型任一环的改动。
- 代码位置：`part2_chat_portal/infra/tasks-api-lambda/handler.py`（`/chat` 分支）、
  `infra/part2-tasks.yaml`（4 条 `/chat/{proxy+}` 路由）、
  `part2_chat_portal/agent/main.py`（`_load_messages`/`_persist_new` 一族）、
  `part1_data_ingestion/infra/setup_tables.sql`（表 6/7）、
  前端 `web/src/chat/api.ts`（TS 类型是契约镜像）。

### 2. Signatures

- 表：`chat_sessions(session_id PK VARCHAR(64), user_name, title,
  status active|deleted, created_at, updated_at)`；
  `chat_messages(message_id BIGSERIAL PK, session_id, seq INT, role user|assistant,
  content JSONB, display_text TEXT NULL, created_at)`，
  `(session_id, seq)` 唯一索引。
- **session_id 一 id 三用**：前端 `web-<uuid>` 同时是 Runtime 会话隔离 id
  （≥33 字符）、chat_sessions 主键、chat_messages 外键。服务端校验
  `^web-[0-9a-f-]{36}$`，CLI 的其他格式 id 消息照落库但注册不进列表。
- Runtime 持久化入口：`_load_messages(session_id) -> list`（回灌）、
  `_persist_new(session_id, messages)`（增量落库，游标 `_PERSISTED_SEQ`）。

### 3. Contracts

- `/chat` 与 `/tasks`、`/runs` 共用 HTTP API、Lambda、JWT authorizer
  （aud=`analysis-runtime`）。响应形状以 `web/src/chat/api.ts` 类型为唯一镜像。
- 归属模型（关键分工，动之前想清楚）：**消息内容归 Runtime**（只有它拿得到含
  toolUse/toolResult 的完整 Strands messages），**会话元数据归 Lambda**（只有它
  拿得到 JWT claims 的 `preferred_username`；不信前端传的任何身份字段）。
- `content` 存 Strands message **原样 JSON**，回灌零转换；`display_text` 是
  text block 拼接的展示降级视图，纯工具轮为 NULL 且读接口过滤。
- 路由语义：
  - `POST /chat/sessions` 幂等注册：不存在 INSERT；已存在 touch `updated_at`
    并把软删恢复 `active`；id 属于别的用户 409。
  - `GET /chat/sessions`：当前用户 + `status='active'`，
    `ORDER BY COALESCE(最后消息时间, updated_at) DESC`；`last_message_at` 可空、
    `message_count` 不含纯工具轮。
  - `GET /chat/sessions/{id}/messages?after_seq=N`：seq 升序单页 200 条 +
    `has_more`（lambda.invoke 响应 6MB 上限，截断必须显形，前端游标续拉）。
  - `PATCH`（重命名）/`DELETE`（软删，消息保留）。
  - `GET /chat/memories`（长期记忆读路径，同一 JWT 信任模型）归
    mem0-long-term-memory.md 管，此处不重复。
- env（Runtime）：`DB_LAMBDA_NAME`（通用数据面 Lambda 函数名），缺失 →
  持久化整体短路，行为等同纯内存旧版（本地裸跑依赖这一点）。SQL 经
  `{"op":"execute","args":{"sql","params"}}` 协议 lambda.invoke 执行——
  RDS Data API 已退役（中国区没有）。
- 流尾一致性：模型正文结束后先更新进程内 `_HISTORY`，再注册 AgentCore tracked
  async task 执行 `_persist_new`；SSE 不等待 db Lambda。SDK 在任务期间通过
  `/ping` 报 `HealthyBusy`，保证响应关闭后 microVM 不冻结后台写入。固定大小的
  persistence executor 按调度顺序处理，各 session 的分片锁保证
  `_PERSISTED_SEQ` 单调前进。
- IAM：Runtime 执行角色对 db Lambda 与记忆 Lambda 的 `lambda:InvokeFunction`
  （deploy.sh 步骤 8 的 `DataPlaneLambdas` 语句）；tasks-api / runner 的
  `InvokeDbLambda` 在 part2-tasks.yaml。

### 4. Validation & Error Matrix

| 条件 | 行为 |
|---|---|
| JWT 无 `preferred_username` claim | 403 |
| session_id 不匹配 `^web-[0-9a-f-]{36}$` | 400 |
| 注册的 id 已属于其他用户 | 409 |
| 读/改/删他人会话或不存在的会话 | 404（不泄漏存在性） |
| Runtime 回灌时 Aurora 冷恢复超 ~20 秒 | 按空历史继续（丢上下文不挂请求） |
| Runtime 后台落库任何异常 | 打日志并完成 tracked task，SSE 已正常结束；游标不前移，下轮靠唯一索引 + DO NOTHING 兜重复 |
| 单条消息 JSON >300KB | 裁剪 toolUse 入参/超长 text 但保留 toolUseId 配对；含工具块且仍超限则跳过该条落库 |
| 库里存在不完整轮次尾巴（上次中途失败） | 回灌时截掉并 DELETE 残行——不删会占住 seq，新消息被唯一索引静默丢弃 |
| 回灌失败（load 抛异常） | 空历史继续，且该 microVM 内后续不再落库（已接受的退化，数据保持故障前完整前缀） |

### 5. Good/Base/Bad Cases

- Good：门户发消息 → SSE 正常结束 → 等后台落库完成 → 强停 runtime session → 同 id 追问
  「刚才那个漏斗」→ 回灌后回答接得上（已实测：强停后答出前文的 41.7%）。
- Base：本地 `python main.py` 裸跑（无 DB_LAMBDA_NAME env）→ 纯内存多轮，无报错无落库。
- Bad：前端自己写消息进库（丢工具轮，回灌失真）；Lambda 信前端传的 user_name
  （越权）；回灌不做配对完整性检查（喂给模型非法序列，模型直接拒答，会话
  永久聊不动）。

### 6. Tests Required

- handler 桩件断言（无部署即可跑）：403/400/409/404 矩阵、每条含 chat 表的 SQL
  都带 `user_name` 过滤、软删是 UPDATE、messages 响应不含 `content` 列。
- 真实库端到端：注册幂等且不覆盖重命名；跨用户四路 404；落库→清内存→回灌
  逐字段一致（含 toolUse/toolResult）；超大消息裁剪后配对仍在。
- 后台收尾：慢 Mem0 不延迟 SSE EOF；上一轮 Mem0 未结束时下一轮历史仍能先落库；
  submit/worker 异常都必须 `complete_async_task`，不能把 `/ping` 永久留在 busy。
- 门户 E2E（Playwright）：发消息 → 刷新 → 列表可见 → 点开恢复（chart 渲染）→
  重命名（`window.prompt`）→ 删除（`window.confirm`）。
- 回灌验收：强停 runtime session（见下面 Wrong vs Correct）后追问前文。

### 7. Wrong vs Correct

#### Wrong：用 SigV4 停 JWT 认证的 Runtime 会话

```bash
aws bedrock-agentcore stop-runtime-session ...   # AccessDeniedException:
agentcore stop-session ...                        # Authorization method mismatch
```

Runtime 配了 customJWTAuthorizer 后数据面（含 StopRuntimeSession）只认 bearer。

#### Correct：带用户 token 直调数据面

```bash
TOKEN=$(./scripts/get_token.sh)
curl -X POST "https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencode 的 Runtime ARN>/stopruntimesession" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <session id>" -d '{}'
```

#### Wrong：`_shrink` 超限兜底把带工具块的消息降级成纯文本

拆散 toolUse/toolResult 配对，回灌时模型拒收，会话报废。

#### Correct：裁剪只动 toolUse 入参与超长 text，保留 toolUseId 与配对结构；
实在放不下就跳过该条落库，交给回灌的尾部清理收拾。

## 实施期踩过的坑（Common Mistakes）

- **前端流式增量与会话切换竞态**：`abort()` 后仍可能有排队回调到达，增量写入前
  必须比对「本次 send 捕获的 session id」与当前会话 id（`liveSessionRef` 模式），
  否则增量会追加进别的会话的消息数组。
- **App 侧列表刷新要补拍**：Runtime 是流结束后才落库，前端收到流完成立刻拉列表
  可能少最后一轮，1.5 秒后补拉一次。
- **本机 5173 被占时 vite 自动挪端口**，而 Keycloak redirect 里的 `"*"` 能登录、
  Tasks API 的 CORS 只允许 5173 → 表现为「登录正常但 /chat 全挂」。E2E 脚本用
  `--disable-web-security` 绕过即可，生产口径不受影响。
