-- Part 1 数据摄入 pipeline 建表 DDL（Aurora PostgreSQL）
-- 数据库: product_hunting（由 CloudFormation 创建）
--
-- 执行方式：本文件随部署包打进写库 Lambda（infra/writer-lambda/），
-- deploy.sh 触发它的 setup_tables 操作，由 handler 去掉 -- 注释后按分号
-- 拆句逐条执行（连接与凭证都在 VPC 内的 Lambda 里，本机无需数据库客户端）：
--   aws lambda invoke --function-name agentcore-workshop-writer \
--     --cli-binary-format raw-in-base64-out \
--     --payload '{"op": "setup_tables"}' /tmp/out.json --region us-west-2
-- 认证走集群主用户 Secret，无需额外 GRANT。
--
-- 约束：语句必须幂等（IF NOT EXISTS 系），且不得出现 dollar-quoted 块
-- （$$ ... $$）——按分号拆句会把函数体切碎。

-- ============================================================
-- 表 1：选品判定记录表
-- 每个被处理的商品都会留一条判定记录（PASS / 排除 / 补采 / 采集），
-- 用于审计 Agent 的决策过程、统计漏斗转化。
-- ============================================================
CREATE TABLE IF NOT EXISTS selection_verdicts (
    verdict_id      VARCHAR(36)   NOT NULL,          -- UUID
    site            VARCHAR(8)    NOT NULL,          -- 站点：UK / DE ...
    asin_or_url     VARCHAR(1024) NOT NULL,          -- 商品 ASIN 或详情页 URL
    product_title   VARCHAR(2048),                   -- listing 英文标题
    verdict         VARCHAR(16)   NOT NULL,          -- PASS / 排除 / 补采 / 采集
    reason          VARCHAR(2048),                   -- 判定原因（如 "价≤£10"、"黑名单:3C"）
    current_price   DECIMAL(10,2),                   -- 当前价格（默认变体价）
    monthly_sales   INT,                             -- 总月销量（多变体合计）
    product_score   DECIMAL(10,4),                   -- 预估产品点（若算到）
    created_at      TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (verdict_id)
);

CREATE INDEX IF NOT EXISTS idx_verdicts_created_at
    ON selection_verdicts (created_at);

-- ============================================================
-- 表 2：候选档案表（verdict = 采集 的商品）
-- 字段对应 SOP §8 的候选档案清单，供后续"查重 → 寻源 → 利润测算"环节使用。
-- ============================================================
CREATE TABLE IF NOT EXISTS product_candidates (
    candidate_id             VARCHAR(36)   NOT NULL,  -- UUID
    site                     VARCHAR(8)    NOT NULL,  -- 站点：UK / DE ...
    asin_or_url              VARCHAR(1024) NOT NULL,  -- 竞对链接（候选品详情页）
    product_name_cn          VARCHAR(256)  NOT NULL,  -- 商品中文名（AI 命名）
    product_title_en         VARCHAR(2048),           -- 商品英文名（listing 标题）
    search_keyword           VARCHAR(512),            -- 搜索/细分关键词
    current_price            DECIMAL(10,2),           -- 当前价格（默认变体价）
    monthly_sales            INT,                     -- 总月销量（多变体合计）
    competitor_min_price     DECIMAL(10,2),           -- 同赛道竞对最低价
    competitor_min_price_url VARCHAR(1024),           -- 最低价竞品链接
    estimated_price          DECIMAL(10,2),           -- 预估售价 = 竞对最低价 × 0.85
    estimated_monthly_sales  INT,                     -- 预估月销 = 总月销 × 0.5
    product_score            DECIMAL(10,4) NOT NULL,  -- 预估产品点（达标才采集）
    price_flag               VARCHAR(128),            -- 定价标注（如 存疑·待人工补竞品）
    main_image_url           VARCHAR(2048),           -- 主图 URL
    variants                 JSONB,                   -- 子体明细（JSON：变体名/价/销）
    certification            VARCHAR(1024),           -- 认证需求（寻源可直接用）
    material_spec            VARCHAR(1024),           -- 材质/规格
    ai_summary               VARCHAR(4096),           -- AI 选品总结
    created_at               TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_created_at
    ON product_candidates (created_at);

-- ============================================================
-- 表 3：摄入任务表（Part 2 门户「任务管理」模块）
-- 一条记录 = 用户在门户创建的一个摄入任务（一次性或定期）。
-- 定期任务在 EventBridge Scheduler 里有同名 schedule，暂停/恢复同步两边。
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_tasks (
    task_id       VARCHAR(36)   NOT NULL,          -- UUID
    name          VARCHAR(256)  NOT NULL,          -- 任务名（门户列表展示）
    prompt        VARCHAR(4096) NOT NULL,          -- 传给 Runtime 的选品指令
    mode          VARCHAR(16)   NOT NULL,          -- one_time / recurring
    schedule_expr VARCHAR(128),                    -- recurring：rate(...) / cron(...)
    status        VARCHAR(16)   NOT NULL DEFAULT 'active',  -- active / paused / deleted
    created_at    TIMESTAMP     NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id)
);

-- ============================================================
-- 表 4：任务运行记录表
-- 一条记录 = 任务的一次执行（一次性任务通常一条，定期任务每次触发一条）。
-- browser_* 字段供门户生成 Browser Live View 的 presigned URL。
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id             VARCHAR(36)   NOT NULL,     -- UUID
    task_id            VARCHAR(36)   NOT NULL,     -- 所属任务
    status             VARCHAR(16)   NOT NULL DEFAULT 'queued',  -- queued/running/succeeded/failed
    runtime_session_id VARCHAR(64),                -- 调 Part 1 Runtime 用的 session id（≥33 字符）
    browser_id         VARCHAR(128),               -- 云端浏览器 ID（Live View 用）
    browser_session_id VARCHAR(128),               -- 云端浏览器会话 ID（Live View 用）
    stats              JSONB,                      -- {candidates, verdicts:{...}, duration_s}
    error              VARCHAR(2048),              -- 失败原因
    started_at         TIMESTAMP,
    finished_at        TIMESTAMP,
    created_at         TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_task
    ON ingestion_runs (task_id, created_at);

-- ============================================================
-- 表 5：运行事件表（进度阶段 / 工具调用 / 关键日志）
-- runner Lambda 消费 Runtime 的 SSE 流后逐条落库，seq 是 run 内自增序号，
-- 门户按 after_seq 游标增量轮询。
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_run_events (
    event_id   BIGSERIAL,
    run_id     VARCHAR(36)   NOT NULL,
    seq        INT           NOT NULL,             -- run 内自增，前端增量拉取游标
    kind       VARCHAR(24)   NOT NULL,             -- phase/tool/log/browser/error/result
    message    VARCHAR(4096),
    payload    JSONB,
    created_at TIMESTAMP     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_run
    ON ingestion_run_events (run_id, seq);

-- ============================================================
-- 结果关联：两张业务表加可空 run_id，标记记录由哪次运行写入。
-- 可空 = 旧数据与命令行调用（无 run_id）不受影响。
-- ============================================================
ALTER TABLE selection_verdicts ADD COLUMN IF NOT EXISTS run_id VARCHAR(36);

ALTER TABLE product_candidates ADD COLUMN IF NOT EXISTS run_id VARCHAR(36);

CREATE INDEX IF NOT EXISTS idx_verdicts_run
    ON selection_verdicts (run_id);

CREATE INDEX IF NOT EXISTS idx_candidates_run
    ON product_candidates (run_id);

-- ============================================================
-- 表 6：分析会话表（Part 2 门户「分析对话」历史）
-- 一条记录 = 门户里的一个对话会话，session_id 与 Runtime 的会话隔离 id 同值。
-- 由 tasks-api Lambda 在前端首次发送前幂等注册（user_name 取自 JWT claims）；
-- 命令行调用（chat_test.sh）不注册，消息照落库但不出现在门户列表里。
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id VARCHAR(64)  NOT NULL,          -- 前端生成 web-<uuid>（≥33 字符）
    user_name  VARCHAR(128) NOT NULL,          -- Keycloak preferred_username
    title      VARCHAR(256) NOT NULL,          -- 默认首条输入截断，可重命名
    status     VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active / deleted
    created_at TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user
    ON chat_sessions (user_name, updated_at);

-- ============================================================
-- 表 7：会话消息表（Strands message 全量 JSON）
-- Part 2 Runtime 每轮对话结束后把新增消息逐条落库，seq 是会话内序号；
-- microVM 回收后按 seq 回灌 content 即可恢复上下文（含 toolUse/toolResult）。
-- display_text 是给门户回看用的降级视图，纯工具轮为 NULL（读接口过滤掉）。
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id   BIGSERIAL,
    session_id   VARCHAR(64) NOT NULL,
    seq          INT         NOT NULL,         -- 会话内 0 起自增，回灌按 seq 排序
    role         VARCHAR(16) NOT NULL,         -- user / assistant
    content      JSONB       NOT NULL,         -- 完整 Strands message（原样，保真回灌）
    display_text TEXT,                         -- content 里 text block 拼接
    created_at   TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_session_seq
    ON chat_messages (session_id, seq);
