# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""mem0 长期记忆后端 —— 跨会话的用户记忆（自部署，非 AgentCore 托管 Memory）。

架构（与 workshop「计算留公网、私网访问全交 VPC 内 Lambda」哲学一致）：

    Runtime（本模块，公网）                       VPC 内
    ┌──────────────────────────────┐    lambda.invoke   ┌──────────────────┐
    │ mem0 Memory                  │ ─────────────────> │ 记忆向量 Lambda    │
    │  · LLM 事实提取（OpenAI 兼容） │                    │  pg8000 → Aurora │
    │  · embedding（OpenAI 兼容）    │ <───────────────── │  pgvector        │
    │  · AuroraLambdaVectorStore ──┘                    └──────────────────┘
    └──────────────────────────────┘

mem0 的 LLM 与 embedding 调用留在 Runtime（能出公网访问 OpenAI 兼容端点）；
向量库读写通过 lambda.invoke 走 infra/memory-lambda/handler.py（Aurora VPC 无
IGW/NAT，Runtime 也不挂 VPC——这是两侧网络约束下唯一的拆法，且中国区可原样
移植）。AuroraLambdaVectorStore 把 mem0 VectorStoreBase 的每个方法转成一次
Lambda RPC，SQL 语义由 Lambda 侧忠实复刻 mem0 自带 pgvector 实现。

对 main.py 暴露三个入口，全部**永不抛异常**（记忆是增强功能，任何故障都
只降级不阻断对话）：
    recall(user_id, query)            -> list[str]   语义检索该用户长期记忆
    memorize(user_id, prompt, reply)  -> None        本轮对话交 mem0 提取写回
    enabled()                         -> bool        当前进程记忆功能是否可用

配置全部走环境变量（deploy.sh 透传；测试环境端点/key 不落仓库）：
    MEMORY_LAMBDA_NAME        向量 Lambda 函数名（缺省 agentcore-workshop-memory）
    MEM0_COLLECTION           pgvector 表名（缺省 mem0_ltm）
    MEM0_LLM_MODEL / MEM0_LLM_BASE_URL / MEM0_LLM_API_KEY      事实提取 LLM（必填）
    MEM0_EMBEDDER_MODEL       embedding 模型（必填）
    MEM0_EMBEDDER_BASE_URL / MEM0_EMBEDDER_API_KEY             缺省沿用 LLM 的
    MEM0_EMBEDDER_DIMS        向量维度（缺省 1024，须与表结构一致）
    MEM0_EMBEDDER_PASS_DIMS   =true 时把 dimensions 参数发给 embedding API
                              （仅 matryoshka 系模型支持，缺省不发）
    MEM0_LLM_REASONING        缺省 true：按推理模型对待，不发采样参数
                              （Claude Opus 4.x 等只接受默认采样值）；
                              =false 时发 MEM0_LLM_TEMPERATURE（缺省 1.0）
    MEM0_SEARCH_TIMEOUT / MEM0_ADD_TIMEOUT   检索/写回预算秒数（缺省 3 / 20）

注意：mem0 的 openai provider 会 fallback 读 OPENAI_API_KEY / OPENAI_BASE_URL
环境变量，而这两个在本 Runtime 里已被主模型（bedrock-mantle）占用——因此
下面的配置全部显式传 api_key + openai_base_url，绝不依赖 fallback。
"""

import asyncio
import inspect
import json
import logging
import os
import sys
import threading
import types
from dataclasses import dataclass

# ---- 必须在 import mem0 之前：关遥测、把 SQLite 历史库指到可写目录 ----
os.environ.setdefault("MEM0_TELEMETRY", "false")
os.environ.setdefault("MEM0_DIR", "/tmp/.mem0")

logger = logging.getLogger(__name__)


def _env_num(name: str, default, cast):
    """数值型环境变量的守护解析：畸形值回退默认并告警。

    这些解析在模块 import 时执行（main.py 顶层 import 本模块），裸 int()/
    float() 遇到 '1,536' 这类坏值会让整个 Agent 起不来——违反"记忆故障只
    降级"的承诺，必须兜底。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except (ValueError, TypeError):
        logger.warning("[mem0] 环境变量 %s=%r 非法，回退默认 %s", name, raw, default)
        return default


REGION = os.getenv("AWS_REGION", "us-west-2")
MEMORY_LAMBDA_NAME = os.getenv("MEMORY_LAMBDA_NAME", "agentcore-workshop-memory")
MEM0_COLLECTION = os.getenv("MEM0_COLLECTION", "mem0_ltm")

MEM0_LLM_MODEL = os.getenv("MEM0_LLM_MODEL", "")
MEM0_LLM_BASE_URL = os.getenv("MEM0_LLM_BASE_URL", "")
MEM0_LLM_API_KEY = os.getenv("MEM0_LLM_API_KEY", "")
MEM0_EMBEDDER_MODEL = os.getenv("MEM0_EMBEDDER_MODEL", "")
MEM0_EMBEDDER_BASE_URL = os.getenv("MEM0_EMBEDDER_BASE_URL", "") or MEM0_LLM_BASE_URL
MEM0_EMBEDDER_API_KEY = os.getenv("MEM0_EMBEDDER_API_KEY", "") or MEM0_LLM_API_KEY
MEM0_EMBEDDER_DIMS = _env_num("MEM0_EMBEDDER_DIMS", 1024, int)
MEM0_EMBEDDER_PASS_DIMS = os.getenv("MEM0_EMBEDDER_PASS_DIMS", "").lower() == "true"

SEARCH_TIMEOUT_S = _env_num("MEM0_SEARCH_TIMEOUT", 3.0, float)
# 正常一次 add = 2 次 LLM 调用 + embedding + 数次 Lambda RPC，典型 7~12s；
# 20s 留足余量。这个预算决定 tracked async task 和专用线程的占用上限，不宜放大
ADD_TIMEOUT_S = _env_num("MEM0_ADD_TIMEOUT", 20.0, float)
RECALL_LIMIT = 8
# memorize 连续失败熔断阈值：达到即进程级禁用（microVM 空闲 ~15 分钟会被
# 回收重建，"进程级"不是永久；避免端点持续故障时每轮都白付整个预算）
_ADD_FAIL_LIMIT = 3

# 进程级状态：初始化失败一次即永久降级（绝不反复重试拖慢请求）
_memory = None
_disabled_reason: str | None = None
_lock = threading.Lock()
_add_fail_streak = 0

# 记忆专用线程池：recall/memorize 的底层调用（OpenAI 兼容端点、lambda.invoke）
# 即便被 wait_for 放弃，线程仍会跑到自身超时为止——用独立小池把这些"尾巴"
# 与 asyncio 默认执行器隔离，绝不拖累 main.py 的 _load_messages/_persist_new
# 等同步操作；在途任务打满时直接跳过本次记忆操作（best-effort）
from concurrent.futures import ThreadPoolExecutor

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem0")
_inflight = 0
_inflight_lock = threading.Lock()


async def _run_bounded(fn, *args):
    """在专用池里跑同步函数；池满（在途 >= 2）抛 RuntimeError 由调用方降级。"""
    global _inflight
    with _inflight_lock:
        if _inflight >= 2:
            raise RuntimeError("记忆线程池饱和，跳过本次操作")
        _inflight += 1

    def _wrapped():
        global _inflight
        try:
            return fn(*args)
        finally:
            with _inflight_lock:
                _inflight -= 1

    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, _wrapped)


@dataclass
class OutputData:
    """mem0 全程只按 .id / .score / .payload 属性访问检索结果。"""

    id: str | None
    score: float | None
    payload: dict | None


def _missing_config() -> str | None:
    required = {
        "MEM0_LLM_MODEL": MEM0_LLM_MODEL,
        "MEM0_LLM_BASE_URL": MEM0_LLM_BASE_URL,
        "MEM0_LLM_API_KEY": MEM0_LLM_API_KEY,
        "MEM0_EMBEDDER_MODEL": MEM0_EMBEDDER_MODEL,
    }
    missing = [k for k, v in required.items() if not v]
    return ("缺少环境变量: " + ", ".join(missing)) if missing else None


# ---- mem0 相关定义整体放进 try：mem0ai 未安装/不兼容时模块仍可 import，
#      main.py 不受影响，记忆功能进程级禁用 ----
try:
    import boto3
    from botocore.config import Config as _BotoConfig
    from pydantic import BaseModel

    from mem0 import Memory
    from mem0.utils.factory import VectorStoreFactory
    from mem0.vector_stores.base import VectorStoreBase
    from mem0.vector_stores.configs import VectorStoreConfig

    class AuroraLambdaConfig(BaseModel):
        """自定义向量后端的配置模型（VectorStoreConfig validator 实例化）。

        字段即 AuroraLambdaVectorStore.__init__ 的 kwargs（工厂按 model_dump
        展开）。collection_name 保持可变：mem0 的 entity_store 懒加载会深拷贝
        本配置并改写成 <collection>_entities 再走一次工厂。
        """

        collection_name: str = "mem0_ltm"
        embedding_model_dims: int = 1024
        function_name: str = "agentcore-workshop-memory"
        region: str = REGION  # 单点取自 AWS_REGION，不留第二份硬编码默认

    # 共享 lambda client：Runtime 与 Lambda 走 AWS 内网 API，禁重试
    # （对齐 Part 1 aurora_tools.py：重试只会放大尾延迟；insert 有 UUID
    # 主键天然幂等，但 mem0 决策流程串行，宁可快速失败走降级）。
    # read_timeout 120s < Lambda timeout 300s：数据面长时间不可用时放弃本次
    # 记忆操作（Agent 侧还有更短的 wait_for 预算）。
    _lambda_client = None

    def _get_lambda_client():
        global _lambda_client
        if _lambda_client is None:
            _lambda_client = boto3.client(
                "lambda",
                region_name=REGION,
                config=_BotoConfig(
                    connect_timeout=10, read_timeout=120, retries={"max_attempts": 0}
                ),
            )
        return _lambda_client

    class AuroraLambdaVectorStore(VectorStoreBase):
        """mem0 VectorStoreBase → 记忆向量 Lambda 的 RPC 适配器。

        协议与返回值形状忠实对应 mem0 自带 pgvector 实现（SQL 在 Lambda 侧），
        保证 mem0 Memory 的检索与去重行为不因换传输层而变化。
        """

        def __init__(self, collection_name, embedding_model_dims, function_name, region):
            self.collection_name = collection_name
            self.embedding_model_dims = int(embedding_model_dims)
            self.function_name = function_name
            self.region = region

        def _rpc(self, op: str, **args):
            payload = {
                "op": op,
                "args": {
                    "collection": self.collection_name,
                    "dims": self.embedding_model_dims,
                    **args,
                },
            }
            resp = _get_lambda_client().invoke(
                FunctionName=self.function_name,
                Payload=json.dumps(payload).encode("utf-8"),
            )
            body = json.loads(resp["Payload"].read())
            if resp.get("FunctionError") or not isinstance(body, dict) or body.get("status") != "ok":
                raise RuntimeError(f"memory lambda {op} 失败: {body}")
            return body.get("result")

        @staticmethod
        def _row(r) -> OutputData:
            return OutputData(id=r["id"], score=r.get("score"), payload=r.get("payload"))

        # -- VectorStoreBase 接口 --

        def create_col(self, name=None, vector_size=None, distance=None):
            # pgvector 语义：忽略入参，按自身 collection/dims 幂等建表建索引
            self._rpc("create_col")

        def insert(self, vectors, payloads=None, ids=None):
            items = [
                {"id": str(i), "vector": v, "payload": p or {}}
                for i, v, p in zip(ids, vectors, payloads or [{}] * len(vectors))
            ]
            self._rpc("insert", items=items)

        def search(self, query, vectors, top_k=5, filters=None):
            rows = self._rpc("search", vector=vectors, top_k=top_k, filters=filters or {})
            return [self._row(r) for r in rows]

        def keyword_search(self, query, top_k=5, filters=None):
            # 返回 None = 告诉 mem0 关键词检索不可用，回退纯语义（绝不抛异常）
            try:
                rows = self._rpc("keyword_search", query=query, top_k=top_k, filters=filters or {})
            except Exception:
                return None
            if rows is None:
                return None
            return [self._row(r) for r in rows]

        def get(self, vector_id):
            row = self._rpc("get", id=str(vector_id))
            return self._row(row) if row else None

        def update(self, vector_id, vector=None, payload=None):
            self._rpc("update", id=str(vector_id), vector=vector, payload=payload)

        def delete(self, vector_id):
            self._rpc("delete", id=str(vector_id))

        def list(self, filters=None, top_k=100):
            rows = self._rpc("list", filters=filters or {}, top_k=top_k)
            # pgvector.list 返回嵌套 list，mem0 靠这个形状做兼容解包
            return [[self._row(r) for r in rows]]

        def list_cols(self):
            return self._rpc("list_cols")

        def delete_col(self):
            self._rpc("delete_col")

        def col_info(self):
            return self._rpc("col_info")

        def reset(self):
            self._rpc("reset")

    # ---- 把自定义后端注册进 mem0 的两级 provider 注册表 ----
    # 1) 工厂：provider 名 → 实现类 import 路径
    VectorStoreFactory.provider_to_class["aurora_lambda"] = (
        "memory_backend.AuroraLambdaVectorStore"
    )
    # 2) 配置校验：provider 名 → 配置类名（pydantic v2 私有属性默认值），
    #    校验器用 __import__ 加载 mem0.configs.vector_stores.<provider>——
    #    __import__ 先查 sys.modules，注入一个虚拟模块即可通过
    VectorStoreConfig.__private_attributes__["_provider_configs"].default[
        "aurora_lambda"
    ] = "AuroraLambdaConfig"
    _cfg_mod = types.ModuleType("mem0.configs.vector_stores.aurora_lambda")
    _cfg_mod.AuroraLambdaConfig = AuroraLambdaConfig
    sys.modules["mem0.configs.vector_stores.aurora_lambda"] = _cfg_mod

except Exception as e:  # mem0ai 未安装 / 版本不兼容
    _disabled_reason = f"mem0 导入或注册失败: {type(e).__name__}: {e}"
    logger.warning("[mem0] %s，长期记忆禁用", _disabled_reason)


# 提取范围约束。mem0 2.0.x 走 V3「只加不改」流水线，内置提取 prompt
# （ADDITIVE_EXTRACTION_PROMPT）要求同时从 user 与 assistant 消息尽量多提取、
# 「拿不准就提取」——在本门户表现为每个业务提问（"看下选品漏斗"）都写成一条
# 英文记忆，助手给的建议也进记忆（实测）。本段经配置键 custom_instructions 注入：
# mem0 把它追加在 user prompt 末尾的 "## Custom Instructions" 段，内置 prompt 声明
# 该段优先级最高。三条硬约束：
#   - 键名必须是 custom_instructions：MemoryConfig 静默忽略未知字段，写错不报错
#     （曾误用 custom_fact_extraction_prompt，规则从未生效），_get_memory 有自检；
#   - 示例与说明必须沿用 mem0 的输出结构 {"memory": [{"id", "text"}]}：mem0 按
#     .get("memory") 解析，换成别的键会导致一条都存不进；
#   - mem0 的输入语言开关（use_input_language）写入路径不开，中文记录全靠本段要求。
MEMORY_EXTRACTION_RULES = """以下规则针对「跨境电商选品分析门户」，优先级高于上文全部通用指令；与通用指令冲突时（包括「同时从 user 和 assistant 消息提取」「拿不准就提取」），一律以本节为准。

【只从用户消息提取】
只依据 New Messages 中 role 为 user 的消息提取。assistant 消息只用来理解用户在说什么，其中的建议、数据、结论、汇率、口径一律不提取——除非用户在本轮明确确认并要求以后沿用（如「好，以后都按这个汇率」）。

【只记跨会话长期成立的内容】只提取以下四类：
1. 分析偏好：币种与汇率口径、关注的站点或市场、月销/价格等门槛、结论的呈现方式（如注明数据来源）
2. 身份与职责：岗位、负责的品类或业务线
3. 长期规则：用户要求今后一直遵守的约定（黑名单、合规口径、输出风格）
4. 用户明确要求「记住」的内容

【一律不提取，输出 {"memory": []}】
- 一次性的业务提问、查询或报表请求（如「看下最近30天的选品漏斗报表」「候选池里产品点最高的5个商品是什么」「第一名为什么判 PASS」）。提问本身不是偏好，也不要据此推断用户的兴趣或意图
- 查询结果、数据、商品信息、分析结论
- 只对本轮生效的要求（如「这次用表格展示」；用户说「以后都」才算长期规则）
- 问候、闲聊、对系统的评价

【写法】
- 用用户消息的语言书写：用户说中文就写中文，不要翻译成英文，不要用「User …」作主语
- 每条一句话，简洁、可脱离对话理解，以「用户」开头；不写日期
- 输出结构保持不变：{"memory": [{"id": "0", "text": "…"}]}

【示例】
New Messages: [{"role": "user", "content": "看下最近30天的选品漏斗报表"}, {"role": "assistant", "content": "最近30天共处理 42 个商品，其中采集 6 个……"}]
Output: {"memory": []}

New Messages: [{"role": "user", "content": "记住我的分析偏好：金额一律换算成人民币，我只关注英国站"}, {"role": "assistant", "content": "好的，今后金额按 £1≈¥9.2 换算成人民币，只看英国站数据。"}]
Output: {"memory": [{"id": "0", "text": "用户要求报告中的金额一律换算成人民币"}, {"id": "1", "text": "用户只关注英国站（UK）的数据"}]}
（汇率 £1≈¥9.2 是助手提出的、用户没有确认，不提取）

New Messages: [{"role": "user", "content": "英镑兑人民币按多少算合适？"}, {"role": "assistant", "content": "建议按 £1≈¥9.2 的参考汇率换算。"}]
Output: {"memory": []}

New Messages: [{"role": "user", "content": "我是风控合规方向的分析师，以后结论都要注明数据来源"}, {"role": "assistant", "content": "明白。"}]
Output: {"memory": [{"id": "0", "text": "用户是风控合规方向的分析师"}, {"id": "1", "text": "用户要求今后的结论都注明数据来源"}]}"""


def _rules_active(mem) -> bool:
    """提取范围约束是否真的进了 mem0（未来 mem0 改配置名时，静默失效会被这里拦下）。"""
    return getattr(mem, "custom_instructions", None) == MEMORY_EXTRACTION_RULES


def _build_config() -> dict:
    """组装 mem0 配置。LLM 与 embedding 都走 OpenAI 兼容端点（显式传
    api_key/base_url，避开被主模型占用的 OPENAI_* 环境变量 fallback）。"""
    embedder_cfg: dict = {
        "model": MEM0_EMBEDDER_MODEL,
        "openai_base_url": MEM0_EMBEDDER_BASE_URL,
        "api_key": MEM0_EMBEDDER_API_KEY,
    }
    if MEM0_EMBEDDER_PASS_DIMS:
        # 只有 matryoshka 系模型接受 dimensions 参数；表维度始终由
        # MEM0_EMBEDDER_DIMS 决定（vector_store 配置），两者解耦
        embedder_cfg["embedding_dims"] = MEM0_EMBEDDER_DIMS
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": MEM0_LLM_MODEL,
                "openai_base_url": MEM0_LLM_BASE_URL,
                "api_key": MEM0_LLM_API_KEY,
                # 默认按推理模型对待：mem0 会丢弃 temperature/top_p/max_tokens
                # 采样参数——推理系模型（Claude Opus 4.x、o 系等）只接受默认
                # 采样值，传低温/低 top_p 会被端点 400 拒掉；提取任务对采样
                # 参数不敏感，不发最普适。需要温度控制时设
                # MEM0_LLM_REASONING=false 并配 MEM0_LLM_TEMPERATURE
                "is_reasoning_model": os.getenv("MEM0_LLM_REASONING", "true").lower() != "false",
                "temperature": _env_num("MEM0_LLM_TEMPERATURE", 1.0, float),
                "max_tokens": 2000,
            },
        },
        "embedder": {"provider": "openai", "config": embedder_cfg},
        # 收窄提取范围：只沉淀长期偏好/身份/规则，业务问答不进记忆（见常量注释）
        "custom_instructions": MEMORY_EXTRACTION_RULES,
        "vector_store": {
            "provider": "aurora_lambda",
            "config": {
                "collection_name": MEM0_COLLECTION,
                "embedding_model_dims": MEM0_EMBEDDER_DIMS,
                "function_name": MEMORY_LAMBDA_NAME,
                "region": REGION,
            },
        },
        "version": "v1.1",
    }


def _get_memory():
    """带锁懒单例。任何一步失败 → 记录原因并进程级禁用（返回 None）。"""
    global _memory, _disabled_reason
    if _memory is not None:
        return _memory
    if _disabled_reason is not None:
        return None
    with _lock:
        if _memory is not None or _disabled_reason is not None:
            return _memory
        missing = _missing_config()
        if missing:
            _disabled_reason = missing
            logger.warning("[mem0] %s，长期记忆禁用", _disabled_reason)
            return None
        try:
            mem = Memory.from_config(_build_config())
            if not _rules_active(mem):
                # 宁可不记，也不让业务问答与助手内容灌进长期记忆
                _disabled_reason = (
                    "提取范围约束未生效（当前 mem0 版本不认 custom_instructions，"
                    "请核对 requirements.txt 锁定的 mem0ai 版本）"
                )
                logger.warning("[mem0] %s，长期记忆禁用", _disabled_reason)
                return None
            # 收紧 openai client 超时：mem0 用库默认（600s + 重试），挂死的
            # 端点会让被 wait_for 放弃的后台线程占着专用池几十分钟
            import httpx

            mem.llm.client = mem.llm.client.with_options(
                timeout=httpx.Timeout(30.0, connect=5.0), max_retries=1
            )
            mem.embedding_model.client = mem.embedding_model.client.with_options(
                timeout=httpx.Timeout(15.0, connect=5.0), max_retries=1
            )
            # 维度探测：embedding 实际输出与表结构不符时，写入会在 Lambda 侧
            # 逐条失败、记忆静默失效——一次性探测把根因显形并干净禁用
            probe = mem.embedding_model.embed("dims probe")
            if len(probe) != MEM0_EMBEDDER_DIMS:
                _disabled_reason = (
                    f"embedder 实际输出 {len(probe)} 维 ≠ 表结构 {MEM0_EMBEDDER_DIMS} 维"
                    "（改 MEM0_EMBEDDER_DIMS 并换 MEM0_COLLECTION 重建，"
                    "或对 matryoshka 系模型设 MEM0_EMBEDDER_PASS_DIMS=true）"
                )
                logger.warning("[mem0] %s，长期记忆禁用", _disabled_reason)
                return None
            _memory = mem
            logger.info(
                "[mem0] 初始化完成: collection=%s dims=%s lambda=%s llm=%s embedder=%s",
                MEM0_COLLECTION, MEM0_EMBEDDER_DIMS, MEMORY_LAMBDA_NAME,
                MEM0_LLM_MODEL, MEM0_EMBEDDER_MODEL,
            )
        except Exception as e:
            _disabled_reason = f"Memory 初始化失败: {type(e).__name__}: {e}"
            logger.warning("[mem0] %s，长期记忆禁用", _disabled_reason)
            return None
        return _memory


def enabled() -> bool:
    """当前进程记忆功能是否可用（不触发初始化）。"""
    return _disabled_reason is None


def _recall_sync(user_id: str, query: str) -> list[str]:
    mem = _get_memory()
    if mem is None:
        return []
    # mem0 v2 API：search 必须带 filters；条数参数 2.0.17 叫 top_k（旧版叫
    # limit），按签名自适应。threshold=0.0 关掉 mem0 默认 0.1 分数过滤——
    # 跨语言场景（中文提问召回英文事实）得分常低于默认阈值（travel agent 结论）
    params = inspect.signature(mem.search).parameters
    count_kwarg = "top_k" if "top_k" in params else "limit"
    result = mem.search(
        query=query,
        filters={"user_id": user_id},
        threshold=0.0,
        **{count_kwarg: RECALL_LIMIT},
    )
    rows = result.get("results", []) if isinstance(result, dict) else (result or [])
    return [str(r.get("memory", "")).strip() for r in rows if r.get("memory")]


async def recall(user_id: str | None, query: str) -> list[str]:
    """按用户语义检索长期记忆，按相关度排序返回记忆文本。

    预算 MEM0_SEARCH_TIMEOUT 秒（数据面或模型端点超时 → 空结果）；任何异常
    返回 []。调用方（main.py）只在会话首轮调用，结果缓存整个会话。
    """
    if not user_id or _disabled_reason is not None:
        return []
    try:
        memories = await asyncio.wait_for(
            _run_bounded(_recall_sync, user_id, query), timeout=SEARCH_TIMEOUT_S
        )
        logger.info("[mem0] recall user=%s 命中 %d 条", user_id, len(memories))
        return memories
    except asyncio.TimeoutError:
        logger.warning("[mem0] recall 超时（%.0fs，数据面或模型端点瞬时不可用），本会话无记忆注入", SEARCH_TIMEOUT_S)
        return []
    except Exception as e:
        logger.warning("[mem0] recall 失败: %s: %s", type(e).__name__, e)
        return []


def _add_turn(mem, user_id: str, prompt: str, reply: str) -> int:
    """把一轮问答交给 mem0 提取写回，返回新增记忆条数（scripts/memory_extraction_check.py 复用）。"""
    result = mem.add(
        messages=[
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": reply},
        ],
        user_id=user_id,
        infer=True,  # LLM 按 MEMORY_EXTRACTION_RULES 提取；与已有记忆语义等价的不再写入
    )
    rows = result.get("results", []) if isinstance(result, dict) else (result or [])
    return len(rows)


def _memorize_sync(user_id: str, prompt: str, reply: str) -> int:
    mem = _get_memory()
    if mem is None:
        return 0
    return _add_turn(mem, user_id, prompt, reply)


async def memorize(user_id: str | None, prompt: str, reply: str) -> None:
    """把本轮对话交给 mem0 提取写回（只新增；与已有记忆语义等价的由 mem0 跳过）。

    由 main.py 的 AgentCore tracked async task 在历史落库之后 await；该 task
    通过 HealthyBusy 防止响应结束后 microVM 冻结，而 SSE 不等待本函数。预算
    MEM0_ADD_TIMEOUT 秒（默认 20s），超时/异常只打日志；连续失败
    _ADD_FAIL_LIMIT 次触发进程级熔断，停止每轮白付预算。
    """
    global _add_fail_streak, _disabled_reason
    if not user_id or not (prompt and reply):
        return
    if _disabled_reason is not None:
        return
    try:
        events = await asyncio.wait_for(
            _run_bounded(_memorize_sync, user_id, prompt, reply), timeout=ADD_TIMEOUT_S
        )
        _add_fail_streak = 0
        logger.info("[mem0] memorize user=%s 记忆事件 %d 条", user_id, events)
        return
    except asyncio.TimeoutError:
        logger.warning("[mem0] memorize 超时（%.0fs），本轮记忆丢弃", ADD_TIMEOUT_S)
    except Exception as e:
        logger.warning("[mem0] memorize 失败: %s: %s", type(e).__name__, e)
    _add_fail_streak += 1
    if _add_fail_streak >= _ADD_FAIL_LIMIT:
        _disabled_reason = f"memorize 连续失败 {_add_fail_streak} 次，熔断禁用（microVM 重建后自动恢复）"
        logger.warning("[mem0] %s", _disabled_reason)
