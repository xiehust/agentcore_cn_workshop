#!/usr/bin/env python3
# Copyright (c) 2026 Amazon.com and Affiliates.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI 兼容模型网关容量验证（活动前演练）：~100 并发，有界请求总数。

三个场景依次执行，每个场景维持 CONCURRENCY 个请求持续在途（闭环：完成一个
补一个），请求总数固定；场景内累计错误率超过 50% 立即停止该场景，避免空打。

  chat-short    流式对话、短提示：网关并发连接 / RPM 上限
  chat-context  流式对话、~CONTEXT_TOKENS 上下文：贴近 Agent 真实请求的 TPM 压力
                （每个请求带唯一前缀，不吃前缀缓存——按最坏情况测）
  embedding     embedding 请求：mem0 写入/检索

用法（变量名与 workshop .env 一致，可直接 source .env 后运行）：
  OPENAI_BASE_URL=<网关>/v1 MODEL_ID=<对话模型> MEM0_EMBEDDER_MODEL=<embedding 模型> \\
    python3 llm_loadtest.py < key.txt        # key 取 OPENAI_API_KEY，缺省读 stdin 第一行
可选环境变量：
  CONCURRENCY（100）  CHAT_SHORT_REQUESTS（100）  CHAT_CONTEXT_REQUESTS（200）
  EMBED_REQUESTS（500）  CONTEXT_TOKENS（6000）  MAX_TOKENS（256）
"""

import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
CHAT_MODEL = os.environ["MODEL_ID"]
EMB_MODEL = os.environ.get("MEM0_EMBEDDER_MODEL", "")
KEY_SOURCE = "env OPENAI_API_KEY" if os.environ.get("OPENAI_API_KEY") else "stdin"
KEY = os.environ.get("OPENAI_API_KEY") or sys.stdin.readline().strip()
CONC = int(os.environ.get("CONCURRENCY", "100"))
N_SHORT = int(os.environ.get("CHAT_SHORT_REQUESTS", "100"))
N_CONTEXT = int(os.environ.get("CHAT_CONTEXT_REQUESTS", "200"))
N_EMBED = int(os.environ.get("EMBED_REQUESTS", "500"))
CONTEXT_TOKENS = int(os.environ.get("CONTEXT_TOKENS", "6000"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "256"))
STOP_ERROR_RATE = 0.5

if not KEY:
    sys.exit("❌ 缺少 key：设 OPENAI_API_KEY 或从 stdin 传入")


def _post(path: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _fail(t0: float, status, err: str) -> dict:
    return {"ok": False, "status": str(status), "err": err[:200], "lat": time.monotonic() - t0}


def chat_once(prompt: str) -> dict:
    t0 = time.monotonic()
    ttft = None
    usage = None
    chunks = 0
    payload = {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    try:
        with _post("/chat/completions", payload, timeout=300) as resp:
            for raw in resp:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if ev.get("error"):
                    return _fail(t0, "stream-error", json.dumps(ev["error"], ensure_ascii=False))
                if ev.get("usage"):
                    usage = ev["usage"]
                for choice in ev.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content") or delta.get("reasoning_content"):
                        chunks += 1
                        if ttft is None:
                            ttft = time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return _fail(t0, e.code, e.read().decode("utf-8", "ignore"))
    except Exception as e:  # 连接/超时/TLS
        return _fail(t0, type(e).__name__, str(e))
    if chunks == 0:
        return _fail(t0, "empty-stream", "200 但无任何内容增量")
    return {"ok": True, "status": "200", "lat": time.monotonic() - t0, "ttft": ttft, "usage": usage}


def embed_once(text: str) -> dict:
    t0 = time.monotonic()
    try:
        with _post("/embeddings", {"model": EMB_MODEL, "input": text}, timeout=60) as resp:
            body = json.load(resp)
        dims = len(body["data"][0]["embedding"])
    except urllib.error.HTTPError as e:
        return _fail(t0, e.code, e.read().decode("utf-8", "ignore"))
    except Exception as e:
        return _fail(t0, type(e).__name__, str(e))
    if dims == 0:
        return _fail(t0, "empty-embedding", "向量为空")
    return {"ok": True, "status": "200", "lat": time.monotonic() - t0, "usage": body.get("usage")}


def _pct(values: list, p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def run_phase(name: str, fn, inputs: list) -> None:
    """闭环维持 CONC 个在途请求；累计错误率超阈值后剩余请求直接跳过。"""
    if not inputs:
        return
    lock = threading.Lock()
    state = {"done": 0, "err": 0, "stop": False}

    def task(x):
        if state["stop"]:
            return None
        r = fn(x)
        with lock:
            state["done"] += 1
            state["err"] += 0 if r["ok"] else 1
            if state["done"] >= CONC and state["err"] / state["done"] > STOP_ERROR_RATE:
                state["stop"] = True
        return r

    print(f"\n==> [{name}] 并发 {CONC}，请求 {len(inputs)}", flush=True)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=CONC) as pool:
        results = [r for r in pool.map(task, inputs) if r is not None]
    wall = time.monotonic() - t0

    ok = [r for r in results if r["ok"]]
    lat = [r["lat"] for r in ok]
    ttft = [r["ttft"] for r in ok if r.get("ttft") is not None]
    p_tok = sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in ok)
    c_tok = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in ok)
    errors = Counter(r["status"] for r in results if not r["ok"])

    print(f"### {name}")
    print(f"完成 {len(results)}/{len(inputs)}  成功 {len(ok)}  失败 {len(results) - len(ok)}"
          + ("  ⚠️ 错误率超阈值已提前停止" if state["stop"] else ""))
    if errors:
        print(f"失败分布 {dict(errors)}")
        sample = next(r for r in results if not r["ok"])
        print(f"失败样例 status={sample['status']} err={sample['err']}")
    print(f"墙钟 {wall:.1f}s  吞吐 {len(ok) / wall:.2f} req/s")
    if lat:
        print(f"总耗时 p50={_pct(lat, 50):.2f}s p90={_pct(lat, 90):.2f}s "
              f"p99={_pct(lat, 99):.2f}s max={max(lat):.2f}s")
    if ttft:
        print(f"首包   p50={_pct(ttft, 50):.2f}s p90={_pct(ttft, 90):.2f}s "
              f"p99={_pct(ttft, 99):.2f}s max={max(ttft):.2f}s")
    if p_tok or c_tok:
        print(f"token  输入 {p_tok}  输出 {c_tok}  "
              f"折合 {p_tok / wall * 60:,.0f} 输入TPM / {c_tok / wall * 60:,.0f} 输出TPM")


def _context_prompt(i: int) -> str:
    # 每行实测约 32 token；唯一前缀放最前，让每个请求都无法命中前缀缓存
    rows = max(1, CONTEXT_TOKENS // 32)
    lines = [f"[req-{i}-{time.time_ns()}] 以下是选品候选数据表（UK 站）："]
    for k in range(rows):
        lines.append(f"SKU-{k:04d} | desk organizer variant {k % 37} | £{9 + (k * 7) % 41}.99 "
                     f"| monthly sales {120 + (k * 13) % 900} | seller rating {90 + k % 10}%")
    lines.append("基于以上数据，用两句话总结价格分布与月销集中区间。")
    return "\n".join(lines)


def main() -> None:
    print(f"目标 {BASE}  对话模型 {CHAT_MODEL}  embedding 模型 {EMB_MODEL or '(未配置，跳过)'}")
    # 只露来源与长度：压测机上残留的同名环境变量（如占位值 EMPTY）会静默顶掉真 key
    print(f"key 来源={KEY_SOURCE} 长度={len(KEY)}")
    pre = chat_once("ping")
    if not pre["ok"]:
        sys.exit(f"❌ 预检请求失败，不开压：status={pre['status']} err={pre['err']}")
    print(f"预检通过（{pre['lat']:.2f}s）")
    print(f"START {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    run_phase("chat-short", chat_once,
              [f"[{i}] 用一句话介绍跨境电商选品的一个要点。" for i in range(N_SHORT)])
    run_phase("chat-context", chat_once, [_context_prompt(i) for i in range(N_CONTEXT)])
    if EMB_MODEL:
        run_phase("embedding", embed_once,
                  [f"[{i}] 分析偏好：金额一律换算成人民币，只关注英国站，月销门槛 300 件" for i in range(N_EMBED)])
    print(f"\nEND {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")


if __name__ == "__main__":
    main()
