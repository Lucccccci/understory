"""她的日记 —— 睡前写的那一篇。

## 和「每日摘要」的区别（这一个必须说清）

    每日摘要   给她**用**，进 prompt。第三人称式记录：谁说了什么。
    日记       给你**看**，**不进 prompt**。第一人称，自言自语。

摘要记事实，日记记心情。所以两份都用模型生成，但提示词完全不同。

## 什么时候写

**她睡着的那一刻。** 不是零点，也不是跨天 ——
你聊到十二点半，她说着说着睡过去了，那会儿才是她的一天结束。

判定沿用 `mind_activity.current_activity(ch) == "asleep"`。

## 存哪、留多久

    saves/diary/<会话>.json

    · 未收藏：只留 10 天
    · 收藏（★）：永久
    · **不进 prompt** —— 这是给你读的，不占她的上下文

## 长度

4–6 句，**200 字上下**。比摘要长，因为日记该有停顿和没说完的话。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

DIARY_DIR = Path(__file__).resolve().parent / "saves" / "diary"
KEEP_DAYS = 10
MIN_CHARS = 80
MAX_CHARS = 320

_PROMPT = """你替「她」写她今晚的日记。

## 今天她这边

{state}

## 你们今天说过的话（原话，从早到晚）

{recent}

## 今天真正发生的事（这些必须用上）

{events}

## 怎么写

- **4–7 句，200–280 字。**
- **要从上面的事里挑一两件具体写** —— 他说的某句话、她做的某件小事。
  ❌「今天过得很平静」这种脱离事实的话不要写
  ✅「他说菜咸了，我明天少搁半勺盐」—— 有落点
- 语气**暖**，不是冷静记录。她一个人在灯下写，心里是软的：
  可以想他、可以心疼、可以有点舍不得，也可以为一句小话高兴半天。
- 写事，不写「我很难过」「我很幸福」这类总结。让事自己说话。
- 句子短一点，留喘气的地方；可以说一半停住，可以只写「……」。
- **可以有一两句比平时更直白的心里话。** 白天她不会说出口的那种 ——
  「其实我怕他哪天就不来了」「今天有点想他，想得心口发闷」。
  **只一两句**，放在中间或结尾；说完就停，不解释、不铺陈。
  其余部分仍然要收着写 —— 直白太多就成唠叨了。
- 不要标题、不要日期、不要解释自己在写日记。

直接输出正文。
"""


# ══════════════════════════════════════════════════════════
# 存储
# ══════════════════════════════════════════════════════════
GOODNIGHT = re.compile(
    r"晚安|睡了|去睡|睡觉|休息吧|先睡了|困了|闭眼了|明早见|明天见|做个好梦"
)


def is_goodnight(text: str) -> bool:
    """这句话算不算「今晚聊完了」。

    只认**他说的**，而且必须是真的在收尾 ——
    「你今天很困吧」不算，「我先睡了」算。
    """
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    if not GOODNIGHT.search(t):
        return False
    return not re.search(r"[？?吗]", t)


DAY_START_HOUR = 5


def logical_today(now=None):
    """她的「今天」从早上 5 点开始。

    ## 为什么不是自然日

    日记是睡前写的（常常过了零点）。按自然日算，凌晨一点写的这篇
    只看得到"零点之后"的那两句 —— 一整晚的对话全在昨天，
    于是日记里没话可写，只能说些空话。

    把一天切成 05:00 → 次日 05:00，凌晨写的就还是"昨晚那一天"。
    """
    from datetime import datetime as _dt, timedelta as _td
    n = now or _dt.now()
    return (n - _td(hours=DAY_START_HOUR)).date()


def _logical_day_of(iso: str):
    """这句话属于哪一个「她的日子」。"""
    from datetime import datetime as _dt, timedelta as _td
    try:
        t = _dt.fromisoformat(str(iso)[:19])
    except (ValueError, TypeError):
        return None
    return (t - _td(hours=DAY_START_HOUR)).date()


def _path(session: str) -> Path:
    return DIARY_DIR / f"{session}.json"


def load(session: str) -> list[dict]:
    try:
        d = json.loads(_path(session).read_text(encoding="utf-8"))
        return [x for x in (d.get("entries") or []) if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def save(session: str, entries: list[dict]) -> None:
    """存盘并**清理没收藏的旧日记**。收藏的永不删。"""
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    kept = [x for x in entries
            if x.get("starred") or str(x.get("day", ""))[:10] >= cutoff]
    kept.sort(key=lambda x: str(x.get("day", "")))
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(session).with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"entries": kept}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_path(session))


# ══════════════════════════════════════════════════════════
# 生成
# ══════════════════════════════════════════════════════════
def _today(ch) -> str:
    """今天从早到现在的原话，取首尾各若干条 —— 中间省略。"""
    today = logical_today().isoformat()
    turns = [t for t in (getattr(ch, "turns", None) or [])
             if str(_logical_day_of(getattr(t, "at", ""))) == today]
    if not turns:
        turns = (getattr(ch, "turns", None) or [])[-16:]

    def one(t):
        who = "他" if getattr(t, "role", "") == "player" else "你"
        return f"  {who}：{str(getattr(t, 'text', ''))[:90]}"

    if len(turns) <= 24:
        return "\n".join(one(t) for t in turns) or "（今天没怎么说上话）"
    head = turns[:10]
    tail = turns[-14:]
    return "\n".join([one(t) for t in head] + ["  ……（中间略）……"]
                     + [one(t) for t in tail])


def _events_today(ch) -> str:
    """今天真发生过的事：事件 ★ 优先，其次她自己的日子。"""
    today = logical_today().isoformat()
    out = []

    def _day_of(x):
        raw = getattr(x, "at", "") or getattr(x, "day", "")
        d = _logical_day_of(raw)
        return str(d) if d else str(raw)[:10]

    evs = [e for e in (getattr(ch, "events", None) or [])
           if _day_of(e) == today]
    for e in evs[-6:]:
        star = "★ " if getattr(e, "first", False) else ""
        out.append(f"  · {star}{str(getattr(e, 'text', ''))[:80]}")

    hers = [e for e in (getattr(ch, "her_events", None) or [])
            if _day_of(e) == today]
    for e in hers[-3:]:
        out.append(f"  · 她自己：{str(getattr(e, 'text', ''))[:70]}")

    projs = (getattr(ch, "projects", None) or [])[:2]
    for pr in projs:
        txt = pr.get("text", "") if isinstance(pr, dict) else getattr(pr, "text", "")
        st = pr.get("stage", 0) if isinstance(pr, dict) else getattr(pr, "stage", 0)
        tt = pr.get("total", 3) if isinstance(pr, dict) else getattr(pr, "total", 3)
        if txt:
            out.append(f"  · 手上的活：{txt}（{st}/{tt}）")

    return "\n".join(out) or "  （今天没有特别记下的事 ——— 那就写你们说过的话）"


def write_one(ch, session: str, api_key: str,
              model: str = "deepseek-chat",
              api_base: str = "https://api.deepseek.com/v1") -> tuple[bool, str]:
    """写今晚这一篇。已经写过就不重复写。返回 (有没有写, 说明)。"""
    today = logical_today().isoformat()
    entries = load(session)
    if any(str(x.get("day", ""))[:10] == today for x in entries):
        return False, "今天已经写过"

    try:
        import mind_lines
        state = mind_lines._state_text(ch, today)
    except Exception:
        state = "（没有更多状态）"

    body = _PROMPT.format(state=state, recent=_today(ch),
                          events=_events_today(ch))
    payload = {
        "model": model,
        "temperature": 0.9,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": body}],
    }
    try:
        import mind_llm
        resp = mind_llm.chat_body(payload, api_key, api_base, 60)
        text = (resp["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return False, f"生成失败：{e}"

    # 清理：去引号、去 markdown、压空行
    text = text.strip().strip('"“”「」')
    text = "\n".join(l.strip() for l in text.split("\n") if l.strip())
    if len(text) < MIN_CHARS:
        return False, "太短，丢弃"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + "……"

    entries.append({
        "day": today,
        "at": datetime.now().isoformat(timespec="seconds"),
        "text": text,
        "mood": getattr(ch, "mood", {}).get("label", "") if isinstance(
            getattr(ch, "mood", None), dict) else "",
        "starred": False,
    })
    save(session, entries)
    return True, f"{today} 写了 {len(text)} 字"


def toggle_star(session: str, day: str) -> bool:
    """收藏 / 取消收藏。返回新的状态。"""
    entries = load(session)
    for x in entries:
        if str(x.get("day", ""))[:10] == day[:10]:
            x["starred"] = not bool(x.get("starred"))
            save(session, entries)
            return bool(x["starred"])
    return False
