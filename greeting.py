#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开场问候语：按关系阶段与离别时长挑选。

## 为什么需要这个

原本开场白是写死的「你就是这片农场的新主人？我是她」，
结果**交往 9 心的人打开窗口，她还像第一次见面** —— 和人设自相矛盾。

问候语是最先看到的一句话，它决定了整场对话的基调。

## 两条设计规则

1. **阶段化**：问候语必须承接当前关系。低阶段有距离感（不是冷淡，是不熟），
   高阶段的亲密体现在具体小事上（灶上温着粥），而不是称呼变甜。

2. **一天只挑一次，并且存下来**：如果每次刷新页面都重挑，
   问候语会变来变去，反而露馅。所以选定后写进存档，
   只在「新的一天」或「关系阶段变了」时才重挑。

## 久别问候

隔了几天没来，她会先提这件事。
这比什么都更能体现「时间在流动」——她过着自己的日子，也在数你离开的日子。
"""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
GREETINGS_FILE = HERE / "greetings.json"


def load_greetings(path: Path = GREETINGS_FILE) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pick(options: list[str], seed: str) -> str:
    """用种子挑选，保证同一条件下结果稳定。

    用 seed（而不是真随机）是为了：同一天刷新多次，问候语不变。
    """
    if not options:
        return ""
    rng = random.Random(seed)
    return rng.choice(options)


def build_greeting(relation_key: str, call_name: str, days_since_last: int | None,
                   greeting_day: str | None = None, data: dict | None = None) -> str:
    """组装问候语。

    Args:
        relation_key: 关系阶段键，如 hearts_4_5 / married
        call_name: 她怎么称呼你
        days_since_last: 距上次说话多少天；None 表示第一次
        greeting_day: 随机种子用的日期字符串（YYYY-MM-DD）。
                      **不传则用今天** —— 这样问候语会随日子轮换。
        data: 已加载的 greetings.json；不传则自己读
    """
    if data is None:
        data = load_greetings()

    # 种子必须包含真实日期，否则问候语永远不变
    day_key = greeting_day or date.today().isoformat()

    name = call_name or "你"

    # 阶段问候语
    stage_lines = data.get(relation_key) or data.get("hearts_0_1") or []
    if not isinstance(stage_lines, list):
        stage_lines = []
    stage_text = _pick([str(x) for x in stage_lines],
                       f"{day_key}|{relation_key}|stage").replace("{name}", name)

    # 久别追加句（按阶段分开写，见 greetings.json 的说明）
    tail = ""
    if days_since_last is not None and days_since_last >= 3:
        per_stage = (data.get("absence") or {}).get(relation_key) or []
        for entry in per_stage:
            try:
                need = int(entry.get("min_days", 0))
            except (TypeError, ValueError):
                continue
            if days_since_last >= need:
                lines = [str(x) for x in (entry.get("lines") or [])]
                if lines:
                    tail = _pick(lines, f"{day_key}|{days_since_last}|absence")
                    tail = tail.replace("{days}", str(days_since_last)).replace("{name}", name)
                break

    if tail:
        return f"{stage_text}\n{tail}"
    return stage_text


def days_between(last_iso: str, today: date | None = None) -> int | None:
    """两个 ISO 时间相差多少天。解析失败返回 None。"""
    if not last_iso:
        return None
    try:
        last = date.fromisoformat(last_iso[:10])
    except (ValueError, TypeError):
        return None
    return ((today or date.today()) - last).days


if __name__ == "__main__":
    print("=" * 68)
    print("各关系阶段的问候语（同一天结果稳定）")
    print("=" * 68)
    for key, cn in [("hearts_0_1", "初识"), ("hearts_2_3", "渐渐熟悉"),
                    ("hearts_4_5", "朋友"), ("hearts_6_7", "心里有你"),
                    ("hearts_8_plus", "情根深种"), ("dating", "交往"),
                    ("engaged", "订婚"), ("married", "成婚")]:
        g = build_greeting(key, "阿明", None, "2026-10-12")
        print(f"\n【{cn}】")
        print("  " + g.replace("\n", "\n  "))

    print()
    print("=" * 68)
    print("久别问候（交往阶段，隔了 9 天）")
    print("=" * 68)
    print("  " + build_greeting("dating", "阿明", 9, "2026-10-12").replace("\n", "\n  "))
