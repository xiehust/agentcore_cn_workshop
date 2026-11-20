<!-- Copyright (c) 2026 Amazon.com and Affiliates. -->
<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# 长期记忆模块（mem0 自部署，后端契约）

> 2026-08-13 随分支 `feat/mem0-long-term-memory` 落地。跨层契约
> （Runtime 内 mem0 ↔ 记忆向量 Lambda ↔ Aurora pgvector ↔ chat_sessions
> 归属模型），改动任何一环前先读这份契约。

## Scenario: 跨会话记住用户分析偏好

### 1. Scope / Trigger

- 触发：涉及记忆检索/写回时机、向量 Lambda RPC 协议、pgvector 表结构、
  mem0 版本升级、记忆归属模型任一环的改动。
- 代码位置：`part2_chat_portal/agent/memory_backend.py`（mem0 封装 +
  自定义向量后端）、`agent/main.py`（`_memory_user`/`_MEMORY_SECTION`/
  invoke 织入点）、`infra/memory-lambda/handler.py`（pgvector 数据面）、
  `infra/part2-analytics.yaml`（MemoryLambdaRole）、`scripts/deploy.sh`
  （步骤 4/7/8）、`scripts/memory_test.sh`。

### 2. 架构决策（为什么这样拆）

- **为什么不用 AgentCore 托管 Memory**：workshop 要验证中国区可移植形态，
  自部署 mem0 + Aurora pgvector 全链路无区域锁定（`--disable-memory`
  与本模块无关，禁的是托管 Memory）。
- **为什么 mem0 跑在 Runtime、向量走 Lambda**（拆分架构）：Aurora VPC 无
  IGW/NAT（仅 Secrets Manager 端点），VPC 内 Lambda 出不去公网调 LLM/
  embedding；Runtime 不挂 VPC 直连不了 5432。于是 mem0 的计算面
  （LLM 事实提取 + embedding，都走 OpenAI 兼容端点）留在公网 Runtime，
  数据面（pgvector SQL）经 `lambda.invoke` 下沉到 VPC 内 Lambda——与
  Part 1 写库 Lambda / Part 2 分析 Lambda 同构，中国区零改动。
- **为什么自动模式而非工具模式**：门户是聊天产品形态，记忆应当无感生效；
  代价是每轮多一次 LLM 提取调用。remember/recall 工具模式（travel agent
  的选择）适合演示记忆操作本身，此处不取。
- **为什么要加提取范围约束**（配置键 `custom_instructions`，常量
  `MEMORY_EXTRACTION_RULES`）：mem0 2.0.x 走 V3「只加不改」流水线（单次 LLM
  调用，没有 UPDATE/DELETE），内置 `ADDITIVE_EXTRACTION_PROMPT` 要求同时从
  user 与 assistant 消息尽量多提取（用户的「requests」也算、「拿不准就提取」），
  且不开语言要求——实测一个漏斗报表提问就写入 5 条带日期的记忆（商品数据、
  助手说的汇率都在内），面板被问答淹没。约束把范围收窄为：只取用户消息、
  只沉淀跨会话成立的分析偏好/身份/长期规则/明确「记住」的内容、用中文记录。
  mem0 把它追加在 user prompt 末尾的 `## Custom Instructions` 段，内置 prompt
  声明该段优先级最高。**曾误用 `custom_fact_extraction_prompt`**：`MemoryConfig`
  静默忽略未知字段，规则从未生效，因此 `_get_memory` 初始化后用 `_rules_active`
  自检，不生效直接禁用记忆（宁可不记，不灌垃圾）。规则里的示例必须沿用 mem0
  的输出结构 `{"memory": [{"id", "text"}]}`（mem0 按 `.get("memory")` 解析）。
  语义去重的副作用：同一偏好再说一遍不会新增条目——已有记忆就是它。
  `seed_memories.py` 直插向量库（`infer=False` 语义），不经提取，不受影响。
- **为什么会话级 recall（只在首轮检索一次）**：记忆注入 system prompt；
  每轮都 recall 会让 system prompt 逐轮变化，主模型 prompt cache 前缀
  全部失效。首轮检索后缓存整个会话（含空结果），同一会话内 system prompt
  逐字节不变。本会话新沉淀的事实对当前会话不注入（上下文本来在场），
  下个新会话首轮生效——这正是"跨会话记忆"的期望形态。

### 3. Signatures

- 表（由 Lambda 按 mem0 pgvector 语义建，随用随建 + deploy 预建）：
  `mem0_ltm(id UUID PK, vector vector(1024), payload JSONB)` +
  HNSW（`vector_cosine_ops`）+ GIN(`to_tsvector('simple',
  payload->>'text_lemmatized')`)；`mem0_ltm_entities` 同构（mem0
  entity_store 懒加载用，无 spaCy 时不会写入）。payload 键由 mem0 管理
  （`data`/`user_id`/`hash`/`created_at`/...）。
- Lambda RPC（`infra/memory-lambda/handler.py`）：
  `{"op": <操作>, "args": {"collection", "dims", ...}}` →
  `{"status": "ok", "result": ...}` / `{"status": "error", "error"}`。
  op 与 mem0 `VectorStoreBase` 一一对应（另加 deploy 专用 `setup`）；
  SQL 语义忠实复刻 mem0 自带 `vector_stores/pgvector.py`（score =
  `max(0, 1-cosine_distance)`、`list` 返回嵌套数组、`keyword_search`
  失败返回 null → mem0 回退纯语义）。
- Runtime 入口（`memory_backend.py`，全部永不抛异常）：
  `recall(user_id, query) -> list[str]`（预算 `MEM0_SEARCH_TIMEOUT`=3s）、
  `memorize(user_id, prompt, reply)`（预算 `MEM0_ADD_TIMEOUT`=20s，连续失败
  3 次熔断禁用）、`enabled() -> bool`。记忆操作跑在专用 2 线程池（超时后
  残留线程不与 `_load_messages`/`_persist_new` 共享默认执行器）；初始化时
  一次性探测 embedding 实际维度，与表结构不符直接禁用并给出根因。
- 记忆归属：`user_id` = `chat_sessions.user_name`（session_id 反查，
  Runtime 进程内缓存 `_SESSION_USER`）。查不到（`smoke-*` 会话、本地
  裸跑）→ 该会话记忆整体跳过。**信 chat_sessions 不信前端**：user_name
  由 tasks-api Lambda 从已验证 JWT 写入（见 chat-sessions-module.md）。
- **REST 读路径**：`GET /chat/memories`（tasks-api Lambda，与 `/chat/sessions`
  同一 JWT 信任模型——身份取 `preferred_username`，绝不接受前端传参）→
  lambda.invoke 记忆 Lambda `list` op `filters={"user_id": <user>}` →
  映射 `payload.data/created_at` 为 `{memories: [{id, memory, created_at}]}`
  按创建时间倒序（只加不改：同一偏好再说不会刷新时间，较早的偏好会被后来的
  条目压到下面）；tasks-api 使用禁重试的 8 秒读超时，必须在 HTTP API 约 30 秒
  集成上限前主动降级（记忆 Lambda 可在后台继续唤醒 Aurora），Lambda 不可达/
  超时统一返回 `{memories: [], degraded: true}`，不抛 500/503。前端镜像类型在
  `web/src/chat/api.ts`（MemoryItem）。
- **多分析师演示**：Keycloak realm 预置 `analyst`（无预置记忆）/`vivian`/
  `marco` 三账号；`scripts/seed_memories.py` 给后两者种入偏好事实——
  **payload 键形复刻 mem0 `_create_memory`**（`data`/`hash`=md5(data)/
  `created_at`/`updated_at`/`user_id`/`text_lemmatized`），幂等靠脚本侧
  hash 去重（mem0 `add(infer=False)` 自身无去重）。mem0 升级时种子脚本
  与 memory_test.sh 一起回归。

### 4. Contracts

- **降级铁律**：记忆是增强功能。mem0 未安装/env 未配置/Lambda 不可达/
  数据面瞬时故障/LLM 提取失败，任何一种只导致本轮或本进程无记忆，
  **绝不阻断 SSE 对话**。初始化失败进程级禁用（不反复重试拖慢请求）。
- **写回必须由 tracked async task await**：普通 fire-and-forget 会在响应后随
  microVM 冻结而丢写；同步挂在 async generator 尾部又会让 SSE 长时间没有字节。
  `main.py` 因此先用 `app.add_async_task` 注册后台收尾，历史落库完成后由独立
  memory executor await `memorize`，最后在 `finally` 调
  `complete_async_task`。任务期间 `/ping` 报 `HealthyBusy`，SSE 则立即 EOF。
  写回仍受 `MEM0_ADD_TIMEOUT`=20s 和连续失败 3 次熔断约束。写回前对缓存的 None
  归属做一次补查——前端注册会话（INSERT）与首轮 SELECT 并发，输掉竞态不能让
  整个会话的记忆写入静默丢失。
- **prompt cache 契约**：`_MEMORY_SECTION[session_id]` 一经写入（含空串）
  本会话不再变化；任何"每轮刷新记忆"的改动都会打穿主模型前缀缓存，
  改之前先想清楚。
- **mem0 私有 API 触点与提取语义**（版本精确锁 `mem0ai==2.0.20` 的原因）：
  `VectorStoreFactory.provider_to_class` 注册表、
  `VectorStoreConfig.__private_attributes__["_provider_configs"]` 默认值、
  `sys.modules["mem0.configs.vector_stores.aurora_lambda"]` 注入、
  pgvector 表结构约定、检索结果 `.id/.score/.payload` 形状；以及提取语义——
  内置 prompt、`custom_instructions` 的拼接位置与输出结构都随补丁版本变化
  （2.0.x 内已换过整条流水线）。升级 mem0 前先跑
  `scripts/memory_extraction_check.py`（复用 `_build_config()`/`_add_turn()`，
  本地 qdrant + 真实模型网关按剧本断言，3 轮全过才算过），再跑
  `memory_test.sh` 与 memory_backend 的注册冒烟。
- **禁 import `mem0.vector_stores.pgvector`**：模块级 try-import psycopg，
  Runtime 不装 psycopg（向量操作已远程化）。
- **env 隔离**：mem0 的 openai provider 会 fallback 读 `OPENAI_API_KEY`/
  `OPENAI_BASE_URL`（已被主模型 bedrock-mantle 占用）——mem0 配置必须
  显式传 `api_key`+`openai_base_url`（`MEM0_*` 系环境变量，deploy.sh
  透传，端点/key 不落仓库）。
- **采样参数**：缺省 `is_reasoning_model=True`（`MEM0_LLM_REASONING`），
  不向提取 LLM 发送 temperature/top_p/max_tokens——推理系模型（Claude
  Opus 4.x 等）只接受默认采样值，发低温会被端点 400 拒掉（实测教训）；
  设 `MEM0_LLM_REASONING=false` 才发 `MEM0_LLM_TEMPERATURE`。
- **维度绑定**：`MEM0_EMBEDDER_DIMS`（默认 1024）决定 pgvector 表结构，
  换 embedding 模型/维度必须同时换 `MEM0_COLLECTION` 重建表。
  `MEM0_EMBEDDER_PASS_DIMS=true` 才把 `dimensions` 参数发给 embedding
  API（仅 matryoshka 系模型支持）。
- IAM：Runtime 执行角色新增 `lambda:InvokeFunction`（仅 memory Lambda，
  deploy.sh 步骤 8 内联策略 `Mem0MemoryLambda` 语句）；MemoryLambdaRole
  = VPC ENI 管理 + 读集群 Secret（与分析 Lambda 同构）。

### 5. Validation & Error Matrix

| 条件 | 行为 |
|---|---|
| `MEM0_LLM_*`/`MEM0_EMBEDDER_MODEL` 任一缺失 | 进程级禁用，`[mem0]` 日志说明缺哪个 |
| 数值型 `MEM0_*` 环境变量为畸形值 | 回退默认并告警（守护解析，import 不抛错） |
| mem0ai 未安装 / 注册失败 | import 不抛错，进程级禁用 |
| 提取范围约束未生效（mem0 改了配置名或结构） | 初始化自检 `_rules_active` 失败，进程级禁用（宁可不记，不灌垃圾） |
| embedding 实际维度 ≠ `MEM0_EMBEDDER_DIMS` | 初始化探测直接禁用，日志给出两种修法 |
| session 查不到 user_name | recall 跳过；流尾 memorize 前弹掉 None 缓存补查一次（防注册竞态） |
| recall 超 3s（数据面或模型端点超时） | 本会话无记忆注入，缓存空串不重试 |
| memorize 超 20s / LLM 提取失败 | 后台丢弃本轮记忆并完成 tracked task；SSE 不受影响；连续 3 次熔断进程级禁用（microVM 重建自愈） |
| Lambda RPC 非 ok / FunctionError | 抛给 mem0 → recall/memorize 捕获降级 |
| Lambda `_run` 重放 insert（已提交但响应丢失） | `ON CONFLICT (id) DO NOTHING` 吸收，不误报失败 |
| 记忆线程池饱和（在途 ≥ 2） | 跳过本次操作，仅日志 |
| 并发 microVM 同一用户写入 | mem0 hash 去重 + LLM 判重，偶发重复事实可容忍 |

### 6. 验证

`scripts/memory_test.sh`（向量 Lambda 全生命周期冒烟）；
`scripts/memory_test.sh --e2e`（同一用户两个新会话跨会话回忆）；
Runtime CloudWatch 日志过滤 `[mem0]`（初始化/recall 命中数/memorize 事件数/
降级原因）。

### 7. 延伸（有意不做）

- 动态徽标（"已载入 N 条记忆"）：需要新增 SSE 事件类型接线 api.ts/
  ChatView/App 三层，超出 workshop 范围；前端徽标目前是静态"已启用"。
- 记忆管理 UI（查看/删除单条记忆）：mem0 的 get_all/delete 已由 Lambda
  RPC 支持，缺的只是门户页面与 REST 路由。
