# 实施计划：分析会话与历史

依赖顺序：数据层 → API → Runtime → 前端 → 部署脚本 → 文档。
每步末尾是该步的验证命令；⛔ 标记评审/回滚点。

## 步骤 1：数据层（DDL）

- [ ] `part1_data_ingestion/infra/setup_tables.sql` 追加表 6 `chat_sessions`、
      表 7 `chat_messages`（DDL 见 design §2），注释风格与任务三表一致。
- [ ] 手工建表（不整跑 Part 1 deploy）：从 `agentcore-workshop-aurora` 栈输出取
      ARN，逐条 `aws rds-data execute-statement` 执行新增语句。

验证：
```bash
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database product_hunting --region us-west-2 \
  --sql "SELECT to_regclass('chat_sessions'), to_regclass('chat_messages')"
```

## 步骤 2：API（tasks-api Lambda + CFN 路由）

- [ ] `infra/tasks-api-lambda/handler.py`：`_dispatch` 挂 `/chat` 分支，新增
      5 个路由实现（design §4 表格）；用户名从
      `event.requestContext.authorizer.jwt.claims.preferred_username` 取，
      缺失回 403；`session_id` 校验 `^web-[0-9a-f-]{36}$`。
- [ ] `infra/part2-tasks.yaml`：新增 `GET/POST/PATCH/DELETE /chat/{proxy+}`
      4 条 Route（同款 JWT authorizer），CORS `AllowMethods` 补 `PATCH`。

⛔ 评审点 A：handler 路由与 SQL 走查（重点看用户过滤与软删条件是否每个查询都带）。

验证（部署后）：
```bash
TOKEN=$(part2_chat_portal/scripts/get_token.sh)   # 具体取 token 方式看脚本
curl -s -H "Authorization: Bearer $TOKEN" "$TASKS_API_URL/chat/sessions"   # 期望 {"sessions":[]}
curl -s -X POST -H "Authorization: Bearer $TOKEN" -d '{"session_id":"web-<uuid>","title":"测试"}' \
  "$TASKS_API_URL/chat/sessions"
```

## 步骤 3：Runtime（agent/main.py）

- [ ] 新增持久化助手：`_load_messages` / `_persist_new`（RDS Data API、
      20 秒冷恢复退避、>300KB 裁剪、异常吞掉打日志）。
- [ ] `invoke`：`_HISTORY` miss 时回灌；结束后 diff append（design §3）。
- [ ] 环境变量 `AURORA_CLUSTER_ARN` / `AURORA_SECRET_ARN` 缺省 → 行为完全同现状。

验证（本地，不依赖部署）：
```bash
cd part2_chat_portal && python -c "import ast; ast.parse(open('agent/main.py').read())"
# 本地冒烟：裸跑降级路径（无 Aurora 环境变量）
cd agent && python -c "import main"   # 无 import 错误即可
```

## 步骤 4：前端（web/src/）

- [ ] 抽 `web/src/lib/http.ts`：`ApiError` + `request`（从 tasks/api.ts 提取，
      tasks/api.ts 改为引用，行为不变）。
- [ ] 新增 `web/src/chat/api.ts`：类型 + listSessions / registerSession /
      getMessages / renameSession / deleteSession。
- [ ] `App.tsx`：sessionId 提升；侧栏「历史会话」区（列表 / 点击恢复 /
      重命名 / 删除）；样式复用现有 token，不加新美学（按约定先过
      frontend-design skill 确认延伸方向）。
- [ ] `ChatView.tsx`：接受受控 sessionId；恢复会话时载入 display_text 渲染；
      首次发送上抛注册事件。

验证：
```bash
cd part2_chat_portal/web && npm run build          # tsc -b && vite build
npm run dev  # Playwright（本机 python 版）过一遍：登录 -> 发消息 -> 刷新 -> 恢复 -> 追问
```

## 步骤 5：部署脚本

- [ ] `part2_chat_portal/scripts/deploy.sh`：
  - `agentcore launch` 追加 `--env AURORA_CLUSTER_ARN --env AURORA_SECRET_ARN`；
  - 第 7 步给 Runtime 角色 attach Part 1 栈输出的 `AgentDataAccessPolicy`
    （幂等：attach-role-policy 重复执行无害）。
- [ ] 全量重跑 `./scripts/deploy.sh` 验证幂等。

⛔ 回滚点：本步之前所有云端资源未变；出问题还原代码重跑 deploy 即回到现状。

## 步骤 6：端到端验收（对照 PRD 验收标准 1–7）

- [ ] 门户对话 → 刷新 → 列表可见、回看完整（含 chart）。
- [ ] 重部署 Runtime（顶掉 microVM）后对旧会话追问，上下文延续。
- [ ] 新对话 / 重命名 / 删除。
- [ ] `./scripts/chat_test.sh '看下最近30天的选品漏斗'` 及带 session id 二轮。

## 步骤 7：文档与收尾

- [ ] 同步三处文档：`实验手册.md`、两个 README（部署行为变化：Runtime 新增
      环境变量与权限、API 新增 /chat 路由、门户新增历史会话）。
- [ ] spec 更新（3.3）：`backend/ingestion-tasks-module.md` 或新文档记录
      chat 域契约；前端如有新套路补 frontend spec。
- [ ] 中文 commit（3.4）。
