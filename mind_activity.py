"""她「此刻在做什么」—— 从对话末尾推断，补上缺失的那一档。

## 用户报的 bug（2026-10-17）

    对话  21:50 「（黑暗里没了声音，只余均匀的呼吸）……晚安。$l」
          她已经睡着了

    侧边栏 手边 靠着门框看云，没看明白就走了
           心里 想着有件小事一直没办，也不急

## 根因（两层）

    ① `refresh_day` 按**系统时钟**跨天（today_key = 2026-10-18），
       而对话停在 10-17 21:50 —— 于是两栏说的是「明天白天的事」，
       对话说的是「今晚的事」。**两张皮。**

    ② 她的状态里**没有「在做什么」这一档** —— 只有精力/情绪。
       所以即使没跨天，也没人知道她睡着了。`pick_task` 会照抽白天的活。

## 修法

新增 `current_activity(ch)`：**从对话末尾 + 心情推断她此刻在做什么**。

    睡着      她最后一句有睡意 / 你说「晚安」「睡吧」
    在做活    她最后一句提到手上在干什么
    刚回来    隔了很久没说话
    不知道    -> 不干预，退回原逻辑

睡着时：
    手边/心里 换成「睡着」档的句子，**不抽白天的活**
    语气上加一句「你已经半睡了」—— 那时候人不该讨论农事

## 为什么从对话读而不是从时钟读

时钟只知道几点，不知道你们聊到哪儿了。
**对话才是事实**：她说了「睡了」，那她就是睡了，哪怕现在凌晨三点。
（而凌晨三点你还没走，那是另一件事 —— 她会问你怎么还不睡。）
"""
from __future__ import annotations

import re
from datetime import datetime

# 她说「要睡了 / 已经睡了」的信号
_SLEEP_RE = re.compile(
    r"睡了|睡吧|晚安|睡着了|睡下|歇下|闭上眼睛|没了声音|呼吸匀|半睡|"
    r"明天再说|明儿再说|早些睡|早点睡|别熬|不早了.*睡")

# 他道晚安 / 让她睡
_GOODNIGHT_RE = re.compile(r"晚安|睡吧|早点睡|去睡|睡了")

# 她最后一句很响地表示「已经躺下了」
_DEEP_SLEEP_RE = re.compile(
    r"没了声音|呼吸匀|半睡|睡着了|蒙住脸|被子里|被子.*拉|由着.*呼吸")

# ── 深夜判据（2026-10-19 加）────────────────────────────
#
# 原来只按对话内容判「她睡了没」。于是出现：凌晨两点、你一小时没说话，
# 她还在「算这块地的收成」。补一条时间判据。
#
# ⚠️ 必须和「静默」一起判 —— 你凌晨三点还在聊，她当然是醒的。
NIGHT_START_HOUR = 0        # 00:00
NIGHT_END_HOUR = 6          # 到 05:59
# 静默阈值。
#
# 原来是 45 —— 但凌晨两点你停手十几分钟，她其实已经睡了。
# 45 分钟会让「明显该睡了」的时段还显示成在看云。
# 20 分钟：你一停下，她很快就睡了；你在聊的时候又绝不会误判
# （新一轮把间隔清零）。
NIGHT_QUIET_MINUTES = 20

# 睡着时「手边」该说什么
ASLEEP_TASK = [
    "已经躺下了，被子拉到肩上",
    "半睡着，手还搭在被角上",
    "睡过去了，屋里只剩下呼吸声",
]

# 睡着时「心里」该说什么
ASLEEP_THOUGHT = [
    "临睡前还想着明天要记得的事，没说全就睡过去了",
    "心里没什么悬着的了",
    "想着明天早起，别的事都放下了",
]


def _last_her_turn(ch) -> tuple[str, str]:
    """她最后一句话的 (正文, 时间戳)。没有就返回 ("", "")。"""
    for t in reversed(getattr(ch, "turns", None) or []):
        if getattr(t, "role", "") == "her":
            return (getattr(t, "text", "") or "", getattr(t, "at", "") or "")
    return "", ""


def _last_player_turn(ch) -> tuple[str, str]:
    for t in reversed(getattr(ch, "turns", None) or []):
        if getattr(t, "role", "") == "player":
            return (getattr(t, "text", "") or "", getattr(t, "at", "") or "")
    return "", ""


def _hours_since(at: str) -> float:
    if not at:
        return 0.0
    try:
        return (datetime.now() - datetime.fromisoformat(at)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


def current_activity(ch) -> str:
    """推断她此刻在做什么。返回 "asleep" / "awake" / ""。

    ## 判据（要保守）

    **她最后一句有睡意** 且 **他也道过晚安**（或她最后一句是明确的躺下）
        -> asleep

    **他隔了很久没说话**，而她最后一句是道晚安 -> 仍然 asleep
        （她不会在 21:50 说过晚安之后，凌晨三点改去看云）

    其余 -> ""（不干预，让原逻辑跑）
    """
    her_text, her_at = _last_her_turn(ch)
    his_text, his_at = _last_player_turn(ch)
    if not her_text:
        return ""

    # 她说得很明确（已经躺下、没了声音）
    deep = bool(_DEEP_SLEEP_RE.search(her_text))
    sleepish = bool(_SLEEP_RE.search(her_text))
    goodnight = bool(_GOODNIGHT_RE.search(his_text))

    if deep and (sleepish or goodnight):
        return "asleep"
    if sleepish and goodnight:
        return "asleep"

    # 她说了「睡了」但你没接话 —— 那她也在睡。
    # （判据是她最后一句说了睡，且之后没再聊起来）
    if sleepish and her_at and his_at and her_at >= his_at:
        return "asleep"

    # ── 深夜 + 静默（2026-10-19 加）──────────────────────
    #
    # 放在**最后**：前面两条是「她明确说了躺下」，更硬，不能被这条盖掉。
    #
    # 用户报的是：凌晨两点、他一小时没说话，两栏还写着「在算这块地的收成」。
    # 那种时候她该是睡着的 —— 不该在算收成，也不该被叫起来聊农事。
    try:
        now = datetime.now()
        if NIGHT_START_HOUR <= now.hour < NIGHT_END_HOUR:
            last_at = her_at or his_at
            if last_at and _hours_since(last_at) * 60 >= NIGHT_QUIET_MINUTES:
                return "asleep"
    except Exception:
        pass

    return ""


def sleep_block(ch) -> str:
    """睡着时给 prompt 的一段。醒着返回空串。"""
    if current_activity(ch) != "asleep":
        return ""

    _, her_at = _last_her_turn(ch)
    hrs = _hours_since(her_at)
    lines = ["## 你已经躺下了（**要紧**）"]
    lines.append("- 你已经睡下了，此刻是被他弄醒的、或者只是在迷糊里应声。")
    if hrs >= 1:
        lines.append(f"- 距你们上一句话已经过去 {hrs:.0f} 小时 —— **你一直睡着**。")
    lines.append("")
    lines.append(
        "所以你的话该是：**短、含糊、断断续续的**。\n"
        "不要讨论农事、不要盘算明天、不要问他一整天干了什么。\n"
        "他要是说话，你应一声就够了；他要是说多了，你可以迷糊地应一半。\n"
        "**这不是冷淡** —— 是困。困着的人对谁都这样，不生他的气。")
    return "\n".join(lines)


def display_override(ch) -> tuple[str, str] | None:
    """若她在睡，返回该显示的那两句；否则返回 None。

    ## 为什么不是「改写字段」（这是 2026-10-18 修的一个真故障）

    原来的 `apply_to()` 直接改 `ch.today_task` / `ch.today_thought`。
    它只在**推断为睡着**时执行，而**醒来时没有任何东西把它改回来**：

        refresh_day()   只在跨天时重抽
        today_key       早就是今天了 -> 直接 return False

    结果：昨晚她躺下那一刻写的两句，**在界面上粘了一整天**。
    用户的原话：「心里想的事又没有及时更新」。

    ## 现在的做法

    存档里**永远**存当天的真实状态，只在**显示/注入 prompt 的那一刻**
    临时换掉。睡着就显示睡着的，醒了自动回到真实的 —— 因为从未被改写。
    """
    if current_activity(ch) != "asleep":
        return None
    import random as _r
    seed = datetime.now().strftime("%Y-%m-%d")
    rng = _r.Random(seed + "|asleep")
    return rng.choice(ASLEEP_TASK), rng.choice(ASLEEP_THOUGHT)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    APP = Path(__file__).resolve().parent
    sys.path.insert(0, str(APP))
    from sessions import store

    for key in ("desktop", "test"):
        try:
            ch = store.load(key)
        except Exception:
            continue
        act = current_activity(ch) or "（不知道）"
        her, at = _last_her_turn(ch)
        print(f"  [{key}]")
        print(f"      她最后一句 [{at[11:16]}] {her[:50].replace(chr(10), ' / ')}")
        print(f"      推断: {act}")
        if act == "asleep":
            print(f"      -> 手边该改成: {ASLEEP_TASK[0]}")
            print(f"         {sleep_block(ch)[:110].replace(chr(10), chr(10) + '         ')}")
        print()


def place_display(ch) -> str:
    """睡觉时她在地图上的位置。

    和 display_override 同一套道理（2026-11-04）：
    **不改存档里的 today_place**，只在显示时临时换成"卧房"。
    否则第二天醒来，位置还粘在卧房上。
    """
    if current_activity(ch) != "asleep":
        return ""
    return "卧房"


# 位置线索（2026-11-05）
#   为什么要有这一层：模型那条路径**不是每次都吐 place**，
#   实测「（先起身，往水缸那边走）」这种明确的移动，它也给了空。
#   于是地图一直停在旧值 —— 用户看到的就是"逻辑不对"。
#
#   这里用她**自己的动作**做兜底：括号里出现"往 X 走/去了 X"就认 X。
#   顺序有意义：先判更具体的（厨房、储物角），再判大的（院子、外面）。
_PLACE_CUES = (
    ("储物角", ("储物角", "木箱", "旧箱", "翻箱")),
    ("卫生间", ("浴室", "洗澡", "冲澡", "上个厕所", "洗手")),
    ("后廊", ("后廊", "洗衣", "晾衣", "拖把", "工具台")),
    ("厨房", ("厨房", "灶", "锅", "烧水", "添柴", "切菜", "煮")),
    ("卧房", ("卧房", "床上", "被子", "躺下", "枕头", "床头")),
    ("院子", ("院子", "水缸", "井边", "晒", "劈柴", "葡萄架", "晾")),
    ("外面", ("出门", "镇上", "代收点", "集市", "地里", "街上", "路上")),
)


def place_infer(text: str) -> str:
    """从一句动作描述里推断她在哪儿。认不出返回空串。"""
    t = str(text or "")
    if not t:
        return ""
    # 只看动作部分（括号里），避免把"我去厨房给你端"当成她动
    acts = "".join(__import__("re").findall(r"[（(]([^）)]*)[）)]", t))
    scope = acts if acts else t
    best = ""
    best_pos = 10 ** 6
    for place, cues in _PLACE_CUES:
        for cue in cues:
            i = scope.find(cue)
            if i >= 0 and i < best_pos:
                best, best_pos = place, i
    return best
