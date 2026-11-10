#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她记得你答应过的事。

## 为什么单独做这个

记忆系统记的是**事实**（「我妹妹叫小满」）。承诺不是事实，是**悬着的事**——
它有「到期」的概念：你说明天带鱼来，那明天就该有个交代。

现在她记不住这一层，于是你说过的话像没说过。

## 她怎么用它（要紧）

**她永远不催。** 这是这个人物的底色，不能为了「提醒你」就破掉。

    你说「明天给你带点鱼」-> 她记下
    第二天你没提          -> 她**不主动讨**，但如果聊到吃的，会顺口说
                            「上次说带鱼来的，忘了也不要紧」

    你做过了              -> 她也不会专门道谢，只是那句话不再出现

判据很简单：**把它当成她心里的一件小事，而不是一笔账。**

## 怎么认出承诺

用句式匹配，不调模型 —— 承诺的说法就那么几种，而且这里误判的代价
是「她记了一件你没答应的事」，比漏掉更尴尬。所以宁可严一点。

    「明天给你带点鱼」       -> 答应的事 + 时间
    「我下次一定来」         -> 时间模糊
    「等忙完这阵子我请你吃饭」-> 条件式

## 时间怎么算

中文里的相对时间（明天/后天/下周）要换算成日期才能判断「到期」。
但**不设死线**：到了也不催，只是让这条更容易被提起。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ── 句式 ─────────────────────────────────────────────
# 时间词 -> 相对天数
WHEN = [
    (r"明天|明日|明儿", 1),
    (r"后天", 2),
    (r"大后天", 3),
    (r"今晚|今天晚上|夜里", 0),
    (r"周末|这周末", 6),
    (r"下周|下个星期|下礼拜", 7),
    (r"下个月", 30),
    (r"过几天|过两天|这几天", 3),
    (r"忙完(?:这阵子|这段时间|这段)?", 14),   # 条件式，给个宽限
    (r"等.{0,6}完了", 14),
]

# 表示「我答应/打算做什么」的句式
# A. 带主语：我 (+时间) (+副词) (+会/要/打算…) + 动词
_PROMISE_A = re.compile(
    r"我[^，。！？；]{0,8}?"
    r"(给你|送你|请你|陪你|帮你|带你去|带你|带点|拿点|弄点|做|修|送|带|请|陪|帮|"
    r"来|去|找|留|买|种|钓|寄)")

# B. 不带主语：中文里承诺常省略主语（「明天给你带点鱼来」）
_PROMISE_B = re.compile(
    r"(明天|后天|明儿|今晚|今天晚上|夜里|周末|这周末|下周|下个星期|下礼拜|"
    r"下个月|过几天|过两天|这几天|回头|改天|下次|说好了|说好的)"
    r"[^，。！？；]{0,10}?"
    r"(给你|送你|请你|陪你|帮你|带你去|带你|带点|拿点|弄点|做|修|送|带|请|陪|帮|"
    r"来|去|找|留|买|种|钓|寄)")

# 触发「这是承诺」的关键词
_TRIGGER = ("给你", "送你", "请你", "陪你", "帮你", "带你去", "一定的",
            "说好了", "下次", "回头", "改天", "等", "答应")

# 不算承诺的（问句、否定、假设）
_NOT = re.compile(r"[？?]|(?:不|没|别|哪能|怎么会)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def detect(text: str) -> str | None:
    """从一句话里认出承诺。认出就返回那句承诺（截断过），否则 None。

    ## 为什么偏保守

    误判的代价是「她记了一件你没答应的事」——她会因此显得自作多情。
    漏判的代价只是少记一件。所以否定、问句、假设一律不算。
    """
    t = _norm(text)
    if not (5 <= len(t) <= 80):
        return None
    if _NOT.search(t):
        return None
    if not any(w in t for w in _TRIGGER):
        return None
    if not (_PROMISE_A.search(t) or _PROMISE_B.search(t)):
        return None

    # 截到句末标点，别把整段话都当承诺
    m = re.split(r"[。！？；，,]", t)
    for piece in m:
        if any(w in piece for w in _TRIGGER) and (_PROMISE_A.search(piece) or _PROMISE_B.search(piece)):
            return piece[:40]
    return t[:40]


def due_date(text: str, base: datetime | None = None) -> str:
    """从承诺里推算到期日。认不出时间就给一个默认的宽限期。"""
    base = base or datetime.now()
    for pat, days in WHEN:
        if re.search(pat, text):
            return (base + timedelta(days=days)).strftime("%Y-%m-%d")
    # 没提时间 -> 三天后算「可以轻轻提一句了」
    return (base + timedelta(days=3)).strftime("%Y-%m-%d")


@dataclass
class Promise:
    """一件你答应过的事。"""
    text: str               # 承诺的原话（截断过）
    at: str                 # 说这话的时间（ISO）
    due: str = ""           # 到期日 YYYY-MM-DD
    said_by: str = "player" # 现在只记你说的
    kept: bool = False      # 是否已经做到
    kept_at: str = ""
    mentioned: int = 0      # 被提起过几次（提多了就不该再提）

    def to_dict(self) -> dict:
        return {"text": self.text, "at": self.at, "due": self.due,
                "said_by": self.said_by, "kept": self.kept,
                "kept_at": self.kept_at, "mentioned": self.mentioned}

    @classmethod
    def from_dict(cls, d: dict) -> "Promise":
        return cls(
            text=str(d.get("text", "")),
            at=str(d.get("at", "")),
            due=str(d.get("due", "")),
            said_by=str(d.get("said_by", "player")),
            kept=bool(d.get("kept", False)),
            kept_at=str(d.get("kept_at", "")),
            mentioned=int(d.get("mentioned", 0) or 0),
        )

    @property
    def overdue(self) -> bool:
        if self.kept or not self.due:
            return False
        return datetime.now().strftime("%Y-%m-%d") >= self.due


def make(text: str, now: datetime | None = None) -> Promise | None:
    """从一句话造一条承诺。认不出返回 None。"""
    now = now or datetime.now()
    t = detect(text)
    if not t:
        return None
    return Promise(text=t, at=now.isoformat(timespec="seconds"),
                   due=due_date(t, now))


# ── 进 prompt ────────────────────────────────────────
def promise_block(promises: list[Promise], limit: int = 3) -> str:
    """拼成给模型看的一段。

    ## 写法要点

    这里必须把「不催」写清楚，否则模型会写成讨债 ——
    「你说过要给我带鱼的，怎么还没带」（错）
    「上次说带鱼来的，忘了也不要紧」（对）
    """
    live = [p for p in promises if not p.kept]
    if not live:
        return ""

    # 到期的排前面，但被提过很多次的不再排
    live = [p for p in live if p.mentioned < 3]
    if not live:
        return ""
    live.sort(key=lambda p: (not p.overdue, p.at))
    picked = live[:limit]

    lines = ["## 他答应过的事（**她心里记着，但绝不催**）", ""]
    for p in picked:
        when = p.at[:10]
        tag = "（已经过了说好的日子）" if p.overdue else ""
        lines.append(f"- {when} 他说：{p.text}{tag}")
    lines += [
        "",
        "**怎么用这几条 —— 这一条极其重要：**",
        "- 你记得这些事，但你**永远不催**、不讨、不抱怨他没做到。",
        "- 绝不可以说「你说过要给我的」「你怎么还没」「你答应过的呢」——",
        "  那是讨债，不是你了。",
        "- 合适的用法是：聊到相关话题时顺口带一句，而且**给他台阶**。",
        "  比如「上次说带鱼来的……忘了也不要紧，我又不是非吃那个」。",
        "- 他做到了，你也不用专门道谢，自然接过去就好。",
        "- 如果他这轮没提到相关的事，**就不要提**。多数时候它是沉默的。",
    ]
    return "\n".join(lines)

# ── 用事件判定「承诺已兑现」 ──────────────────────────────
#
# ## 要解决的问题
#
# 承诺和事件是两套独立记录，于是会出现自相矛盾：
#
#     promises  「好明天帮你夯一下桩子」   kept=False
#     events    「来找我之前，他把答应我的夯子弄完了。」
#
# **她一边记着「你答应要夯」，一边记着「你夯完了」。**
# 说的时候可能前后打架。
#
# ## 判据
#
# 三条都要满足（宁可漏判，也不能把没做的记成做到了）：
#
#     ① 共有一个实词      承诺说「桩子」，事件也说「桩子」
#     ② 事件里有完成标记    「弄完」「做好了」「给你了」……
#     ③ 时间不早于承诺日    **没做不能算兑现** —— 这条最容易漏
#
# 第 ③ 条是必须的：如果他说「明天帮你夯」，然后当天又说「桩子松了」，
# 那件事不该被标成做到。

# 完成标记 —— 事件里出现这些，说明事情做完了。
#
# ## 第一版是坏的（2026-10 重写）
#
# 原来写的是单字表：
#
#     ("完", "好", "成", "妥", "弄", "做", "给", "送", "带", "办", "兑现")
#
# **这 11 个字里一半是高频常用字**（好 / 做 / 给 / 带），
# 另一半是承诺里也会出现的动词（弄 / 带 / 送）。
# 结果它同时干两件事（判完成 + 匹配承诺），两件都干不好。
#
# 实测 11 条对抗样本里错 8 条：
#
#     天气真好，桩子那边晒得暖。   -> 被当成做完了（「好」）
#     桩子还没好。               -> 被当成做完了（否定里的「好」）
#     桩子松了，明天得弄。         -> 被当成做完了（「弄」）
#     我做了个梦，梦见桩子。       -> 被当成做完了（「做」）
#
# 现在改用**明确表完成的双字词** —— 这些在中文里几乎只用来表示做完：
# ## 为什么用「组合规」而不是列举
#
# 前两版都是列举：先是 11 个单字，再是双字短语。两版都不行 ——
# 中文的完成表达太灵活：
#
#     夯好了 / 弄完了 / 修好了 / 带来了 / 送去了 / 办妥了 / 做成了
#
# 列举永远追不上。而它们**有共同的语法形态**：
#
#     动作动词 + 完成补语
#
# 所以用正则判两个组合规，比列举稳得多：
#
#     ① 动词 + 好/完/成/妥/了      夯好了、弄完、办妥
#     ② 动词 + 来/去/到 + 了        带来、送去、收到了
#
# ⚠️ 光有完成补语不够（「天气真好」里的「好」会命中），
#    所以必须**动词和补语一起出现**。
_DONE_RE = re.compile(
    # ① 动词 + 完成补语（紧邻）
    r"(?:做|弄|修|办|搞|整|夯|干|洗|收|写|画|建|搭|补|钉|扎|挖|种|摘)"
    r"(?:好|完|成|妥)"
    # ② 动词 + 「了」/「回/来/去/到」
    #    「了」出现即表完成，而否定（还没弄）由 _UNDONE 挡掉
    r"|(?:带|送|拿|取|收|买|找|做|弄|修|办|夯|干|洗|收)"
    r"(?:了|回|来|去|到)"
    # ②b 中间隔一点距离也算：「带了两条鱼回来」
    r"|(?:带|送|拿|取|收|买|找)[^。！？，,]{0,6}(?:回|来|去|到)"
    # ③ 完成副词 / 词
    r"|已经|早就|兑现"
)
# 否定 / 未完成 —— 出现这些就**不能**算做到。
# 单靠 _DONE 挡不住「还没弄好」这种。
_UNDONE = re.compile(
    r"(还没|没弄|没做|没好|没完|没给|没送|没带|没办|"
    r"要弄|要做|得弄|得做|该弄|该做|想弄|想做|回头|下次|明天再|待会儿|"
    r"忘了|算了|不用了|作罢|"
    # 「做了个梦」「做了个决定」这类 —— 万能动词 + 量词，不是做事
    r"做(?:了|个)梦|做了个|想(?:了|起)|梦(?:见|到))"
)
# 判「承诺被兑现」时，两边的共有词要**足够具体**。
#
# 用二字片段，但滤掉虚词，并且要求至少一个片段长度 2 且不是动作动词。
_match_stop = set("的了是我你他她它们在有和与就都也还很"
                  "不没别太一二三四五六七八九十个这那哪什么"
                  "帮你给送带弄做办弄好完")

# 不算「共有实词」的字 —— 虚词 + **纯动作动词**。
#
# 为什么要滤掉动作动词：承诺里几乎都带「帮 / 给 / 送 / 带 / 弄 / 做 / 办」，
# 留着它们，下面两件事会互相匹配上：
#
#     承诺「帮我夯一下桩子」 × 事件「帮我修篱笆」   -> 共有「帮」
#     承诺「明天给你带两条鱼」 × 事件「给你带了种子」-> 共有「给」「带」
#
# 滤掉之后，「桩子」「篱笆」「鱼」「种子」这些**具体名词**才是匹配依据。
_STOP = set("的了是我你他她它们在有和与就都也还很"
            "不没别太一二三四五六七八九十个这那哪什么"
            "帮给送带弄做办干搞整把被让叫")


def _content_grams(text: str, n: int = 2) -> set[str]:
    """切出有信息量的二字片段。和 mind_character 里那套同一个思路。"""
    import re as _re
    s = _re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    out = set()
    for i in range(len(s) - n + 1):
        g = s[i:i + n]
        if any(ch not in _STOP for ch in g):
            out.add(g)
    return out


def mark_kept_by_events(promises: list, events: list,
                        now: datetime | None = None) -> list[str]:
    """拿事件去兑现承诺。返回被标记的那几条的说明。

    Args:
        promises: Promise 列表（**会被就地修改**）。
        events: 事件列表（Event 对象或 dict）。
        now: 当前时间。

    Returns:
        被标记的承诺的短说明，比如 ["夯桩子"]。
    """
    done: list[str] = []
    if not promises or not events:
        return done

    def ev_text(e):
        return getattr(e, "text", "") if not isinstance(e, dict) else str(e.get("text", ""))

    def ev_day(e):
        return getattr(e, "day", "") if not isinstance(e, dict) else str(e.get("day", ""))

    for p in promises:
        if getattr(p, "kept", False):
            continue
        pg = _content_grams(p.text)
        if not pg:
            continue
        p_day = (getattr(p, "at", "") or "")[:10]

        for e in events:
            t = ev_text(e)
            if not t:
                continue
            # ① 共有**具体**的词。
            #    虚词和纯动作动词（帮/给/送/带/弄/做/办/完）不算 ——
            #    否则「帮我夯桩子」和「帮我修篱笆」会因为「帮」而互相匹配。
            if not (pg & _content_grams(t)):
                continue
            # ② 有明确表完成的词
            if not _DONE_RE.search(t):
                continue
            # ②b 但**不能被否定吞掉**
            #     「桩子还没弄好」有「弄好」，可它说的正是没做。
            if _UNDONE.search(t):
                continue
            # ③ 不早于承诺日 —— 没做不能算兑现
            e_day = ev_day(e)
            if p_day and e_day and e_day < p_day:
                continue

            p.kept = True
            p.kept_at = (now or datetime.now()).isoformat(timespec="seconds")
            done.append(p.text[:20])
            break

    return done
