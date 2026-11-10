"""她自己的过去 —— 每天攒一条「昨天她那边发生的事」。

## 为什么

她的 `today_task` 是「今天此刻在干什么」，一天一换、不留痕。
于是你三天没来，她那边**过去了三天，却一个字都说不出来** ——
只会说「你回来了」。

`events` 是「你们之间发生过的」，`memories` 是关于你的。
**没有一类东西是「她自己的日子」。** 这个模块补的就是这个缺口。

## 怎么生成

    主路   后台调一次模型（1 天 1 次）
    兜底   语料池 her_day_pool.json（按季节分池）

## 为什么必须是昨天

人是这样说话的 ——「昨天那场雨把棚子掀了一角」。
当下的事已经有 `today_task` 了，再重复一遍没意义。

## 为什么必须异步

`refresh_day()` 在聊天路径上。同步调模型会把当天第一句话拖慢三秒。
"""
from __future__ import annotations

import mind_llm
import json
import random
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CORPUS = Path(__file__).resolve().parent / "tools" / "corpus" / "her_day_pool.json"

MAX_HER_EVENTS = 30          # 约一个月
MAX_CHARS = 34

SEASONS = ("春", "夏", "秋", "冬")


# ══════════════════════════════════════════════════════════
# 语料
# ══════════════════════════════════════════════════════════
def _pool() -> dict:
    try:
        d = json.loads(CORPUS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def pick_from_corpus(season: str, existing: list[str], seed: str = "") -> str:
    """按季节抽一条，跳过已有的。"""
    d = _pool()
    pool = list(d.get(season) or []) + list(d.get("常") or [])
    if not pool:
        return ""
    have = set(existing or [])
    pool = [x for x in pool if x not in have]
    if not pool:
        return ""
    rng = random.Random(seed or date.today().isoformat())
    return rng.choice(pool)


# ══════════════════════════════════════════════════════════
# 模型生成
# ══════════════════════════════════════════════════════════
_PROMPT = """你在替一个角色写「她昨天那天发生的一件小事」。

## 她是谁

这座镇子的农户，寡言，日子过得踏实。种地、修东西、腌菜、喂猫 ——
她的日子由这些小事组成。

## 背景

季节：{season}
天气：{weather}
她昨天的精力：{tier}
她今天手边的事：{task}

## 她最近这些天的事（**不要重复，也不要换个说法重复**）

{recent}

## 要求

1. 一句话，**{max_chars} 字以内**，中文。
2. 必须是一件**具体的小事**，要有小后果或小麻烦：
   写「院角那畦葱太密，挖出来重栽了一排，腰直不起来」，
   不写「今天去地里干活了」。
3. **只写她自己。**不要提到他，不要写「我们」。
4. 不要大病、不要灾害、不要死讯。日子是平常的。
5. 不要用引号、不要编号、不要解释、不要写日期。

直接输出那一句。
"""


def _recent_lines(ch) -> str:
    evs = [str(e.text) for e in (getattr(ch, "her_events", None) or [])
           if getattr(e, "text", "")]
    if not evs:
        return "（还没有）"
    return "\n".join(f"- {x}" for x in evs[-10:])


def generate(ch, api_key: str, season: str = "", weather: str = "",
             model: str = "deepseek-chat",
             api_base: str = "https://api.deepseek.com/v1",
             timeout: int = 25) -> str:
    """让模型写一条。失败返回空串（调用方退到语料）。"""
    if not api_key:
        return ""

    tier = ""
    try:
        from mind_mood import Mood
        tier = Mood.from_dict(getattr(ch, "mood", {}) or {}).tier
    except Exception:
        pass

    prompt = _PROMPT.format(
        season=season or "不詳",
        weather=weather or "不詳",
        tier=tier or "不詳",
        task=getattr(ch, "today_task", "") or "不詳",
        recent=_recent_lines(ch),
        max_chars=MAX_CHARS,
    )
    payload = {
        "model": model,
        "temperature": 0.9,        # 这件事要有点意外，别每次都一个调子
        "max_tokens": 120,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        out = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""
    return _clean(out)


def _clean(t: str) -> str:
    t = (t or "").strip()
    t = t.split("\n")[0].strip()
    t = t.strip("　 \"'“”「」『』-*")
    # 去掉可能的编号
    t = t.lstrip("0123456789.、)） ")
    if len(t) < 6:
        return ""
    if len(t) > MAX_CHARS + 12:          # 明显跑题了
        return ""
    # 不该提他
    if "他" in t or "你" in t:
        return ""
    return t[:MAX_CHARS]


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════
def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def run_once(ch, api_key: str, season: str = "", weather: str = "") -> tuple[bool, str]:
    """给她补一条「昨天的事」。返回 (有没有改动, 说明)。"""
    y = _yesterday()
    evs = getattr(ch, "her_events", None) or []
    # 昨天已经有了就不再加（多次跨天检查会重复触发）
    if any(str(getattr(e, "day", ""))[:10] == y for e in evs):
        return False, f"{y} 已经有过了"

    existing = [str(e.text) for e in evs if getattr(e, "text", "")]
    text = generate(ch, api_key, season=season, weather=weather)
    src = "模型"
    if not text:
        text = pick_from_corpus(season, existing, seed=y)
        src = "语料"
    if not text:
        return False, "模型和语料都没给出东西"

    ch.note_her_event(text, day=y)
    return True, f"[{src}] {text}"
