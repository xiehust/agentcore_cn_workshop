-- Part 1 数据摄入 pipeline 建表 DDL（Aurora PostgreSQL）
-- 数据库: product_hunting（由 CloudFormation 创建）
--
-- 执行方式（RDS Data API，无需客户端；rds-data 一次只能执行一条语句，
-- deploy.sh 会按分号拆分逐条执行）：
--   aws rds-data execute-statement \
--     --resource-arn <ClusterArn> --secret-arn <SecretArn> \
--     --database product_hunting --sql "<单条语句>" --region us-west-2
-- 认证走集群主用户 Secret，无需额外 GRANT。

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
