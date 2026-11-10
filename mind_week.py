"""周摘要 —— 把 7 天的日摘要再压成 2-3 行。

## 为什么需要

日摘要上限 14 天，再往前的「轮廓」就没了。而直接抬高上限不行：
5 天就占 960 字，60 天要一万多字，prompt 直接撑爆。

所以**分层压缩**：

    L0  原文窗口     8 小时
    L1  日摘要       7 天（逐日，细节多）
    L2  周摘要       8 周（每周 2-3 行，只剩骨架）   ← 这个模块
    L3  长期条目     events / her_events / memories
    L4  召回         关键词命中，无限回溯

一周 7 天 × 5 条 ≈ 1050 字的输入，压出来 2-3 行（约 90 字）。
8 周合计约 700 字 —— 换回「两个月前大致发生了什么」，很划算。

## 什么时候压

跨天时检查：**有 7 天日摘要还没被任何周覆盖**，就压一次。

这样它是自然往前滚的，不需要算星期几，也不会漏。

## 只压最早的 7 天

不是「最近 7 天」。最近的还在逐日注入，压了反而重复。
从最旧那端开始，保证「日摘要 → 周摘要」是一条单向的传送带。
"""
from __future__ import annotations

import mind_llm
import json
import urllib.request
from datetime import date

WEEK_DAYS = 7        # 几天压一周

# 最近这几天**不压** —— 保持逐日细节。
#
# 实测踩过：只有 7 天时，7 天全被折成 3 行，摘要块从 1348 字掉到 223 字。
# 省了 token，但「前天他说了什么」这种近处的细节也没了。
# 近处要能说得出，远处有轮廓就够。
KEEP_DAILY = 4
MAX_WEEKS = 8        # 留几周
MAX_LINES = 3
MAX_CHARS = 40

_PROMPT = """下面是你这七天里大致发生过的事（每天几条要点）。

## 这七天

{body}

## 要求

把它压成 **{n} 条**要点，输出**一个 JSON 数组**，每条是一句话：

[{{"text":"……"}}, {{"text":"……"}}]

- 每条 **{max_chars} 字以内**
- 只留**骨架**：关系有没有变化、答应过什么、发生过什么要紧的事、
  有什么一直没解决的
- **不要写日常琐事**（腌菜、劈柴、喂猫这些日摘要里有了，这一层不要）
- 用「他」称呼对方，用「你」称呼自己
- 不要编，上面没写的不要写

直接输出 JSON 数组。
"""


def _covered(ch) -> set[str]:
    """已经被周摘要覆盖的日子。"""
    out: set[str] = set()
    for w in (getattr(ch, "weeks", None) or []):
        if not isinstance(w, dict):
            continue
        a, b = str(w.get("from", ""))[:10], str(w.get("to", ""))[:10]
        for d in (getattr(ch, "digests", None) or []):
            day = str(d.get("day", ""))[:10]
            if a <= day <= b:
                out.add(day)
    return out


def pending_group(ch) -> list[str]:
    """还没被周覆盖的、最旧的 7 天。不够 7 天返回空。"""
    days = sorted({str(d.get("day", ""))[:10]
                   for d in (getattr(ch, "digests", None) or [])
                   if d.get("lines")})
    cov = _covered(ch)
    free = [d for d in days if d not in cov]
    # 最近 KEEP_DAILY 天留着逐日，不参与压缩
    older = free[:-KEEP_DAILY] if len(free) > KEEP_DAILY else []
    return older[:WEEK_DAYS] if len(older) >= WEEK_DAYS else []


def _body(ch, days: list[str]) -> str:
    by = {str(d.get("day", ""))[:10]: d for d in (getattr(ch, "digests", None) or [])}
    out = []
    for day in days:
        d = by.get(day)
        if not d:
            continue
        out.append(f"【{day}】")
        out += [f"- {x}" for x in (d.get("lines") or [])]
    return "\n".join(out)


def summarize(ch, days: list[str], api_key: str,
              model: str = "deepseek-chat",
              api_base: str = "https://api.deepseek.com/v1",
              timeout: int = 60) -> list[str]:
    """把这 7 天压成几条。失败返回空列表（下周再试，不丢数据）。"""
    if not api_key or not days:
        return []
    prompt = _PROMPT.format(body=_body(ch, days), n=MAX_LINES, max_chars=MAX_CHARS)
    payload = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        out = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return []

    t = out.strip()
    t = t[t.find("["):t.rfind("]") + 1] if "[" in t and "]" in t else ""
    try:
        arr = json.loads(t) if t else []
    except (json.JSONDecodeError, ValueError):
        return []

    lines = []
    for x in arr:
        s = str(x.get("text") if isinstance(x, dict) else x).strip()
        s = s.strip("　 \"'“”「」")
        if len(s) < 4:
            continue
        if len(s) > MAX_CHARS:
            s = s[:MAX_CHARS]
        if s not in lines:
            lines.append(s)
        if len(lines) >= MAX_LINES:
            break
    return lines


def run_once(ch, api_key: str) -> tuple[bool, str]:
    """有空要压就压一周。返回 (有没有改动, 说明)。"""
    days = pending_group(ch)
    if not days:
        return False, "还不够 7 天"

    lines = summarize(ch, days, api_key)
    if not lines:
        return False, f"{days[0]}~{days[-1]} 压缩失败"

    ch.add_week(days[0], days[-1], lines)
    return True, f"{days[0]}~{days[-1]}：{len(lines)} 条 —— {lines[0][:24]}"
