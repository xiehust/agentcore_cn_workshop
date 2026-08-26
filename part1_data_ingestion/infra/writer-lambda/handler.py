"""Part 1 写库 Lambda —— Agent 与 deploy.sh 共用的数仓写入入口。

部署在本目录 aurora-serverless.yaml 创建的 Aurora VPC 内（私有子网 +
LambdaClientSecurityGroup），用 pg8000 直连集群 5432 端口。调用方：
- Part 1 的 Agent（公网 Runtime，不挂 VPC）：boto3 lambda.invoke，
  见 agent/tools/aurora_tools.py；
- deploy.sh 建表步骤：aws lambda invoke，op=setup_tables。

为什么不用 RDS Data API：中国区没有该服务。本函数与 Part 2 的
analytics Lambda 同构（Gateway -> Lambda(VPC) -> Aurora），共同验证
「计算留在公网、私网访问全部由 VPC 内 Lambda 承担」的可移植形态。
数据库凭证从 Secrets Manager 取（走 Part 1 栈里的 VPC 接口端点，
子网无 IGW/NAT）。

请求协议（event）：
    {"op": "save_verdict" | "save_candidate" | "setup_tables" | "check_tables",
     "args": {…工具入参原样…},
     "run_id": "uuid 或 null"}
响应：
    {"status": "ok", "detail": …} / {"status": "error", "error": "…"}
"""

import json
import os
import re
import ssl
import time
import uuid

import boto3
import pg8000.native

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "product_hunting")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]

SETUP_SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_tables.sql")

VERDICTS = ("PASS", "排除", "补采", "采集")

_CREDS = None
_CONN = None


def _credentials():
    global _CREDS
    if _CREDS is None:
        sm = boto3.client("secretsmanager")
        secret = json.loads(sm.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"])
        _CREDS = (secret["username"], secret["password"])
    return _CREDS


def _connect():
    """建立连接。集群 MinCapacity=0.5 保持 warm standby；维护或瞬时故障时
    失败重试而不是直接报错。等待上限 240 秒——这份连接恢复职责下沉到本函数
    （Lambda timeout 300s，Agent 侧 read_timeout 310s）。

    timeout=10 是 socket 超时，pg8000 连上后不会重置它，所以后续每条语句的
    读等待也被这 10 秒卡着（下面的 statement_timeout 只是服务端兜底）：
    本函数的语句都是单行 INSERT 与 IF NOT EXISTS 系 DDL，必须保持这个量级。"""
    user, password = _credentials()
    # Aurora PostgreSQL 强制 SSL；RDS CA 不在系统信任链里，demo 不做证书校验
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + 240
    while True:
        try:
            return pg8000.native.Connection(
                user=user, password=password, host=DB_HOST, port=5432,
                database=DB_NAME, ssl_context=ctx, timeout=10,
            )
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(5)


def _run(sql: str, **params):
    """执行一条 SQL（pg8000.native 自动提交），连接跨调用复用，失效则重连一次。

    参数用 :name 命名占位符，值传原生 Python 类型（str/int/float/None）；
    同一名字在 SQL 里出现多次也只占一个占位符（pg8000 会去重）。
    重连重跑不会写出重复行：两张业务表都以调用方一次调用内生成的 uuid 作主键，
    第一次已提交时重跑会撞主键而不是插第二条。
    """
    global _CONN
    for attempt in (1, 2):
        try:
            if _CONN is None:
                _CONN = _connect()
                _CONN.run("SET statement_timeout = '60s'")
            return _CONN.run(sql, **params)
        except Exception:
            _CONN = None
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


# ---- 入参归一化：模型给的值类型不稳，且列长度有限，统一在这里收口 ----

def _text(value, limit: int) -> str:
    return "" if value is None else str(value)[:limit]


def _opt_text(value, limit: int):
    """空串按 NULL 处理（run_id 等可空列）。"""
    s = _text(value, limit)
    return s or None


def _opt_float(value):
    if value is None or value == "":
        return None
    return float(value)


def _opt_int(value):
    if value is None or value == "":
        return None
    return int(float(value))


# ---- 操作实现 ----

def save_verdict(args: dict, run_id: str | None) -> dict:
    """写入一条选品判定记录。"""
    verdict = _text(args.get("verdict"), 16)
    if verdict not in VERDICTS:
        return {"status": "error", "error": f"verdict 非法: {verdict}"}

    _run(
        """
        INSERT INTO selection_verdicts
            (verdict_id, site, asin_or_url, product_title, verdict, reason,
             current_price, monthly_sales, product_score, run_id, created_at)
        VALUES
            (:verdict_id, :site, :asin_or_url, :product_title, :verdict, :reason,
             :current_price, :monthly_sales, :product_score, :run_id, NOW())
        """,
        verdict_id=str(uuid.uuid4()),
        site=_text(args.get("site") or "UK", 8),
        asin_or_url=_text(args.get("asin_or_url"), 1024),
        product_title=_text(args.get("product_title"), 2048),
        verdict=verdict,
        reason=_text(args.get("reason"), 2048),
        current_price=_opt_float(args.get("current_price")),
        monthly_sales=_opt_int(args.get("monthly_sales")),
        product_score=_opt_float(args.get("product_score")),
        run_id=_opt_text(run_id, 36),
    )
    return {"status": "ok", "detail": verdict}


def save_candidate(args: dict, run_id: str | None) -> dict:
    """写入一条候选档案。预估售价/预估月销是派生列，在 SQL 里算。"""
    # variants 必须是合法 JSON 才能转 JSONB，先校验，坏数据降级包装
    variants = _text(args.get("variants"), 8192)
    variants_json = None
    if variants:
        try:
            json.loads(variants)
            variants_json = variants
        except (ValueError, TypeError):
            variants_json = json.dumps({"raw": variants[:4000]}, ensure_ascii=False)

    _run(
        """
        INSERT INTO product_candidates
            (candidate_id, site, asin_or_url, product_name_cn, product_title_en,
             search_keyword, current_price, monthly_sales,
             competitor_min_price, competitor_min_price_url, estimated_price,
             estimated_monthly_sales, product_score, price_flag,
             main_image_url, variants, certification, material_spec,
             ai_summary, run_id, created_at)
        VALUES
            (:candidate_id, :site, :asin_or_url, :product_name_cn, :product_title_en,
             :search_keyword, :current_price, :monthly_sales,
             :competitor_min_price, :competitor_min_price_url,
             ROUND(CAST(:competitor_min_price AS DECIMAL(10,2)) * 0.85, 2),
             CAST(:monthly_sales AS INT) / 2,
             :product_score, :price_flag,
             :main_image_url, CAST(:variants AS JSONB), :certification,
             :material_spec, :ai_summary, :run_id, NOW())
        """,
        candidate_id=str(uuid.uuid4()),
        site=_text(args.get("site") or "UK", 8),
        asin_or_url=_text(args.get("asin_or_url"), 1024),
        product_name_cn=_text(args.get("product_name_cn"), 256),
        product_title_en=_text(args.get("product_title_en"), 2048),
        search_keyword=_text(args.get("search_keyword"), 512),
        current_price=_opt_float(args.get("current_price")),
        monthly_sales=_opt_int(args.get("monthly_sales")),
        competitor_min_price=_opt_float(args.get("competitor_min_price")),
        competitor_min_price_url=_text(args.get("competitor_min_price_url"), 1024),
        product_score=_opt_float(args.get("product_score")),
        price_flag=_text(args.get("price_flag"), 128),
        main_image_url=_text(args.get("main_image_url"), 2048),
        variants=variants_json,
        certification=_text(args.get("certification"), 1024),
        material_spec=_text(args.get("material_spec"), 1024),
        ai_summary=_text(args.get("ai_summary"), 4096),
        run_id=_opt_text(run_id, 36),
    )
    return {"status": "ok", "detail": args.get("product_name_cn")}


def check_tables(args: dict, _run_id: str | None) -> dict:
    """只读预检：确认给定表是否都已存在（Part 2 部署脚本用——中国区没有
    RDS Data API，预检不能再走 aws rds-data，改为 invoke 本函数）。

    入参 args: {"tables": ["chat_sessions", ...]}
    响应 detail: {"expected": N, "found": M, "missing": [...]}
    """
    tables = [str(t) for t in (args.get("tables") or []) if str(t).strip()]
    if not tables:
        return {"status": "error", "error": "缺少 tables 入参"}
    rows = _run(
        "SELECT tablename FROM pg_tables WHERE tablename = ANY(:names)",
        names=tables,
    )
    found = {r[0] for r in (rows or [])}
    missing = [t for t in tables if t not in found]
    return {
        "status": "ok",
        "detail": {"expected": len(tables), "found": len(found), "missing": missing},
    }


def setup_tables(_args: dict, _run_id: str | None) -> dict:
    """建表：执行随部署包一起打进来的 setup_tables.sql（不吃入参，签名对齐 OPS 分发）。

    去掉 -- 注释后按分号拆句逐条执行——语句全是 CREATE ... IF NOT EXISTS /
    ALTER ... ADD COLUMN IF NOT EXISTS 系幂等 DDL，重跑安全。
    约束：setup_tables.sql 里不得出现 dollar-quoted 块（$$ ... $$），
    否则分号拆分会把函数体切碎。
    """
    with open(SETUP_SQL_FILE, encoding="utf-8") as f:
        text = re.sub(r"--[^\n]*", "", f.read())
    statements = [s.strip() for s in text.split(";") if s.strip()]
    for stmt in statements:
        _run(stmt)
    return {"status": "ok", "detail": f"已执行 {len(statements)} 条 DDL"}


OPS = {
    "save_verdict": save_verdict,
    "save_candidate": save_candidate,
    "setup_tables": setup_tables,
    "check_tables": check_tables,
}


def lambda_handler(event, context):
    event = event or {}
    op = str(event.get("op") or "")
    fn = OPS.get(op)
    if fn is None:
        return {"status": "error", "error": f"未知操作: {op}"}
    try:
        return fn(event.get("args") or {}, event.get("run_id"))
    except Exception as e:  # 给调用方可读的错误信息而不是 Lambda 异常堆栈
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
