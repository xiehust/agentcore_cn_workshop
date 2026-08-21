"""Part 2 摄入任务执行器 Lambda —— 代门户去调 Part 1 的数据摄入 Agent。

为什么要这一层：Part 1 的 Runtime 保持 IAM（SigV4）入站认证，`agentcore invoke`
的用法不变；门户里的用户只有 Keycloak token，不能直接调它。于是由本函数用自己
的执行角色代签调用，同时把 SSE 流里的进度落成事件表，供前端轮询展示。

入参：
    {"task_id": "...", "run_id": "..."}   tasks-api 触发（run 记录已是 queued）
    {"task_id": "..."}                    EventBridge Scheduler 触发（自建 run）

流程：
    读任务（非 active 直接终止）-> run 置 running
    -> invoke_agent_runtime（独立 runtime session id，payload 带 run_id）
    -> 消费 SSE：文本增量聚合成 log 事件，结构化事件落 tool/browser/error
    -> 收尾：按 run_id 统计入库结果，写 stats + succeeded/failed

事件契约（Part 1 侧约定，不可改）：
    {"event": "tool_use", "name": ..., "tool_use_id": ...}
    {"event": "browser_session", "browser_id": ..., "session_id": ...}
    其余是裸文本增量（str）。

只依赖 boto3/botocore（Lambda runtime 自带），zip 里就一个文件。
"""

import json
import os
import time
import urllib.parse
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-west-2")
DB_LAMBDA_NAME = os.environ.get("DB_LAMBDA_NAME", "")
PART1_RUNTIME_ARN = os.environ.get("PART1_RUNTIME_ARN", "")

# 文本增量节流：攒够 FLUSH_CHARS 或者遇到换行且距上次 flush 超过 FLUSH_SECONDS
# 才落一条 log 事件，避免 2~5 分钟的运行把事件表刷爆
FLUSH_CHARS = 400
FLUSH_SECONDS = 2.0

_db_lambda_client = None
_runtime_client = None


# ---------------------------------------------------------------- 数据访问 ----

def _db_lambda():
    """数据面 db Lambda 专用 client：read_timeout=70s + 禁重试。

    runner 自身 timeout 900s，但等 DB 不该占掉消费 SSE 的预算；事件写失败
    已被 emit 的 try/except 兜底，等不到就放弃。
    """
    global _db_lambda_client
    if _db_lambda_client is None:
        _db_lambda_client = boto3.client(
            "lambda", region_name=REGION,
            config=Config(connect_timeout=5, read_timeout=70,
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
            # deadline_s < read_timeout(70s)：保住「超时 = 零写入」原子失败语义
            {"op": "execute", "args": {"sql": sql, "params": dict(parameters or []), "deadline_s": 60}},
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


# ---------------------------------------------------------------- 事件写入 ----

class EventWriter:
    """把运行进度写进 ingestion_run_events；seq 在进程内自增，供前端做游标。"""

    def __init__(self, run_id: str, start_seq: int = 0):
        self.run_id = run_id
        self.seq = start_seq

    def emit(self, kind: str, message: str = "", payload: dict | None = None) -> None:
        self.seq += 1
        try:
            _execute(
                "INSERT INTO ingestion_run_events (run_id, seq, kind, message, payload) "
                "VALUES (:run_id, :seq::INT, :kind, NULLIF(:message, ''), "
                "NULLIF(:payload, '')::JSONB)",
                [
                    _p("run_id", self.run_id),
                    _p("seq", self.seq),
                    _p("kind", kind),
                    _p("message", (message or "")[:4096]),
                    _p("payload", json.dumps(payload, ensure_ascii=False) if payload else ""),
                ],
            )
        except Exception as e:  # 落事件失败不该中断任务本身
            print(f"[runner] 写事件失败({kind}): {type(e).__name__}: {e}")


# -------------------------------------------------------- 调 Part 1 Runtime ----

def _build_runtime_prompt(country: str | None, prompt: str) -> str:
    """把任务国家作为通用 Skill 路由约束置于原始任务指令之前。"""
    stored_country = " ".join(str(country or "").split()) or "UK"
    directive = (
        f"任务国家/站点：{stored_country}。\n"
        "请选择明确支持该国家/站点的 Skill 执行；该任务配置优先于原始任务文本中的冲突描述。\n"
        "域名、币种、筛选规则和入库 site 值均由匹配到的 Skill 定义。\n"
        "如果没有匹配的 Skill，必须停止并明确报错，不得回退到其他国家/站点或写入错误数据。"
    )
    return f"{directive}\n\n{prompt}"


def _runtime():
    """AgentCore 数据面 client：摄入任务可运行 2~5 分钟，不能用 SDK 默认 60s 读超时。"""
    global _runtime_client
    if _runtime_client is None:
        _runtime_client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(connect_timeout=5, read_timeout=870,
                          retries={"max_attempts": 0}),
        )
    return _runtime_client


def _invoke_runtime_stream(session_id: str, payload: dict):
    """调 Part 1 Runtime 并返回 SSE 的按行迭代器。

    优先用 boto3 的 bedrock-agentcore 客户端；若运行时自带的 boto3 版本还没有
    这个数据面服务模型，退回手写 SigV4 直接打数据面 HTTP 端点（与
    scripts/chat_test.sh 的 URL 一致，只是认证从 Bearer 换成 SigV4）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        client = _runtime()
    except Exception as e:
        print(f"[runner] boto3 无 bedrock-agentcore 数据面模型，改用 SigV4 直连: {e}")
        return _invoke_runtime_stream_signed(session_id, body)

    resp = client.invoke_agent_runtime(
        agentRuntimeArn=PART1_RUNTIME_ARN,
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="text/event-stream",
        payload=body,
    )
    return resp["response"].iter_lines()


def _invoke_runtime_stream_signed(session_id: str, body: bytes):
    """SigV4 直连数据面端点的兜底实现，返回按行迭代器。

    端点不走 boto3 解析：本路径本身就是「boto3 缺 bedrock-agentcore 服务模型」
    时才会走的（数据面与控制面模型同批发布，缺一个多半两个都缺），所以按
    REGION 前缀推导分区域名后缀（cn-* -> amazonaws.com.cn），不依赖任何服务模型。
    """
    escaped = urllib.parse.quote(PART1_RUNTIME_ARN, safe="")
    dns_suffix = "amazonaws.com.cn" if REGION.startswith("cn-") else "amazonaws.com"
    url = (
        f"https://bedrock-agentcore.{REGION}.{dns_suffix}"
        f"/runtimes/{escaped}/invocations?qualifier=DEFAULT"
    )
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)
    req = urllib.request.Request(
        url, data=body, headers=dict(request.headers.items()), method="POST"
    )
    return urllib.request.urlopen(req, timeout=870)


def _sse_chunks(lines):
    """从 SSE 行流里解出一个个 chunk（str 文本增量或 dict 结构化事件）。"""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line.startswith("data: "):
            continue
        try:
            yield json.loads(line[6:])
        except (ValueError, TypeError):
            yield line[6:]


# ------------------------------------------------------------------- 主流程 ----

def _load_task(task_id: str) -> dict | None:
    rows = _execute(
        "SELECT task_id, prompt, COALESCE(country, 'UK') AS country, status "
        "FROM ingestion_tasks WHERE task_id = :task_id",
        [_p("task_id", task_id)],
    )
    return rows[0] if rows else None


def _begin_run(task_id: str, run_id: str, session_id: str) -> None:
    """把 run 置为 running；tasks-api 已插过 queued 记录时走 UPDATE，
    Scheduler 触发时（没有 run 记录）走 INSERT。"""
    exists = _execute(
        "SELECT run_id FROM ingestion_runs WHERE run_id = :run_id",
        [_p("run_id", run_id)],
    )
    if exists:
        _execute(
            "UPDATE ingestion_runs SET status = 'running', started_at = NOW(), "
            "runtime_session_id = :session_id WHERE run_id = :run_id",
            [_p("session_id", session_id), _p("run_id", run_id)],
        )
        return
    _execute(
        "INSERT INTO ingestion_runs (run_id, task_id, status, runtime_session_id, started_at) "
        "VALUES (:run_id, :task_id, 'running', :session_id, NOW())",
        [_p("run_id", run_id), _p("task_id", task_id), _p("session_id", session_id)],
    )


def _bind_browser(run_id: str, browser_id: str, session_id: str) -> None:
    """记下浏览器会话，Live View 接口据此签直播流 URL。"""
    _execute(
        "UPDATE ingestion_runs SET browser_id = :browser_id, "
        "browser_session_id = :session_id WHERE run_id = :run_id",
        [
            _p("browser_id", (browser_id or "")[:128]),
            _p("session_id", (session_id or "")[:128]),
            _p("run_id", run_id),
        ],
    )


def _collect_stats(run_id: str, duration_s: int) -> dict:
    """按 run_id 统计本次运行写进数仓的结果。"""
    stats: dict = {"candidates": 0, "verdicts": {}, "duration_s": duration_s}
    try:
        rows = _execute(
            "SELECT COUNT(*) AS n FROM product_candidates WHERE run_id = :run_id",
            [_p("run_id", run_id)],
        )
        stats["candidates"] = int(rows[0]["n"]) if rows else 0
        rows = _execute(
            "SELECT verdict, COUNT(*) AS n FROM selection_verdicts "
            "WHERE run_id = :run_id GROUP BY verdict",
            [_p("run_id", run_id)],
        )
        stats["verdicts"] = {r["verdict"]: int(r["n"]) for r in rows}
    except Exception as e:
        print(f"[runner] 统计失败: {type(e).__name__}: {e}")
        stats["stats_error"] = f"{type(e).__name__}: {e}"
    return stats


def _finish_run(run_id: str, status: str, stats: dict, error: str = "") -> None:
    _execute(
        "UPDATE ingestion_runs SET status = :status, finished_at = NOW(), "
        "stats = NULLIF(:stats, '')::JSONB, error = NULLIF(:error, '') "
        "WHERE run_id = :run_id",
        [
            _p("status", status),
            _p("stats", json.dumps(stats, ensure_ascii=False) if stats else ""),
            _p("error", (error or "")[:2000]),
            _p("run_id", run_id),
        ],
    )


def _consume(events: EventWriter, run_id: str, lines, deadline: float | None = None) -> None:
    """消费 SSE 流：结构化事件直接落表，文本增量节流聚合成 log 事件。

    deadline 是本次消费的最晚时间（Lambda 超时前留出的收尾余量）。
    """
    buffer = ""
    last_flush = time.time()
    seen_tools: set[str] = set()

    def flush(force: bool = False) -> None:
        nonlocal buffer, last_flush
        text = buffer.strip()
        if not text:
            buffer = ""
            return
        if force or len(buffer) >= FLUSH_CHARS or (
            "\n" in buffer and time.time() - last_flush >= FLUSH_SECONDS
        ):
            events.emit("log", text)
            buffer = ""
            last_flush = time.time()

    for chunk in _sse_chunks(lines):
        # Lambda 超时是硬杀（不走 except），run 会永远停在 running、前端一直转圈；
        # 剩余时间不够时主动抛出，交给外层收尾成 failed
        if deadline is not None and time.time() > deadline:
            raise TimeoutError("超出本次执行的时间上限，已中断（Agent 侧可能仍在继续）")
        if isinstance(chunk, str):
            buffer += chunk
            flush()
            continue
        if not isinstance(chunk, dict):
            continue

        kind = chunk.get("event")
        if kind == "tool_use":
            flush(force=True)
            key = chunk.get("tool_use_id") or chunk.get("name") or ""
            if key in seen_tools:
                continue
            seen_tools.add(key)
            events.emit("tool", chunk.get("name") or "工具调用", chunk)
        elif kind == "browser_session":
            flush(force=True)
            browser_id = chunk.get("browser_id") or ""
            session_id = chunk.get("session_id") or ""
            _bind_browser(run_id, browser_id, session_id)
            events.emit("browser", "云端浏览器会话已就绪", chunk)
        elif chunk.get("error"):
            flush(force=True)
            events.emit("error", str(chunk.get("error"))[:4096], chunk)
        else:
            # 未知结构化事件：原样留档，前端按 kind=log 展示
            flush(force=True)
            events.emit("log", json.dumps(chunk, ensure_ascii=False)[:4096], chunk)

    flush(force=True)


def lambda_handler(event, context):
    task_id = (event or {}).get("task_id")
    run_id = (event or {}).get("run_id") or str(uuid.uuid4())
    if not task_id:
        return {"ok": False, "error": "缺少 task_id"}
    if not PART1_RUNTIME_ARN:
        return {"ok": False, "error": "缺少 PART1_RUNTIME_ARN 环境变量"}

    # Scheduler 触发只带 task_id；tasks-api 触发（新建/立即运行）会带 run_id，
    # 且已经预建了一条 queued 记录 —— 这两种来源的中止处理不一样
    scheduled = not (event or {}).get("run_id")

    task = _load_task(task_id)
    gone = task is None or task.get("status") == "deleted"
    # 暂停只停掉自动调度：Scheduler 的在途触发要丢弃，门户「立即运行」仍放行
    if gone or (scheduled and task.get("status") != "active"):
        reason = "任务不存在" if task is None else f"任务状态为 {task.get('status')}"
        print(f"[runner] {reason}，跳过本次执行: {task_id}")
        if not scheduled:
            # 不收尾的话，tasks-api 预建的那条记录会永远停在「排队中」
            _finish_run(run_id, "failed", {}, reason)
        return {"ok": False, "run_id": run_id, "error": reason}

    # session id 要求 ≥33 字符；每个 run 一个独立会话 -> Part 1 各自 microVM，并行不串
    session_id = f"ingest-run-{run_id}"
    _begin_run(task_id, run_id, session_id)
    events = EventWriter(run_id)
    events.emit("phase", "任务已启动，正在调用数据摄入 Agent", {"session_id": session_id})

    started = time.time()
    # 留 40 秒收尾（写统计与最终状态），剩余时间不足时主动中断而不是被 Lambda 硬杀
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    deadline = None
    if callable(remaining):
        deadline = started + max(30.0, remaining() / 1000 - 40)

    try:
        lines = _invoke_runtime_stream(session_id, {
            "prompt": _build_runtime_prompt(
                task.get("country"),
                task.get("prompt") or "",
            ),
            "run_id": run_id,
        })
        _consume(events, run_id, lines, deadline)
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        print(f"[runner] run {run_id} 失败: {message}")
        events.emit("error", message)
        stats = _collect_stats(run_id, int(time.time() - started))
        _finish_run(run_id, "failed", stats, message)
        return {"ok": False, "run_id": run_id, "error": message}

    stats = _collect_stats(run_id, int(time.time() - started))
    _finish_run(run_id, "succeeded", stats)
    events.emit("result", "本次运行已完成", stats)
    return {"ok": True, "run_id": run_id, "stats": stats}
