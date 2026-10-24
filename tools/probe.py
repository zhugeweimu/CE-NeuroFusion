# -*- coding: utf-8 -*-
"""probe.py —— 设备自检：测出这台机器能喂多长、能不能并行

为什么要有它：上下文上限、并行能力、显存余量，每台机器都不一样。
写死数字，到了别的机器上不是浪费就是爆炸（失败表现还常常是"静默截断"，比报错更难发现）。

用法：
    python tools/probe.py                 # 自检并把结果写进 ce\\capacity.json
    python tools/probe.py --yes           # 不改配置，只体检
    python tools/probe.py --write D:\\x\\capacity.json

产出（三样）：
    1) 控制台报告：n_ctx / slot 数 / 显存 / 单请求可容纳字数 / 能否并行
    2) ce\\capacity.json：给 CE 读的机器能力文件
    3) 一句结论：这台机器该并行还是一个个来
"""
import argparse
import concurrent.futures
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "ce", "capacity.json")


def _get(url, timeout=8):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, payload, timeout=180):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe_server(base):
    """llama-server 的上下文与并发槽数。"""
    info = {"ok": False, "n_ctx": None, "slots": None, "model": None}
    try:
        try:
            d = _get(base + "/props")
        except Exception as e:
            # 不是 llama.cpp（Ollama / LM Studio / vLLM）就没有 /props，
            # 退回通用接口：至少把真实模型名和可服务性测出来。
            log("没有 /props（%r），改用 /v1/models 探测" % (e,))
            models = _get(base + "/v1/models")
            ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict)]
            return {"ok": True, "model": (ids[0] if ids else MODEL), "models": ids,
                    "slots": None, "n_ctx": None,
                    "note": "非 llama.cpp：拿不到 n_ctx/slots，请按 conservative 档手填 source_chars"}
        info["ok"] = True
        info["n_ctx"] = (d.get("default_generation_settings") or {}).get("n_ctx")
        info["slots"] = d.get("total_slots")
        info["model"] = d.get("model_path") or d.get("model_alias")
    except Exception as e:
        info["error"] = repr(e)
    return info


def probe_vram():
    """NVIDIA 显存（没有就返回 None，不当作错误）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        total, used, free = [int(x) for x in out.stdout.strip().splitlines()[0].split(",")]
        return {"total_mb": total, "used_mb": used, "free_mb": free}
    except Exception:
        return None


def measure_ratio(base, model, sample_chars=600):
    """粗测「字数 → token」比例：用同一段文字两次请求，比较 usage。"""
    sample = ("记忆与执行是两件事。" * (sample_chars // 11 + 1))[:sample_chars]
    payload = {
        "model": model, "messages": [{"role": "user", "content": sample}],
        "max_tokens": 8, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        d = _post(base + "/v1/chat/completions", payload, timeout=120)
        usage = d.get("usage") or {}
        pt = usage.get("prompt_tokens") or 0
        if pt > 0:
            return sample_chars / pt          # 每 token 多少字
    except Exception:
        pass
    return None


def measure_latency(base, model, n=1):
    payload = {
        "model": model, "messages": [{"role": "user", "content": "只回两个字：收到"}],
        "max_tokens": 8, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.time()
    for _ in range(n):
        _post(base + "/v1/chat/completions", payload, timeout=180)
    return (time.time() - t0) / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llama", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="qwen3")
    ap.add_argument("--write", default=DEFAULT_OUT)
    args = ap.parse_args()

    print("=" * 66)
    print("设备自检  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 66)

    srv = probe_server(args.llama)
    if not srv.get("ok"):
        print("  ✗ 连不上模型服务 %s（先启动 llama-server）" % args.llama)
        print("    ", srv.get("error"))
        return 1
    n_ctx = int(srv.get("n_ctx") or 4096)
    slots = int(srv.get("slots") or 1)
    print("\n[1] 模型服务")
    print("  n_ctx        %d tokens" % n_ctx)
    print("  total_slots  %d  %s" % (slots, "（单槽：并行只会排队）" if slots <= 1 else "（可并行）"))
    print("  model        %s" % srv.get("model"))

    print("\n[2] 显存")
    vram = probe_vram()
    if vram:
        print("  %d MiB 总 / %d 已用 / %d 空闲" % (
            vram["total_mb"], vram["used_mb"], vram["free_mb"]))
    else:
        print("  读不到（不是 NVIDIA 或驱动没装）")
        print("  → 没有 N 卡也能用：忽略并行这一项，只看下面「单请求可容纳原文」那个数，")
        print("    CPU / AMD 照常跑，只是像串行一样一块一块来")

    print("\n[3] 测字数↔token 比例")
    ratio = measure_ratio(args.llama, args.model)
    if ratio:
        print("  约 %.2f 字 / token" % ratio)
    else:
        ratio = 1.2
        print("  测不出来，按保守值 1.20 字/token 估算")

    print("\n[4] 测单次往返耗时")
    lat = measure_latency(args.llama, args.model, 1)
    print("  %.2f 秒 / 次（空跑）" % lat)

    # 单请求能塞多少原文：留 1200 tokens 给系统提示 + 输出
    reserve = int(1200)
    usable_tokens = max(512, n_ctx - reserve)
    safe_source_chars = int(usable_tokens * ratio * 0.85)   # 再打 85 折，防估算偏差
    print("\n[5] 结论")
    print("  单请求可容纳原文  约 %d 字（n_ctx %d - 预留 %d tokens，再乘 0.85）"
          % (safe_source_chars, n_ctx, reserve))

    can_parallel = slots > 1
    max_workers = 1
    why = []
    if not can_parallel:
        why.append("服务只有 1 个 slot")
    if vram and vram["free_mb"] < 1200:
        why.append("显存余量 %d MiB 不够再开一路 KV cache" % vram["free_mb"])
        can_parallel = False
    if can_parallel:
        max_workers = min(slots, 4)
        # 每路 KV cache 大致按显存余量估：留 800 MiB 自用
        if vram:
            by_vram = max(1, (vram["free_mb"] - 800) // 700)
            max_workers = max(1, min(max_workers, by_vram))
        why.append("slot=%d，显存允许 %d 路" % (slots, max_workers))
    if max_workers <= 1:
        print("  并行              不行 —— " + "；".join(why) if why else "  并行 不行")
        print("                      → CE 会一块一块地跑（功能不变，只是慢）")
    else:
        print("  并行              可以，最多 %d 路（%s）" % (max_workers, "；".join(why)))

    cap = {
        "_说明": "本文件由 tools/probe.py 生成，CE 启动时读取。换机器/换模型请重跑自检。",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "llama_url": args.llama,
        "model": args.model,
        "n_ctx": n_ctx,
        "slots": slots,
        "chars_per_token": round(ratio, 2),
        "latency_sec": round(lat, 2),
        "reserve_tokens": reserve,
        "safe_source_chars": safe_source_chars,
        "max_parallel": max_workers,
        "vram": vram,
        "reasons": why,
    }
    try:
        os.makedirs(os.path.dirname(args.write), exist_ok=True)
        with io.open(args.write, "w", encoding="utf-8") as f:
            json.dump(cap, f, ensure_ascii=False, indent=2)
        print("\n  已写入  %s" % args.write)
        print("  （CE 下次启动会按这个文件决定：单请求喂多少字、能不能并行）")
    except Exception as e:
        print("\n  ✗ 写不进去 %s: %r" % (args.write, e))

    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
