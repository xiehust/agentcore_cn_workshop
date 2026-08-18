"""Part 2 门户后端 API Lambda —— 「数据摄入任务管理」+「分析会话历史」。

链路（见 .trellis/tasks/08-08-ingestion-task-manager/design.md §1）：

    SPA（Keycloak 用户 token）
      -> HTTP API（JWT authorizer 校验 issuer + aud=analysis-runtime）
      -> 本函数（不进 VPC，lambda.invoke → VPC 内 db Lambda（pg8000 直连）读写任务表）
         ├─ 一次性任务/手动重跑：异步 Invoke runner Lambda
         ├─ 定期任务：EventBridge Scheduler 建/改/删 schedule
         ├─ Live View：GetBrowserSession 确认会话在线 + SigV4 presign 直播流
         └─ /chat/*：分析会话的元数据（注册/列表/重命名/软删）与消息回看
            （见 .trellis/tasks/08-11-chat-sessions-history/design.md §4；
             消息内容由 Part 2 Runtime 落库，本函数只读）

设计说明：
- 数据访问走 lambda.invoke → VPC 内 db Lambda（pg8000 直连 Aurora；中国区没有
  RDS Data API，此为可移植形态），本函数自身不进 VPC；db Lambda 直连时不设
  只读参数，也就不受分析链路那个只读会话（default_transaction_read_only=on）
  的限制。
- 集群 MinCapacity=0 会空闲自动暂停，首次查询触发冷恢复——等待下沉在 db Lambda
  内部（上限 240s），_execute 只管自身超时（55s），等不到就放弃报错。
- 只依赖 boto3/botocore（Lambda runtime 自带），zip 里就一个文件。
- Live View 的 presigned URL 必须由服务端签（SigV4 凭证不下发浏览器）。
"""

import json
import os
import re
import urllib.parse
import uuid

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
DB_LAMBDA_NAME = os.environ.get("DB_LAMBDA_NAME", "")
RUNNER_FUNCTION_NAME = os.environ.get("RUNNER_FUNCTION_NAME", "")
RUNNER_FUNCTION_ARN = os.environ.get("RUNNER_FUNCTION_ARN", "")
SCHEDULE_GROUP = os.environ.get("SCHEDULE_GROUP", "agentcore-workshop-ingestion")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
# 长期记忆（mem0）读路径：GET /chat/memories 直接 invoke 记忆向量 Lambda 的
# list op（按 JWT 用户过滤）。与 Runtime 侧写路径共用同一套 collection/dims 约定
MEMORY_LAMBDA_NAME = os.environ.get("MEMORY_LAMBDA_NAME", "")
MEM0_COLLECTION = os.environ.get("MEM0_COLLECTION", "mem0_ltm")
MEM0_EMBEDDER_DIMS = int(os.environ.get("MEM0_EMBEDDER_DIMS", "1024") or "1024")

# Live View 画面尺寸：与 Part 1 browser 会话的 viewport、前端 BrowserLiveView
# 的 remoteWidth/remoteHeight 三处必须一致，否则画面被裁切或留黑边
LIVE_VIEW_WIDTH = 1600
LIVE_VIEW_HEIGHT = 900
LIVE_VIEW_EXPIRES = 300  # AgentCore 上限就是 300 秒

# PostgreSQL 时间戳统一按 ISO8601（UTC）输出给前端
TS = "YYYY-MM-DD\"T\"HH24:MI:SS\"Z\""

_clients: dict = {}


def _client(name: str):
    """惰性创建并复用 boto3 客户端。"""
    if name not in _clients:
        _clients[name] = boto3.client(name, region_name=REGION)
    return _clients[name]


# ---------------------------------------------------------------- 数据访问 ----

_db_lambda_client = None


def _db_lambda():
    """数据面 db Lambda 专用 client：独立超时 + 禁重试。

    不复用 _client("lambda")（默认配置，runner 的异步触发还在用它）：对 db
    Lambda 的同步 invoke 需要 read_timeout=55s——tasks-api 自身 timeout 60s，
    等不到就放弃报错，而不是被 Lambda 硬杀。
    """
    global _db_lambda_client
    if _db_lambda_client is None:
        _db_lambda_client = boto3.client(
            "lambda", region_name=REGION,
            config=Config(connect_timeout=5, read_timeout=55,
                          retries={"max_attempts": 0}),
        )
    return _db_lambda_client


def _execute(sql: str, parameters: list | None = None) -> list[dict]:
    """执行一条 SQL 并返回行（list[dict]，等价原 Data API formatRecordsAs=JSON）。

    经 lambda.invoke 调 VPC 内数据面 db Lambda（pg8000 直连 Aurora，:name
    占位符与 ::TYPE cast 原样支持）；集群冷恢复的等待在 db Lambda 内部完成
    （上限 240s），这里只管超时放弃。
    """
    if not DB_LAMBDA_NAME:
        raise RuntimeError("缺少 DB_LAMBDA_NAME 环境变量")
    resp = _db_lambda().invoke(
        FunctionName=DB_LAMBDA_NAME,
        Payload=json.dumps(
            # deadline_s < read_timeout(55s)：调用方放弃时 Lambda 必已先放弃
            # 连接、语句从未执行——避免「前端 500 但任务几分钟后冒出来」的幽灵写
            {"op": "execute", "args": {"sql": sql, "params": dict(parameters or []), "deadline_s": 45}},
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError") or not isinstance(body, dict) or body.get("status") != "ok":
        raise RuntimeError(f"db lambda execute 失败: {body}")
    return body.get("rows") or []


def _p(name: str, value) -> tuple[str, str]:
    """SQL 命名参数（统一转字符串，SQL 里按需 cast / NULLIF——与 Data API 时代同语义）。"""
    return (name, "" if value is None else str(value))


def _json_field(raw):
    """JSONB 列（SQL 里已用 ::text 收口成字符串）解析成对象返回给前端。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- HTTP 工具 ----

def _resp(status: int, body: dict) -> dict:
    """统一 JSON 响应（CORS 头由 HTTP API 的 CorsConfiguration 负责）。"""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _err(status: int, message: str) -> dict:
    return _resp(status, {"error": message})


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_param(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(params.get(key, default)), hi))
    except (TypeError, ValueError):
        return default


def _normalize_country(value) -> str:
    """国家/站点是自由文本：压缩空白，旧调用未传或传空时按 UK 处理。"""
    if value is None:
        return "UK"
    normalized = " ".join(str(value).split())
    return normalized or "UK"


def _user_name(event: dict) -> str:
    """从 JWT claims 取登录用户名（会话归属的唯一依据）。

    authorizer 已经校验过签名/issuer/audience，claims 可信；Keycloak 的
    preferred_username 就是登录名（门户账号 analyst/vivian/marco）。取不到说明路由挂错了
    authorizer——宁可 403 也不能把会话落到空用户名下。
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    ) or {}
    return (claims.get("preferred_username") or "").strip()


# ---------------------------------------------------------------- 任务读写 ----

# 列名统一带 t. 前缀：列表查询里 join 了最近一次 run，status/created_at 会重名
TASK_COLUMNS = (
    "t.task_id, t.name, t.prompt, COALESCE(t.country, 'UK') AS country, "
    "t.mode, t.schedule_expr, t.status, "
    f"to_char(t.created_at, '{TS}') AS created_at, "
    f"to_char(t.updated_at, '{TS}') AS updated_at"
)

RUN_COLUMNS = (
    "run_id, task_id, status, error, browser_id, browser_session_id, "
    "stats::text AS stats, "
    f"to_char(started_at, '{TS}') AS started_at, "
    f"to_char(finished_at, '{TS}') AS finished_at, "
    f"to_char(created_at, '{TS}') AS created_at"
)


def _run_obj(row: dict) -> dict:
    """统一 run 对象形状（stats 解析为对象或 null）。"""
    return {
        "run_id": row.get("run_id"),
        "task_id": row.get("task_id"),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "error": row.get("error"),
        "stats": _json_field(row.get("stats")),
        "browser_id": row.get("browser_id"),
        "browser_session_id": row.get("browser_session_id"),
    }


def _get_task(task_id: str) -> dict | None:
    rows = _execute(
        f"SELECT {TASK_COLUMNS} FROM ingestion_tasks t WHERE t.task_id = :task_id",
        [_p("task_id", task_id)],
    )
    return rows[0] if rows else None


def _create_run(task_id: str) -> str:
    """先落一条 queued 的 run 记录，再交给 runner 接管（前端立刻能看到排队态）。"""
    run_id = str(uuid.uuid4())
    _execute(
        "INSERT INTO ingestion_runs (run_id, task_id, status, runtime_session_id) "
        "VALUES (:run_id, :task_id, 'queued', :session_id)",
        [
            _p("run_id", run_id),
            _p("task_id", task_id),
            _p("session_id", f"ingest-run-{run_id}"),
        ],
    )
    return run_id


def _invoke_runner(task_id: str, run_id: str) -> None:
    """异步（Event）触发 runner：多个任务天然并行，互不阻塞。"""
    _client("lambda").invoke(
        FunctionName=RUNNER_FUNCTION_NAME or RUNNER_FUNCTION_ARN,
        InvocationType="Event",
        Payload=json.dumps({"task_id": task_id, "run_id": run_id}).encode("utf-8"),
    )


# ------------------------------------------------------------ 定期任务调度 ----

def _put_schedule(task_id: str, schedule_expr: str, schedule_tz: str | None) -> None:
    """按任务 id 幂等 upsert 一个 schedule（目标 = runner Lambda）。"""
    args: dict = {
        "Name": task_id,
        "GroupName": SCHEDULE_GROUP,
        "ScheduleExpression": schedule_expr,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": "ENABLED",
        "Target": {
            "Arn": RUNNER_FUNCTION_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"task_id": task_id}),
        },
    }
    if schedule_tz:
        args["ScheduleExpressionTimezone"] = schedule_tz
    scheduler = _client("scheduler")
    try:
        scheduler.create_schedule(**args)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConflictException":
            raise
        scheduler.update_schedule(**args)


def _set_schedule_state(task_id: str, state: str) -> None:
    """暂停/恢复：UpdateSchedule 是整体覆盖，先取回原定义再改 State。"""
    scheduler = _client("scheduler")
    current = scheduler.get_schedule(Name=task_id, GroupName=SCHEDULE_GROUP)
    target = current["Target"]
    args: dict = {
        "Name": task_id,
        "GroupName": SCHEDULE_GROUP,
        "ScheduleExpression": current["ScheduleExpression"],
        "FlexibleTimeWindow": current.get("FlexibleTimeWindow", {"Mode": "OFF"}),
        "State": state,
        "Target": {
            "Arn": target["Arn"],
            "RoleArn": target["RoleArn"],
            "Input": target.get("Input", json.dumps({"task_id": task_id})),
        },
    }
    if current.get("ScheduleExpressionTimezone"):
        args["ScheduleExpressionTimezone"] = current["ScheduleExpressionTimezone"]
    scheduler.update_schedule(**args)


def _delete_schedule(task_id: str) -> None:
    try:
        _client("scheduler").delete_schedule(Name=task_id, GroupName=SCHEDULE_GROUP)
    except ClientError as e:
        # 一次性任务或已删过：容忍
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


# ------------------------------------------------------------------ 路由实现 ----

def create_task(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    country = _normalize_country(body.get("country"))
    mode = body.get("mode") or "one_time"
    schedule_expr = (body.get("schedule_expr") or "").strip()
    schedule_tz = (body.get("schedule_tz") or "").strip() or None

    if not name:
        return _err(400, "缺少任务名称 name")
    if not prompt:
        return _err(400, "缺少任务指令 prompt")
    if len(country) > 64:
        return _err(400, "国家/站点 country 不能超过 64 个字符")
    if mode not in ("one_time", "recurring"):
        return _err(400, "mode 只能是 one_time 或 recurring")
    if mode == "recurring" and not schedule_expr.startswith(("rate(", "cron(")):
        return _err(400, "定期任务的 schedule_expr 必须是 rate(...) 或 cron(...)")

    task_id = str(uuid.uuid4())
    _execute(
        "INSERT INTO ingestion_tasks "
        "(task_id, name, prompt, country, mode, schedule_expr, status) "
        "VALUES (:task_id, :name, :prompt, :country, :mode, "
        "NULLIF(:schedule_expr, ''), 'active')",
        [
            _p("task_id", task_id),
            _p("name", name[:256]),
            _p("prompt", prompt[:4096]),
            _p("country", country),
            _p("mode", mode),
            _p("schedule_expr", schedule_expr),
        ],
    )

    run_id = None
    if mode == "one_time":
        run_id = _create_run(task_id)
        _invoke_runner(task_id, run_id)
    else:
        _put_schedule(task_id, schedule_expr, schedule_tz)

    return _resp(201, {"task": _get_task(task_id), "run_id": run_id})


def list_tasks() -> dict:
    """任务列表：每个任务带最近一次 run 的摘要（LEFT JOIN LATERAL）。"""
    rows = _execute(
        f"SELECT {TASK_COLUMNS}, r.run_id AS last_run_id, r.status AS last_run_status, "
        f"to_char(r.started_at, '{TS}') AS last_run_started_at, "
        f"to_char(r.finished_at, '{TS}') AS last_run_finished_at "
        "FROM ingestion_tasks t "
        "LEFT JOIN LATERAL ("
        "  SELECT run_id, status, started_at, finished_at FROM ingestion_runs "
        "  WHERE task_id = t.task_id ORDER BY created_at DESC LIMIT 1"
        ") r ON TRUE "
        "WHERE t.status <> 'deleted' ORDER BY t.created_at DESC"
    )
    tasks = []
    for row in rows:
        last_run = None
        if row.get("last_run_id"):
            last_run = {
                "run_id": row["last_run_id"],
                "status": row.get("last_run_status"),
                "started_at": row.get("last_run_started_at"),
                "finished_at": row.get("last_run_finished_at"),
            }
        task = {k: v for k, v in row.items() if not k.startswith("last_run")}
        task["last_run"] = last_run
        tasks.append(task)
    return _resp(200, {"tasks": tasks})


def get_task(task_id: str) -> dict:
    task = _get_task(task_id)
    if task is None or task.get("status") == "deleted":
        return _err(404, "任务不存在")
    runs = _execute(
        f"SELECT {RUN_COLUMNS} FROM ingestion_runs WHERE task_id = :task_id "
        "ORDER BY created_at DESC LIMIT 50",
        [_p("task_id", task_id)],
    )
    return _resp(200, {"task": task, "runs": [_run_obj(r) for r in runs]})


def run_task(task_id: str) -> dict:
    """手动再跑一次（也是并行验证入口：连点两次得到两个独立 run）。"""
    task = _get_task(task_id)
    if task is None or task.get("status") == "deleted":
        return _err(404, "任务不存在")
    run_id = _create_run(task_id)
    _invoke_runner(task_id, run_id)
    return _resp(202, {"run_id": run_id})


def set_task_state(task_id: str, action: str) -> dict:
    task = _get_task(task_id)
    if task is None or task.get("status") == "deleted":
        return _err(404, "任务不存在")
    if task.get("mode") != "recurring":
        return _err(400, "只有定期任务支持暂停/恢复")

    state, status = ("DISABLED", "paused") if action == "pause" else ("ENABLED", "active")
    try:
        _set_schedule_state(task_id, state)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        # schedule 丢了（如手工删除）：恢复时按任务定义重建，暂停时无事可做
        if action == "resume":
            _put_schedule(task_id, task.get("schedule_expr") or "", None)
    _execute(
        "UPDATE ingestion_tasks SET status = :status, updated_at = NOW() "
        "WHERE task_id = :task_id",
        [_p("status", status), _p("task_id", task_id)],
    )
    return _resp(200, {"task": _get_task(task_id)})


def delete_task(task_id: str) -> dict:
    """软删：保留历史 run 与结果，仅从列表中移除并停掉调度。"""
    if _get_task(task_id) is None:
        return _err(404, "任务不存在")
    _execute(
        "UPDATE ingestion_tasks SET status = 'deleted', updated_at = NOW() "
        "WHERE task_id = :task_id",
        [_p("task_id", task_id)],
    )
    _delete_schedule(task_id)
    return _resp(200, {"ok": True})


def get_run(run_id: str) -> dict:
    rows = _execute(
        f"SELECT {RUN_COLUMNS} FROM ingestion_runs WHERE run_id = :run_id",
        [_p("run_id", run_id)],
    )
    if not rows:
        return _err(404, "运行记录不存在")
    return _resp(200, {"run": _run_obj(rows[0])})


def get_run_events(run_id: str, params: dict) -> dict:
    """增量拉事件：前端用 after_seq 做游标，运行中每 3 秒轮询一次。"""
    after_seq = _int_param(params, "after_seq", 0, 0, 10_000_000)
    rows = _execute(
        "SELECT seq, kind, message, payload::text AS payload, "
        f"to_char(created_at, '{TS}') AS created_at "
        "FROM ingestion_run_events WHERE run_id = :run_id AND seq > :after_seq::INT "
        "ORDER BY seq LIMIT 500",
        [_p("run_id", run_id), _p("after_seq", after_seq)],
    )
    events = [
        {
            "seq": int(r["seq"]),
            "kind": r.get("kind"),
            "message": r.get("message"),
            "payload": _json_field(r.get("payload")),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]
    return _resp(200, {"events": events})


# 明细表：只读展示，列固定写死（不接受前端传列名，避免注入）
_CANDIDATE_COLUMNS = (
    "candidate_id, site, asin_or_url, product_name_cn, product_title_en, "
    "search_keyword, current_price, monthly_sales, competitor_min_price, "
    "estimated_price, estimated_monthly_sales, product_score, price_flag, "
    "main_image_url, ai_summary, "
    f"to_char(created_at, '{TS}') AS created_at"
)
_VERDICT_COLUMNS = (
    "verdict_id, site, asin_or_url, product_title, verdict, reason, "
    "current_price, monthly_sales, product_score, "
    f"to_char(created_at, '{TS}') AS created_at"
)


def get_run_results(run_id: str, params: dict) -> dict:
    table = params.get("table") or "candidates"
    if table == "candidates":
        relation, columns = "product_candidates", _CANDIDATE_COLUMNS
    elif table == "verdicts":
        relation, columns = "selection_verdicts", _VERDICT_COLUMNS
    else:
        return _err(400, "table 只能是 candidates 或 verdicts")

    page = _int_param(params, "page", 1, 1, 10_000)
    page_size = _int_param(params, "page_size", 20, 1, 100)
    offset = (page - 1) * page_size

    total_rows = _execute(
        f"SELECT COUNT(*) AS total FROM {relation} WHERE run_id = :run_id",
        [_p("run_id", run_id)],
    )
    total = int(total_rows[0]["total"]) if total_rows else 0
    rows = _execute(
        f"SELECT {columns} FROM {relation} WHERE run_id = :run_id "
        f"ORDER BY created_at DESC LIMIT {page_size} OFFSET {offset}",
        [_p("run_id", run_id)],
    )
    return _resp(200, {"rows": rows, "total": total, "page": page, "page_size": page_size})


def get_live_view(run_id: str) -> dict:
    """生成 Browser Live View 的 presigned URL（有效期 300 秒，到期前端重拉）。"""
    rows = _execute(
        "SELECT status, browser_id, browser_session_id FROM ingestion_runs "
        "WHERE run_id = :run_id",
        [_p("run_id", run_id)],
    )
    if not rows:
        return _err(404, "运行记录不存在")
    run = rows[0]
    browser_id = run.get("browser_id")
    session_id = run.get("browser_session_id")
    # 409 与 410 语义必须分清：409 = 还没就绪（前端保留重试），
    # 410 = 已经结束（前端关掉实况入口），混用会让入口一直停在重试态
    if run.get("status") in ("succeeded", "failed"):
        return _err(410, "本次运行已结束，实况画面不可用")
    if run.get("status") != "running" or not browser_id or not session_id:
        return _err(409, "云端浏览器会话尚未就绪，请稍后重试")

    # 会话可能已经关闭（Agent 抓完就 stop），此时直播流不可用
    try:
        session = _client("bedrock-agentcore").get_browser_session(
            browserIdentifier=browser_id, sessionId=session_id
        )
        if session.get("status") != "READY":
            return _err(410, "浏览器会话已结束，实况画面不可用")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in (
            "ResourceNotFoundException", "ValidationException"
        ):
            return _err(410, "浏览器会话已结束，实况画面不可用")
        raise
    except Exception:
        # 老版本 boto3 里可能没有 bedrock-agentcore 数据面模型，跳过状态校验直接签
        pass

    # 数据面端点优先从 boto3 SDK 解析（aws / aws-cn 分区后缀不同）；
    # 老版本 boto3 缺 bedrock-agentcore 模型时按 REGION 前缀手拼兜底
    #（与上面状态校验的"跳过直接签"兜底、runner 的 SigV4 兜底同一处置）
    try:
        endpoint = _client("bedrock-agentcore").meta.endpoint_url.rstrip("/")
    except Exception:
        dns_suffix = "amazonaws.com.cn" if REGION.startswith("cn-") else "amazonaws.com"
        endpoint = f"https://bedrock-agentcore.{REGION}.{dns_suffix}"
    host = urllib.parse.urlparse(endpoint).netloc
    url = (
        f"{endpoint}"
        f"/browser-streams/{urllib.parse.quote(browser_id, safe='')}"
        f"/sessions/{urllib.parse.quote(session_id, safe='')}/live-view"
    )
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(
        method="GET", url=url,
        headers={"host": host},
    )
    SigV4QueryAuth(
        credentials=credentials, service_name="bedrock-agentcore",
        region_name=REGION, expires=LIVE_VIEW_EXPIRES,
    ).add_auth(request)

    return _resp(200, {
        "url": request.url,
        "width": LIVE_VIEW_WIDTH,
        "height": LIVE_VIEW_HEIGHT,
        "expires_in": LIVE_VIEW_EXPIRES,
    })


# -------------------------------------------------------------- 分析会话 ----

# 会话 id 由前端生成（web- + uuid4），同时用作 Runtime 的会话隔离 id。
# 服务端校验格式：只接受这个前缀，防止把命令行会话或任意字符串注册到自己名下
SESSION_ID_RE = re.compile(r"^web-[0-9a-f-]{36}$")

# 会话对象形状（与 web/src/chat/api.ts 的 ChatSession 一一对应）：
# last_message_at 是最后一条消息的时间，注册了还没发过消息时为 null；
# 排序另用 COALESCE(..., updated_at) 兜底，保证新会话不会掉到列表最后
CHAT_SESSION_COLUMNS = (
    "s.session_id, s.title, s.status, "
    f"to_char(s.created_at, '{TS}') AS created_at, "
    f"to_char(s.updated_at, '{TS}') AS updated_at, "
    f"to_char(m.last_at, '{TS}') AS last_message_at, "
    "COALESCE(m.message_count, 0) AS message_count"
)

# 消息数只统计有文本的（纯工具轮 display_text 为 NULL，前端也不展示，
# 计进去会让「2 条消息」的会话显示成 5 条）
CHAT_SESSION_FROM = (
    "FROM chat_sessions s "
    "LEFT JOIN LATERAL ("
    "  SELECT MAX(created_at) AS last_at, "
    "         COUNT(*) FILTER (WHERE display_text IS NOT NULL) AS message_count "
    "  FROM chat_messages WHERE session_id = s.session_id"
    ") m ON TRUE "
)


def _chat_session_obj(row: dict) -> dict:
    return {
        "session_id": row.get("session_id"),
        "title": row.get("title"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_message_at": row.get("last_message_at"),
        "message_count": int(row.get("message_count") or 0),
    }


def _get_chat_session(session_id: str, user_name: str) -> dict | None:
    """按「归属当前用户 + 未软删」取会话；其余情况都当不存在。"""
    rows = _execute(
        f"SELECT {CHAT_SESSION_COLUMNS} {CHAT_SESSION_FROM}"
        "WHERE s.session_id = :session_id AND s.user_name = :user_name "
        "AND s.status <> 'deleted'",
        [_p("session_id", session_id), _p("user_name", user_name)],
    )
    return rows[0] if rows else None


def register_chat_session(body: dict, user_name: str) -> dict:
    """注册会话（幂等 upsert）：前端在新会话首次发送时调一次。

    ON CONFLICT 上的 user_name 条件是防越权：别人的 session_id 撞进来时
    UPDATE 被跳过（不报错也不改数据），随后按归属读回拿不到行 -> 409。
    已软删的会话被重新注册时恢复成 active——用户正在这个会话里继续对话，
    让它从列表里消失反而更费解。
    """
    session_id = (body.get("session_id") or "").strip()
    title = (body.get("title") or "").strip() or "未命名会话"
    if not SESSION_ID_RE.match(session_id):
        return _err(400, "session_id 格式非法（须为 web-<uuid>）")

    _execute(
        "INSERT INTO chat_sessions (session_id, user_name, title) "
        "VALUES (:session_id, :user_name, :title) "
        "ON CONFLICT (session_id) DO UPDATE "
        "SET updated_at = NOW(), status = 'active' "
        "WHERE chat_sessions.user_name = :user_name",
        [
            _p("session_id", session_id),
            _p("user_name", user_name),
            _p("title", title[:256]),
        ],
    )
    session = _get_chat_session(session_id, user_name)
    if session is None:
        return _err(409, "该会话 id 已被占用")
    return _resp(200, {"session": _chat_session_obj(session)})


def list_chat_sessions(user_name: str) -> dict:
    """当前用户的会话列表，按最后活动倒序。"""
    rows = _execute(
        f"SELECT {CHAT_SESSION_COLUMNS} {CHAT_SESSION_FROM}"
        "WHERE s.user_name = :user_name AND s.status <> 'deleted' "
        "ORDER BY COALESCE(m.last_at, s.updated_at) DESC LIMIT 200",
        [_p("user_name", user_name)],
    )
    return _resp(200, {"sessions": [_chat_session_obj(r) for r in rows]})


def get_chat_messages(session_id: str, user_name: str, params: dict) -> dict:
    """会话回看：只回展示用字段，纯工具轮（display_text IS NULL）过滤掉。

    完整的 Strands message（content 列）只给 Runtime 回灌用，不下发前端。
    单页 200 条 + after_seq 游标：Lambda 响应有 6MB 上限，长会话靠
    has_more 继续拉，避免静默截断。
    """
    if _get_chat_session(session_id, user_name) is None:
        return _err(404, "会话不存在")

    limit = 200
    after_seq = _int_param(params, "after_seq", -1, -1, 10_000_000)
    rows = _execute(
        "SELECT seq, role, display_text, "
        f"to_char(created_at, '{TS}') AS created_at "
        "FROM chat_messages WHERE session_id = :session_id "
        "AND seq > :after_seq::INT AND display_text IS NOT NULL "
        f"ORDER BY seq LIMIT {limit + 1}",
        [_p("session_id", session_id), _p("after_seq", after_seq)],
    )
    has_more = len(rows) > limit
    messages = [
        {
            "seq": int(r["seq"]),
            "role": r.get("role"),
            "display_text": r.get("display_text"),
            "created_at": r.get("created_at"),
        }
        for r in rows[:limit]
    ]
    return _resp(200, {"messages": messages, "has_more": has_more})


def rename_chat_session(session_id: str, user_name: str, body: dict) -> dict:
    title = (body.get("title") or "").strip()
    if not title:
        return _err(400, "缺少会话标题 title")
    if _get_chat_session(session_id, user_name) is None:
        return _err(404, "会话不存在")
    _execute(
        "UPDATE chat_sessions SET title = :title, updated_at = NOW() "
        "WHERE session_id = :session_id AND user_name = :user_name",
        [
            _p("title", title[:256]),
            _p("session_id", session_id),
            _p("user_name", user_name),
        ],
    )
    return _resp(200, {"session": _chat_session_obj(
        _get_chat_session(session_id, user_name) or {}
    )})


def delete_chat_session(session_id: str, user_name: str) -> dict:
    """软删：消息全部保留，只是不再出现在列表里。"""
    if _get_chat_session(session_id, user_name) is None:
        return _err(404, "会话不存在")
    _execute(
        "UPDATE chat_sessions SET status = 'deleted', updated_at = NOW() "
        "WHERE session_id = :session_id AND user_name = :user_name",
        [_p("session_id", session_id), _p("user_name", user_name)],
    )
    return _resp(200, {"ok": True})


def list_user_memories(user_name: str) -> dict:
    """当前用户的 mem0 长期记忆条目（按沉淀时间倒序）。

    读路径：lambda.invoke 记忆向量 Lambda 的 list op，payload 里的 data 即
    记忆文本（键形约定见 .trellis/spec/backend/mem0-long-term-memory.md）。
    记忆是增强功能：Lambda 未配置/不可达/集群冷恢复时降级返回空列表 +
    degraded 标记，不给门户抛 500。
    """
    if not MEMORY_LAMBDA_NAME:
        return _resp(200, {"memories": [], "degraded": True})
    try:
        resp = _client("lambda").invoke(
            FunctionName=MEMORY_LAMBDA_NAME,
            Payload=json.dumps({
                "op": "list",
                "args": {
                    "collection": MEM0_COLLECTION,
                    "dims": MEM0_EMBEDDER_DIMS,
                    "filters": {"user_id": user_name},
                    "top_k": 100,
                },
            }).encode("utf-8"),
        )
        body = json.loads(resp["Payload"].read())
        if resp.get("FunctionError") or body.get("status") != "ok":
            raise RuntimeError(f"memory lambda list 失败: {body}")
    except Exception as e:
        print(f"[mem0] 读取用户记忆失败（降级空列表）: {type(e).__name__}: {e}")
        return _resp(200, {"memories": [], "degraded": True})

    rows = body.get("result") or []
    memories = [
        {
            "id": r.get("id"),
            "memory": (r.get("payload") or {}).get("data") or "",
            "created_at": (r.get("payload") or {}).get("created_at"),
        }
        for r in rows
        if (r.get("payload") or {}).get("data")
    ]
    # 记忆 Lambda 的 list 无 ORDER BY，这里按沉淀时间倒序（ISO8601 字符串可比）
    memories.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return _resp(200, {"memories": memories})


def _dispatch_chat(method: str, segments: list[str], event: dict, params: dict) -> dict:
    """/chat/... 路由。所有查询都按 JWT 里的用户名过滤（会话与记忆不跨用户可见）。"""
    user_name = _user_name(event)
    if not user_name:
        return _err(403, "token 缺少 preferred_username claim，无法确定会话归属")

    if segments == ["chat", "memories"] and method == "GET":
        return list_user_memories(user_name)

    if segments[:2] == ["chat", "sessions"]:
        if len(segments) == 2:
            if method == "GET":
                return list_chat_sessions(user_name)
            if method == "POST":
                return register_chat_session(_body(event), user_name)
        elif len(segments) == 3:
            session_id = segments[2]
            if method == "PATCH":
                return rename_chat_session(session_id, user_name, _body(event))
            if method == "DELETE":
                return delete_chat_session(session_id, user_name)
        elif len(segments) == 4 and method == "GET" and segments[3] == "messages":
            return get_chat_messages(segments[2], user_name, params)

    return _err(404, f"未知路径: {method} /{'/'.join(segments)}")


# ------------------------------------------------------------------- 入口 ----

def _dispatch(method: str, segments: list[str], event: dict, params: dict) -> dict:
    # /tasks ...
    if segments[:1] == ["tasks"]:
        if len(segments) == 1:
            if method == "GET":
                return list_tasks()
            if method == "POST":
                return create_task(_body(event))
        elif len(segments) == 2:
            task_id = segments[1]
            if method == "GET":
                return get_task(task_id)
            if method == "DELETE":
                return delete_task(task_id)
        elif len(segments) == 3 and method == "POST":
            task_id, action = segments[1], segments[2]
            if action == "run":
                return run_task(task_id)
            if action in ("pause", "resume"):
                return set_task_state(task_id, action)

    # /runs ...
    if segments[:1] == ["runs"] and method == "GET":
        if len(segments) == 2:
            return get_run(segments[1])
        if len(segments) == 3:
            run_id, sub = segments[1], segments[2]
            if sub == "events":
                return get_run_events(run_id, params)
            if sub == "results":
                return get_run_results(run_id, params)
            if sub == "live-view":
                return get_live_view(run_id)

    # /chat ...（分析会话历史）
    if segments[:1] == ["chat"]:
        return _dispatch_chat(method, segments, event, params)

    return _err(404, f"未知路径: {method} /{'/'.join(segments)}")


def lambda_handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    raw_path = event.get("rawPath") or "/"
    segments = [s for s in raw_path.strip("/").split("/") if s]
    params = event.get("queryStringParameters") or {}
    try:
        return _dispatch(method, segments, event, params)
    except Exception as e:  # 兜底：给前端结构化错误而不是 502
        # 只回异常类名给排查用；原始信息（可能含 SQL 语句 / ARN）只进 CloudWatch
        print(f"[tasks-api] {method} {raw_path} 失败: {type(e).__name__}: {e}")
        return _err(500, f"服务内部错误（{type(e).__name__}），详情见 tasks-api Lambda 日志")
