"""事件记忆 —— 你们之间发生过的事。

## 为什么要单独一层

现在她的记忆分四种，但**没有一种在管「事件」**：

    memories      他说的信息（「我妹妹叫小满」）
    observations  她看出来的行为模式（「他累了不吭声」）
    promises      他答应过的事（「明天帮你夯桩子」）
    turns         逐字对话（靠字面召回翻）

**缺的是「你们之间发生过什么」**：

    第一次他在这边过夜
    那天去海边看了月光水母，回来路上他一句话没说
    他连着三天没来，第四天带了个蛋糕

这类东西现在**散在 turns 里，靠关键词召回**。而实测踩过：

    你：昨天的月光水母好看嘛
    她：月光水母……你说的是海边那种泛蓝光的？／我没去看。你去了？

**她不知道你们一起去过。** 因为那句里没有「月光水母」四个字恰好
落在能被召回的位置。而那件事对她是有分量的 —— 她后来自己想起了
「那天回来路上你一句话没说」。

**事件该是一等公民，不该等触发词。**

## 判据：只记「第一次」和「转折」

**误判的代价是她在意一件你根本没当回事的事** —— 那比漏记更糟。
所以刻意保守：只认结构清晰的句子，不做语义推测。

    ✓ 「我们不是一起去的吗」        -> 不算（问句）
    ✓ 「昨天我们一起去看水母了」     -> 算（时间 + 一起 + 动作）
    ✓ 「我还是第一次在这儿过夜」     -> 算（第一次）

**宁可漏，不可滥。**
"""

from __future__ import annotations

import mind_llm
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

# ── 判据 ──────────────────────────────────────────────
#
# 「第一次」类：明说了是头一回。这类最可靠。
_FIRST = re.compile(
    r"(第一次|头一回|头一次|初次|从没(?:这么|这样)?|以前没|从未)"
)

# 「一起」类：必须有时间锚点，否则日常的「一起吃个饭」会全被记下来。
_TOGETHER = ("一起", "一块", "陪我", "陪你", "跟你", "和你", "咱俩", "我们俩")

# 时间锚点。**「一起」类没有它就不算**，否则「一起吃饭吧」这种会被记。
_TIME_ANCHOR = re.compile(
    r"(昨天|前天|那天|昨晚|今早|今天|刚才|刚刚|上回|上次|那次|"
    r"[一二三四五六七八九十\d]+天前|[一二三四五六七八九十\d]+个?月前)"
)

# 活动动词 —— 让「事件」有个具体的事，而不是纯情绪。
_ACTIVITY = ("去", "来", "看", "吃", "喝", "钓", "下矿", "过夜", "住", "走", "逛",
             "买", "送", "留", "陪", "做", "摘", "种", "收", "赶", "参加", "过节",
             "舞", "宴", "市集")

# 否定 / 假设 / 问句 —— 一律不算（和 mind_promise 同一个原则）
_NOT = re.compile(r"[？?吗]|(?:不是|没有去过|没去|要是|如果|假如|要不|算了)")

# 「两个人在场」的迹象。
#
# 放宽这一档是因为实测漏了真实的话：
#
#     你：嘿嘿 她最好了 昨天的月光水母好看嘛
#         有「昨天」（时间）有「看」（活动），但**没有「一起」两个字**
#         —— 按原判据被漏掉，而那句说的正是你们一起做过的事
#
# 现在只要「提到她」（你 / 她 / 她）就算两个人在场。
# 单人称的（「今天我在地里忙了一整天」）仍然不收 —— 那是他的日常，不是你们的事。
_PEOPLE = ("你", "她")

# 太短没信息，太长多半是整段感慨
_MIN, _MAX = 6, 90


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).strip()


def detect(text: str) -> tuple[str, str] | None:
    """从一句话里认出「值得记的事件」。

    Args:
        text: 玩家说的话。

    Returns:
        (事件的短描述, 类型) —— 类型是 "first" / "together"。
        认不出返回 None。
    """
    t = _norm(text)
    if not (_MIN <= len(t) <= _MAX):
        return None
    if _NOT.search(t):
        return None
    # ① 「第一次」类 —— **不要求活动词**（2026-10-21 改）
    #
    # 原来这一条在活动词后面，于是这类话全被吞掉：
    #     「今天是第一次呢」   -> 没有「去/看/吃」这类词 -> 直接 return
    # 而「第一次」恰恰是最该记的一类，它本身就是事件。
    #
    # 为什么这样安全：上面的 `_NOT` 已经挡掉疑问和假设 ——
    #     「这不是第一次吧？」「你第一次来镇上？」 都过不来。
    if _FIRST.search(t):
        return _clip(t), "first"

    # 其余类仍然要有具体活动词（「一起去看了…」「昨天去了…」）
    if not any(w in t for w in _ACTIVITY):
        return None

    # ② 「一起」类 —— 时间锚 + 两个人在场
    #
    # ⚠️ 两个条件都要，缺一不可：
    #     只有时间 + 活动     ->  漏收「昨天的月光水母好看嘛」（那确实是你们的事）
    #     只有活动 + 提到她   ->  会收「你今天吃饭了吗」这种日常寒暄
    if not _TIME_ANCHOR.search(t):
        return None
    if not any(w in t for w in _TOGETHER) and not any(w in t for w in _PEOPLE):
        return None

    return _clip(t), "together"


def _clip(t: str) -> str:
    """截到句子边界，别把整段话都当事件。"""
    parts = re.split(r"[。！？；]", t)
    for p in parts:
        if p:
            return p[:60]
    return t[:60]


def summarize(raw: str, kind: str, api_key: str = "",
              model: str = "deepseek-chat",
              api_base: str = "https://api.deepseek.com/v1") -> str:
    """把他那句话**改写成她自己会怎么记这件事**。

    ## 为什么不能直接存原话

    实测捞出来的两条长这样：

        · 今天在来找你之前其实我已经把答应你的夯桩子弄完啦
        · 嘿嘿她最好了昨天的月光水母好看嘛

    **那是他的口语原话**，没标点、带语气词。直接存进去有两个坏处：

        ① 她回想时会复读他的口吻，不像她
        ② 「嘿嘿她最好了昨天的月光水母好看嘛」这句本身不成为一个事件

    所以分两步：**判据负责「是不是事件」，这里负责「怎么说」。**

    ## 失败就退回原话

    这一步要调模型，可能失败（没 key、网络、额度）。失败时返回原文 ——
    语气差一点，总比丢了一件事好。

    Args:
        raw: 他说的那句话。
        kind: "first" / "together"。
        api_key: 留空则从 `.apikey` 读。

    Returns:
        一句话，比如「那天晚上跟他去海边看了月光水母」。
    """
    import json as _json
    import urllib.request
    from pathlib import Path as _Path

    if not api_key:
        try:
            kf = _Path(__file__).resolve().parent / ".apikey"
            if kf.is_file():
                api_key = kf.read_text(encoding="utf-8").strip()
        except Exception as e:
            # ⚠️ 这里踩过：原来只写 `api_key = ""`，把一个 NameError 吞了 ——
            # 表现是「改写静默失效，一直退回原话」，很难查。
            # 失败要留痕，不然下次还得从头查一遍。
            import sys as _sys
            print(f"[mind_event] 读 .apikey 失败：{type(e).__name__}: {e}",
                  file=_sys.stderr, flush=True)
            api_key = ""
    if not api_key:
        return raw

    hint = ("一件**头一回**发生的事" if kind == "first"
            else "一件发生过的事")
    prompt = (
        f"他说的下面这句话里，藏着{hint}。\n"
        f"把它写成**你自己心里记下的一句话**。\n\n"
        f"要求：\n"
        f"- 一句话，不超过 25 字。\n"
        f"- **第一人称省略式**：像「那天和他去看了水母」这样，"
        f"主语是你（可以省略），用「他」指他。\n"
        f"- **不要写成转述** —— 不许出现「他说」「他告诉」「我陪他」这类。\n"
        f"  那是记录别人说的话，不是你的记忆。\n"
        f"- 平静、具体，不带语气词和感叹。\n"
        f"- 别加他没说的细节。\n"
        f"- 只输出那一句话，不要解释。\n\n"
        f"他说：{raw}\n\n"
        f"例子（照这个感觉，但不要照抄内容）：\n"
        f"    他说「今天我把答应你的桩子夯完啦」 -> 他把答应我的桩子夯完了\n"
        f"    他说「昨天那个水母真好看」        -> 那天晚上和他去海边看了水母\n"
        f"    ✗ 反例：「他说桩子夯完了」         <- 转述，不要\n"
        f"    ✗ 反例：「他告诉我水母好看」       <- 转述，不要"
    )

    try:
        payload = {
            "model": model,
            "temperature": 0.7,
            "max_tokens": 120,
            "messages": [{"role": "user", "content": prompt}],
        }
        body = mind_llm.chat_body(payload, api_key, api_base, 30)
        out = (body["choices"][0]["message"]["content"] or "").strip()
        # 去掉可能的引号、换行、markdown
        out = out.strip('"“”\'' + "\n ")
        out = out.split("\n")[0].strip()
        if 4 <= len(out) <= 40:
            return out
    except Exception:
        pass
    return raw


@dataclass
class Event:
    """一件发生过的事。"""
    text: str                      # 短描述（多半是他自己的原话）
    kind: str = "together"         # first / together
    at: str = ""                   # 认出来的时间（ISO）
    day: str = ""                  # 日期，用来按天分组
    with_her: bool = True          # 有没有她参与

    @staticmethod
    def make(text: str, kind: str, now: datetime | None = None) -> "Event":
        now = now or datetime.now()
        return Event(text=text, kind=kind,
                     at=now.isoformat(timespec="seconds"),
                     day=now.strftime("%Y-%m-%d"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            text=str(d.get("text") or ""),
            kind=str(d.get("kind") or "together"),
            at=str(d.get("at") or ""),
            day=str(d.get("day") or ""),
            with_her=bool(d.get("with_her", True)),
        )


def her_event_block(ch, limit: int = 6) -> str:
    """拼「她自己这些天的事」那一段。

    ## 和 event_block 的区别（语气要求正好相反）

        event_block     你们之间发生过的事  —— 关系史，**别主动回顾**
        her_event_block **她自己的日子**    —— 可以主动说，因为那是她的一天

    所以两段不能合并，各自的告诫也不一样。
    """
    evs = [e for e in (getattr(ch, "her_events", None) or [])
           if getattr(e, "text", "")]
    if not evs:
        return ""
    picked = sorted(evs, key=lambda e: str(getattr(e, "day", "")))[-limit:]

    lines = ["## 你自己这些天的事（跟他没关系，但你可以说）", ""]
    for e in picked:
        # mark 后面不再带空格 —— 拼出来是「- ★（日期）…」
        mark = "★" if getattr(e, "kind", "") == "done" else "·"
        lines.append(f"- {mark}（{getattr(e, 'day', '')}）{e.text}")
    lines += [
        "",
        "⚠️ 这是**你自己的日子**，不是你们俩的事。",
        "- 可以提，但像随口说的，不是汇报。",
        "- **不要每天提。** 多数时候不提；他问起、或话题碰上了再说。",
        "- 日期要说准 —— 别把五天前说成昨天。",
    ]
    return "\n".join(lines)


def event_block(events: list, limit: int = 8) -> str:
    """拼成给模型看的一段。

    ## 写法要点

    **事件是关系史，不是谈资。** 所以要明确要求：
    不要主动一件件回顾，只在话题相关时自然带过。

    否则她会变成「上次我们……还有上上次我们……」那种记账腔。
    """
    if not events:
        return ""
    items = [Event.from_dict(e) if isinstance(e, dict) else e for e in events]
    items = [e for e in items if e.text]

    # 「第一次」比「一起做过」重，排在前面
    items.sort(key=lambda e: (e.kind != "first", e.day))
    picked = items[-limit:]

    lines = ["## 你们之间发生过的事（这些是真发生过的，不是他说的）", ""]
    for e in picked:
        mark = "★ " if e.kind == "first" else "· "
        lines.append(f"- {mark}（{e.day}）{e.text}")
    lines += [
        "",
        "带 ★ 的是「头一回」，那类事对你有分量。",
        "",
        "⚠️ **这是关系史，不是谈资。**",
        "- 不要主动一件件回顾，那是记账，不是过日子。",
        "- 只在话题真的相关时自然带一句，像人想起旧事那样。",
        "- 也不要每次都用「我们上次……」开头 —— 那会显得你在数日子。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print("  判据自测")
    print()
    cases = [
        ("昨天我们一起去看水母了", True, "时间+一起+活动"),
        ("我还是第一次在这儿过夜", True, "第一次"),
        ("头一回跟他去下矿", True, "第一次"),
        ("我们不是一起去的吗", False, "问句"),
        ("一起吃个饭吧", False, "没有时间锚点"),
        ("要是能一起去看海就好了", False, "假设"),
        ("今天天气不错", False, "没有活动"),
        ("上次跟你去钓鱼，你一条都没钓上来", True, "时间+一起+活动"),
        ("第一次", False, "太短"),
    ]
    ok = 0
    for t, want, note in cases:
        r = detect(t)
        got = f"{r[1]:<9} {r[0]}" if r else "不收"
        good = bool(r) == want
        if good:
            ok += 1
        print(f"  {'OK ' if good else '!! '}{t[:30]:<32} -> {got:<30} （{note}）")
    print()
    print(f"  {ok}/{len(cases)} 符合预期")
    print()
    # 用他真实说过的话试
    print("  用真实说过的话试：")
    real = [
        "！那她愿意过段时间陪我去钓鱼嘛",
        "我们不是一起去的吗 镇长他们都在呢",
        "嘿嘿 她最好了 昨天的月光水母好看嘛",
        "今天我在地里忙了一整天",
    ]
    for t in real:
        r = detect(t)
        print(f"    {t[:36]:<38} -> " + (f"收为「{r[0]}」({r[1]})" if r else "不收"))
