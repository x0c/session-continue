#!/usr/bin/env python3
"""pickup 可复现性能基准；只输出耗时和数量，不输出任何会话正文。"""

from __future__ import annotations

import argparse
import json
import statistics
import time


def _measure(fn, rounds: int) -> dict:
    values = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        values.append((time.perf_counter() - started) * 1000)
    ordered = sorted(values)
    return {
        "rounds": rounds,
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max_ms": round(max(values), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 pickup 本地性能基准（不输出会话内容）。")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    from pickup import embed
    from pickup.native import available
    from pickup.runtime import default_registry

    frame_line = "\x1b[38;2;90;180;255m" + ("中文🙂 pickup performance " * 10) + "\x1b[0m"
    frame = "\n".join(frame_line for _ in range(61))
    registry = default_registry()
    last_count = 0

    def scan():
        nonlocal last_count
        result = registry.scan_all(args.limit)
        last_count = sum(len(bucket) for bucket in result.values())

    output = {
        "native": available(),
        "scan_all": _measure(scan, max(3, args.rounds // 4)),
        "session_count": last_count,
        "ansi_frame_174x61": _measure(
            lambda: embed.parse_screen_rows(frame, 174, 61), args.rounds,
        ),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
