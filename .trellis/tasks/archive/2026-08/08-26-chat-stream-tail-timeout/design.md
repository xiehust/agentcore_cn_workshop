# Chat Portal 流尾后台收尾设计

## 1. 已确认的故障链

当前调用链：

```text
Strands stream_async
  -> yield 正文增量
  -> await _persist_new
  -> await memory_backend.memorize
  -> async generator return / SSE EOF
  -> ChatView busy=false
```

线上 Runtime v12 把 `MEM0_ADD_TIMEOUT` 配成 60 秒。同一请求最后正文日志时间为
`01:07:24.693Z`，`memorize 超时（60s）` 为 `01:08:25.442Z`，形成约 60.7 秒
无 SSE 字节的静默窗口。连接断开后，前端又会把已累积正文整体替换成 network error。

## 2. 目标调用链

```text
Strands stream_async
  -> yield 正文增量
  -> 更新 _HISTORY，捕获 messages/reply 快照
  -> app.add_async_task("chat_turn_finalize")
  -> 提交有界后台 executor
  -> async generator return / SSE EOF
       |
       +-> 串行化该 session 的 _persist_new（经 db Lambda）
       +-> 串行化该 session 的 memory_backend.memorize
       +-> finally: app.complete_async_task(task_id)
```

AgentCore SDK 用 tracked async task 让 `/ping` 在后台工作期间报告 `HealthyBusy`，
响应可以先结束，又不会把未完成线程当空闲工作冻结。

## 3. 并发与失败边界

- 先注册 async task，再提交 executor；submit 失败立即 complete。
- persistence 与 memory 使用独立小型 executor 和独立固定分片锁。
- 第二轮历史落库不等待第一轮最长 60 秒的记忆提取；同 session 的记忆仍按轮次串行。
- worker 在自己的 event loop 中 await `memory_backend.memorize()`，所有退出路径都
  complete tracked task。
- 数据访问继续使用现有 `DB_LAMBDA_NAME` / lambda.invoke 协议，不引入 Data API。

## 4. 前端错误语义

- 未收到正文就失败：维持现有整轮错误卡片。
- 已收到正文后失败：保留正文，追加固定 Markdown warning；异常写入 console。
- AbortError 和会话切换仍不向新会话写入提示。

## 5. 数据一致性与回滚

- `_HISTORY` 在调度前更新；Aurora 持久化变为最终一致，但在 Mem0 前执行。
- 不新增 done 事件，不改变 SSE/REST/数据库契约。
- 若 tracked async task 不能阻止 microVM 冻结，则回滚 Runtime version，不以普通
  fire-and-forget 替代。
