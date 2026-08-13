# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""Part 2 记忆向量 Lambda —— mem0 长期记忆的 pgvector 数据面。

部署在 Part 1 aurora-serverless.yaml 创建的 Aurora VPC 内（私有子网 +
LambdaClientSecurityGroup），用 pg8000 直连集群 5432 端口。调用方是
Part 2 聊天 Agent（公网 Runtime，不挂 VPC）里的 mem0 自定义向量后端
（agent/memory_backend.py 的 AuroraLambdaVectorStore）：mem0 的
embedding / LLM 提取留在 Runtime（公网可达 OpenAI 兼容端点），仅向量
读写经 lambda.invoke 进到本函数——与 Part 1 写库 Lambda、Part 2 分析
Lambda 同构（计算留公网，私网访问全部由 VPC 内 Lambda 承担），中国区
可原样移植。

各 op 的 SQL 语义忠实复刻 mem0 2.0.x 自带的
mem0/vector_stores/pgvector.py（表结构 id UUID / vector / payload JSONB +
HNSW 余弦索引 + text_lemmatized GIN 索引；search 返回
score = max(0, 1 - cosine_distance)），保证 mem0 的去重/更新决策流程
不因换了传输层而变化。

请求协议（event）：
    {"op": "<操作名>", "args": {"collection": "表名", "dims": 1024, ...}}
响应：
    {"status": "ok", "result": ...} / {"status": "error", "error": "..."}

操作一览（除 setup 外与 mem0 VectorStoreBase 一一对应）：
    setup           deploy.sh 专用：建 pgvector 扩展 + 预建长期记忆两表
    create_col      建扩展 + 建表 + 建索引（幂等）
    insert          批量插入 [{id, vector, payload}]（非 upsert；ON CONFLICT
                    DO NOTHING 仅吸收 _run 重放，见 op_insert 注释）
    search          余弦相似检索（vector + filters + top_k）
    keyword_search  全文检索（失败返回 null，客户端转 mem0 纯语义降级）
    get / update / delete / list / list_cols / delete_col / col_info / reset
"""

import json
import os
import re
import ssl
import time

import boto3
import pg8000.native

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "product_hunting")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]

# 表名（collection）白名单：pg8000 无法参数化标识符，只能拼接，先严格校验
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# mem0 pgvector 的 filters 操作符 → SQL 模板（占位符名运行时生成）
# (模板, 值是否按 numeric 处理)
_OPERATOR_SQL_MAP = {
    "eq": ('payload->>{k} = {v}', False),
    "ne": ('payload->>{k} != {v}', False),
    "gt": ('(payload->>{k})::numeric > {v}', True),
    "gte": ('(payload->>{k})::numeric >= {v}', True),
    "lt": ('(payload->>{k})::numeric < {v}', True),
    "lte": ('(payload->>{k})::numeric <= {v}', True),
    "in": ('payload->>{k} = ANY({v})', False),
    "nin": ('NOT (payload->>{k} = ANY({v}))', False),
    "contains": ("payload->>{k} LIKE {v} ESCAPE '\\'", False),
    "icontains": ("payload->>{k} ILIKE {v} ESCAPE '\\'", False),
}

_CREDS = None
_CONN = None
# 每个 Lambda 容器内已确认存在的表，避免每次 RPC 都查 information_schema
_ENSURED: set[str] = set()


def _credentials():
    global _CREDS
    if _CREDS is None:
        sm = boto3.client("secretsmanager")
        secret = json.loads(sm.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"])
        _CREDS = (secret["username"], secret["password"])
    return _CREDS


def _connect():
    """建立连接。集群 MinCapacity=0 空闲自动暂停，首个连接触发冷恢复
    （约 15 秒~1 分钟），失败重试而不是直接报错，等待上限 240 秒
    （< Lambda timeout 300s，Agent 侧 read_timeout 120s 会先放弃——
    记忆是增强功能，Agent 侧超时只是本轮无记忆，不影响对话）。"""
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
    """执行一条 SQL（pg8000.native 自动提交），连接跨调用复用，失效则重连一次。"""
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


def _ident(name: str) -> str:
    """校验并引用一个 SQL 标识符（表名/索引名）。"""
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"非法 collection 名: {name!r}")
    return f'"{name}"'


def _vec_literal(vector) -> str:
    """向量 → pgvector 字面量字符串（经 CAST(:v AS vector) 传入）。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _payload_dict(value):
    """pg8000 对 JSONB 列可能返回 dict 或 str，统一成 dict。"""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _build_filter_conditions(filters):
    """把 mem0 的 filters dict 翻译成 (SQL 片段列表, 命名参数 dict)。

    语义复刻 mem0 pgvector.py 的 _build_filter_conditions；$or/$not 在
    本项目里不会出现（实际只收 {"user_id": ...} 等值），明确拒绝以免
    静默给出错误结果。
    """
    conditions: list[str] = []
    params: dict = {}
    if not filters:
        return conditions, params

    def _next(prefix, value):
        name = f"{prefix}{len(params)}"
        params[name] = value
        return f":{name}"

    for key, value in filters.items():
        if key in ("$or", "$not"):
            raise ValueError(f"不支持的过滤操作符: {key}")
        if value == "*":
            conditions.append(f"payload ? {_next('f', key)}")
        elif isinstance(value, dict):
            for op, op_value in value.items():
                if op not in _OPERATOR_SQL_MAP:
                    raise ValueError(f"不支持的过滤操作符: {op}")
                template, is_numeric = _OPERATOR_SQL_MAP[op]
                k_ph = _next("f", key)
                if op in ("in", "nin"):
                    v_ph = _next("f", [str(v) for v in op_value])
                elif op in ("contains", "icontains"):
                    escaped = (
                        str(op_value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    )
                    v_ph = _next("f", f"%{escaped}%")
                elif is_numeric:
                    v_ph = _next("f", float(op_value))
                else:
                    v_ph = _next("f", str(op_value))
                conditions.append(template.format(k=k_ph, v=v_ph))
        elif isinstance(value, list):
            k_ph = _next("f", key)
            v_ph = _next("f", [str(v) for v in value])
            conditions.append(f"payload->>{k_ph} = ANY({v_ph})")
        else:
            k_ph = _next("f", key)
            if isinstance(value, bool):
                v_ph = _next("f", json.dumps(value))
            else:
                v_ph = _next("f", str(value))
            conditions.append(f"payload->>{k_ph} = {v_ph}")

    return conditions, params


# ---- 操作实现（args 均含 collection 与 dims） ----

def _create_col(collection: str, dims: int):
    table = _ident(collection)
    _run("CREATE EXTENSION IF NOT EXISTS vector")
    _run(
        f"CREATE TABLE IF NOT EXISTS {table} "
        f"(id UUID PRIMARY KEY, vector vector({int(dims)}), payload JSONB)"
    )
    _run(
        f"CREATE INDEX IF NOT EXISTS {_ident(collection + '_hnsw_idx')} "
        f"ON {table} USING hnsw (vector vector_cosine_ops)"
    )
    _run(
        f"CREATE INDEX IF NOT EXISTS {_ident(collection + '_text_lemmatized_idx')} "
        f"ON {table} USING gin(to_tsvector('simple', payload->>'text_lemmatized'))"
    )


def _ensure_collection(collection: str, dims: int):
    if collection in _ENSURED:
        return
    _create_col(collection, dims)
    _ENSURED.add(collection)


def op_setup(args: dict):
    """deploy.sh 专用：建扩展并预建长期记忆主表与 entities 表
    （mem0 的 entity_store 懒加载会用 <collection>_entities）。"""
    collection = args["collection"]
    dims = int(args["dims"])
    _create_col(collection, dims)
    _create_col(f"{collection}_entities", dims)
    return f"已就绪: {collection}, {collection}_entities (vector({dims}))"


def op_create_col(args: dict):
    _create_col(args["collection"], int(args["dims"]))
    _ENSURED.add(args["collection"])
    return None


def op_insert(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    table = _ident(collection)
    for item in args["items"]:
        # ON CONFLICT DO NOTHING 不是 upsert：mem0 每次 insert 的 id 都是新生成
        # 的 uuid4，同 id 重复只可能来自 _run 的"已提交但响应丢失"重放——
        # pg8000 逐语句 autocommit，没有它重放会撞主键把成功写入误报成失败
        _run(
            f"INSERT INTO {table} (id, vector, payload) "
            f"VALUES (:id, CAST(:vec AS vector), CAST(:payload AS JSONB)) "
            f"ON CONFLICT (id) DO NOTHING",
            id=str(item["id"]),
            vec=_vec_literal(item["vector"]),
            payload=json.dumps(item.get("payload") or {}, ensure_ascii=False),
        )
    return None


def op_search(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    conditions, params = _build_filter_conditions(args.get("filters"))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = _run(
        f"SELECT id, vector <=> CAST(:qvec AS vector) AS distance, payload "
        f"FROM {_ident(collection)} {where} ORDER BY distance LIMIT :top_k",
        qvec=_vec_literal(args["vector"]),
        top_k=int(args.get("top_k") or 5),
        **params,
    )
    return [
        {
            "id": str(r[0]),
            "score": max(0.0, 1.0 - float(r[1])),
            "payload": _payload_dict(r[2]),
        }
        for r in rows
    ]


def op_keyword_search(args: dict):
    """全文检索（'simple' 配置，中文无分词，效果有限——mem0 侧把 None
    当作降级信号回退纯语义检索，这里失败直接返回 None 同义。"""
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    conditions, params = _build_filter_conditions(args.get("filters"))
    extra = ("AND " + " AND ".join(conditions)) if conditions else ""
    try:
        rows = _run(
            f"SELECT id, ts_rank_cd(to_tsvector('simple', payload->>'text_lemmatized'), "
            f"plainto_tsquery('simple', :q)) AS score, payload "
            f"FROM {_ident(collection)} "
            f"WHERE to_tsvector('simple', payload->>'text_lemmatized') @@ plainto_tsquery('simple', :q) "
            f"{extra} ORDER BY score DESC LIMIT :top_k",
            q=str(args["query"]),
            top_k=int(args.get("top_k") or 5),
            **params,
        )
    except Exception:
        return None
    return [
        {"id": str(r[0]), "score": float(r[1]), "payload": _payload_dict(r[2])}
        for r in rows
    ]


def op_get(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    rows = _run(
        f"SELECT id, payload FROM {_ident(collection)} WHERE id = :id",
        id=str(args["id"]),
    )
    if not rows:
        return None
    return {"id": str(rows[0][0]), "score": None, "payload": _payload_dict(rows[0][1])}


def op_update(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    table = _ident(collection)
    if args.get("vector") is not None:
        _run(
            f"UPDATE {table} SET vector = CAST(:vec AS vector) WHERE id = :id",
            vec=_vec_literal(args["vector"]),
            id=str(args["id"]),
        )
    if args.get("payload") is not None:
        _run(
            f"UPDATE {table} SET payload = CAST(:payload AS JSONB) WHERE id = :id",
            payload=json.dumps(args["payload"], ensure_ascii=False),
            id=str(args["id"]),
        )
    return None


def op_delete(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    _run(
        f"DELETE FROM {_ident(collection)} WHERE id = :id",
        id=str(args["id"]),
    )
    return None


def op_list(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    conditions, params = _build_filter_conditions(args.get("filters"))
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = _run(
        f"SELECT id, payload FROM {_ident(collection)} {where} LIMIT :top_k",
        top_k=int(args.get("top_k") or 100),
        **params,
    )
    return [
        {"id": str(r[0]), "score": None, "payload": _payload_dict(r[1])}
        for r in rows
    ]


def op_list_cols(args: dict):
    rows = _run(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return [r[0] for r in rows]


def op_delete_col(args: dict):
    collection = args["collection"]
    _run(f"DROP TABLE IF EXISTS {_ident(collection)}")
    _ENSURED.discard(collection)
    return None


def op_col_info(args: dict):
    collection = args["collection"]
    _ensure_collection(collection, int(args["dims"]))
    table = _ident(collection)
    rows = _run(
        f"SELECT (SELECT COUNT(*) FROM {table}), "
        f"pg_size_pretty(pg_total_relation_size(CAST(:name AS regclass)))",
        name=collection,
    )
    return {"name": collection, "count": rows[0][0], "size": rows[0][1]}


def op_reset(args: dict):
    op_delete_col(args)
    _create_col(args["collection"], int(args["dims"]))
    return None


OPS = {
    "setup": op_setup,
    "create_col": op_create_col,
    "insert": op_insert,
    "search": op_search,
    "keyword_search": op_keyword_search,
    "get": op_get,
    "update": op_update,
    "delete": op_delete,
    "list": op_list,
    "list_cols": op_list_cols,
    "delete_col": op_delete_col,
    "col_info": op_col_info,
    "reset": op_reset,
}


def lambda_handler(event, context):
    event = event or {}
    op = str(event.get("op") or "")
    fn = OPS.get(op)
    if fn is None:
        return {"status": "error", "error": f"未知操作: {op}"}
    try:
        return {"status": "ok", "result": fn(event.get("args") or {})}
    except Exception as e:  # 给调用方可读的错误信息而不是 Lambda 异常堆栈
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
