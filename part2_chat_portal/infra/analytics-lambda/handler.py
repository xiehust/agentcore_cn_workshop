"""Part 2 数据查询 Lambda —— AgentCore Gateway 的 Lambda target。

部署在 Part 1 创建的 Aurora VPC 内（私有子网 + LambdaClientSecurityGroup），
用 pg8000 直连集群 5432 端口，验证「Gateway -> Lambda(VPC) -> Aurora(同 VPC)」
方案：Gateway 侧零 VPC egress 配置，私网访问全部由本函数承担。
数据库凭证从 Secrets Manager 取（走 VPC 接口端点，子网无 IGW/NAT）。

Gateway 调用约定（见 AWS 文档 gateway-add-target-lambda）：
- event = 工具入参（inputSchema 里 properties 的键值）
- context.client_context.custom["bedrockAgentCoreToolName"] = "目标名___工具名"
"""

import json
import os
import ssl
import time

import boto3
import pg8000.native

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "product_hunting")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]

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
    失败后重试而不是直接报错。"""
    user, password = _credentials()
    # Aurora PostgreSQL 强制 SSL；RDS CA 不在系统信任链里，demo 不做证书校验
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + 50
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


def _run(sql: str):
    """执行只读 SQL，返回 (columns, rows)。连接跨调用复用，失效则重连一次。"""
    global _CONN
    for attempt in (1, 2):
        try:
            if _CONN is None:
                _CONN = _connect()
                _CONN.run("SET default_transaction_read_only = on")
                _CONN.run("SET statement_timeout = '15s'")
            rows = _CONN.run(sql)
            columns = [c["name"] for c in (_CONN.columns or [])]
            return columns, rows
        except Exception:
            _CONN = None
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


# ---- 工具实现 ----

def get_selection_funnel(days: int = 30) -> dict:
    """选品漏斗：判定分布（PASS/排除/补采/采集）+ 候选档案入库数。"""
    days = max(1, min(int(days or 30), 365))
    _, verdict_rows = _run(
        "SELECT verdict, COUNT(*) FROM selection_verdicts "
        f"WHERE created_at > NOW() - INTERVAL '{days} days' GROUP BY verdict"
    )
    _, cand_rows = _run(
        "SELECT COUNT(*) FROM product_candidates "
        f"WHERE created_at > NOW() - INTERVAL '{days} days'"
    )
    by_verdict = {r[0]: int(r[1]) for r in verdict_rows}
    total = sum(by_verdict.values())
    return {
        "days": days,
        "processed_total": total,
        "by_verdict": by_verdict,
        "candidates_saved": int(cand_rows[0][0]),
        "note": "漏斗口径：处理总数 = 全部判定记录；采集 = 达标进入候选档案的商品",
    }


def get_category_distribution(days: int = 90) -> dict:
    """类目/赛道分布。数仓没有独立类目字段，用候选档案的搜索/细分关键词
    近似赛道，另附各站点判定分布，结果里已注明口径。"""
    days = max(1, min(int(days or 90), 365))
    _, keyword_rows = _run(
        "SELECT COALESCE(NULLIF(search_keyword, ''), '(未记录)') AS segment, "
        "COUNT(*), ROUND(AVG(current_price), 2), ROUND(AVG(product_score), 4) "
        "FROM product_candidates "
        f"WHERE created_at > NOW() - INTERVAL '{days} days' "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
    )
    _, site_rows = _run(
        "SELECT site, verdict, COUNT(*) FROM selection_verdicts "
        f"WHERE created_at > NOW() - INTERVAL '{days} days' "
        "GROUP BY site, verdict ORDER BY site"
    )
    return {
        "days": days,
        "note": "口径：数仓无类目字段，赛道 = 候选档案的搜索/细分关键词",
        "candidate_segments": [
            {"segment": r[0], "count": int(r[1]),
             "avg_price": float(r[2]) if r[2] is not None else None,
             "avg_product_score": float(r[3]) if r[3] is not None else None}
            for r in keyword_rows
        ],
        "verdicts_by_site": [
            {"site": r[0], "verdict": r[1], "count": int(r[2])} for r in site_rows
        ],
    }


def run_sql_query(sql: str, max_rows: int = 100) -> dict:
    """开放式即席分析：执行只读 SELECT。多重防护：语句白名单 + 会话级
    read only + 15s 超时 + 行数上限。"""
    if not sql or not isinstance(sql, str):
        return {"error": "缺少 sql 参数"}
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        return {"error": "只允许单条语句"}
    if not stripped.lower().startswith(("select", "with")):
        return {"error": "只允许 SELECT / WITH 开头的只读查询"}
    max_rows = max(1, min(int(max_rows or 100), 500))
    columns, rows = _run(stripped)
    truncated = len(rows) > max_rows
    return {
        "columns": columns,
        "rows": [[_jsonable(v) for v in row] for row in rows[:max_rows]],
        "row_count": min(len(rows), max_rows),
        "truncated": truncated,
    }


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


TOOLS = {
    "get_selection_funnel": get_selection_funnel,
    "get_category_distribution": get_category_distribution,
    "run_sql_query": run_sql_query,
}


def lambda_handler(event, context):
    # Gateway 工具名带 "目标名___" 前缀，需自行剥掉
    tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
    delimiter = "___"
    if delimiter in tool_name:
        tool_name = tool_name[tool_name.index(delimiter) + len(delimiter):]

    fn = TOOLS.get(tool_name)
    if fn is None:
        return {"error": f"未知工具: {tool_name}"}
    try:
        return fn(**(event or {}))
    except Exception as e:  # 给 Agent 可读的错误信息而不是 Lambda 异常堆栈
        return {"error": f"{type(e).__name__}: {e}"}
