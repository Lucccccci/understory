#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她的角色状态：记忆、关系、日程。

## 为什么需要这个文件

这座镇子的对话系统是**单轮**的：她说完一句就忘了，没有连续对话，
也不会记得你上周说过什么。所以「完整的人生」必须由这个程序补上三样东西：

1. **对话记忆** —— 她记得刚才聊了什么（短期），也记得你提过的重要事情（长期）
2. **关系进展** —— 从陌生到熟悉是**慢慢累积**的，不是靠刷次数
3. **她自己的日子** —— 她有自己的作息与心事，不是站在原地等你按对话键

第 3 条是最关键的差别：**她应该有自己的生活，而不是一个问答机器。**
所以她会随时间推移产生「自己的事」，并在合适的时机主动提起。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
from datetime import datetime, timedelta

import mind_life
import mind_event
from mind_event import Event
from mind_mood import Mood, apply_turn, refresh_if_due
from mind_note import Note
from mind_promise import Promise, make as make_promise, mark_kept_by_events
from typing import Any


# ─────────────────────────────────────────────────────────────
# 关系阶段（键名必须与 tools/corpus/stage_examples.json 一致）
#
# ⚠️ 阶段**由这座镇子存档决定**，不再由窗口自己累积好感。
#
# 为什么改成这样（原设计的教训）：
# 一开始窗口里自己攒好感、游戏里也有好感，两套数值互相打架 ——
# 游戏里 9 心交往换算过来落在「成婚」档，而游戏状态又说「交往」，
# 结果生效阶段完全错乱。
#
# 现在只有一套数值：游戏里的 Friendship.Points（250 点 = 1 心，上限 2500）。
# 窗口只负责**读**，不负责**涨**。这样永远不会不一致。
# ─────────────────────────────────────────────────────────────
STAGES = [
    # (键, 中文名, 所需好感, 说明)
    ("hearts_0_1",   "初识",     0,    "客气、有距离，只聊农事与天气"),
    ("hearts_2_3",   "渐渐熟悉",  60,   "记住你的一些事，关心还是乡邻式的"),
    ("hearts_4_5",   "朋友",     160,  "认可你，愿意讲自己的事，会开淡玩笑"),
    ("hearts_6_7",   "心里有你",  320,  "越在意越装作平常，话说到一半会停住"),
    ("hearts_8_plus", "情根深种", 520,  "不留痕迹的偏心，仍然不说破"),
    ("dating",       "交往",     700,  "关系挑明了，但还在慢慢走"),
    ("engaged",      "订婚",     950,  "许了婚约，礼数还在，收着"),
    ("married",      "成婚",     1200, "亲密体现在理所当然的日常里"),
]


def stage_of(affection: int) -> tuple[str, str]:
    """好感值 -> (阶段键, 中文名)。"""
    key, name = STAGES[0][0], STAGES[0][1]
    for k, n, need, _ in STAGES:
        if affection >= need:
            key, name = k, n
    return key, name


def next_stage(affection: int) -> tuple[str, int] | None:
    """下一个阶段与所需好感；已到顶则 None。"""
    for k, n, need, _ in STAGES:
        if affection < need:
            return n, need - affection
    return None


def stage_from_game(game_ctx) -> tuple[str, str, int]:
    """由游戏存档决定关系阶段。

    返回 (阶段键, 中文名, 好感点数)。

    ## 映射规则

    | 游戏里的状态 | 窗口里的阶段 |
    |---|---|
    | Status = Married | 成婚 |
    | Status = Engaged | 订婚 |
    | Status = Dating  | 交往 |
    | 其余，按心数分档 | 0-1心 初识 / 2-3心 渐渐熟悉 / 4-5心 朋友 |
    |                  | 6-7心 心里有你 / 8心+ 情根深种 |

    读不到存档（或存档里还没认识她）就返回「初识」——
    因为你们确实还没在那个世界里相识。
    """
    try:
        rel = getattr(game_ctx, "her", None)
    except Exception:
        rel = None

    if rel is None or not rel.found:
        k, n = STAGES[0][0], STAGES[0][1]
        return k, n, 0

    points = max(0, rel.points)
    hearts = points // 250
    status = (rel.status or "").strip()

    # 关系既成事实优先
    if status == "Married":
        return "married", "成婚", points
    if status == "Engaged":
        return "engaged", "订婚", points
    if status == "Dating":
        return "dating", "交往", points

    # 其余按心数分档
    key = "hearts_0_1"
    if hearts >= 8:
        key = "hearts_8_plus"
    elif hearts >= 6:
        key = "hearts_6_7"
    elif hearts >= 4:
        key = "hearts_4_5"
    elif hearts >= 2:
        key = "hearts_2_3"

    name = dict((k, n) for k, n, _, _ in STAGES)[key]
    return key, name, points


def stage_by_key(key: str) -> str:
    """阶段键 -> 中文名。"""
    return dict((k, n) for k, n, _, _ in STAGES).get(key, key)


def derive_stage(status_name: str, points: int) -> tuple[str, str]:
    """从**缓存的**关系字段推导阶段。

    界面与 prompt 都可能在没有存档（比如游戏没装/没玩过）时被调用，
    所以需要一条不依赖 GameContext 的路径。
    """
    name_to_key = {n: k for k, n, _, _ in STAGES}

    # 缓存里存的是中文阶段名
    if status_name in name_to_key:
        return name_to_key[status_name], status_name

    hearts = max(0, points) // 250
    if hearts >= 8:
        key = "hearts_8_plus"
    elif hearts >= 6:
        key = "hearts_6_7"
    elif hearts >= 4:
        key = "hearts_4_5"
    elif hearts >= 2:
        key = "hearts_2_3"
    else:
        key = "hearts_0_1"
    return key, stage_by_key(key)


# ─────────────────────────────────────────────────────────────
# 她自己的日子
#
# 这是「完整的人生」的核心：她有作息、有手边的事、有自己的心事。
# 每次开场时，程序会告诉她「你现在正在做什么、心里在想什么」，
# 她就能自然地把这些讲出来，而不是每次都从零开始找话题。
#
# ⚠️ 配比提醒（实测踩过）：这一节曾几乎全是农活，导致她开口闭口播种翻土，
# 被使用者指出「过于拘束于农事」。现在刻意混入大量与农活无关的事 ——
# 她在缝补、在发呆、在算账、在想家、在听雨。
# **农事只是她生活的一部分。** 增删条目时请保持这个平衡。
# ─────────────────────────────────────────────────────────────
DAILY_LIFE = {
    "凌晨": [
        "还没睡，就着灯看了会儿稻种的成色",
        "起夜时顺手把灶上的火压了压",
        "睡不着，在院里站了会儿，听风",
        "翻一本旧册子，翻到一半就搁下了",
    ],
    "清早": [
        "刚把院子扫了，顺手洒了点水",
        "煮了一锅粥，还温在灶上",
        "在檐下梳头，梳了好一会儿",
        "给窗台上那盆草换了水",
        "趁露水没干，去地里看了一圈苗",
    ],
    "上午": [
        "给人写回信，写了一半停住了",
        "在翻箱倒柜找一样东西，还没找着",
        "把旧衣裳拆了，想改成别的用场",
        "拿炭条在纸上画东西，画得不太像",
        "在试验田里间苗，腰有些酸",
        "把晒着的种子翻了一遍",
    ],
    "中午": [
        "刚吃罢饭，在檐下歇脚",
        "把腌的菜翻了个坛子，尝了口咸淡",
        "歪在椅子上打了个盹，梦见点事，醒了就忘了",
        "就着日头补衣裳，针脚比往常粗",
    ],
    "下午": [
        "在补一张渔网，补得有些烦了",
        "去镇上换了点盐，顺路看了看那棵老树",
        "坐在门坎上，看云往哪边走",
        "同镇上的人说了会儿话，说的都是闲事",
        "在河边坐了一阵，没钓，就是坐着",
        "翻一本讲草木的书，看得慢",
    ],
    "傍晚": [
        "把晾着的衣裳收了",
        "在地头站了一会儿，看日头落下去",
        "烧水，准备晚饭",
        "把灯挑亮了些，屋里亮堂些心里也松快",
        "听隔壁院子里孩子吵闹，听了一会儿",
    ],
    "夜里": [
        "在灯下翻一本旧书",
        "把明天要用的农具擦了一遍",
        "听着外面的虫声，有些出神",
        "算了算日子，然后又算了算别的",
        "给自己倒了碗热水，坐了很久",
    ],
}

# 她的心事（随时间浮现，让她有内在生活，而不是只有反应）
#
# 同样刻意控制农事占比：一千年的生命里，土地只是其中一件放不下的事。
INNER_THOUGHTS = [
    "想起家里那几个小的，不知道他们近来乖不乖",
    "有些记不清家乡春天的样子了",
    "算了算自己在这世上待了多久，然后就不算了",
    "惦记着一件很久以前没做完的事",
    "想着有个人答应过要回来，后来没回来",
    "盘算着这块地明年该轮种什么",
    "念着那句『人误地一时，地误人一年』",
    "想着今年收成要是好，该给大家分些什么",
    "想起以前住过的一个地方，院子里的那棵树",
    "在想一件事要不要说出口，想了很久了",
]


def period_of(dt: datetime) -> str:
    h = dt.hour
    if h < 5:
        return "凌晨"
    if h < 9:
        return "清早"
    if h < 12:
        return "上午"
    if h < 14:
        return "中午"
    if h < 17:
        return "下午"
    if h < 20:
        return "傍晚"
    return "夜里"


# ─────────────────────────────────────────────────────────────
# 本次运行的随机种子。让问候语「同一次运行内稳定、下次启动会变」。
# 原来只用日期做种子，同一天开多次会听到同一句 —— 第三次就露馅了。
_RUN_SEED = str(random.randint(100000, 999999))


_PUNCT_RE = re.compile(r"[\s，。！？、；：“”‘’（）【】\[\]…—·]+")

# 高频虚词。二字片段里若全是这些字，等于没提供信息
# （比如「我家」「那条」），不该用来判定相关性。
# 召回的最低分（2026-10-23 加）。
#
# ## 为什么要有门槛
#
# 原来的判据是「任意一个二字片段重合就算命中」，于是：
#     「你知道吗」 -> 37 条命中，全是含「知道」的轮次
#     「今天好累」 -> 34 条命中，全是含「今天」的轮次
# 取前 6 条注入，等于随机挑。白烧 token，还可能让她顺口提一件不相干的事。
#
# ## 为什么是 IDF 加权而不是简单比例
#
# 「今天」在 1455 轮里出现几百次，「坛子」只出现十几次 ——
# 两种命中不该同分。按片段稀有度加权后：
#     那个坛子  1.00 → 0.44（不变，还是强命中）
#     今天好累  只有「最近好累呢」那条浮到 0.37，其余掉到 0.23 以下
#
# ## 0.25 是怎么定的
#
# 实测（见提交说明）：
#     具体话题词 40/40 仍能翻到 —— 一个不丢
#     14% 的句子不再触发召回，抽样看全是「好呢」「走过去开始」这类措辞重合
#     平均注入 198 字/句 → 85 字/句
#
# 已知代价：**长句里顺带提一句旧事会翻不到**（>40 字的句子只占 2%）。
# 试过切小句来救，结果救错对象（提坛子的长句拉回来的是「收拾屋子」），
# 所以不做。
RECALL_MIN_SCORE = 0.25

_STOP_CHARS = set("的了我你他她它是在有和与就不都也很还这那什么吗呢吧啊呀"
                  "一个们到去来会着过被把给对从为以及之其此等")


def _normalize_for_match(text: str) -> str:
    """去掉标点与空白，用于字词重合度比较。

    中文没有空格，所以比较前先把标点抹掉，剩下的连续汉字才能切出二字片段。
    """
    return _PUNCT_RE.sub("", text or "")


def _content_grams(text: str) -> set[str]:
    """切出「有信息量的」二字片段。

    过滤掉全是虚词的片段 —— 否则「我家那条狗」和「我家有块地」会因为
    「我家」而互相匹配，那没有意义。
    """
    body = _normalize_for_match(text)
    out: set[str] = set()
    for i in range(len(body) - 1):
        g = body[i:i + 2]
        if any(ch not in _STOP_CHARS for ch in g):
            out.add(g)
    return out


@dataclass
class Memory:
    """一条长期记忆。

    分两类，用途完全不同：
        fact     —— 她记住的关于你的事（「我家的狗叫大黄」）
        guidance —— **你让她注意的事**（「别老提我钓鱼」）

    guidance 来自「你能纠正她」这个功能。它不是事实，而是**行为要求**，
    所以进 prompt 的方式不同（放在规则区，而不是记忆区）。
    """
    text: str
    created: str            # ISO 时间
    weight: int = 1         # 初始权重（模型判的 salience 0–3）
    last_touch: str = ""    # 最后一次被检索到的时间（决定衰减）
    hits: int = 0           # 被检索到的次数
    kind: str = "fact"      # fact | guidance
    source: str = "chat"    # chat | manual | correction

    @staticmethod
    def now(text: str, kind: str = "fact", source: str = "chat",
            weight: int = 1) -> "Memory":
        ts = datetime.now().isoformat(timespec="seconds")
        return Memory(text=text, created=ts, kind=kind, source=source,
                      weight=weight, last_touch=ts)


@dataclass
class Turn:
    """一轮对话。"""
    role: str               # "player" | "her"
    text: str
    at: str
    emotion: str = ""       # 立绘表情记号（$h/$s/$u/$l/$a）
    # 从哪儿来的：web / qq / qqgroup（2026-11-08）
    # ⚠️ 只做记录，**不进提示词** —— 她不该知道"你是在手机上说的"。
    channel: str = ""

    @staticmethod
    def make(role: str, text: str, emotion: str = "",
             channel: str = "web") -> "Turn":
        return Turn(role=role, text=text,
                    at=datetime.now().isoformat(timespec="seconds"),
                    emotion=emotion, channel=channel)


@dataclass
class Favorite:
    """收藏的一句话 —— 连同当时的场景。

    ## 为什么要带 context 和 mood

    只存「……我收下了」这种孤句，过半年翻出来完全想不起前因后果。
    带上「你当时说了什么」和「她那天的状态」，才算留下了那一刻。
    """
    text: str                   # 她说的那句话
    at: str                     # 收藏时间（ISO）
    context: str = ""           # 你上一句说了什么
    mood: str = ""              # 她当时的心情标签，如「懒懒的」
    stage: str = ""             # 当时的关系阶段中文名
    emotion: str = ""           # 立绘表情记号
    note: str = ""              # 你自己写的一句备注

    @staticmethod
    def now(text: str, context: str = "", mood: str = "",
            stage: str = "", emotion: str = "") -> "Favorite":
        return Favorite(text=text,
                        at=datetime.now().isoformat(timespec="seconds"),
                        context=context, mood=mood, stage=stage,
                        emotion=emotion)


def _quarantine_corrupt(path: str, err: Exception) -> None:
    """把读不出来的存档改名留证，并喊一声。

    **绝不删除** —— 文件坏了不等于内容全没，人工还能从里面捞。
    """
    import os
    from datetime import datetime as _dt
    try:
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        bad = f"{path}.corrupt-{stamp}"
        os.replace(path, bad)
        print(f"[存档] 读不出来，已隔离（没删）：{bad}\n"
              f"       原因：{type(err).__name__}: {err}",
              file=__import__("sys").stderr, flush=True)
    except OSError:
        # 连改名都做不到，那就只能照旧了 —— 但至少别挡住她开口
        pass


@dataclass
class Character:
    """她的状态。

    ⚠️ **关系阶段不在这里** —— 它由这座镇子存档决定（见 stage_from_game）。

    窗口只保存「她自己的东西」：说过的话、记住的事、今天在做什么。
    关系是你们在那个世界里的经历，窗口不该有第二份。
    """

    turns: list[Turn] = field(default_factory=list)
    memories: list[Memory] = field(default_factory=list)
    # 她今天手边的事 / 心里的念头（每天换）
    today_task: str = ""
    today_thought: str = ""
    today_key: str = ""
    # 统计
    first_met: str = ""
    total_turns: int = 0

    # 最近一次从游戏读到的关系（只读缓存，供界面显示与 prompt 使用）
    game_points: int = 0
    game_status: str = ""
    game_npc_key: str = ""
    # 游戏存档的读取时间，用于界面提示「这是什么时候的数据」
    game_read_at: str = ""

    # 开场问候语。存下来而不是每次现挑 —— 否则刷新页面问候语就变了。
    # greeting_seed 记录「它是用哪个随机种子挑的」：
    #   * 同一次运行内不变（刷新页面问候语不跳）
    #   * 下次启动会变（同一天开多次不会永远听到同一句）
    #   * 换了一天或关系变了也会重挑
    greeting: str = ""
    greeting_seed: str = ""
    greeting_day: str = ""
    greeting_stage: str = ""

     # 当时隔了多久（「两天」这类，给人看的）
    # 上次说话的日子，用于算「隔了几天没来」
    last_talk_date: str = ""

    # 她此刻的心情（情绪 + 精力 + 今天发生的一件小事）
    # 一小时刷新一次，见 mind_mood.py 的说明。
    mood: dict = field(default_factory=dict)

    # 你收藏的她的话。**独立于 turns** —— turns 有 2000 条上限会被裁，
    # 收藏是你要长期留住的，不能跟着一起被滚掉。
    favorites: list[Favorite] = field(default_factory=list)

    # 她给你留的话（你不在的时候写的），见 mind_note.py
    notes: list[Note] = field(default_factory=list)

    # 你答应过的事。她记着，但**绝不催** —— 见 mind_promise.py
    promises: list[Promise] = field(default_factory=list)

    # 她自己的日子：小麻烦、当天的口味、以及**她眼里的你**
    # （最后一项是亲密本身，不是记忆条目 —— 见 mind_life.py）
    # 今天的季节 —— refresh_day 时写进来，供「她自己的事」生成时用
    today_season: str = ""
    today_trouble: str = ""
    today_tastes: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    # 每条观察是什么时候看出来的（文本 -> ISO 时间）。
    #
    # 为什么要单独存而不把 observations 改成 list[dict]：
    #     `observations` 还被 `mind_life.trouble_block` 和前端当字符串列表用，
    #     改结构会牵连三处。并一个按文本索引的字典是**纯增量**的，
    #     旧存档没有这个字段也能读（`Character.load` 只挑认识的键）。
    # 「手边/心里」是哪一天由模型生成的（用来判断今天要不要重新生成）
    # 她此刻在哪个地方（院子/堂屋/厨房/卧房/后廊/卫生间/储物角）
    today_place: str = ""
    life_brief_day: str = ""
    # 从对话里长出来的口味变化：[{item, verdict, day}]
    taste_notes: list = field(default_factory=list)
    observation_meta: dict[str, str] = field(default_factory=dict)
    # 观察的「最后一次被看到」。和 meta 分开，避免动老数据。
    observation_touch: dict[str, str] = field(default_factory=dict)
    # 和已有观察矛盾、等再看见一次才改判的候选
    # 很旧的对话压成的一段摘要（见 mind_rollup.py）
    context_rollup: str = ""
    # 已经折进摘要的轮次数（turns[:rollup_count] 都在摘要里）
    rollup_count: int = 0
    observation_pending: list = field(default_factory=list)
    # 每条观察的证据：{文本: {"first": 日期, "last": 日期,
    #                        "days": [日期...], "quote": 一句原话}}
    # 2026-11-08 加 —— 没有证据的观察读起来像心理评估，
    # 有了证据她才能说"上次你说累了还硬撑着"，而不是"你不爱惜自己"。
    observation_evidence: dict = field(default_factory=dict)

    # 上一次「用模型试着看出新观察」的时间和轮次 —— 冷却用。
    # 不管成没成都要记，否则看不出新的时会每轮重试。
    observation_tried_at: str = ""
    observation_tried_turn: int = 0

    # ── 观察的「候选池」（2026-10-20 加）─────────────────
    #
    # 一条观察要进 `observations`（显示在她眼里），得先在**至少 3 个不同的
    # 日期**被看见。只看过一两次的留在这里，**不显示给用户**。
    #
    # 为什么要有这一层：原来的判据是「每 30 分钟、只看最近 10 轮、产一条」，
    # 于是 26 小时攒了 8 条，其中 5 条是一次性反应或心理推断。
    # 一次行为不构成「他是什么样的人」—— 那需要一个证据门槛。
    #
    # 结构：[{"text": "他……", "days": ["10-19","10-20"], "first": ISO,
    #         "last": ISO, "n": 2}]
    #
    # 用 list[dict] 而不是 dataclass：纯增量字段，旧存档没有也能读
    # （`Character.load` 只挑认识的键，缺了就是默认空列表）。
    observation_candidates: list[dict] = field(default_factory=list)

    # 你们之间**发生过的事** —— 关系史，不是谈资。见 mind_event.py
    #
    # 和另外几种的分工：
    #     memories      他说的信息
    #     observations  她看出来的行为模式
    #     promises      他答应的事
    #     events        **你们一起做过的事**（第一次 / 那一次）
    events: list = field(default_factory=list)

    # ── 她自己的过去（2026-10-20 加）──────────────────────
    #
    # `events` 是**你们之间**发生过的事。这一类是**她自己的日子**：
    # 「院角那畦葱太密，挖出来重栽了一排」「去镇上换盐，碰见乔迪」。
    #
    # 为什么要有：`today_task` 一天一换、不留痕，于是你三天没来，
    # 她那边过去了三天却一个字都说不出来。这一类是**累积的**。
    #
    # 复用 `Event` 的 `with_her` 字段（它一直存在但从没用过）——
    # with_her=False 表示「这件事里没有他」。
    her_events: list = field(default_factory=list)

    # ── 每日摘要（2026-10-23 加）──────────────────────────
    #
    # 结构：[{"day": "2026-10-21", "lines": ["...", "..."]}]
    #
    # 补的是「昨天到一周前」那条带子：最近的对话有 3000 字窗口，
    # 再往前就只剩零星的 memory / event，中间是断的。
    #
    # 上限 14 天。每次跨天压一份，见 mind_digest.py。
    digests: list = field(default_factory=list)

    # ── 周摘要（2026-10-24 加）────────────────────────────
    #
    # 结构：[{"from":"2026-10-17","to":"2026-10-23","lines":["...","..."]}]
    #
    # 日摘要只留 7 天逐日，再往前压成周摘要（8 周）。见 mind_week.py。
    weeks: list = field(default_factory=list)

    # ── 她手上没做完的活（2026-10-20 加）──────────────────
    #
    # 见 mind_project.py。进度由天数推出来，不是随机。
    # 做完会写进 her_events，然后从这儿移除。
    projects: list = field(default_factory=list)

    # 上次说话的时刻。last_talk_date 只到「天」，算不了「隔了几小时」，
    # 而留话要按小时判断。两个都留，各有各的用处。
    last_talk_at: str = ""

    # ── 事件 ────────────────────────────────────────────
    # ── 事件的 ★ 与裁剪（2026-10-20 加）────────────────────
    #
    # `kind == "first"` 就是「头一回」。原来只有检测器能判它，
    # 你没法手动标 —— 而「哪件事算头一回」本来就该由你说了算。
    #
    # 标了 ★ 的**永远不会被 120 条上限裁掉**。理由：头一回是关系史里
    # 最不该丢的东西，丢了它，这段记忆就断了。代价是 ★ 多了会撑大存档，
    # 但那是你自己标的，可控。

    def set_event_first(self, index: int, first: bool = True) -> bool:
        """把第 index 条事件标成 / 取消「头一回」。"""
        if not (0 <= index < len(self.events)):
            return False
        ev = self.events[index]
        want = "first" if first else "together"
        if getattr(ev, "kind", "") == want:
            return False
        ev.kind = want
        return True

    def _trim_events(self, limit: int = 120) -> None:
        """超上限时删最旧的 —— **但 ★ 一条都不删**。

        ## 为什么
        原来是无条件 `self.events[-limit:]`，于是「头一回」会和普通事件
        一起被最旧的规则淘汰。而头一回正是最不该丢的那类。
        """
        evs = self.events
        if len(evs) <= limit:
            return
        stars = [e for e in evs if getattr(e, "kind", "") == "first"]
        rest = [e for e in evs if getattr(e, "kind", "") != "first"]
        keep_rest = max(0, limit - len(stars))
        # 非 ★ 的按原顺序留最新的 keep_rest 条
        kept = rest[-keep_rest:] if keep_rest else []
        keep_ids = {id(e) for e in stars} | {id(e) for e in kept}
        # **保持原来的先后顺序**（不能先 star 后 rest，日期会乱）
        self.events = [e for e in evs if id(e) in keep_ids]

    # ── 她自己的日子 / 手上的活（2026-10-20 加）─────────────

    def note_her_event(self, text: str, kind: str = "solo",
                       day: str = "") -> object | None:
        """记一条**她自己的事**（他不在场）。

        和 `note_event` 的区别：那个是「你们之间发生过的事」，
        这个只有她。存进 `her_events`，`with_her=False`。
        """
        text = (text or "").strip()
        if not text:
            return None
        for e in (self.her_events or []):
            if getattr(e, "text", "") == text:
                return None
        day = (day or datetime.now().strftime("%Y-%m-%d"))[:10]
        ev = mind_event.Event(
            text=text, kind=kind,
            at=datetime.now().isoformat(timespec="seconds"),
            day=day, with_her=False)
        self.her_events.append(ev)
        # 上限 30 条（约一个月）—— 久了就没味道了，而且她会开始重复
        if len(self.her_events) > 30:
            self.her_events = self.her_events[-30:]
        return ev

    def advance_projects(self, season: str = "") -> list:
        """跨天推进她手上的活。返回**今天刚做完的**那几件的文字。

        推进是确定性的（天数 x 季节速度），见 mind_project.advance。
        做完的会写进 her_events，然后从 projects 移除；
        手上少于 3 件时补一件新的（做完当天不补，隔一天再说）。
        """
        import mind_project as SP
        from datetime import date as _date

        today = _date.today()
        finished: list = []
        alive = []

        for raw in (self.projects or []):
            pr = SP.Project.from_dict(raw) if isinstance(raw, dict) else raw
            SP.advance(pr, today, season)
            if pr.done:
                finished.append(pr.text)
                # 做完 -> 进她自己的旧事，这样她以后提得起这件事
                # ⚠️ 只用 text，不能带 what。
                # what 是「还差什么」，做完之后再说「还差一条腿这事了了」
                # 是自相矛盾的。
                self.note_her_event(
                    f"{pr.text}总算弄完了。",
                    kind="done", day=today.isoformat())
            else:
                # ⚠️ 存**对象**，不是 dict。
                # load() 已经把 projects 转成 Project 了，这里再转回 dict 的话，
                # 同一份数据在内存里会有两种形状 —— 接口那边按 dict 取 .get()
                # 就会 AttributeError（我刚踩过，表现为 /api/memory 直接 500）。
                alive.append(pr)

        self.projects = alive

        # 补新的：只在**手上空了**、或者已经隔了一天以上才补，
        # 免得「刚做完立刻又来一件」显得像个任务队列。
        if len(self.projects) < SP.MAX_ACTIVE:
            def _f(x, k):
                return str(getattr(x, k, "") or "") if not isinstance(x, dict) \
                    else str(x.get(k) or "")
            last = max((_f(x, "last_touched") or _f(x, "started")
                        for x in self.projects), default="")
            gap_ok = True
            if finished and last:
                try:
                    gap_ok = (_date.today() -
                              _date.fromisoformat(last[:10])).days >= 1
                except (ValueError, TypeError):
                    gap_ok = True
            if gap_ok:
                newp = SP.pick_new(self.projects, seed=today.isoformat())
                if newp is not None:
                    self.projects.append(newp)
        return finished

    def add_digest(self, day: str, lines: list) -> None:
        """存一天的摘要。同一天已有就替换（不会重复攒）。"""
        day = (day or "")[:10]
        lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
        if not day or not lines:
            return
        self.digests = [d for d in (self.digests or [])
                        if str(d.get("day", ""))[:10] != day]
        self.digests.append({"day": day, "lines": lines[:5]})
        self.digests.sort(key=lambda d: str(d.get("day", "")))
        # 上限 14 天 —— 更久的本来就该模糊了，留着占地方还会和事件打架
        if len(self.digests) > 14:
            self.digests = self.digests[-14:]

    def add_week(self, start: str, end: str, lines: list) -> None:
        """存一份周摘要。同一段（from~to）已有就替换。"""
        start, end = (start or "")[:10], (end or "")[:10]
        lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
        if not start or not end or not lines:
            return
        self.weeks = [w for w in (self.weeks or [])
                      if not (str(w.get("from", ""))[:10] == start
                              and str(w.get("to", ""))[:10] == end)]
        self.weeks.append({"from": start, "to": end, "lines": lines[:3]})
        self.weeks.sort(key=lambda w: str(w.get("from", "")))
        if len(self.weeks) > 8:
            self.weeks = self.weeks[-8:]

    def add_event_manual(self, text: str, first: bool = False):
        """手动记一件「你们之间发生过的事」。

        ## 为什么要有手动这条路

        `detect` 是从你那句话里**剪**出一个片段，判据刻意保守：
            「今天是第一次呢」      -> 现在能收了（第一次不要求活动词）
            「（抱着她…）」         -> 仍然收不到：没有活动词、也不是第一次

        而且即使自动收到，剪出来的也是半个句子 ——
        「今天是第一次呢」两个月后单看，毫无信息量。

        所以给你一个直接写的入口：**哪些事值得记、怎么措辞，你说了算。**
        """
        text = (text or "").strip()
        if not text:
            return None
        for e in (self.events or []):
            if getattr(e, "text", "") == text:
                return None
        ev = mind_event.Event.make(text, "first" if first else "together")
        self.events.append(ev)
        self._trim_events()
        return ev

    def remove_her_event(self, index: int) -> bool:
        """删掉第 index 条「她自己的事」。"""
        if 0 <= index < len(self.her_events):
            self.her_events.pop(index)
            return True
        return False

    def edit_her_event(self, index: int, text: str) -> bool:
        """改写第 index 条「她自己的事」。"""
        text = (text or "").strip()
        if not text or not (0 <= index < len(self.her_events)):
            return False
        ev = self.her_events[index]
        if getattr(ev, "text", "") == text:
            return False
        ev.text = text
        return True

    def remove_project(self, index: int) -> bool:
        """删掉第 index 件没做完的活。"""
        if 0 <= index < len(self.projects):
            self.projects.pop(index)
            return True
        return False

    def edit_project(self, index: int, text: str = "",
                     what: str | None = None, stage: int | None = None) -> bool:
        """改一件活：名字 / 还差什么 / 进度。

        进度允许你手动改 —— 你比她清楚做到哪一步了。
        """
        if not (0 <= index < len(self.projects)):
            return False
        pr = self.projects[index]
        is_obj = not isinstance(pr, dict)
        g = (lambda k, d=None: getattr(pr, k, d)) if is_obj \
            else (lambda k, d=None: pr.get(k, d))
        st_ = (lambda k, v: setattr(pr, k, v)) if is_obj \
            else (lambda k, v: pr.__setitem__(k, v))
        changed = False
        t = (text or "").strip()
        if t and g("text") != t:
            st_("text", t)
            changed = True
        if what is not None and g("what") != what:
            st_("what", str(what))
            changed = True
        if stage is not None:
            lo, hi = 0, int(g("total") or 3)
            s2 = max(lo, min(hi, int(stage)))
            if g("stage") != s2:
                st_("stage", s2)
                changed = True
        return changed

    def complete_project(self, index: int) -> str:
        """把第 index 件活**立刻**标成做完：写进她的旧事，然后移除。

        和 `advance_projects` 里那套的区别：那个是按天数自动推进，
        这个是**你手动说她做完了**（比如你在游戏里替她弄好了）。
        做完立刻进 her_events，第二天聊天里她就能提这件事。
        """
        if not (0 <= index < len(self.projects)):
            return ""
        pr = self.projects[index]
        text = (pr.get("text", "") if isinstance(pr, dict)
                else getattr(pr, "text", ""))
        if not text:
            self.projects.pop(index)
            return ""
        self.note_her_event(f"{text}总算弄完了。", kind="done")
        self.projects.pop(index)
        return text

    def note_event(self, text: str) -> "Event | None":
        """从他说的话里认出「你们之间发生过的一件事」。

        与记忆、观察的分工见 `events` 字段的注释。
        判据在 `mind_event.detect`，**刻意保守** ——
        误判的代价是她在意一件你根本没当回事的事，那比漏记更糟。

        顺带做一件事：**新事件可能兑现了某条承诺**。见下面那段。
        """
        r = mind_event.detect(text)
        if not r:
            return None
        body, kind = r
        # 同一天同一句话不重复记（他会连着说几遍）
        for e in self.events:
            if e.text == body:
                return None
        ev = mind_event.Event.make(body, kind)
        self.events.append(ev)
        self._trim_events()

        # 承诺兑现检查。
        #
        # 为什么放在这里：承诺和事件原来是两套独立记录，于是自相矛盾 ——
        #     promises  「好明天帮你夯一下桩子」   kept=False
        #     events    「他把答应我的夯桩子弄完了。」
        # 她一边记着「你答应要夯」，一边记着「你夯完了」。
        #
        # 标记成 kept 之后，`promise_block` 会自动把它从 prompt 里去掉
        # （那段已经 `if not p.kept` 过滤过了）。
        try:
            mark_kept_by_events(self.promises, [ev])
        except Exception:
            pass
        return ev

    # ── 承诺 ────────────────────────────────────────────
    def note_promise(self, text: str, role: str = "player") -> Promise | None:
        """从一句话里认出承诺并记下。认不出返回 None。

        ## role 这个参数是必须的

        只记**玩家**答应的。实测踩过：她在回复里说「我来帮你揉揉腰」，
        被 regex 命中后记成了「他答应的事」——归属完全反了。

        同一条只记一次（按文本判重），重复说不会堆出好几份。
        """
        if role != "player":
            return None
        p = make_promise(text)
        if p is None:
            return None
        for old in self.promises:
            if old.text.strip() == p.text.strip():
                return old
        self.promises.append(p)
        # 没做到的只留最近 30 条
        live = [x for x in self.promises if not x.kept]
        if len(live) > 30:
            keep = set(id(x) for x in live[-30:])
            self.promises = [x for x in self.promises
                             if x.kept or id(x) in keep]
        return p

    def live_promises(self) -> list["Promise"]:
        return [x for x in self.promises if not x.kept]

    def mark_promise_kept(self, text: str = "", at: str = "") -> bool:
        for p in self.promises:
            # 原话比日期精确：同一天可能有好几条（2026-11-08）
            if ((text and p.text.strip() == text.strip())
                    or (not text and at and p.at == at)):
                p.kept = True
                p.kept_at = datetime.now().isoformat(timespec="seconds")
                return True
        return False

    def forget_promise(self, text: str = "", at: str = "") -> bool:
        for i, p in enumerate(self.promises):
            # 原话比日期精确：同一天可能有好几条（2026-11-08）
            if ((text and p.text.strip() == text.strip())
                    or (not text and at and p.at == at)):
                del self.promises[i]
                return True
        return False

    def pending_notes(self) -> list[Note]:
        """还没看过的留话，旧的在前（按她写的顺序读）。"""
        return [x for x in self.notes if not x.seen]

    def add_note(self, note: Note) -> None:
        self.notes.append(note)
        # 看过的只留最近 50 条，免得存档无限涨
        seen = [x for x in self.notes if x.seen]
        if len(seen) > 50:
            keep = set(id(x) for x in seen[-50:])
            self.notes = [x for x in self.notes if not x.seen or id(x) in keep]

    def mark_notes_seen(self) -> int:
        """把没读过的留话**删掉**，返回删了几条。

        ## 为什么是删，不是标已读（2026-10-19 改）

        原来是「标成已读，留着」。于是折叠头会变成「她留过的（2）」，
        一叠读过的纸条永远挂在界面上。用户的原话：
        「这一层改成已读一次后就清理」。

        现在读完即删。⚠️ 是真删 —— 只有前端在「展开」那一下才调它。
        """
        # ⚠️ 是**清空**，不是「删已读、留未读」。
        #
        # 写成 `[x for x in self.notes if not x.seen]` 会把方向搞反 ——
        # 那样删掉的是已读的，反而把没读的留下了。
        n = len(self.notes)
        self.notes = []
        return n

    # ── 收藏 ────────────────────────────────────────────
    def add_favorite(self, text: str, context: str = "", mood: str = "",
                     stage: str = "", emotion: str = "",
                     note: str = "") -> Favorite:
        """收藏一句话。同一句重复收藏会返回已有的那条，不产生重复项。"""
        t = (text or "").strip()
        for f in self.favorites:
            if f.text.strip() == t:
                if note and not f.note:
                    f.note = note
                return f
        fav = Favorite.now(t, context=context, mood=mood,
                           stage=stage, emotion=emotion)
        fav.note = note
        # 新的放最前 —— 翻的时候先看到最近的
        self.favorites.insert(0, fav)
        return fav

    def remove_favorite(self, at: str = "", text: str = "") -> bool:
        """按收藏时间或原句删除。"""
        for i, f in enumerate(self.favorites):
            if (at and f.at == at) or (text and f.text.strip() == text.strip()):
                del self.favorites[i]
                return True
        return False

    def edit_favorite(self, at: str, note: str = "") -> bool:
        """改备注（不动台词本身 —— 那是她说过的，不该被改）。"""
        for f in self.favorites:
            if f.at == at:
                f.note = note
                return True
        return False

    def is_favorite(self, text: str) -> bool:
        t = (text or "").strip()
        return any(f.text.strip() == t for f in self.favorites)

    # ── 心情 ────────────────────────────────────────────
    def refresh_mood(self) -> tuple["Mood", bool]:
        """距上次超过一小时就重掷心情。返回 (心情, 是否刷新过)。

        调用时机：每次进入对话之前。**不刷新就没有变化** ——
        所以反复刷新页面不该重置它，只有真的过了一小时才换。
        """
        m = Mood.from_dict(self.mood)
        m, changed = refresh_if_due(m)

        # 精力现在是**按时刻算的**（时辰曲线 + 会话疲劳），不是累加值。
        # 所以每次进来都重算一遍 —— 否则界面上的数会停在几小时前。
        # 情绪仍由 roll 决定，这里不动。
        try:
            import mind_activity
            _asleep = mind_activity.current_activity(self) == "asleep"
        except Exception:
            _asleep = False
        try:
            import mind_mood as _M
            _e = _M.derive_energy(session_turns=self.contiguous_turns(),
                                  asleep=_asleep)
            if _e != m.energy:
                m.energy = _e
                changed = True
        except Exception:
            mind_diag.note("refresh_mood 里的 derive_energy")

        if changed:
            self.mood = m.to_dict()
        return m, changed

    def contiguous_turns(self, gap_hours: float = 2.0) -> int:
        """这一段连续聊了几轮。往回数，遇到间隔 >= gap_hours 就停。

        ## 为什么从 turns 现算，不存计数器

        存了就要维护：什么时候清零、存档迁移、多端并发 —— 处处可能忘。
        从时间戳直接算，**永远和事实一致**。

        Returns:
            这一段里的对话条数（player + shu 都算）。
        """
        ts = []
        for t in reversed(self.turns or []):
            at = getattr(t, "at", "") or ""
            if not at:
                continue
            try:
                ts.append(datetime.fromisoformat(at))
            except (ValueError, TypeError):
                continue
        if not ts:
            return 0
        n = 1
        for i in range(len(ts) - 1):
            if (ts[i] - ts[i + 1]).total_seconds() >= gap_hours * 3600:
                break
            n += 1
        return n

    def note_turn_mood(self, user_text: str, her_reply: str = "",
                       valence_delta: int | None = None) -> None:
        """一轮对话之后微调心情。

        Args:
            valence_delta: 情绪增量。**由 `mind_feel.judge` 判出来**。
                不给就退回词表（见 `mind_mood.apply_turn` 的说明）。
        """
        m = Mood.from_dict(self.mood)
        m, _ = apply_turn(m, user_text, her_reply, valence_delta,
                          session_turns=self.contiguous_turns())
        self.mood = m.to_dict()

    # ── 与游戏存档同步 ────────────────────────────────────
    def sync_from_game(self, game_ctx) -> tuple[bool, str]:
        """从游戏存档刷新关系。

        返回 (是否有变化, 说明)。

        ## 只读，不累积

        窗口**不产生**关系进展 —— 那是游戏里的事（送礼、说话、送花束）。
        窗口只负责如实反映它。这样永远不会出现两套数值打架。

        你在这个世界的进展，取决于你在这座镇子里怎么待她。
        """
        # 读不到存档时**直接返回**，绝不覆盖已有值。
        # 脱钩后仍有三处在调它；那时的 game_ctx 是空的 ——
        # 不挡住就会把「交往」写回「初识」（2026-10-27 实测）。
        if game_ctx is None or not getattr(game_ctx, "found", False):
            return False, ""
        key, name, points = stage_from_game(game_ctx)

        changed = (points != self.game_points) or (name != self.game_status)
        old_name = self.game_status or "初识"

        try:
            rel = getattr(game_ctx, "her", None)
            self.game_status = name
            self.game_points = points
            self.game_npc_key = rel.npc_name if (rel and rel.found) else ""
            self.game_read_at = datetime.now().isoformat(timespec="seconds")
        except Exception:
            pass

        note = ""
        if changed and points:
            note = f"关系：{old_name} → {name}（{points // 250} 心）"
        return changed, note

    def effective_stage(self) -> tuple[str, str]:
        """当前关系阶段（由游戏决定，这里只是把缓存读出来）。"""
        return derive_stage(self.game_status, self.game_points)

    # ── 开场问候语 ────────────────────────────────────────
    #
    #  #### 现在是实时生成的
    #
    #  原来是从 greetings.json 里按「关系阶段 + 离别时长」挑一句。
    #  八档关系改成一档之后那份文件作废了，而且**挑**本身就不够 ——
    #  固定候选里没有「今天的天气」「她手边在做什么」，读起来像模板。
    #
    #  现在改法：
    #
    #      greeting        存着上一次生成的结果
    #      greeting_seed   记着它是哪一次运行生成的
    #
    #  `refresh_greeting()` 只在「本次运行还没生成过」时返回空，
    #  由 server 那边起后台线程调 `set_greeting()` 写进来。
    #  **不在这个函数里调模型** —— 它被 status 轮询调用，会卡住页面。

    def greeting_stale(self) -> bool:
        """本次运行是否还没有开场白（要重新生成）。"""
        return (not self.greeting) or (self.greeting_seed != _RUN_SEED)

    # ── 重做一轮 ────────────────────────────────────────
    def drop_last_exchange(self) -> int:
        """把最后那对问答从记忆里去掉。返回去掉了几条。

        ## 为什么要这个

        「重生成」和「改我的话重发」是同一件事：**先把旧的抹掉再重跑**。

        不抹掉的话她会看到重复的输入：

            [你的原话] [她的旧回复] [你的原话]      <- 她以为你说了两遍

        实测踩过：连着问两次同一个问题，她回「这话你今天是第二回说了」。

        ## 两种情况都要处理

            正常      最后两条是 [player, shu]     -> 去掉两条
            上次失败   最后一条是 [player]（没回复）-> 去掉一条
        """
        if not self.turns:
            return 0
        n = 0
        # 先看末尾是不是她的回复
        if self.turns[-1].role == "her":
            self.turns.pop()
            n += 1
        if self.turns and self.turns[-1].role == "player":
            self.turns.pop()
            n += 1
        return n

    def set_greeting(self, text: str) -> None:
        """存下刚生成的开场白。由后台线程调。"""
        self.greeting = (text or "").strip()
        self.greeting_day = datetime.now().strftime("%Y-%m-%d")
        self.greeting_seed = _RUN_SEED
        # stage 字段留着，但已经不再参与判断了（八档改成一档）
        self.greeting_stage = "together"

    def refresh_greeting(self) -> str:
        """返回当前的开场白。**不生成**，没有就返回空串。

        生成走 server 的后台线程（见 `_maybe_spawn_greeting`）。
        这里保留一个同名入口，是为了不改 server 里那一行调用。
        """
        if self.greeting_stale():
            return ""
        return self.greeting

    def mark_talked(self) -> None:
        """记下今天说过话。用于下次算「隔了几天」。"""
        self.last_talk_at = datetime.now().isoformat(timespec="seconds")
        self.last_talk_date = datetime.now().strftime("%Y-%m-%d")

    # ── 持久化 ────────────────────────────────────────────
    @staticmethod
    def load(path: str) -> "Character":
        if not os.path.isfile(path):
            c = Character(first_met=datetime.now().isoformat(timespec="seconds"))
            c.refresh_day(force=True)
            return c
        try:
            raw = json.loads(open(path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as e:
            # ⚠️ 原来这里直接返回一个全新的空档，**而把坏文件留在原位** ——
            # 下一次任何保存就会把它盖成空档。几个月的记忆，
            # 在一行 except 里没了，而且不报错。
            #
            # 这和用户误触「重新开始」那次是同一个模式：
            # 看起来正常，实际丢了。
            #
            # 现在：**坏档改名留证**，让人能看见、能救。
            # 新档照建（否则她一开口就崩），但旧文件还在，
            # 事后能人工捞回来。
            _quarantine_corrupt(path, e)
            c = Character(first_met=datetime.now().isoformat(timespec="seconds"))
            c.refresh_day(force=True)
            return c

        # 只挑数据类真正认识的字段，多余的忽略、缺失的用默认值。
        # 这样旧存档能读，新加的字段也不会因为忘记改这里而永远读不到。
        known = {f.name for f in dataclass_fields(Character)}
        kwargs = {k: v for k, v in raw.items() if k in known and k != "turns"}
        # 嵌套的数据类要手动还原 —— asdict 存的是 dict，读回来得转成对象。
        # 漏了这步不会报错，但用的时候会炸（比如 f.text 报 'dict' has no attribute）。
        kwargs["memories"] = [Memory(**m) for m in raw.get("memories", [])]
        kwargs["favorites"] = [Favorite(**{k: v for k, v in f.items()
                                           if k in Favorite.__dataclass_fields__})
                               for f in raw.get("favorites", [])]
        kwargs["notes"] = [Note.from_dict(x) for x in raw.get("notes", [])]
        kwargs["promises"] = [Promise.from_dict(x)
                              for x in raw.get("promises", [])]
        # 事件同理。旧存档没有这个字段，raw.get 会给空列表 —— 不用迁移。
        kwargs["events"] = [mind_event.Event.from_dict(x)
                            for x in raw.get("events", [])]
        # ⚠️ 新加的两类**必须在这里转换**。
        # 不补的话读出来是裸 dict —— 一用就 AttributeError，
        # 而且会被 except 吞掉，表现成「功能上线了但从来不生效」。
        kwargs["her_events"] = [mind_event.Event.from_dict(x)
                                for x in raw.get("her_events", [])]
        import mind_project as _SP
        kwargs["projects"] = [_SP.Project.from_dict(x)
                              for x in raw.get("projects", [])
                              if isinstance(x, dict)]
        kwargs.pop("memories_raw", None)
        c = Character(**kwargs)
        # 只挑 Turn 认识的字段：旧档没有 channel（用默认值），
        # 将来字段变多时也不会因为多一个键就整份读不出来。
        _tknown = {f.name for f in dataclass_fields(Turn)}
        # 旧存档兼容：role 曾经叫 "shu"，读进来统一成 "her"（2026-11-11）
        c.turns = []
        for _t in raw.get("turns", []):
            _d = {k: v for k, v in _t.items() if k in _tknown}
            if "role" in _d:
                _d["role"] = _norm_role(_d["role"])
            c.turns.append(Turn(**_d))
        # ⚠️ 跨天重抽发生在**这里**，所以外部再调 `refresh_day()` 会得到 False
        # —— 信号已经被读档用掉了。
        #
        # 实测踩过：server 里写 `if ch.refresh_day(): 生成昨天的事`，
        # 永远不会触发，因为 load() 已经把 today_key 推到今天了。
        #
        # 所以把结果挂在实例上（不是 dataclass 字段，asdict 会忽略它）。
        c._refreshed = bool(c.refresh_day())
        return c

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 用 asdict 自动带上全部字段。
        #
        # ⚠️ 这里原来是**逐字段手写**的清单，加新字段时极易漏 ——
        # 实测踩过：新增 mood 字段时两边都没同步，于是心情存进去是空的、
        # 读出来还是空的，而且**不报任何错**。症状很迷惑：
        # 心情每次读都在变，说贴心话后情绪反而掉。
        #
        # 改成 asdict 之后，以后再加字段不会再漏。
        payload = asdict(self)
        # 临时文件名**必须带进程号**（2026-11-08）：
        # 固定名字时，两个进程会往同一个 .tmp 里各写一遍，
        # 内容交错 → replace 出一份坏 JSON（"Extra data"），
        # 坏档被隔离后新建空角色 —— 桌面档就是这样被清空过一次。
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)   # 原子替换，避免写一半崩掉损坏存档

    # ── 日子 ──────────────────────────────────────────────
    def refresh_day(self, force: bool = False) -> bool:
        """跨天则刷新她手边的事与心事。返回是否换了新的一天。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if not force and self.today_key == today:
            return False

        rng = random.Random(today)          # 同一天结果稳定，不会每次刷新都变

        # ★ 按心情挑，而不是纯随机。
        #
        # 原来「手边的事」和「心里的念头」各自独立随机，于是会出现
        # 「精力 15（很累）」配上「把谷仓角落那口旧铫子翻出来擦了半日」
        # —— 一个累到不想动的人不会去擦半日铫子。这类矛盾比冷淡更出戏。
        mood = Mood.from_dict(self.mood)

        # 游戏状态：季节 / 农业等级 / 矿洞深度。
        # 读不到就退回纯随机池（不阻塞任何事）。
        # 季节改从**现实**来（2026-10-27 与游戏脱钩）。
        # 原来读存档：你不开游戏，她那边的季节就冻住；而且游戏一个月 28 天，
        # 和现实日期映射不上。现在她按现实日历过日子（当地）。
        _season = ""
        _farm_lv = 0
        _mine = 0
        try:
            import mind_season
            _season, _ = mind_season.season_of()
        except Exception:
            mind_diag.note("refresh_day 里的 mind_season")

        self.today_task = mind_life.pick_task(mood.energy, today,
                                            season=_season,
                                            farm_level=_farm_lv,
                                            mine_depth=_mine)
        self.today_thought = mind_life.pick_thought(mood.valence, today,
                                                  season=_season,
                                                  mine_depth=_mine)

        # 原来缺的两样：她自己的小麻烦、当天的口味
        self.today_trouble = mind_life.pick_trouble(today)
        self.today_tastes = mind_life.pick_tastes(today)

        # 若她已经躺下了，把「手边/心里」换成睡着档的。
        #
        # ## 为什么（2026-10-17 用户报的 bug）
        #
        # refresh_day 按**系统时钟**跨天重抽，而对话停在昨晚 21:50「她睡着了」。
        # 于是侧边栏写着「靠着门框看云，没看明白就走了」，对话里她明明在睡。
        #
        # 这不改 refresh_day 的时机（跨天确实是新的一天），
        # 只是在**她已经躺下**时把这两栏对齐到事实。
        # ⚠️ 这里**不再**用 mind_activity 改写 today_task / today_thought。
        #
        # 原来在跨天重抽之后又调了 `apply_to(self)`：她在睡就把两栏换成
        # 睡着档的句子。问题是**醒来时没人改回来** —— refresh_day 只在跨天
        # 时重抽，而 today_key 已经是今天了。于是那两句粘一整天。
        #
        # 现在改成渲染时替换（见 mind_activity.display_override），
        # 存档里永远是当天的真实状态。

        # ── 她手上的活往前推一格（2026-10-20 加）──────────
        #
        # 放在跨天这里，因为「一天过去了一点」这件事只在跨天成立。
        # 进度是**按天数算的**（见 mind_project），所以哪怕某天没跨成、
        # 下次跨天时会一次补齐 —— 不会漏账。
        self.today_season = _season
        try:
            self.advance_projects(_season)
        except Exception as e:
            mind_diag.note("refresh_day 里的 advance_projects", e)

        self.today_key = today
        return True

    def note_observation(self, text: str) -> str | None:
        """从对话里看出「他是怎样的人」，攒起来。

        与记忆系统的分工：
            记忆  = 他说的**信息**（「我妹妹叫小满」）
            观察  = 她看出来的**行为模式**（「他累了不吭声」）

        后者才是「认识一个人」。见 mind_life.observe 的判据。
        """
        obs = mind_life.observe(text, self.observations)
        if obs:
            self.add_observation(obs)
        return obs

    # ── 观察 / 事件 / 承诺 的改删（2026-10-19 加）────────────
    #
    # ## 为什么需要
    #
    # 面板上这几类原来是**只读**的。于是她记错一条（比如把
    # 「你明天还得早起」推成「他答应的事都记在心上」），你没有
    # 任何办法去掉它 —— 只能等它被上限顶掉。
    #
    # ## 为什么用 index 而不是文本匹配
    #
    # 文本可能重复（同一个事件她记两遍），index 更确定。
    # 面板每次都会重新拉全量，所以 index 在你点的那一刻是准的。

    def remove_observation(self, index: int) -> bool:
        """删掉第 index 条观察。返回是否真的删了。"""
        if 0 <= index < len(self.observations):
            gone = self.observations.pop(index)
            # 时间戳也要一起清 —— 否则 observation_meta 会越攒越脏
            self.observation_meta.pop(gone, None)
            return True
        return False

    def edit_observation(self, index: int, text: str) -> bool:
        """改写第 index 条观察。时间戳跟着搬过去（视为你修正了它）。"""
        text = (text or "").strip()
        if not text or not (0 <= index < len(self.observations)):
            return False
        old = self.observations[index]
        if old == text:
            return False
        self.observations[index] = text
        at = self.observation_meta.pop(old, "")
        if at:
            self.observation_meta[text] = at
        return True

    def remove_event(self, index: int) -> bool:
        """删掉第 index 条事件。"""
        if 0 <= index < len(self.events):
            self.events.pop(index)
            return True
        return False

    def edit_event(self, index: int, text: str) -> bool:
        """改写第 index 条事件（只改正文，不动 kind/day）。"""
        text = (text or "").strip()
        if not text or not (0 <= index < len(self.events)):
            return False
        ev = self.events[index]
        if getattr(ev, "text", "") == text:
            return False
        ev.text = text
        return True

    def edit_promise(self, at: str, text: str) -> bool:
        """改写某条承诺（用 at 定位；at 为空则用原文匹配）。

        ⚠️ 只改措辞，**不改 kept** —— 那件事做到没做到是事实，不该被编辑。
        """
        text = (text or "").strip()
        if not text:
            return False
        for pr in self.promises:
            if at and getattr(pr, "at", "") == at:
                if pr.text == text:
                    return False
                pr.text = text
                return True
        return False

    def note_observation_seen(self, obs: str, day: str = "",
                              quote: str = "") -> None:
        """记一次「又看见他这样做」。

        观察需要三件事才能读起来像相处：第一次、最近一次、具体那句话。
        没有它，面板上就只有一句判断，看的人（和模型）都会觉得空。
        """
        obs = (obs or "").strip()
        if not obs:
            return
        day = (day or datetime.now().isoformat(timespec="seconds"))[:10]
        ev = self.observation_evidence.setdefault(obs, {})
        ev.setdefault("first", day)
        ev["last"] = max(str(ev.get("last") or ""), day)
        days = list(ev.get("days") or [])
        if day not in days:
            days.append(day)
        ev["days"] = days[-12:]
        if quote:
            ev["quote"] = quote[:80]
        self.observation_meta[obs] = datetime.now().isoformat(timespec="seconds")

    def add_observation(self, obs: str) -> None:
        """加一条观察。超过上限就**淘汰最旧的**。

        ## 为什么淘汰而不是拒绝

        原来 `observe()` 满额直接返回 None —— 于是攒到 6 条那天起，
        她再也不会认识新的他。而人是会变的：三个月前的他和现在的他
        不一样，旧的那条本来就该让位。

        「最旧的」= 列表最前面那条（append 顺序就是时间顺序）。
        """
        obs = (obs or "").strip()
        if not obs:
            return
        if obs in self.observations:
            return
        self.observations.append(obs)
        now = datetime.now().isoformat(timespec="seconds")
        self.observation_meta[obs] = now
        ev = self.observation_evidence.setdefault(obs, {})
        ev.setdefault("first", now[:10])
        ev["last"] = now[:10]
        ev.setdefault("days", [])
        ev.setdefault("quote", "")

        # 超了就从最前面淘汰（连同它的时间戳）
        # 按**强度**淘汰（2026-11-02）：原来 FIFO 掐最旧的，
        # 于是「他很乐观」这种一直成立的会被上周的琐事顶掉。
        try:
            import mind_weight
        
            def _key(o):
                return dict(kind='observation', weight=2,
                            last_touch=(self.observation_touch.get(o)
                                        or self.observation_meta.get(o, '')),
                            hits=0, key=o)
        
            self.observations = mind_weight.evict(
                self.observations, mind_life.MAX_OBSERVATIONS, _key)
            keep = set(self.observations)
            for gone in list(self.observation_meta):
                if gone not in keep:
                    self.observation_meta.pop(gone, None)
                    self.observation_touch.pop(gone, None)
        except Exception:
            while len(self.observations) > mind_life.MAX_OBSERVATIONS:
                old = self.observations.pop(0)
                self.observation_meta.pop(old, None)
    # ── 对话 ──────────────────────────────────────────────
    def add_turn(self, role: str, text: str, emotion: str = "",
                 channel: str = "web") -> None:
        self.turns.append(Turn.make(role, text, emotion, channel))
        self.total_turns += 1
        # 保留上限。原来是 200，太小了 —— 聊一个月就丢掉了大半，
        # 而「隔了很久她还记得」正是这个程序的核心价值。
        # 2000 轮约 1~2 MB，对本地存档完全可接受。
        # 窗口上限 3000 轮（约 1.5 MB）。**超出的只是"不再放进提示词"，
        # 累计轮次 total_turns 一直在涨** —— 界面上「已经聊了多少轮」
        # 不该因为窗口裁剪而停住（2026-11-05 修）。
        if len(self.turns) > 3000:
            self.turns = self.turns[-3000:]

    def recent_turns(self, n: int = 12) -> list[Turn]:
        return self.turns[-n:]

    def recent_turns_by_chars(self, budget: int, min_turns: int = 8,
                              start: int = 0) -> list[Turn]:
        """最近若干轮，但按**总字数**封顶，而不是按轮数。

        ## 为什么要换指标

        原来是 `recent_turns(40)` —— 按轮数。但轮数是个很差的代理：

            40 轮「嗯」「哦」「晚安」        ≈   200 字
            40 轮认真聊一件事               ≈ 15,000 字

        用轮数控制，等于把「聊得深」和「聊得浅」一视同仁。而实测：
        39 轮的真实对话**总共只有 883 字** —— 而模型的上下文窗口有
        1,000,000 tokens（我们在用 1%）。

        **也就是说「她忘事」不是窗口不够，是我们主动把历史掐掉了。**

        ## 为什么留 min_turns

        字数预算在小段落上会切得太狠 —— 连续几句「嗯」「哦」加起来不到 50 字，
        但它们是**语气连贯**的关键。所以保证至少给 min_turns 轮。

        Args:
            budget: 总字数上限。
            min_turns: 至少给这么多轮（哪怕超预算）。

        Returns:
            从旧到新排列的轮次列表。
        """
        if not self.turns[start:]:
            return []
        picked: list[Turn] = []
        used = 0
        for t in reversed(self.turns[start:]):
            n = len(t.text or "")
            if picked and used + n > budget:
                break
            picked.append(t)
            used += n
        if len(picked) < min_turns:
            # 不够就往前多拿一点 —— 但不超过实际的轮数
            picked = list(self.turns[start:][-min_turns:])
        picked.reverse()
        return picked

    def recall_turns(self, query: str, limit: int = 6,
                     exclude_recent: int = 40) -> list[Turn]:
        """按关键词从**较早的**对话里找回相关片段。

        ## 为什么需要它

        上下文窗口再大也有限，而你们的对话会持续几个月。
        光靠「最近 N 轮」迟早会忘掉更早的事。

        这里用一个很朴素的信号：**字词重合**。
        中文没有空格，所以用连续二字片段（bigram）做重合度 ——
        简单、确定、不需要任何额外服务，而且对「上次说的那条狗」这种
        具体名词的召回效果相当好（抽象话题本来就该靠长期记忆，不靠翻旧账）。

        刻意**排除最近若干轮** —— 那些本来就在上下文里，重复注入只是浪费。
        """
        # ── 这里曾经有个静默失效的 bug（2026-10 修）──────
        #
        # 原来开头是：
        #
        #     if not query or len(self.turns) <= exclude_recent:
        #         return []
        #
        # 而 exclude_recent 默认 40。于是：
        #
        #     39 轮  ->  直接返回空，一条都召不回
        #     40 轮  ->  older = turns[:-40] = 0 条，还是空
        #     41 轮  ->  older 才有 1 条
        #
        # **对话没超过 40 轮之前，召回从来没生效过。**
        # 而它失败得毫无声息 —— 返回空列表，调用方当「没找到旧事」处理。
        # 是靠「试召回」那个面板试出来的（三个词全是 0 条，才起疑）。
        #
        # 修法：去掉早退。**对话不够长时干脆不排除** ——
        # 那意味着「最近的那些也在上下文里」，重复注入一点，
        # 好过整个函数在早期等于不存在。
        #
        # 注意别写成 `older = turns[:n - exclude]` —— 语义是反的：
        # 39 轮、exclude=40 时会变成「只有最前面 2 轮可搜」，
        # 而「桩子」「线头」那些恰恰都在最近的轮次里。
        if not query:
            return []

        grams = _content_grams(query)
        if not grams:
            return []

        n = len(self.turns)
        if exclude_recent > 0 and n > exclude_recent + 2:
            older = self.turns[:-exclude_recent]
        else:
            # 不够长，或者调用方明确要求不排除（exclude_recent=0）—— 全部可召回。
            #
            # ⚠️ 这里必须显式判断 `exclude_recent > 0`：
            # Python 里 `turns[:-0]` 等于 `turns[:0]`（空列表），因为 -0 == 0。
            # 写成 `turns[:-exclude_recent] if exclude_recent else turns`
            # 才对，写成算术式就会静默返回空 —— 踩过。
            older = self.turns
        if not older:
            return []

        # ── 稀有度加权（2026-10-23）────────────────────────
        #
        # `df[g]` = 这个片段在「可召回的那些轮」里出现过几次。
        # 出现得越少，命中它就越说明「真的在说同一件事」。
        #
        # 代价：这里要为 older 里每一轮切一次 gram —— 但**原来也是这么做的**
        # （循环里的 `_content_grams(t.text)`），所以没有额外开销。
        # 实测 1460 轮约 17 ms。
        t_grams_list = [_content_grams(t.text) for t in older]
        df: dict[str, int] = {}
        for tg in t_grams_list:
            for g in tg:
                df[g] = df.get(g, 0) + 1

        n_docs = len(older)

        def _idf(g: str) -> float:
            return math.log((n_docs + 1) / (df.get(g, 0) + 1)) + 1.0

        denom = sum(_idf(g) for g in grams) or 1.0

        scored: list[tuple[float, int, Turn]] = []
        for idx, t in enumerate(older):
            t_grams = t_grams_list[idx]
            if not t_grams:
                continue
            overlap = grams & t_grams
            if not overlap:
                continue
            score = sum(_idf(g) for g in overlap) / denom
            # 门槛：只有「真正说同一件事」才注入。
            # 低于它的，是「含同一个常见词」—— 那是噪声，不是记忆。
            if score < RECALL_MIN_SCORE:
                continue
            scored.append((score, idx, t))

        scored.sort(key=lambda x: -x[0])
        # 取分最高的若干条，并按时间顺序还原，读起来才自然
        top = sorted(scored[:limit], key=lambda x: x[1])
        return [t for _, _, t in top]

    def recent_player_lines(self, n: int = 6) -> list[str]:
        return [t.text for t in self.turns if t.role == "player"][-n:]

    def recent_her_lines(self, n: int = 6) -> list[str]:
        return [t.text for t in self.turns if t.role == "her"][-n:]

    # ── 记忆 ──────────────────────────────────────────────
    def remember(self, text: str, kind: str = "fact", weight: int = 1, source: str = "chat") -> bool:
        # 写入前先过一道"这能不能算一条稳定事实"（2026-11-08）
        _bad = _fact_reject_reason(text)
        if _bad:
            try:
                import mind_diag
                mind_diag.note(f"事实被拒收：{_bad}", RuntimeError(str(text)[:80]))
            except Exception:
                pass
            return False
        """记下一件事。返回是否新增（重复的只加权）。"""
        text = text.strip()
        if not text or len(text) > 200:
            return False

        # 高度相似的不重复记，只加重权重
        for m in self.memories:
            if m.kind == kind and (text in m.text or m.text in text):
                m.weight += 1
                return False

        self.memories.append(
            Memory.now(text, kind=kind, source=source, weight=weight))

        # 超过上限时按**强度**淘汰（2026-11-02）：
        # 强度 = 初始权重 × 时间衰减 + 被用到次数。
        # 原来是"留最后 120 条"—— 一条十条没被提起的重要事实，
        # 会输给昨天刚说的琐事。guidance 是行为要求，尽量留住。
        if len(self.memories) > 120:
            try:
                import mind_weight
                facts = [m for m in self.memories if m.kind == "fact"]
                keep = [m for m in self.memories if m.kind != "fact"]

                def _key(m):
                    return dict(kind="fact",
                                weight=getattr(m, "weight", 1),
                                last_touch=getattr(m, "last_touch", "")
                                or getattr(m, "created", ""),
                                hits=getattr(m, "hits", 0),
                                key=getattr(m, "text", ""))

                self.memories = keep + mind_weight.evict(facts, 120 - len(keep), _key)
            except Exception:
                facts = [m for m in self.memories if m.kind == "fact"]
                keep = [m for m in self.memories if m.kind != "fact"]
                self.memories = keep + facts[-(120 - len(keep)):]
        return True

    def correct(self, text: str) -> bool:
        """记下一条「你希望她注意」的要求。

        这是「你能纠正她」的落点 —— 和记忆的区别在于：
        记忆是事实，而这是行为约束，会以规则的形式进入 prompt。
        """
        return self.remember(text, kind="guidance", source="correction")

    def forget(self, index: int) -> bool:
        """删掉第 index 条记忆。"""
        if 0 <= index < len(self.memories):
            del self.memories[index]
            return True
        return False

    def edit_memory(self, index: int, text: str) -> bool:
        """改写第 index 条记忆。"""
        text = text.strip()
        if not (0 <= index < len(self.memories)) or not text:
            return False
        self.memories[index].text = text[:200]
        return True

    def memories_of(self, kind: str) -> list[Memory]:
        return [m for m in self.memories if m.kind == kind]

    def memory_list(self) -> list[dict]:
        """给界面用的记忆清单（带下标，供删除/编辑）。"""
        return [
            {
                "index": i,
                "text": m.text,
                "kind": m.kind,
                "source": m.source,
                "weight": m.weight,
                "created": m.created,
            }
            for i, m in enumerate(self.memories)
        ]

    def status(self) -> dict[str, Any]:
        """给界面用的状态快照。

        注意：**没有「好感」这个字段了** —— 关系完全由游戏决定，
        界面显示的应该是游戏里的心数，不是窗口自己攒的数字。
        """
        self.refresh_day()
        key, name = self.effective_stage()
        hearts = max(0, self.game_points) // 250

        # 距离下一档还差多少心（心数分档阶段才有意义）
        next_hint = ""
        if name in ("初识", "渐渐熟悉", "朋友", "心里有你", "情根深种"):
            for need_hearts, label in ((2, "渐渐熟悉"), (4, "朋友"),
                                       (6, "心里有你"), (8, "情根深种")):
                if hearts < need_hearts:
                    next_hint = f"距「{label}」还差 {need_hearts - hearts} 心"
                    break

        # 心情：这里只**读**，不刷。
        # 刷新与存盘由 server 的 _status_payload() 负责 —— 因为它才知道
        # 刷新之后要不要写盘。踩过的坑：这里刷了却不写盘，
        # 于是每次请求都重新掷一次心情，看起来像「一小时规则没生效」。
        mood = Mood.from_dict(self.mood)

        return {
            "stage_key": key,
            "stage_name": name,
            "mood": {
                "label": mood.label,
                "tone": mood.tone,
                "event": mood.event,
                "valence": mood.valence,
                "energy": mood.energy,
                "tier": mood.tier,
            },
            "task": self.today_task,
            "thought": self.today_thought,
            "total_turns": self.total_turns,
            "first_met": self.first_met,
            "memories": len(self.memories),
            # 关系（全部来自游戏）
            "game_hearts": hearts,
            "game_points": self.game_points,
            "game_status": self.game_status,
            "game_read_at": self.game_read_at,
            "next_hint": next_hint,
        }


def _ensure_total_turns(ch) -> bool:
    """老存档没有 total_turns 时，用当前窗口长度补一个。

    改成"累计"之前，total_turns 是 0 或者跟窗口一样 —— 补一次就好，
    之后每轮由 add_turn 自增，不会再对齐。
    """
    total = int(getattr(ch, "total_turns", 0) or 0)
    win = len(getattr(ch, "turns", []) or [])
    if total < win:
        ch.total_turns = win
        return True
    return False


_ROLE_MAP = {"shu": "her"}   # 旧存档兼容：role 曾经叫 shu


def _norm_role(role: str) -> str:
    return _ROLE_MAP.get(str(role or "").lower(), role)

def _fact_reject_reason(text: str) -> str:
    """这条能不能当"关于他的稳定事实"。返回拒收原因，空串表示可以。

    2026-11-08 加。起因：库里出现过
        「我爸爸妈妈在我八岁的时候就…」（截断）
        「我做了一个梦…梦到你不在我身边了…我」（梦）
        「他今天时间管理不好」（当天状态）
    这些留在"事实"里，每轮都会被塞进提示词，读起来就是一堆碎念。
    """
    t = (text or "").strip()
    if not t:
        return "空"
    if len(t) < 4:
        return "太短"
    if len(t) > 60:
        return "太长"
    # 截断：结尾是这些，说明原话被切了
    if re.search(r"(我|的|了|和|跟|在|是|把|被|就)\.\.\.$", t) or t.endswith("..."):
        return "截断"
    if t.endswith("我") or t.endswith("的"):
        return "截断"
    # 梦、一时情绪、当天状态
    if re.search(r"梦到|做了个梦|我做了一个梦|梦见", t):
        return "梦"
    if re.search(r"^(他|她)?(今天|刚刚|刚才|今晚).*(累|烦|难受|不舒服|没时间|心情)", t):
        return "当天状态"
    # 结尾是连接词/助词 —— 一句没说完就被切了
    if re.search(r"(就|的|和|跟|在|是|把|被|而|但|还|又|所以|因为|然后|"
                 r"如果|虽然|可是|而且|或者)$", t):
        return "截断"
    return ""
