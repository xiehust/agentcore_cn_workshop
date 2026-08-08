"""Aurora Serverless（PostgreSQL）入库工具。

通过 RDS Data API（无需数据库连接/驱动/VPC）把选品判定结果写入 Aurora：
- save_verdict     : 每个商品的判定记录（PASS / 排除 / 补采 / 采集）
- save_candidate   : 判定为"采集"的商品的完整候选档案

设计说明：
- 使用 boto3 rds-data API（同步返回），Agent 运行角色只需要
  rds-data:ExecuteStatement 等 + 集群主用户 Secret 的读取权限，
  无需网络直连数据库（适合托管 Runtime，Runtime 不用挂 VPC）。
  Part 2 的数据查询 Lambda 则计划挂进 Aurora 所在 VPC 直连集群，
  验证 Gateway -> Lambda(VPC) -> Aurora 方案。
- SQL 一律使用参数化（parameters），避免拼接注入。
- 两张表都带可空的 run_id 列：Part 2 门户发起的任务会通过 set_current_run_id
  注入，命令行调用不注入（写 NULL），互不影响。
- 集群 MinCapacity=0 会空闲自动暂停，首次调用触发冷恢复
  （约 15 秒~1 分钟），_execute 内置重试等待。
- 表结构见 infra/setup_tables.sql。
"""

import contextvars
import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError
from strands import tool

# ---- 环境变量配置（deploy.sh 从 CloudFormation 输出注入） ----
REGION = os.environ.get("AWS_REGION", "us-west-2")
CLUSTER_ARN = os.environ.get("AURORA_CLUSTER_ARN", "")
SECRET_ARN = os.environ.get("AURORA_SECRET_ARN", "")
DATABASE = os.environ.get("AURORA_DATABASE", "product_hunting")

_client = None

# 当前运行 ID：Part 2 门户的任务管理模块调用时由 main.py 注入（payload.run_id），
# 入库时写进两张业务表的 run_id 列，供门户按 run 过滤本次运行的结果。
# 命令行调用（agentcore invoke / local_test）不传，保持 None → 列写 NULL。
# 用 contextvar 而非全局变量：同步工具经 asyncio.to_thread 执行会复制上下文，
# 值能正确传递，且不会被同进程的其他调用串改。
CURRENT_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_run_id", default=None
)


def set_current_run_id(run_id: str | None) -> None:
    """设置当前运行 ID（空字符串按未设置处理）。"""
    CURRENT_RUN_ID.set(run_id or None)


def _rds_client():
    """惰性创建 rds-data 客户端（避免 import 阶段就要求凭证）。"""
    global _client
    if _client is None:
        _client = boto3.client("rds-data", region_name=REGION)
    return _client


def _execute(sql: str, parameters: list | None = None) -> dict:
    """执行一条 SQL，返回执行状态。

    rds-data 是同步 API；集群自动暂停后首次调用会抛 DatabaseResumingException
    （或提示 resuming 的 BadRequestException），等待重试即可。
    """
    if not CLUSTER_ARN or not SECRET_ARN:
        return {
            "status": "error",
            "error": "缺少 AURORA_CLUSTER_ARN / AURORA_SECRET_ARN 环境变量",
        }

    kwargs: dict = {
        "resourceArn": CLUSTER_ARN,
        "secretArn": SECRET_ARN,
        "database": DATABASE,
        "sql": sql,
    }
    if parameters:
        kwargs["parameters"] = parameters

    last_err = "unknown"
    for _ in range(36):  # 最长约 3 分钟，覆盖冷恢复
        try:
            _rds_client().execute_statement(**kwargs)
            return {"status": "ok"}
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = str(e)
            # 集群从暂停中恢复：等待后重试；其余错误直接返回
            if code == "DatabaseResumingException" or "resum" in msg.lower():
                last_err = msg
                time.sleep(5)
                continue
            return {"status": "error", "error": msg}
    return {"status": "error", "error": f"集群恢复等待超时: {last_err}"}


def _p(name: str, value) -> dict:
    """构造 rds-data 的参数对象（值统一转字符串，SQL 里按需 cast）。"""
    return {
        "name": name,
        "value": {"stringValue": "" if value is None else str(value)},
    }


@tool
def save_verdict(
    asin_or_url: str,
    product_title: str,
    verdict: str,
    reason: str,
    site: str = "UK",
    current_price: float | None = None,
    monthly_sales: int | None = None,
    product_score: float | None = None,
) -> str:
    """把一个商品的选品判定结果写入数仓（每个商品判定完必须调用）。

    Args:
        asin_or_url: 商品 ASIN 或详情页 URL
        product_title: 商品英文标题（listing 标题）
        verdict: 判定结果，只能是: PASS / 排除 / 补采 / 采集
        reason: 判定原因，如 "价≤£5"、"黑名单:3C"、"产品点达标"
        site: 站点，默认 UK
        current_price: 当前价格（默认变体价，单位为站点货币）
        monthly_sales: 总月销量（多变体合计）
        product_score: 预估产品点（若算到了）

    Returns:
        入库结果说明
    """
    if verdict not in ("PASS", "排除", "补采", "采集"):
        return f"verdict 非法: {verdict}，必须是 PASS/排除/补采/采集，未入库"

    sql = """
        INSERT INTO selection_verdicts
            (verdict_id, site, asin_or_url, product_title, verdict, reason,
             current_price, monthly_sales, product_score, run_id, created_at)
        VALUES
            (:verdict_id, :site, :asin_or_url, :product_title, :verdict, :reason,
             NULLIF(:current_price, '')::DECIMAL(10,2),
             NULLIF(:monthly_sales, '')::INT,
             NULLIF(:product_score, '')::DECIMAL(10,4),
             NULLIF(:run_id, '')::VARCHAR(36),
             NOW())
    """
    result = _execute(
        sql,
        [
            _p("verdict_id", str(uuid.uuid4())),
            _p("site", site),
            _p("asin_or_url", asin_or_url[:1024]),
            _p("product_title", product_title[:2048]),
            _p("verdict", verdict),
            _p("reason", reason[:2048]),
            _p("current_price", current_price),
            _p("monthly_sales", monthly_sales),
            _p("product_score", product_score),
            _p("run_id", CURRENT_RUN_ID.get()),
        ],
    )
    if result["status"] == "ok":
        return f"判定已入库: {verdict} - {asin_or_url}"
    return f"入库失败: {result['error']}"


@tool
def save_candidate(
    asin_or_url: str,
    product_name_cn: str,
    product_title_en: str,
    search_keyword: str,
    current_price: float,
    monthly_sales: int,
    competitor_min_price: float,
    competitor_min_price_url: str,
    product_score: float,
    main_image_url: str = "",
    variants: str = "",
    certification: str = "",
    material_spec: str = "",
    ai_summary: str = "",
    site: str = "UK",
    price_flag: str = "",
) -> str:
    """把判定为"采集"的商品完整候选档案写入数仓。

    Args:
        asin_or_url: 竞对链接（= 候选品自己的详情页）
        product_name_cn: 商品中文名（简短，如"桌面储物套装"）
        product_title_en: 商品英文名（listing 标题）
        search_keyword: 经哪个词/类目搜到的
        current_price: 当前价格（默认变体价）
        monthly_sales: 总月销量（多变体合计）
        competitor_min_price: 同赛道竞对最低价（无竞品兜底时=当前价）
        competitor_min_price_url: 最低价竞品链接（兜底时可留空）
        product_score: 预估产品点（沙箱计算结果，须达标）
        main_image_url: 主图 URL
        variants: 子体明细 JSON 字符串，如 [{"name":"红色","price":12.9,"sales":"展示用"}]
        certification: 认证需求（一句寻源可直接用的话）
        material_spec: 材质/规格
        ai_summary: AI 选品总结（为什么选它）
        site: 站点，默认 UK
        price_flag: 定价标注，如 "存疑·待人工补竞品"，正常留空

    Returns:
        入库结果说明
    """
    sql = """
        INSERT INTO product_candidates
            (candidate_id, site, asin_or_url, product_name_cn, product_title_en,
             search_keyword, current_price, monthly_sales,
             competitor_min_price, competitor_min_price_url, estimated_price,
             estimated_monthly_sales, product_score, price_flag,
             main_image_url, variants, certification, material_spec,
             ai_summary, run_id, created_at)
        VALUES
            (:candidate_id, :site, :asin_or_url, :product_name_cn, :product_title_en,
             :search_keyword, :current_price::DECIMAL(10,2), :monthly_sales::INT,
             :competitor_min_price::DECIMAL(10,2), :competitor_min_price_url,
             ROUND(:competitor_min_price::DECIMAL(10,2) * 0.85, 2),
             (:monthly_sales::INT / 2),
             :product_score::DECIMAL(10,4), :price_flag,
             :main_image_url, NULLIF(:variants, '')::JSONB, :certification,
             :material_spec, :ai_summary,
             NULLIF(:run_id, '')::VARCHAR(36), NOW())
    """
    # variants 必须是合法 JSON 才能转 JSONB，先做一次校验，坏数据降级包装
    variants_json = ""
    if variants:
        try:
            json.loads(variants)
            variants_json = variants
        except (ValueError, TypeError):
            variants_json = json.dumps({"raw": variants[:4000]}, ensure_ascii=False)

    result = _execute(
        sql,
        [
            _p("candidate_id", str(uuid.uuid4())),
            _p("site", site),
            _p("asin_or_url", asin_or_url[:1024]),
            _p("product_name_cn", product_name_cn[:256]),
            _p("product_title_en", product_title_en[:2048]),
            _p("search_keyword", search_keyword[:512]),
            _p("current_price", current_price),
            _p("monthly_sales", monthly_sales),
            _p("competitor_min_price", competitor_min_price),
            _p("competitor_min_price_url", competitor_min_price_url[:1024]),
            _p("product_score", product_score),
            _p("price_flag", price_flag[:128]),
            _p("main_image_url", main_image_url[:2048]),
            _p("variants", variants_json),
            _p("certification", certification[:1024]),
            _p("material_spec", material_spec[:1024]),
            _p("ai_summary", ai_summary[:4096]),
            _p("run_id", CURRENT_RUN_ID.get()),
        ],
    )
    if result["status"] == "ok":
        return f"候选档案已入库: {product_name_cn} (产品点 {product_score})"
    return f"入库失败: {result['error']}"
