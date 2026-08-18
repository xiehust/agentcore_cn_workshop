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
     "deadline_s": 45}}   # 可选：冷恢复重连预算（秒），应小于调用方的
                          # invoke read_timeout，保住「超时 = 零写入」语义
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


def _connect(deadline_s: float):
    """建立连接。集群 MinCapacity=0 空闲自动暂停，首个连接触发冷恢复
    （约 15 秒~1 分钟），失败重试而不是直接报错。

    重连等待上限由**调用方限定**（args.deadline_s，封顶 240s < Lambda
    timeout 300s）：调用方的 invoke read_timeout（Runtime 30s / tasks-api
    55s / runner 70s）到期放弃时，本函数必已先放弃、语句从未执行——保住
    「调用方超时 = 零写入」的原子失败语义，避免幽灵写（调用方看到 500
    但几分钟后语句仍提交）。

    注意 timeout=10 是 socket 超时，pg8000 连上后不会重置：任何服务端
    静默超过 10 秒的语句（长锁等待、首行前的慢聚合）会以 InterfaceError
    失败并触发 _run 的重连整条重放（非幂等语句有双写风险）；下面的
    statement_timeout='60s' 只对持续回传字节的流式语句是有效约束。
    本通道的语句应保持索引级 OLTP 量级。"""
    user, password = _credentials()
    # Aurora PostgreSQL 强制 SSL；RDS CA 不在系统信任链里，demo 不做证书校验
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + min(240.0, max(5.0, deadline_s))
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


def _run(sql: str, params: dict, deadline_s: float):
    """执行一条 SQL（pg8000.native 自动提交），连接跨调用复用，失效重连一次。
    返回 (rows, columns)：无结果集的语句（INSERT/UPDATE/DELETE/DDL）rows 为 None。
    """
    global _CONN
    for attempt in (1, 2):
        try:
            if _CONN is None:
                _CONN = _connect(deadline_s)
                _CONN.run("SET statement_timeout = '60s'")
            rows = _CONN.run(sql, **params)
            return rows, _CONN.columns
        except pg8000.native.DatabaseError:
            raise  # SQL 本身的错误（语法/约束），重连重跑没有意义
        except Exception:
            _CONN = None
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


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
    # 冷恢复重连预算由调用方限定（应 < 其 invoke read_timeout），缺省 240s
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
