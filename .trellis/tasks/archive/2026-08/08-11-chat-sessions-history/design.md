# 设计：分析会话与历史

## 1. 总体链路

复用任务管理模块打通的门户后端模式，新增「会话」域：

```
读路径（列表/回看/管理）：
  SPA（Keycloak 用户 token, aud=analysis-runtime）
    -> Tasks HTTP API（复用，JWT authorizer 不变）新增 /chat/* 路由
    -> tasks-api Lambda（复用函数，扩展路由；RDS Data API）
    -> Aurora 新表 chat_sessions / chat_messages

写路径（对话内容落库 + 历史回灌）：
  SPA -> Runtime（invoke 流式，链路不变）
  Runtime（agent/main.py）:
    - invoke 开始：_HISTORY 未命中该 session -> 从 chat_messages 回灌完整
      Strands messages（microVM 回收后的上下文延续）
    - invoke 结束：把本轮新增的 agent.messages 逐条 append 进 chat_messages
      （best-effort，失败只打日志不影响 SSE 流）
  Runtime 走 RDS Data API（boto3，与 Part 1 同款，不挂 VPC）
```

职责分工（关键决策）：

- **会话元数据（chat_sessions）归 API Lambda 管**：前端在新会话首次发送前
  `POST /chat/sessions` 注册（幂等 upsert），Lambda 从 JWT claims 取
  `preferred_username` 写 `user_name`——Runtime 拿不到用户身份，放前端注册
  是唯一不引入伪造面的取法。
- **消息内容（chat_messages）归 Runtime 管**：只有 Runtime 手里有完整的
  Strands messages（含 toolUse/toolResult），存全量才能保真回灌；前端只见
  最终文本，由它写库则工具轮次丢失、回灌失真。
- CLI 调用（chat_test.sh）没有注册步骤：消息照落库，但 chat_sessions 无行，
  门户列表自然不显示——满足 PRD 非目标，无额外分支。

## 2. 数据模型（DDL 追加到 part1/infra/setup_tables.sql，延续任务三表做法）

```sql
-- 表 6：分析会话（Part 2 门户「分析对话」历史）
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id  VARCHAR(64)  NOT NULL,   -- 前端生成 web-<uuid>（Runtime session id 同值）
    user_name   VARCHAR(128) NOT NULL,   -- Keycloak preferred_username
    title       VARCHAR(256) NOT NULL,   -- 默认首条输入截断，可重命名
    status      VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active / deleted
    created_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user
    ON chat_sessions (user_name, updated_at);

-- 表 7：会话消息（Strands message 全量 JSON，seq 为会话内序号）
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id   BIGSERIAL,
    session_id   VARCHAR(64) NOT NULL,
    seq          INT         NOT NULL,   -- 会话内 0 起自增；回灌按 seq 排序
    role         VARCHAR(16) NOT NULL,   -- user / assistant（Strands 只有这两种 role）
    content      JSONB       NOT NULL,   -- 完整 Strands message（含 toolUse/toolResult）
    display_text TEXT,                   -- content 里 text block 拼接；无文本(纯工具轮)为 NULL
    created_at   TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_session_seq
    ON chat_messages (session_id, seq);
```

- `content` 存 `agent.messages` 的单条原样 JSON，回灌时 `json.loads` 直接塞回
  `Agent(messages=...)`，不做任何转换——保真是第一目标。
- `display_text` 是给前端回看用的降级视图：user 消息取原文，assistant 消息拼接
  text block；`display_text IS NULL` 的行（纯工具轮）读接口直接过滤掉。
- 会话与 Runtime 会话隔离共用同一个 id（`web-<uuid>`，36+4=40 字符 ≥33），
  不引入第二套 id 映射。

## 3. Runtime 改动（agent/main.py）

新增一个小的持久化助手（同文件内，~60 行）：

```python
_HISTORY: dict[str, list] = {}      # 保留：命中时零 DB 读
_PERSISTED_SEQ: dict[str, int] = {} # session -> 已落库的消息数（写游标）

async def invoke(...):
    ...
    if session_id not in _HISTORY:
        _HISTORY[session_id] = _load_messages(session_id)   # 回灌；DB 空表/异常 -> []
        _PERSISTED_SEQ[session_id] = len(_HISTORY[session_id])
    ...
    async for event in agent.stream_async(prompt): ...
    _HISTORY[session_id] = agent.messages
    _persist_new(session_id, agent.messages)  # [_PERSISTED_SEQ:] 起逐条 INSERT
```

- 读写都走 RDS Data API（boto3 client，模块级缓存）；`AURORA_CLUSTER_ARN` /
  `AURORA_SECRET_ARN` 为空（本地 `python main.py` 裸跑）时整体降级为现状
  （纯内存），不报错。
- `_persist_new` 用 `ON CONFLICT (session_id, seq) DO NOTHING`：同 session 并发
  invoke（前端 busy 状态已挡，理论上只有 CLI 能触发）时不炸，最坏丢一轮工具明细。
- 写失败 `except Exception: print(...)` 后继续——PRD 约束「持久化不阻断主链路」。
- 单条消息 JSON 超过 300KB（Data API 参数上限 512KB 留余量）时降级为只存
  text block 的裁剪版，保证回灌不断链。
- Aurora MinCapacity=0 冷恢复：复用 tasks-api 同款退避重试，但 Runtime 侧把
  deadline 收紧到 ~20 秒（回灌等不起 50 秒；超时按空历史继续，宁可丢上下文
  不可挂请求）。

## 4. API 扩展（tasks-api Lambda + part2-tasks.yaml）

复用现有函数与 HTTP API，新增 `/chat` 路由组（CloudFormation 加 4 条 Route：
`GET/POST /chat/{proxy+}`、`PATCH /chat/{proxy+}`、`DELETE /chat/{proxy+}`；
CORS AllowMethods 补 PATCH）：

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | /chat/sessions | 注册会话（幂等 upsert：不存在 INSERT，存在 touch updated_at 并把软删状态恢复 active——用户删掉当前会话后继续对话应重新可见）；body `{session_id, title}` |
| GET | /chat/sessions | 当前用户会话列表（status='active'，LATERAL join 最后一条消息时间做排序键 + 消息数） |
| GET | /chat/sessions/{id}/messages | 消息列表：`seq, role, display_text, created_at`，过滤 `display_text IS NULL`，按 seq 升序；单页 200 条 + `after_seq` 游标、响应带 `has_more`（Data API 单次 1MB 上限，截断必须显形） |
| PATCH | /chat/sessions/{id} | 重命名；body `{title}` |
| DELETE | /chat/sessions/{id} | 软删（status='deleted'），消息保留 |

- 用户身份从 `event.requestContext.authorizer.jwt.claims.preferred_username` 取
  （HTTP API JWT authorizer 校验通过后必有）；所有会话路由按 `user_name` 过滤，
  取不到 claim 时 403。
- `session_id` 服务端校验格式 `^web-[0-9a-f-]{36}$`，其余拒绝——防止把 CLI
  会话或任意字符串注册到自己名下。
- handler 继续单文件：新增 `/chat` 分支挂进现有 `_dispatch`，风格（`_execute`
  / `_p` / `_resp`）不变。

## 5. 前端改动（web/src/）

- **新增 `web/src/chat/api.ts`**（或并入现有 api.ts；定稿：新建 `chat/api.ts`，
  复用 tasks/api.ts 的 `ApiError`/`request` 模式但不跨目录 import，request 封装
  提到 `web/src/lib/http.ts` 供两处共用）。类型：`ChatSession`、`ChatMessage`。
- **App.tsx**：sessionId 状态从 ChatView 提升到 App（侧栏历史列表要用）；侧栏
  「工作区」与「报表与分析」之间加「历史会话」区：列表（标题 + 相对时间）、
  hover 出重命名/删除按钮；点击项 -> 切到 chat 视图并加载该会话。
- **ChatView.tsx**：
  - props 增加 `sessionId` + `onSessionActivity`（首次发送时上抛，App 触发
    POST 注册 + 列表刷新）；
  - 新增「加载历史会话」路径：置 `messages = GET messages` 的 `display_text`
    映射（role 直接对应，Markdown/chart 渲染复用 `MarkdownBlock`）；
  - 发送逻辑不变（仍 streamChat 到 Runtime）；首条发送成功启动后调注册接口
    （fire-and-forget，失败 toast 级别忽略）。
- **样式**：沿用暗色量化工作台 token（现有 CSS 变量/类），历史列表项复用
  `.nav` 按钮风格，不新增美学方向。改动前按约定过一遍 frontend-design skill
  确认只是延伸现有风格。

会话恢复后的「继续追问」：直接用原 session_id 调 Runtime；microVM 若还活着
`_HISTORY` 命中，死了则回灌——前端无感知。

## 6. deploy.sh 改动

1. 建表段：DDL 追加到 part1/infra/setup_tables.sql，由 Part 1 deploy.sh 的
   「按分号拆条执行」段生效（任务三表同款先例；已核实建表只在 Part 1 跑，
   验收时重跑 Part 1 deploy.sh 或手工 rds-data 执行两条 DDL）。
2. `agentcore launch` 增加 `--env AURORA_CLUSTER_ARN=... --env AURORA_SECRET_ARN=...`
   （值 deploy.sh 已从 Part 1 栈输出取过，供 analytics Lambda 用）。
3. 第 7 步 Runtime 角色内联策略追加 rds-data 四个 Execute*/BatchExecute* 动作
   与读 Secret 权限（或直接 attach Part 1 栈输出的 AgentDataAccessPolicy，
   与两个 Lambda 一致——选 attach，少一份手写策略）。
4. part2-tasks.yaml 加 /chat 路由 + CORS PATCH，`aws cloudformation deploy` 幂等。

## 7. 兼容与回滚

- 表是新增的，旧数据零迁移；Runtime 环境变量缺省时行为=现状，故可先发
  Lambda/API 再发 Runtime，顺序不敏感。
- chat_test.sh：消息会落库（无 session 行），功能不受影响；验收跑一遍确认。
- 回滚：还原 main.py / handler.py / 前端并重部署即可，两张新表留着无副作用。

## 8. 风险与权衡

| 风险 | 处置 |
|---|---|
| 回灌时 Aurora 冷恢复慢（Min 0 ACU） | 20 秒退避上限，超时按空历史继续并打日志；首条消息前端本来就有"分析中"态 |
| toolResult 大 JSON 超 Data API 限制 | >300KB 裁剪为纯文本版；极端场景丢工具明细但对话不中断 |
| 同 session 并发写 seq 冲突 | 唯一索引 + DO NOTHING；前端 busy 已串行化，仅 CLI 可能触发 |
| Strands 版本升级改 message 结构 | content 存原样 JSON，回灌同版本自洽；跨版本升级时旧会话最坏退化为"读得出文本、回灌失败按新会话"（_load 异常返回 []） |
| 只重跑 Part 2 的环境缺 chat 两表 | Part 2 deploy.sh 开头 to_regclass 预检（带冷恢复重试），缺表快速失败并提示先跑 Part 1 |
| 超长会话回灌撞 Data API 1MB 上限 | 已接受（MVP）：异常吞掉按空历史继续，上下文丢但不断链；将来可"只回灌最近 N 轮" |
| 回灌失败后该 microVM 内不再落库 | 已接受：游标为 0 时后续写被唯一索引挡掉，库里保持故障前完整前缀，数据不会错乱 |
| tasks-api Lambda 职责变宽 | 接受：workshop 规模下单函数-单 API 是既定模式；命名不改避免栈资源替换 |
