"""Aurora Serverless（PostgreSQL）入库工具。

把选品判定结果写入 Aurora：
- save_verdict     : 每个商品的判定记录（PASS / 排除 / 补采 / 采集）
- save_candidate   : 判定为"采集"的商品的完整候选档案

设计说明：
- 写路径是 boto3 lambda.invoke -> 写库 Lambda（挂 Aurora 同 VPC）->
  pg8000 直连集群，本模块只做「组 payload + 调 Lambda + 翻译结果」的薄壳，
  SQL、参数归一化、连接重试都在 infra/writer-lambda/handler.py 里。
  Runtime 仍不挂 VPC（计算留在公网），执行角色只需要对写库 Lambda 的
  lambda:InvokeFunction，不接触数据库凭证。
- 为什么不用 RDS Data API：中国区没有该服务，本 workshop 后续要整体移植
  过去，先用等效架构在海外区验证。同一思路已在 Part 2 用
  Gateway -> Lambda(VPC) -> Aurora 验证过（只读侧）。
- SQL 一律使用参数化，避免拼接注入（在写库 Lambda 侧）。
- 两张表都带可空的 run_id 列：Part 2 门户发起的任务会通过 set_current_run_id
  注入，命令行调用不注入（写 NULL），互不影响。
- 集群 MinCapacity=0.5 保持 warm standby；维护或瞬时故障时的等待与重连仍在
  写库 Lambda 内完成（上限 240 秒，Lambda timeout 300 秒），
  本模块只把 boto3 的 read_timeout 放宽到 310 秒并禁用重试，避免两侧都自旋。
- 表结构见 infra/setup_tables.sql。
"""

import contextvars
import json
import os

import boto3
from botocore.config import Config
from strands import tool

# ---- 环境变量配置（deploy.sh 从 CloudFormation 输出注入） ----
REGION = os.environ.get("AWS_REGION", "us-west-2")
WRITER_LAMBDA_NAME = os.environ.get("WRITER_LAMBDA_NAME", "agentcore-workshop-writer")

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


def _lambda_client():
    """惰性创建 lambda 客户端（避免 import 阶段就要求凭证）。

    read_timeout 略大于写库 Lambda 的 300 秒超时：连接等待由 Lambda 承担，
    这里等它返回就好；关掉 boto3 重试，避免超时后重复写入。
    """
    global _client
    if _client is None:
        _client = boto3.client(
            "lambda",
            region_name=REGION,
            config=Config(
                connect_timeout=10, read_timeout=310, retries={"max_attempts": 0}
            ),
        )
    return _client


def _invoke_writer(op: str, args: dict) -> dict:
    """同步调用写库 Lambda，返回它的 {"status": ...} 响应。

    run_id 从 contextvar 读出放进 payload 顶层（不进工具 schema、模型不感知）。
    """
    payload = {"op": op, "args": args, "run_id": CURRENT_RUN_ID.get()}
    try:
        resp = _lambda_client().invoke(
            FunctionName=WRITER_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    except Exception as e:
        return {"status": "error", "error": f"调用写库 Lambda 失败: {type(e).__name__}: {e}"}

    body = resp["Payload"].read().decode("utf-8")
    # 函数内部未捕获的异常（如超时）不会抛给调用方，靠 FunctionError 判别
    if resp.get("FunctionError"):
        return {"status": "error", "error": f"写库 Lambda 异常: {body[:512]}"}
    try:
        result = json.loads(body)
    except ValueError:
        return {"status": "error", "error": f"写库 Lambda 返回非 JSON: {body[:512]}"}
    if not isinstance(result, dict) or "status" not in result:
        return {"status": "error", "error": f"写库 Lambda 返回异常: {body[:512]}"}
    return result


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
    # 枚举在写库 Lambda 里也校验，这里先挡一道省一次 invoke
    if verdict not in ("PASS", "排除", "补采", "采集"):
        return f"verdict 非法: {verdict}，必须是 PASS/排除/补采/采集，未入库"

    result = _invoke_writer(
        "save_verdict",
        {
            "site": site,
            "asin_or_url": asin_or_url,
            "product_title": product_title,
            "verdict": verdict,
            "reason": reason,
            "current_price": current_price,
            "monthly_sales": monthly_sales,
            "product_score": product_score,
        },
    )
    if result["status"] == "ok":
        return f"判定已入库: {verdict} - {asin_or_url}"
    return f"入库失败: {result.get('error', '未知错误')}"


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
    # 入参归一化（列长度截断、variants 的 JSON 校验）在写库 Lambda 侧统一做
    result = _invoke_writer(
        "save_candidate",
        {
            "site": site,
            "asin_or_url": asin_or_url,
            "product_name_cn": product_name_cn,
            "product_title_en": product_title_en,
            "search_keyword": search_keyword,
            "current_price": current_price,
            "monthly_sales": monthly_sales,
            "competitor_min_price": competitor_min_price,
            "competitor_min_price_url": competitor_min_price_url,
            "product_score": product_score,
            "price_flag": price_flag,
            "main_image_url": main_image_url,
            "variants": variants,
            "certification": certification,
            "material_spec": material_spec,
            "ai_summary": ai_summary,
        },
    )
    if result["status"] == "ok":
        return f"候选档案已入库: {product_name_cn} (产品点 {product_score})"
    return f"入库失败: {result.get('error', '未知错误')}"
