"""Strata 本机测速：输入（prefill / prompt read）与输出（decode / generated）。

数字不靠客户端计时，而是解析引擎自己打在 8083 日志里的那一行：
  strata serve: prompt 8242 tokens = 0 reused + 8242 read in 3253 ms (2533.6 tok/s),
                578 generated in 8976 ms (64.4 tok/s), drafts accepted 173 of 264
所以量到的就是引擎的真实吞吐（含 MTP 投机解码的实际收益）。

用法（栈已在 8083 上跑起来时）：
  .venv\\Scripts\\python.exe H:\\Strata-custom\\bench\\bench_speed.py
可选：
  --reps 3 --max-tokens 256 --prompts 1024,4096,16384,32768 --tag schemeA
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

LOG_DIR = Path(r"H:\Strata-custom\log")
OUT_DIR = Path(r"H:\Strata-custom\bench\results")
BASE = "http://127.0.0.1:8083"
MODEL = "qwen3.8-flash-next-abliterated-iq3_s"

LINE_RE = re.compile(
    r"strata serve: prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms "
    r"\(([\d.]+) tok/s\), (\d+) generated in (\d+) ms \(([\d.]+) tok/s\)"
)

WORDS = (
    "kernel expert arena prefill decode tensor shard cache stream profile router "
    "bandwidth latency throughput pipeline buffer window block quant scale residual "
    "attention embedding logits sampler draft verify window context resident overflow "
    "调度 专家 缓存 分片 推理 带宽 延迟 吞吐 上下文 量化 采样 窗口 显存 内存 通道"
).split()


def make_prompt(target_tokens: int, nonce: str) -> str:
    """按目标长度造文本；开头带 nonce，保证不被前一次的 prompt cache 命中。"""
    rnd = random.Random(nonce)
    parts = [f"[case {nonce}]",
             "Read the following technical log dump and then summarise the three most "
             "important numbers in it, one per line."]
    approx = 0
    # 中文约 1 token/字，英文约 1.3 token/词；宁可多给，实际 token 数以引擎回报为准
    while approx < target_tokens:
        sentence = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(8, 16)))
        parts.append(f"{sentence} #{approx}.")
        approx += len(sentence.split()) * 1.3 + 4
    parts.append("Summarise now.")
    return "\n".join(parts)


def newest_log() -> Path:
    logs = sorted(LOG_DIR.glob("8083_main_*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise SystemExit("找不到 8083_main_*.log，栈起来了吗？")
    return logs[-1]


def wait_for_line(path: Path, offset: int, timeout: float = 30.0):
    """从 offset 之后找第一条 prompt 统计行（可能一次请求打多条，取最后一条）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            chunk = f.read()
        found = LINE_RE.findall(chunk)
        if found:
            return found[-1]
        time.sleep(0.25)
    return None


def gpu_state() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,clocks.sm",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().split(",")
        used, total, util, temp, sm = [x.strip() for x in out[:5]]
        return {"vram_used_mib": int(used), "vram_total_mib": int(total),
                "gpu_util_pct": int(util), "temp_c": int(temp), "sm_clock_mhz": int(sm)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def ram_state() -> dict:
    try:
        ps = (
            "Get-CimInstance Win32_PhysicalMemory | "
            "Select-Object @{n='GB';e={$_.Capacity/1GB}},Speed,ConfiguredClockSpeed,DeviceLocator | "
            "ConvertTo-Json -Compress"
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=40).stdout.strip()
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return {"modules": data}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def run_case(prompt_tokens: int, max_tokens: int, rep: int, log: Path) -> dict:
    nonce = f"{prompt_tokens}-{rep}-{int(time.time())}"
    prompt = make_prompt(prompt_tokens, nonce)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
    }
    offset = log.stat().st_size
    t0 = time.perf_counter()
    r = requests.post(f"{BASE}/v1/chat/completions", json=body, timeout=900)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    usage = r.json().get("usage", {})
    hit = wait_for_line(log, offset)
    rec = {
        "target_prompt_tokens": prompt_tokens,
        "rep": rep,
        "http": r.status_code,
        "wall_s": round(wall, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }
    if hit:
        (ptok, reused, read, pms, ptps, gen, gms, gtps) = hit
        rec.update({
            "engine_prompt_tokens": int(ptok),
            "engine_reused_tokens": int(reused),
            "engine_read_tokens": int(read),
            "prefill_ms": int(pms),
            "prefill_tok_s": float(ptps),
            "decode_tokens": int(gen),
            "decode_ms": int(gms),
            "decode_tok_s": float(gtps),
        })
    else:
        rec["engine_line"] = "MISSING"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--prompts", default="1024,4096,16384,32768")
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    log = newest_log()
    print(f"引擎日志: {log}")
    h = requests.get(f"{BASE}/v1/models", timeout=60)
    print(f"8083 /v1/models: HTTP {h.status_code}")
    h.raise_for_status()

    env = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "tag": args.tag,
        "log": str(log),
        "model_meta": h.json()["data"][0].get("meta"),
        "ram": ram_state(),
        "gpu": gpu_state(),
        "max_tokens": args.max_tokens,
        "reps": args.reps,
    }
    print("RAM:", json.dumps(env["ram"], ensure_ascii=False))
    print("GPU:", json.dumps(env["gpu"], ensure_ascii=False))

    # 预热：第一枪要建 KV/填专家缓存，不计入统计
    print("\n预热请求 ...")
    warm = run_case(512, 64, 0, log)
    print(f"  warm: {warm}")

    rows = []
    for pt in [int(x) for x in args.prompts.split(",")]:
        for rep in range(1, args.reps + 1):
            rec = run_case(pt, args.max_tokens, rep, log)
            rows.append(rec)
            if "prefill_tok_s" in rec:
                print(f"  prompt≈{pt:>6} rep{rep}: 读 {rec['engine_read_tokens']:>6} tok "
                      f"= {rec['prefill_tok_s']:>8.1f} tok/s ({rec['prefill_ms']} ms) | "
                      f"出 {rec['decode_tokens']:>4} tok = {rec['decode_tok_s']:>6.1f} tok/s "
                      f"({rec['decode_ms']} ms) | wall {rec['wall_s']} s")
            else:
                print(f"  prompt≈{pt:>6} rep{rep}: 没抓到引擎统计行 {rec}")

    summary = {}
    for pt in sorted({r["target_prompt_tokens"] for r in rows if "prefill_tok_s" in r}):
        sel = [r for r in rows if r["target_prompt_tokens"] == pt and "prefill_tok_s" in r]
        summary[str(pt)] = {
            "n": len(sel),
            "prefill_tok_s_mean": round(statistics.mean(r["prefill_tok_s"] for r in sel), 1),
            "prefill_tok_s_min": round(min(r["prefill_tok_s"] for r in sel), 1),
            "prefill_tok_s_max": round(max(r["prefill_tok_s"] for r in sel), 1),
            "decode_tok_s_mean": round(statistics.mean(r["decode_tok_s"] for r in sel), 1),
            "decode_tok_s_min": round(min(r["decode_tok_s"] for r in sel), 1),
            "decode_tok_s_max": round(max(r["decode_tok_s"] for r in sel), 1),
            "engine_read_tokens_mean": round(statistics.mean(r["engine_read_tokens"] for r in sel)),
        }
    allsel = [r for r in rows if "prefill_tok_s" in r]
    overall = {
        "prefill_tok_s_mean": round(statistics.mean(r["prefill_tok_s"] for r in allsel), 1),
        "decode_tok_s_mean": round(statistics.mean(r["decode_tok_s"] for r in allsel), 1),
        "decode_tok_s_mean_short_prompts_4k_and_below": round(
            statistics.mean(r["decode_tok_s"] for r in allsel if r["target_prompt_tokens"] <= 4096), 1
        ) if any(r["target_prompt_tokens"] <= 4096 for r in allsel) else None,
        "decode_tok_s_mean_long_prompts_16k_plus": round(
            statistics.mean(r["decode_tok_s"] for r in allsel if r["target_prompt_tokens"] >= 16384), 1
        ) if any(r["target_prompt_tokens"] >= 16384 for r in allsel) else None,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out = OUT_DIR / f"{stamp}-{args.tag}.json"
    out.write_text(json.dumps({"env": env, "rows": rows, "summary": summary,
                               "overall": overall}, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n=== 每档平均 ===")
    for pt, s in summary.items():
        print(f"{pt:>6} tok 输入: prefill {s['prefill_tok_s_mean']:>8.1f} tok/s "
              f"({s['prefill_tok_s_min']}~{s['prefill_tok_s_max']}, n={s['n']}) | "
              f"decode {s['decode_tok_s_mean']:>6.1f} tok/s ({s['decode_tok_s_min']}~{s['decode_tok_s_max']})")
    print("\n=== 总平均 ===")
    print(json.dumps(overall, ensure_ascii=False))
    print(f"\n结果已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
