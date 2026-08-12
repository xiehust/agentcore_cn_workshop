# 实施计划：门户 Data Ingestion 任务管理模块

依赖顺序：数据层 → Part 1 agent → 后端 Lambda/基础设施 → 前端 → 文档。
每步末尾有验证命令；标 ★ 的是评审/验证闸口。

## Step 1 数据层（表结构）

- [x] `part1_data_ingestion/infra/setup_tables.sql` 追加：
      `ingestion_tasks` / `ingestion_runs` / `ingestion_run_events` 三表 +
      业务两表 `ADD COLUMN IF NOT EXISTS run_id` + 索引（DDL 见 design.md §2）。
- 验证：`cd part1_data_ingestion && bash scripts/deploy.sh`（只关心步骤 2 建表），
  然后 rds-data `SELECT column_name FROM information_schema.columns WHERE table_name='ingestion_runs'`。

## Step 2 Part 1 Agent 改造（向后兼容）

- [x] `agent/tools/aurora_tools.py`：contextvar `CURRENT_RUN_ID`；
      `save_verdict`/`save_candidate` INSERT 追加 run_id 列。
- [x] `agent/main.py`：payload 读 `run_id`（可空）；有 run_id 时 yield 结构化
      事件（tool_use 去重、browser_session 首现检测），无 run_id 行为不变。
- [x] `agent/tools/browser_ext.py`：`start()` 显式 `viewport 1600x900`。
- 验证：`python scripts/local_test.py "..."`（无 run_id 路径不回归）；
  `agentcore invoke '{"prompt":"...","run_id":"test-xxx"}'` 后查
  `selection_verdicts.run_id` 已写入、SSE 流里有 dict 事件。★

## Step 3 基础设施 + 两个 Lambda

- [x] 新建 `part2_chat_portal/infra/part2-tasks.yaml`（HTTP API + JWT
      authorizer + CORS、两个 Lambda 角色、ScheduleGroup + Scheduler 角色，
      见 design.md §5）。
- [x] `infra/tasks-api-lambda/handler.py`：路由分发 + RDS Data API CRUD +
      Scheduler 管理 + live-view presign（含 Aurora 冷恢复重试）。
- [x] `infra/runner-lambda/handler.py`：建 run → SigV4 invoke Part 1 →
      SSE 消费落事件 → 统计收尾（见 design.md §4.2）。
- [x] `scripts/deploy.sh` 追加步骤：部署栈、zip 上传两个函数、
      `web/.env.local` 追加 `VITE_TASKS_API_URL`；全程幂等。
- 验证：
  ```bash
  cd part2_chat_portal && ./scripts/deploy.sh && ./scripts/deploy.sh  # 重跑幂等
  TOKEN=$(scripts/get_token.sh)   # 无 token 打 API 应 401
  curl -s -X POST "$TASKS_API/tasks" -H "Authorization: Bearer $TOKEN" \
    -d '{"name":"smoke","prompt":"...","mode":"one_time"}'
  # 轮询 GET /runs/{id} 至 succeeded；GET /runs/{id}/events、/results 有数据
  ```
  ★（后端链路闭环后再动前端）

## Step 4 前端

- [x] `App.tsx` 拆出 `ChatView.tsx`，加顶层 view 切换 + 侧栏「任务管理」入口。
- [x] `tasks/api.ts`、`tasks/TasksView.tsx`（列表 + 新建抽屉：一次性/定期）、
      `tasks/RunDetail.tsx`（时间线 + 日志 + 统计卡 + 明细表分页）、
      `tasks/LiveView.tsx`（懒加载 BrowserLiveView + 300s 刷新重连）。
- [x] 构建配置：`.npmrc legacy-peer-deps`、`npm i bedrock-agentcore
      vite-plugin-static-copy`、`vite.config.ts` alias/dedupe/static-copy。
- [x] `styles.css` 追加任务模块样式（沿用既有 token，不新增色彩体系）。
- 验证：`npm run build`（tsc 类型检查过）；`npm run dev` + Python Playwright
  E2E：登录 analyst → 建 2 个并行任务 → 看进度/日志刷新 → 打开 Live View
  截图 → 跑完看统计与明细表 → 建每天 09:00 定期任务 → 暂停/恢复。★

## Step 5 文档同步

- [x] `实验手册.md`、`part1_data_ingestion/README.md`、
      `part2_chat_portal/README.md`：新模块部署/使用/FAQ；
      端点一律 `<api-id>` 占位符；密码只指向 `.deploy/secrets.env`。

## 回滚点

- Step 1-2 独立可回滚（表只增不改、agent 改动向后兼容）。
- Step 3 整栈删除即回滚（`agentcore-workshop-part2-tasks` + 两个函数）。
- Step 4 纯前端，git revert 即可。

## 全量验收（对照 PRD AC1~AC8）

1. 门户建一次性任务全程无 CLI（AC1）；两个任务并行互不串（AC2）。
2. 定期任务按时触发、暂停/恢复生效（AC3，可用 rate(5 minutes) 快速验证）。
3. 运行中：进度/日志自动刷新 + Live View 实况 + 过期刷新（AC4）。
4. 统计卡与明细表按 run_id 过滤正确（AC5）。
5. 无 token 401（AC6）；deploy.sh 重跑幂等（AC7）；三处文档同步（AC8）。
