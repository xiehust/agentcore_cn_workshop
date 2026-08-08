"""Part 2 任务管理 API Lambda —— 门户「数据摄入任务管理」模块的后端。

链路（见 .trellis/tasks/08-08-ingestion-task-manager/design.md §1）：

    SPA（Keycloak 用户 token）
      -> HTTP API（JWT authorizer 校验 issuer + aud=analysis-runtime）
      -> 本函数（不进 VPC，RDS Data API 读写任务表）
         ├─ 一次性任务/手动重跑：异步 Invoke runner Lambda
         ├─ 定期任务：EventBridge Scheduler 建/改/删 schedule
         └─ Live View：GetBrowserSession 确认会话在线 + SigV4 presign 直播流

设计说明：
- 走 RDS Data API（和 Part 1 的 aurora_tools.py 同款），不进 VPC，也就不受
  分析链路那个只读会话（default_transaction_read_only=on）的限制。
- 集群 MinCapacity=0 会空闲自动暂停，首次查询触发冷恢复，_execute 内置退避重试。
- 只依赖 boto3/botocore（Lambda runtime 自带），zip 里就一个文件。
- Live View 的 presigned URL 必须由服务端签（SigV4 凭证不下发浏览器）。
"""

import json
import os
import time
import urllib.parse
import uuid

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-west-2")
CLUSTER_ARN = os.environ.get("AURORA_CLUSTER_ARN", "")
SECRET_ARN = os.environ.get("AURORA_SECRET_ARN", "")
DATABASE = os.environ.get("AURORA_DATABASE", "product_hunting")
RUNNER_FUNCTION_NAME = os.environ.get("RUNNER_FUNCTION_NAME", "")
RUNNER_FUNCTION_ARN = os.environ.get("RUNNER_FUNCTION_ARN", "")
SCHEDULE_GROUP = os.environ.get("SCHEDULE_GROUP", "agentcore-workshop-ingestion")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")

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

def _execute(sql: str, parameters: list | None = None) -> list[dict]:
    """执行一条 SQL 并返回行（list[dict]）。

    用 formatRecordsAs=JSON 让 Data API 直接返回列名->值的 JSON，省掉手工
    拆 stringValue/longValue。集群从暂停中恢复时退避重试（约 50 秒内）。
    """
    if not CLUSTER_ARN or not SECRET_ARN:
        raise RuntimeError("缺少 AURORA_CLUSTER_ARN / AURORA_SECRET_ARN 环境变量")

    kwargs: dict = {
        "resourceArn": CLUSTER_ARN,
        "secretArn": SECRET_ARN,
        "database": DATABASE,
        "sql": sql,
        "formatRecordsAs": "JSON",
    }
    if parameters:
        kwargs["parameters"] = parameters

    deadline = time.time() + 50
    while True:
        try:
            resp = _client("rds-data").execute_statement(**kwargs)
            records = resp.get("formattedRecords") or "[]"
            return json.loads(records)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if (code == "DatabaseResumingException" or "resum" in str(e).lower()) \
                    and time.time() < deadline:
                time.sleep(5)
                continue
            raise


def _p(name: str, value) -> dict:
    """构造 Data API 参数（统一转字符串，SQL 里按需 cast / NULLIF）。"""
    return {"name": name, "value": {"stringValue": "" if value is None else str(value)}}


def _json_field(raw):
    """JSONB 列在 formattedRecords 里是字符串，解析成对象返回给前端。"""
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


# ---------------------------------------------------------------- 任务读写 ----

# 列名统一带 t. 前缀：列表查询里 join 了最近一次 run，status/created_at 会重名
TASK_COLUMNS = (
    "t.task_id, t.name, t.prompt, t.mode, t.schedule_expr, t.status, "
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
    mode = body.get("mode") or "one_time"
    schedule_expr = (body.get("schedule_expr") or "").strip()
    schedule_tz = (body.get("schedule_tz") or "").strip() or None

    if not name:
        return _err(400, "缺少任务名称 name")
    if not prompt:
        return _err(400, "缺少任务指令 prompt")
    if mode not in ("one_time", "recurring"):
        return _err(400, "mode 只能是 one_time 或 recurring")
    if mode == "recurring" and not schedule_expr.startswith(("rate(", "cron(")):
        return _err(400, "定期任务的 schedule_expr 必须是 rate(...) 或 cron(...)")

    task_id = str(uuid.uuid4())
    _execute(
        "INSERT INTO ingestion_tasks (task_id, name, prompt, mode, schedule_expr, status) "
        "VALUES (:task_id, :name, :prompt, :mode, NULLIF(:schedule_expr, ''), 'active')",
        [
            _p("task_id", task_id),
            _p("name", name[:256]),
            _p("prompt", prompt[:4096]),
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

    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/browser-streams/{urllib.parse.quote(browser_id, safe='')}"
        f"/sessions/{urllib.parse.quote(session_id, safe='')}/live-view"
    )
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(
        method="GET", url=url,
        headers={"host": f"bedrock-agentcore.{REGION}.amazonaws.com"},
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
