"""Redshift Serverless 入库工具。

通过 redshift-data API（无需管理连接/驱动）把选品判定结果写入 Redshift Serverless：
- save_verdict     : 每个商品的判定记录（PASS / 排除 / 补采 / 采集）
- save_candidate   : 判定为"采集"的商品的完整候选档案

设计说明：
- 使用 boto3 redshift-data API，Agent 运行角色只需要 redshift-data:* 与
  redshift-serverless:GetCredentials 权限，无需网络直连数仓（适合托管 Runtime）。
- SQL 一律使用参数化（Parameters），避免拼接注入。
- 表结构见 infra/setup_tables.sql。
"""

import json
import os
import time
import uuid

import boto3
from strands import tool

# ---- 环境变量配置（部署时通过 agentcore configure --env 或控制台注入） ----
REGION = os.environ.get("AWS_REGION", "us-west-2")
# Redshift Serverless 工作组名称
WORKGROUP = os.environ.get("REDSHIFT_WORKGROUP", "agentcore-workshop")
# 目标数据库名
DATABASE = os.environ.get("REDSHIFT_DATABASE", "product_hunting")

_client = None


def _rs_client():
    """惰性创建 redshift-data 客户端（避免 import 阶段就要求凭证）。"""
    global _client
    if _client is None:
        _client = boto3.client("redshift-data", region_name=REGION)
    return _client


def _execute(sql: str, parameters: list | None = None) -> dict:
    """执行一条 SQL 并等待完成，返回执行状态。

    redshift-data 是异步 API：先 execute_statement 拿 Id，再轮询 describe_statement。
    """
    client = _rs_client()
    kwargs: dict = {
        "WorkgroupName": WORKGROUP,
        "Database": DATABASE,
        "Sql": sql,
    }
    if parameters:
        kwargs["Parameters"] = parameters

    resp = client.execute_statement(**kwargs)
    statement_id = resp["Id"]

    # 轮询直到语句结束。入库语句通常 1~2 秒完成，但 Redshift Serverless
    # 空闲自动暂停后，冷恢复需要 1~3 分钟，窗口须覆盖恢复时间，
    # 否则语句实际会成功、工具却误报 timeout
    for _ in range(180):
        desc = client.describe_statement(Id=statement_id)
        status = desc["Status"]
        if status == "FINISHED":
            return {"status": "ok", "statement_id": statement_id}
        if status in ("FAILED", "ABORTED"):
            return {
                "status": "error",
                "statement_id": statement_id,
                "error": desc.get("Error", "unknown"),
            }
        time.sleep(1)
    return {"status": "error", "statement_id": statement_id, "error": "timeout"}


def _p(name: str, value) -> dict:
    """构造 redshift-data 的参数对象（值统一转字符串）。"""
    return {"name": name, "value": "" if value is None else str(value)}


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
    """把一个商品的选品判定结果写入 Redshift（每个商品判定完必须调用）。

    Args:
        asin_or_url: 商品 ASIN 或详情页 URL
        product_title: 商品英文标题（listing 标题）
        verdict: 判定结果，只能是: PASS / 排除 / 补采 / 采集
        reason: 判定原因，如 "价≤£10"、"黑名单:3C"、"产品点0.12达标"
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
             current_price, monthly_sales, product_score, created_at)
        VALUES
            (:verdict_id, :site, :asin_or_url, :product_title, :verdict, :reason,
             NULLIF(:current_price, '')::DECIMAL(10,2),
             NULLIF(:monthly_sales, '')::INT,
             NULLIF(:product_score, '')::DECIMAL(10,4),
             GETDATE())
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
    """把判定为"采集"的商品完整候选档案写入 Redshift。

    Args:
        asin_or_url: 竞对链接（= 候选品自己的 Amazon 详情页）
        product_name_cn: 商品中文名（简短，如"桌面储物套装"）
        product_title_en: 商品英文名（listing 标题）
        search_keyword: 经哪个词/类目搜到的
        current_price: 当前价格（默认变体价）
        monthly_sales: 总月销量（多变体合计）
        competitor_min_price: 同赛道竞对最低价（无竞品兜底时=当前价）
        competitor_min_price_url: 最低价竞品链接（兜底时可留空）
        product_score: 预估产品点（沙箱计算结果，必须 > 0.05）
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
             ai_summary, created_at)
        VALUES
            (:candidate_id, :site, :asin_or_url, :product_name_cn, :product_title_en,
             :search_keyword, :current_price::DECIMAL(10,2), :monthly_sales::INT,
             :competitor_min_price::DECIMAL(10,2), :competitor_min_price_url,
             ROUND(:competitor_min_price::DECIMAL(10,2) * 0.85, 2),
             (:monthly_sales::INT / 2),
             :product_score::DECIMAL(10,4), :price_flag,
             :main_image_url, JSON_PARSE(NULLIF(:variants, '')), :certification,
             :material_spec, :ai_summary, GETDATE())
    """
    # variants 必须是合法 JSON 才能 JSON_PARSE，先做一次校验，坏数据降级为空
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
        ],
    )
    if result["status"] == "ok":
        return f"候选档案已入库: {product_name_cn} (产品点 {product_score})"
    return f"入库失败: {result['error']}"
