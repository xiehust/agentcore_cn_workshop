#!/usr/bin/env python3
# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""mem0 提取行为在线检查——升级 mem0 版本或改提取规则后先跑这个。

复用 agent/memory_backend.py 的 _build_config()（同一份 LLM/embedding 配置与
MEMORY_EXTRACTION_RULES）和 _add_turn()（同一种调用形态），只把向量库换成 mem0
自带的本地 qdrant——不需要 AWS，只需要模型网关。用真实模型按固定剧本断言：

  - 业务提问（含助手的长回复）不写入记忆；
  - 助手提出、用户没确认的内容（如参考汇率）不写入记忆；
  - 「记住……」的偏好、长期规则、身份以中文写入；
  - 同一偏好再说一遍，不重复写入。

同一用户按顺序跑完整个剧本（mem0 会带上该用户最近的历史消息，顺序跑才贴近真实
会话）；LLM 有随机性，默认跑 3 轮，每轮换新用户，全部通过才算过。

用法（MEM0_* 与部署用的是同一套，可直接 source 仓库根目录的 .env）：
  source ../.env
  python3 scripts/memory_extraction_check.py [--runs 3]
依赖：pip install 'mem0ai==<agent/requirements.txt 锁定的版本>' boto3
"""

import argparse
import os
import re
import statistics
import sys
import tempfile
import time
import uuid

WORKDIR = tempfile.mkdtemp(prefix="mem0-check-")
# 必须在 import memory_backend 之前：让 mem0 的历史库落在本次检查的临时目录
os.environ["MEM0_DIR"] = os.path.join(WORKDIR, "mem0")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import memory_backend as mb  # noqa: E402

FUNNEL_REPLY = (
    "最近 30 天选品漏斗（UK + US）：\n"
    "| 阶段 | 商品数 | 占比 |\n|---|---|---|\n"
    "| 已处理 | 42 | 100% |\n| 排除 | 21 | 50.0% |\n| 补采 | 9 | 21.4% |\n"
    "| PASS | 6 | 14.3% |\n| 采集 | 6 | 14.3% |\n\n"
    "排除原因 Top3：品牌授权风险 8 个、月销不足 7 个、二手/翻新 6 个。"
    "采集的 6 个商品集中在桌面收纳（4 个）与线缆收纳（2 个），平均产品点 7.8。"
    "补采主要因为详情页销量字段缺失，建议下周重跑这 9 个商品。"
)
TOP5_REPLY = (
    "候选池产品点最高的 5 个商品：\n"
    "1. Bamboo Desk Organizer，£18.99，月销约 420，预估售价 £24.99，产品点 8.6\n"
    "2. Cable Management Box，£15.49，月销约 380，预估售价 £19.99，产品点 8.3\n"
    "3. Drawer Divider Set，£12.99，月销约 510，预估售价 £16.99，产品点 8.1\n"
    "4. Monitor Stand Riser，£22.50，月销约 300，预估售价 £29.99，产品点 7.9\n"
    "5. Travel Cable Pouch，£9.99，月销约 640，预估售价 £13.99，产品点 7.7"
)
PASS_REPLY = (
    "第一名 Bamboo Desk Organizer 判 PASS 的原因：月销约 420 超过门槛 300，"
    "卖家集中度低（前三卖家份额 38%），品牌与专利检索未见风险，"
    "按预估售价测算毛利率 41%，满足 SOP 的采集条件。"
)
CATEGORY_REPLY = (
    "候选商品类目分布：桌面收纳 12 个（38%）、线缆收纳 9 个（28%）、"
    "抽屉收纳 6 个（19%）、旅行收纳 5 个（15%）。桌面收纳赛道候选最多，"
    "但平均客单价最低（£14.2）。"
)
SITE_REPLY = (
    "最近 30 天判定通过率：UK 站 18.2%（33 个中 6 个通过），US 站 9.1%（11 个中 1 个通过）。"
    "US 站排除主要因为物流时效与品牌风险。"
)
RATE_REPLY = "建议按 £1≈¥9.2 的参考汇率换算，也可以按当天中国银行中间价。"
PREF_REPLY = "好的，今后报告里的金额都按 £1≈¥9.2 换算成人民币，并且只看英国站的数据。"
RULE_REPLY = "明白，之后的报表会过滤掉月销低于 300 件的商品。"
ID_REPLY = "了解，之后会更关注家居收纳品类的选品数据。"

PREFERENCE = "记住我的分析偏好：金额一律换算成人民币，我只关注英国站"

# (标签, 用户消息, 助手回复, 期望新增条数: 0 = 必须为 0；1 = 至少 1)
SCRIPT = [
    ("业务提问·漏斗报表", "看下最近30天的选品漏斗报表", FUNNEL_REPLY, 0),
    ("业务提问·类目分布", "看下候选商品的类目/赛道分布", CATEGORY_REPLY, 0),
    ("业务提问·Top5", "候选池里产品点最高的 5 个商品是什么？给出价格、月销和预估售价", TOP5_REPLY, 0),
    ("业务追问·PASS 原因", "第一名的商品为什么判 PASS？", PASS_REPLY, 0),
    ("业务提问·站点对比", "帮我对比一下 UK 和 US 两个站点最近的判定通过率", SITE_REPLY, 0),
    ("助手提议·用户未确认", "英镑兑人民币按多少算合适？", RATE_REPLY, 0),
    ("明确记住·分析偏好", PREFERENCE, PREF_REPLY, 1),
    ("重复说·同一偏好", PREFERENCE, PREF_REPLY, 0),
    ("长期规则", "以后月销低于 300 件的商品不用给我看了", RULE_REPLY, 1),
    ("身份职责", "我主要负责家居收纳品类的选品", ID_REPLY, 1),
]

CJK = re.compile(r"[一-鿿]")
# 出现在最终记忆里即视为泄漏：业务提问内容、助手数据、助手提出的汇率
LEAK = re.compile(r"漏斗|类目|赛道|Top ?5|PASS|通过率|产品点|9\.2|Bamboo|Cable Management")


def build_memory():
    from mem0 import Memory

    cfg = mb._build_config()
    cfg["vector_store"] = {
        "provider": "qdrant",
        "config": {
            "collection_name": "extract_check",
            "embedding_model_dims": mb.MEM0_EMBEDDER_DIMS,
            "path": os.path.join(WORKDIR, "qdrant"),
        },
    }
    mem = Memory.from_config(cfg)
    if not mb._rules_active(mem):
        sys.exit("❌ MEMORY_EXTRACTION_RULES 没进 mem0（custom_instructions 未生效），检查 mem0 版本")
    return mem


def run_once(mem, run_no: int, latencies: list) -> list:
    uid = f"check-{uuid.uuid4().hex[:8]}"
    failures = []
    print(f"\n==> 第 {run_no} 轮（用户 {uid}）")
    for label, prompt, reply, expect in SCRIPT:
        t0 = time.monotonic()
        added = mb._add_turn(mem, uid, prompt, reply)
        dt = time.monotonic() - t0
        latencies.append(dt)
        ok = (added == 0) if expect == 0 else (added >= 1)
        print(f"  {'✅' if ok else '❌'} {label}：新增 {added} 条（{dt:.1f}s）")
        if not ok:
            failures.append(f"第 {run_no} 轮 {label}：期望{'0 条' if expect == 0 else '≥1 条'}，实际 {added} 条")

    texts = [r.get("memory", "") for r in mem.get_all(filters={"user_id": uid}).get("results", [])]
    print("  最终记忆：")
    for t in texts:
        print(f"    - {t}")
    for t in texts:
        if not CJK.search(t) or t.lstrip().lower().startswith("user"):
            failures.append(f"第 {run_no} 轮 非中文记忆：{t}")
        if LEAK.search(t):
            failures.append(f"第 {run_no} 轮 业务/助手内容泄漏进记忆：{t}")
    joined = "".join(texts)
    for must in ("人民币", "英国"):
        if must not in joined:
            failures.append(f"第 {run_no} 轮 「记住」的偏好缺少「{must}」")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="mem0 提取行为在线检查")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    from importlib.metadata import version

    from mem0.configs.base import MemoryConfig

    print(f"mem0ai {version('mem0ai')}  llm={mb.MEM0_LLM_MODEL}  embedder={mb.MEM0_EMBEDDER_MODEL}")
    missing = mb._missing_config()
    if missing:
        sys.exit(f"❌ {missing}（先 source .env）")
    dropped = MemoryConfig(**{"custom_fact_extraction_prompt": "x"}).custom_instructions is None
    print(f"未知配置键会被静默忽略: {dropped}（这正是规则曾经从未生效的原因）")

    mem = build_memory()
    failures, latencies = [], []
    for n in range(1, args.runs + 1):
        failures += run_once(mem, n, latencies)

    lat = sorted(latencies)
    p90 = lat[max(0, int(len(lat) * 0.9) - 1)]
    print(f"\nadd 耗时：p50={statistics.median(lat):.1f}s  p90={p90:.1f}s  max={lat[-1]:.1f}s"
          f"（Runtime 写入预算 MEM0_ADD_TIMEOUT 默认 {mb.ADD_TIMEOUT_S:.0f}s）")
    if failures:
        print(f"\n❌ {len(failures)} 项未通过：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"\n✅ {args.runs} 轮全部通过")


if __name__ == "__main__":
    main()
