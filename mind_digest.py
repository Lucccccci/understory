"""每日摘要 —— 补上「昨天到一周前」那条断掉的带子。

## 为什么需要

1537 轮之后，prompt 的实际构成是：

    系统   15,998 字（84%）
    历史    2,984 字（16%）—— 106 轮，只覆盖最近 8 小时

也就是说：**昨天的事已经出窗了**，只剩下零星的 memory / event。
而召回（recall）是字面匹配 —— 换个说法、同义词、没提到的词，就翻不出来。

    最近 8 小时   完整
    昨天          ✗ 出窗
    前天          ✗ 出窗
    一周前        ✗ 只有零星几条

**中间这一层是空的。** 这个模块补的就是它。

## 怎么用

跨天时把**前一天**的对话压成 3-5 条要点，存进 `digests`：

    digests = [{"day": "2026-10-21",
                "lines": ["你把腰闪了，我去河边找了几味草药",
                          "坛子补好了，你非要多给钱"]}]

进 prompt 时是一块「大概记得」：

    ## 前几天的大致经过（只剩个大概，细节记不清很正常）

## 为什么要允许她「记不清」

这是这个模块最有价值的一条：**人回忆一周前，本来就只有轮廓。**
硬让她记住细节，反而会编 —— 而编出来的旧事比忘记更伤人。
所以提示词里明确写了：可以说「记不太清了」，不要为了显得记得而编。

## 成本

1 天 1 次调用，输出约 150 字。上限 14 天。
"""
from __future__ import annotations

import mind_llm
import json
import re
import urllib.request
from datetime import date, datetime, timedelta

MAX_DAYS = 14          # 存多少天
MAX_LINES = 5          # 每天最多几条
MAX_LINE_CHARS = 40    # 每行最长
MIN_TURNS = 6          # 那天少于这么多轮就不摘要
MAX_INPUT_TURNS = 120  # 一天最多喂多少轮给模型

_PROMPT = """下面是你和他那天（{day}）的对话。请把它压成 {max_lines} 条要点。

## 要求

1. 每条一行，**{max_chars} 字以内**。
2. 只写**发生了什么**：谁做了什么、说了什么要紧的事、事情有什么进展。
   不写情绪评价（「他很温柔」这种不要）。
3. 用「他」称呼对方，用「你」称呼自己。
4. 挑要紧的：承诺、决定、身体、争执、约好的事、后来的变化。
   方括号里是时刻，**按时间先后写**，不要把夜里的事说成「第二天早上」。
5. **不要编。** 对话里没有的，一个字都不要写。

## 输出

每行一条，以「- 」开头（短横线加空格）。不要标题、不要解释、不要编号。

## 对话

{transcript}
"""


def _day_of(turn) -> str:
    at = getattr(turn, "at", "") or ""
    return at[:10] if len(at) >= 10 else ""


def _turns_of(ch, day: str) -> list:
    return [t for t in (getattr(ch, "turns", None) or [])
            if _day_of(t) == day]


def _transcript(turns: list) -> str:
    """把一天压成文本。太多就取**开头一段 + 结尾一段** ——
    开头常常是「今天要做什么」，结尾是「结果怎样」，中间多是闲话。"""
    if len(turns) > MAX_INPUT_TURNS:
        half = MAX_INPUT_TURNS // 2
        turns = list(turns[:half]) + [None] + list(turns[-half:])
    out = []
    for t in turns:
        if t is None:
            out.append("……（中间略）")
            continue
        who = "他" if getattr(t, "role", "") == "player" else "你"
        txt = str(getattr(t, "text", "") or "").replace("\n", " ")[:120]
        # 带上时刻 —— 不带的话模型会排错先后
        # （实测踩过：把上午的事写成「次日早上」）
        at = str(getattr(t, "at", "") or "")
        hhmm = at[11:16] if len(at) >= 16 else ""
        out.append(f"[{hhmm}] {who}：{txt}" if hhmm else f"{who}：{txt}")
    return "\n".join(out)


def _clean_lines(out: str) -> list:
    """从模型输出里取出那几条要点。"""
    lines = []
    for raw in (out or "").split("\n"):
        s = raw.strip()
        if not s:
            continue
        # 只认以短横线开头的行（按提示词要求）
        s = re.sub(r"^[-*·]\s*", "", s)
        # 去掉行首的时刻 —— 摘要是「大概」，精确到点反而假。
        # 时间戳只用来帮模型排先后，不该出现在输出里。
        s = re.sub(r"^[\[（(]\s*\d{1,2}:\d{2}\s*[\]）)]\s*", "", s)
        s = s.strip("　 \"'“”「」")
        if not s:
            continue
        # 太长就是它在解释，截断
        if len(s) > MAX_LINE_CHARS:
            s = s[:MAX_LINE_CHARS]
        if len(s) < 4:
            continue
        if s not in lines:
            lines.append(s)
        if len(lines) >= MAX_LINES:
            break
    return lines


def generate(ch, day: str, api_key: str,
             model: str = "deepseek-chat",
             api_base: str = "https://api.deepseek.com/v1",
             timeout: int = 40) -> list:
    """让模型把 day 那天压成几条要点。失败返回空列表。"""
    if not api_key:
        return []
    turns = _turns_of(ch, day)
    if len(turns) < MIN_TURNS:
        return []

    prompt = _PROMPT.format(day=day, max_lines=MAX_LINES,
                            max_chars=MAX_LINE_CHARS,
                            transcript=_transcript(turns))
    payload = {
        "model": model,
        "temperature": 0.3,        # 摘要要稳，不要发挥
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        out = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return []
    return _clean_lines(out)


def run_once(ch, api_key: str) -> tuple[bool, str]:
    """给「昨天」做一份摘要。返回 (有没有改动, 说明)。"""
    y = (date.today() - timedelta(days=1)).isoformat()

    have = getattr(ch, "digests", None) or []
    if any(str(d.get("day", ""))[:10] == y for d in have):
        return False, f"{y} 已经有摘要了"

    turns = _turns_of(ch, y)
    if len(turns) < MIN_TURNS:
        return False, f"{y} 只有 {len(turns)} 轮，不摘要"

    lines = generate(ch, y, api_key)
    if not lines:
        return False, f"{y} 摘要生成失败"

    ch.add_digest(y, lines)

    # 顺手看看能不能压出一周（够 7 天未覆盖的日摘要才压）
    week_msg = ""
    try:
        import mind_week
        wk_changed, wk_msg = mind_week.run_once(ch, api_key)
        if wk_changed:
            week_msg = f" / 周摘要 {wk_msg}"
    except Exception:
        pass

    return True, f"{y}：{len(lines)} 条 —— {lines[0][:24]}{week_msg}"
