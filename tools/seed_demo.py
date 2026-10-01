#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给面板灌入仿真指标，用于本地预览与截图（不联网、不碰真实账号）。

用法：
    python tools/seed_demo.py [--minutes 30] [--tasks 3] [--db data/metrics.sqlite3]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.metrics import MetricsStore  # noqa: E402

TASKS = [
    ("G1234 北京->上海|2026-10-01|北京-上海", "北京->上海 10-01"),
    ("G1234 北京->上海|2026-10-02|北京-上海", "北京->上海 10-02"),
    ("G88 上海->杭州|2026-10-01|上海-杭州", "上海->杭州 10-01"),
]

OUTCOMES = ["no_ticket", "no_ticket", "no_ticket", "no_ticket", "ticket_found", "server_error", "timeout", "risk_control"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--tasks", type=int, default=3)
    parser.add_argument("--rate", type=float, default=1.6, help="平均查询密度系数")
    parser.add_argument("--db", default=str(ROOT / "data" / "metrics.sqlite3"))
    parser.add_argument("--seed", type=int, default=20261001)
    args = parser.parse_args()

    random.seed(args.seed)
    store = MetricsStore(db_path=Path(args.db), flush_interval=0.0)
    now = time.time()
    window = args.minutes * 60.0
    tasks = TASKS[: max(1, args.tasks)]

    total = 0
    moment = now - window
    while moment < now:
        for key, label in tasks:
            # 造一点「整点前后更密集」的形态，贴近真实放票节奏
            hour_boost = 2.2 if (time.localtime(moment).tm_min % 30) < 3 else 1.0
            if random.random() > min(1.0, 0.05 * args.rate * hour_boost):
                continue
            outcome = random.choice(OUTCOMES)
            if random.random() < 0.012:
                outcome = "risk_control"
            duration = max(80.0, random.gauss(420, 180))
            if outcome == "timeout":
                duration = random.uniform(3000, 5200)
            store.record_query(
                key,
                outcome,
                duration,
                station=label.split(" ")[-1],
                travel_date="2026-10-01",
                label=label,
                ts=moment,
            )
            total += 1
        moment += 4.0

    # 造两条熔断事件，让事件表有内容
    for index, (key, label) in enumerate(tasks[:2]):
        at = now - (600 - index * 180)
        store.record_breaker(
            key,
            "risk_control",
            previous_state="closed",
            state="open",
            wait_seconds=30.0 * (2 ** index),
            reason="HTTP 200 + 您的访问过于频繁",
            label=label,
            ts=at,
        )
        store.record_breaker(
            key,
            "system",
            previous_state="half_open",
            state="closed",
            wait_seconds=0.0,
            reason="探针连续成功，熔断解除",
            label=label,
            ts=at + 120,
        )

    for index, (key, label) in enumerate(tasks):
        store.update_task(
            key,
            label=label,
            breaker_state="backoff" if index == 0 else "closed",
            consecutive_failures=3 if index == 0 else 0,
            open_count=2 if index == 0 else 0,
            next_probe_in=12.0 if index == 0 else 0.0,
            soft_risk_score=1.5 if index == 0 else 0.0,
            last_reason="连续 3 次异常：HTTP 503" if index == 0 else "",
        )

    print("已写入 %d 条查询事件到 %s" % (total, args.db))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
