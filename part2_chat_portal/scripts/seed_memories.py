#!/usr/bin/env python3
# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""预置演示分析师的 mem0 长期记忆 —— 多分析师记忆对比演示的种子数据。

给 vivian（资深买手）与 marco（风控合规）各写入一组分析偏好事实；
analyst 刻意留白，用于现场演示"对话自动沉淀记忆"的过程。

自包含实现（stdlib + boto3，不依赖 mem0ai 安装）：
  每条事实 → OpenAI 兼容端点取 embedding → lambda.invoke 记忆向量 Lambda
  的 insert op。payload 键形复刻 mem0 2.0.x `Memory._create_memory`
  （data / hash / created_at / updated_at / user_id / text_lemmatized），
  检索路径（Runtime recall 与门户 GET /chat/memories）只依赖 data + user_id
  + created_at。键形约定见 .trellis/spec/backend/mem0-long-term-memory.md。

幂等：写入前按 user_id 列出已有记忆的 hash（md5(data)），重复事实跳过——
mem0 的 add(infer=False) 自身没有去重，幂等必须在这里做。

用法（环境变量与部署时的 MEM0_* 同一套；本机或 jumphost 均可跑）：
    export MEM0_EMBEDDER_MODEL=<embedding 模型名>
    export MEM0_EMBEDDER_BASE_URL=<OpenAI 兼容端点>/v1   # 或复用 MEM0_LLM_BASE_URL
    export MEM0_EMBEDDER_API_KEY=<key>                    # 或复用 MEM0_LLM_API_KEY
    python3 scripts/seed_memories.py            # 种入（重复跑不产生重复行）
    python3 scripts/seed_memories.py --wipe vivian   # 清掉某用户全部记忆后可重种
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

REGION = os.getenv("AWS_REGION", "us-west-2")
MEMORY_LAMBDA_NAME = os.getenv("MEMORY_LAMBDA_NAME", "agentcore-workshop-memory")
MEM0_COLLECTION = os.getenv("MEM0_COLLECTION", "mem0_ltm")
MEM0_EMBEDDER_DIMS = int(os.getenv("MEM0_EMBEDDER_DIMS", "1024"))
EMBEDDER_MODEL = os.getenv("MEM0_EMBEDDER_MODEL", "")
EMBEDDER_BASE_URL = (
    os.getenv("MEM0_EMBEDDER_BASE_URL", "") or os.getenv("MEM0_LLM_BASE_URL", "")
).rstrip("/")
EMBEDDER_API_KEY = os.getenv("MEM0_EMBEDDER_API_KEY", "") or os.getenv("MEM0_LLM_API_KEY", "")

# ---- 人设与偏好事实（中文事实，跨语言召回已验证） ----
PERSONAS: dict[str, list[str]] = {
    # 薇薇安 · 资深买手：效率导向，快速拿结论
    "vivian": [
        "金额一律换算成人民币（¥）展示，不要出现英镑原价",
        "只关注 eBay 英国站（UK）的数据，其他站点一律不看",
        "月销低于 50 的商品直接过滤掉，不要出现在推荐和分析里",
        "回答要结论先行，先给判断再给数据，数据用表格呈现",
        "重点关注高毛利机会，预估售价与竞品差价大的商品优先展示",
    ],
    # 马可 · 风控合规：稳健导向，先排雷再看机会
    "marco": [
        "金额保留英镑（£）原币展示，涉及换算时必须注明所用汇率",
        "分析商品时必须核查认证与材质合规信息（CE、材质说明），缺失要标注风险",
        "产品评分低于 3 分的商品直接排除，不进入任何推荐",
        "每条结论都要注明数据来源（哪张表、什么口径、多少行样本）",
        "对拿不准的判断要保守表述，宁可提示风险也不要过度乐观",
    ],
    # analyst 刻意留白：现场演示"说一句偏好 → 新会话自动生效"的沉淀链路
}

_lambda = boto3.client(
    "lambda", region_name=REGION,
    config=Config(connect_timeout=10, read_timeout=310, retries={"max_attempts": 0}),
)


def _rpc(op: str, **args):
    payload = {"op": op, "args": {"collection": MEM0_COLLECTION, "dims": MEM0_EMBEDDER_DIMS, **args}}
    resp = _lambda.invoke(
        FunctionName=MEMORY_LAMBDA_NAME, Payload=json.dumps(payload).encode("utf-8")
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError") or body.get("status") != "ok":
        raise RuntimeError(f"memory lambda {op} 失败: {body}")
    return body.get("result")


def _embed(text: str) -> list[float]:
    req = urllib.request.Request(
        f"{EMBEDDER_BASE_URL}/embeddings",
        data=json.dumps({"model": EMBEDDER_MODEL, "input": text}).encode("utf-8"),
        headers={"Authorization": f"Bearer {EMBEDDER_API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        vec = json.load(r)["data"][0]["embedding"]
    if len(vec) != MEM0_EMBEDDER_DIMS:
        sys.exit(
            f"❌ embedding 实际 {len(vec)} 维 ≠ MEM0_EMBEDDER_DIMS={MEM0_EMBEDDER_DIMS}"
            "（与表结构不符，检查模型与维度配置）"
        )
    return vec


def _existing_hashes(user_id: str) -> set[str]:
    rows = _rpc("list", filters={"user_id": user_id}, top_k=500)
    return {
        (r.get("payload") or {}).get("hash", "")
        for r in rows
        if (r.get("payload") or {}).get("hash")
    }


def seed(user_id: str, facts: list[str]) -> None:
    have = _existing_hashes(user_id)
    added = skipped = 0
    for fact in facts:
        digest = hashlib.md5(fact.encode("utf-8")).hexdigest()
        if digest in have:
            skipped += 1
            continue
        now = datetime.now(timezone.utc).isoformat()
        _rpc("insert", items=[{
            "id": str(uuid.uuid4()),
            "vector": _embed(fact),
            "payload": {
                "data": fact,
                "hash": digest,
                "created_at": now,
                "updated_at": now,
                "user_id": user_id,
                "text_lemmatized": fact,
            },
        }])
        added += 1
    print(f"  {user_id}: 新增 {added} 条，跳过已存在 {skipped} 条")


def wipe(user_id: str) -> None:
    rows = _rpc("list", filters={"user_id": user_id}, top_k=500)
    for r in rows:
        _rpc("delete", id=r["id"])
    print(f"  {user_id}: 已清除 {len(rows)} 条记忆")


def main() -> None:
    parser = argparse.ArgumentParser(description="预置演示分析师的 mem0 长期记忆")
    parser.add_argument("--wipe", metavar="USER", help="清除该用户全部记忆后退出（可再跑一次种入）")
    args = parser.parse_args()

    if args.wipe:
        wipe(args.wipe)
        return

    missing = [k for k, v in {
        "MEM0_EMBEDDER_MODEL": EMBEDDER_MODEL,
        "MEM0_EMBEDDER_BASE_URL(或 MEM0_LLM_BASE_URL)": EMBEDDER_BASE_URL,
        "MEM0_EMBEDDER_API_KEY(或 MEM0_LLM_API_KEY)": EMBEDDER_API_KEY,
    }.items() if not v]
    if missing:
        sys.exit(f"❌ 缺少环境变量: {', '.join(missing)}")

    print(f"==> 种入演示记忆（collection={MEM0_COLLECTION}, lambda={MEMORY_LAMBDA_NAME}）...")
    for user_id, facts in PERSONAS.items():
        seed(user_id, facts)
    print("✅ 完成。门户分别用 vivian / marco 登录，点侧栏「长期记忆」徽标可见各自条目；")
    print("   analyst 无预置记忆，用于现场演示对话沉淀。")


if __name__ == "__main__":
    main()
