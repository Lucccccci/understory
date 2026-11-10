"""记忆强度 —— 权重从哪来、怎么衰减、怎么淘汰。

## 核心原则：把「找得到」和「重不重要」分开

    检索层（关键词 / IDF）   只决定这一轮能不能翻到
    权重层（模型 + 曲线）     只决定这条值不值得留、要不要先给她看

改之前这两件事是同一个关键词打分在做 ——
于是「今天」这种词既能决定翻到哪条，也间接决定了哪条显得重要。
那是之前犯过的错，这里必须拆开。

## 权重从三个来源

    ① 写入时  模型判 salience（0–3）—— 只有 ≥2 的才进库
    ② 躺着时  指数衰减（纯数学，模型不参与）
    ③ 用到时  hits +1，last_touch 刷新（不猜语义，只记"被检索到"）

## 为什么衰减是机械的

「多重要」是语义问题，交给模型；
「多久没被想起」是时间问题，模型算不如钟算。
让模型去估计衰减，只会得到一堆编出来的数字。

## 半衰期

    fact        30 天   会变（换工作、搬家、养了新猫）
    observation 45 天   比事实稳，但人也会变
    event       90 天   发生过就是发生过
    promise     60 天   兑现前一直在
    ★ 事件      永不衰减
"""
from __future__ import annotations

import math
from datetime import date, datetime

HALF_LIFE = {
    "fact": 30.0,
    "guidance": 60.0,      # 你让她注意的事，忘得慢
    "observation": 45.0,
    "event": 90.0,
    "promise": 60.0,
    "her_event": 120.0,    # 她自己的日子，其实不太重要，衰减慢但权重低
}

# 命中一次的加成（加在衰减后的值上，不是乘）
HIT_BONUS = 0.35
# 最多算几次命中 —— 免得一条老记忆靠次数堆着不下去
HIT_CAP = 6
# 随机抖动幅度：避免所有记忆在同一天整齐地掉下去
JITTER = 0.1


def _days_since(iso: str) -> float:
    if not iso:
        return 999.0
    try:
        t = datetime.fromisoformat(str(iso)[:19])
    except (ValueError, TypeError):
        return 999.0
    return max(0.0, (datetime.now() - t).total_seconds() / 86400.0)


def _jitter(key: str) -> float:
    """按内容做一个稳定的小扰动（同一条每次算出来一样）。"""
    h = abs(hash(key)) % 1000 / 1000.0     # 0–1
    return 1.0 + (h - 0.5) * 2 * JITTER


def strength(kind: str, weight: float, last_touch: str,
             hits: int = 0, immortal: bool = False, key: str = "") -> float:
    """一条记忆现在有多强。

    Args:
        kind: fact / observation / event / promise ...
        weight: 写入时模型给的初始权重（0–3）
        last_touch: 最后一次被用到的时间（ISO）
        hits: 被检索到的次数
        immortal: ★ 事件 —— 不衰减
        key: 用来做稳定抖动的字符串（一般传正文）
    """
    base = max(0.0, float(weight or 0))
    if base <= 0:
        return 0.0
    if immortal:
        return base + min(HIT_CAP, max(0, hits)) * HIT_BONUS

    half = HALF_LIFE.get(kind, 60.0)
    days = _days_since(last_touch)
    decayed = base * math.pow(0.5, days / half)
    return decayed + min(HIT_CAP, max(0, hits)) * HIT_BONUS * _jitter(key or kind)


def rank(items, key_of) -> list:
    """按强度从高到低排。

    key_of(item) -> dict(kind=, weight=, last_touch=, hits=, immortal=, key=)
    """
    def score(item):
        return strength(**key_of(item))
    return sorted(items, key=score, reverse=True)


def evict(items, cap: int, key_of) -> list:
    """超过上限时，掐掉最弱的那些。返回保留的列表（保持原有顺序）。"""
    if len(items) <= cap:
        return list(items)
    scored = [(strength(**key_of(x)), i, x) for i, x in enumerate(items)]
    scored.sort(key=lambda t: t[0], reverse=True)
    keep_idx = {i for _, i, _ in scored[:cap]}
    return [x for i, x in enumerate(items) if i in keep_idx]
