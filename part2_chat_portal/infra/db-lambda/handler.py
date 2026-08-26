# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""Part 2 通用数据面 Lambda —— RDS Data API 的中国区可移植替代。

背景：中国区没有 RDS Data API（rds-data 端点在 aws-cn 分区不存在，已实证），
Part 2 原先三个公网调用方（聊天 Runtime 的会话历史、tasks-api 的任务/会话
CRUD、runner 的运行状态机）都靠它读写 Aurora。本函数按 Part 1 写库 Lambda
的同款形态（挂 Aurora 同 VPC + LambdaClientSecurityGroup，pg8000 直连 5432，
凭证走 Secrets Manager VPC 端点）提供一条通用 SQL 通道，调用方只把
rds-data:ExecuteStatement 换成 lambda:InvokeFunction，SQL 与 :name 占位符
原样保留（pg8000 原生支持 :name 风格，且正确处理 :param::TYPE 转换与
to_char 格式串里的引号内冒号——已实测）。

与 Data API formatRecordsAs=JSON 的等价性约定：
- 返回 rows 为 [{列名/别名: 值}]，空结果 []；
- 数字/布尔/NULL 保持 JSON 原生类型；timestamp/date/Decimal/UUID 等
  JSON 不可表示的类型按 str 序列化（调用方 SQL 普遍已用 to_char/::text
  收口成字符串，此为兜底）；
- 权限语义等价：op=execute 是全权 SQL 通道，与原先调用方持有的
  rds-data 全集群权限一致，不算权限扩大（要收敛可后续拆语义化 op）。

请求协议（event）：
    {"op": "execute", "args": {"sql": "...", "params": {"name": "value", ...},
     "deadline_s": 45}}   # 可选：执行应用 SQL 前的健康检查/连接恢复预算（秒）
响应：
    {"status": "ok", "rows": [...]} / {"status": "error", "error": "..."}
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


def _deadline(deadline_s: float) -> float:
    """把相对预算换成一次调用内唯一的 monotonic 绝对截止点。"""
    return time.monotonic() + min(240.0, max(5.0, deadline_s))


def _connect(deadline: float):
    """在绝对截止点前建立连接；恢复重试不会重置整次调用的预算。"""
    user, password = _credentials()
    # Aurora PostgreSQL 强制 SSL；RDS CA 不在系统信任链里，demo 不做证书校验
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("数据库连接预算已耗尽")
        try:
            return pg8000.native.Connection(
                user=user, password=password, host=DB_HOST, port=5432,
                database=DB_NAME, ssl_context=ctx, timeout=10,
            )
        except Exception:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(5.0, remaining))


def _prepare_connection(deadline: float):
    """只在应用 SQL 开始前探活/重连；此阶段失败可安全重试。"""
    global _CONN
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("数据库预检预算已耗尽，未执行应用 SQL")
        try:
            if _CONN is None:
                _CONN = _connect(deadline)
            _CONN.run("SELECT 1")
            return _CONN
        except Exception:
            _CONN = None


def _run(sql: str, params: dict, deadline_s: float):
    """探活后只执行应用 SQL 一次，并返回 (rows, columns)。

    pg8000.native 默认自动提交。若应用 SQL 执行时连接/读取失败，服务端可能尚未执行，
    也可能已经提交但响应丢失；客户端无法区分这个 ambiguous-commit 边界。因此这里
    绝不自动重放应用 SQL，避免把一次非幂等写变成两次。deadline 只保证预算耗尽时
    不再开始应用 SQL，不能把调用方超时等同于“零写入”。
    """
    global _CONN
    deadline = _deadline(deadline_s)
    conn = _prepare_connection(deadline)
    if time.monotonic() >= deadline:
        raise TimeoutError("数据库预检预算已耗尽，未执行应用 SQL")
    try:
        rows = conn.run(sql, **params)
        return rows, conn.columns
    except Exception:
        _CONN = None
        raise


def _jsonable(value):
    """JSON 原生类型直通，其余（datetime/Decimal/UUID/bytes...）转字符串——
    对齐 Data API formatRecordsAs=JSON 的输出面（调用方 SQL 已普遍用
    to_char/::text 收口，此为兜底）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (dict, list)):
        return value  # jsonb 未加 ::text 时 pg8000 已解析成对象，保持
    return str(value)


def op_execute(args: dict):
    sql = args.get("sql") or ""
    if not sql.strip():
        raise ValueError("缺少 sql")
    params = args.get("params") or {}
    # 应用 SQL 开始前的探活/连接恢复预算由调用方限定，缺省 240s
    try:
        deadline_s = float(args.get("deadline_s") or 240)
    except (TypeError, ValueError):
        deadline_s = 240.0
    rows, columns = _run(sql, params, deadline_s)
    if rows is None:
        return []
    names = [c["name"] for c in (columns or [])]
    return [
        {name: _jsonable(value) for name, value in zip(names, row)}
        for row in rows
    ]


OPS = {"execute": op_execute}


def lambda_handler(event, context):
    event = event or {}
    op = str(event.get("op") or "")
    fn = OPS.get(op)
    if fn is None:
        return {"status": "error", "error": f"未知操作: {op}"}
    try:
        return {"status": "ok", "rows": fn(event.get("args") or {})}
    except Exception as e:  # 给调用方可读的错误信息而不是 Lambda 异常堆栈
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
