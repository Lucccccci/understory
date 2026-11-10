#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她给你留话 —— 你不在的时候，她也有自己的日子。

## 与「主动联系你」的区别

    主动联系  ->  她**打扰**你（推送通知、QQ 消息）
    留话      ->  她留张字条，你**下次回来**才看见

后者温和得多，也不用装推送通道，而且更贴她的性子：
她不是那种会追着你发消息的人，但会在灶上给你留一碗。

## 什么时候留

    距上次说话 >= 4 小时，且距上次留话 >= 6 小时   ->  留一条

**不能太勤**：一天留五六条就成刷屏了，那不是她。
理想频率是**一天一到两条**，多的时候你回来看到两条，会觉得她真的过了一天。

## 内容边界（要紧）

留话和心情事件不同 —— 它是她**对你说**的话，所以：
    ✅ 她今天做了什么、看见了什么、忽然想起什么
    ✅ 一句不带要求的挂念（「灶上给你留了」「路上看见…」）
    ❌ 不能说「你怎么不来」「等了你一天」—— 那是索取，不是她
    ❌ 不能编造你做过什么（你昨天来了、你送的东西）—— 那是事实，归存档管
    ❌ 不能提天气季节（存档里有真实季节，编了会打架）

## 为什么用模型生成而不是抽模板

心情事件可以抽（它们是「她自己的事」，不需要和你有关）。
但留话是**对着你说**的，得跟最近聊的内容接得上 ——
比如上次你说腰疼，她留一句「腰还疼不疼」就比随机模板好得多。
"""

from __future__ import annotations

import mind_llm
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYFILE = HERE / ".apikey"

# 触发条件
MIN_SINCE_TALK = timedelta(hours=4)     # 距上次说话
MIN_SINCE_NOTE = timedelta(hours=6)     # 距上次留话（防止刷屏）

# 留话的时段窗口（2026-10-19 加）。
#
# 用户的原话：「优化其时间机制（不要半夜还在干活）」。
# 原来的判据只有「聊过没 / 刚留过没 / 她睡着没」—— 于是她凌晨两点
# 醒着的时候还能生成字条。半夜写条子这件事本身就怪。
#
# 只在 07:00–22:59 之间留。
NOTE_HOUR_START = 7         # 07:00
NOTE_HOUR_END = 23          # 到 22:59

# 最多留几条没看的。攒太多就不是「留话」而是「未读消息」了。
MAX_PENDING = 3


@dataclass
class Note:
    """一条留话。"""
    text: str
    at: str                 # 写下时间（ISO）
    seen: bool = False

    def to_dict(self) -> dict:
        return {"text": self.text, "at": self.at, "seen": self.seen}

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        return cls(text=str(d.get("text", "")),
                   at=str(d.get("at", "")),
                   seen=bool(d.get("seen", False)))


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def should_leave(ch, now: datetime | None = None) -> tuple[bool, str]:
    """该不该留一条。返回 (是否, 原因)。"""
    now = now or datetime.now()

    if not ch.last_talk_date:
        return False, "还没聊过"

    pending = [n for n in ch.notes if not n.seen]
    if len(pending) >= MAX_PENDING:
        return False, f"已有 {len(pending)} 条没看"

    # ⚠️ 她睡着了就不留话（2026-10-17 用户报的）
    #
    # 留话是**她写下**的东西。睡着的人写不了字条 ——
    # 原来这里不查睡意，于是凌晨 3:40 她明明已经躺下，
    # 后台还在给她生成「灶上那锅汤我给你留着」。
    #
    # 判据在 `mind_activity.current_activity`（从对话末尾推断），
    # 和侧边栏那两栏用的是同一个 —— 只在一处判断，不会打架。
    # ── 时段窗口（2026-10-19 加）────────────────────────
    #
    # 只在白天和傍晚留话。**半夜不干活** —— 哪怕她醒着。
    # 判据放在「睡着」之前：先看时辰，再看状态。
    if not (NOTE_HOUR_START <= now.hour < NOTE_HOUR_END):
        return False, f"不在留话时段（{NOTE_HOUR_START}:00-{NOTE_HOUR_END}:00）"

    try:
        import mind_activity
        if mind_activity.current_activity(ch) == "asleep":
            return False, "她睡着"
    except Exception:
        pass

    last_talk = _parse(ch.last_talk_at) if getattr(ch, "last_talk_at", "") else None
    if last_talk is not None and now - last_talk < MIN_SINCE_TALK:
        return False, "刚聊过"

    # 上次留话的时间
    last_note = max((_parse(n.at) for n in ch.notes if _parse(n.at)),
                    default=None)
    if last_note is not None and now - last_note < MIN_SINCE_NOTE:
        return False, "刚留过"

    return True, "可以留"


# ── 生成 ─────────────────────────────────────────────
SYSTEM = """你在替一个角色写「她给你留的一张字条」。

角色：她（shǔ）。从大炎来的年轻天师，如今住在这座镇子的鹈鹕镇，
自己有一间小屋、一小块地。二十出头。话不多，关心人但从不直说，
有点长姐的分寸，也有年轻姑娘的小心思。

你是在写**她留给他的一句话**，不是对话。要求：

1. **总共不超过 25 个字。** 这是字条，不是日记 ——
   宁可少写，也不要写满。一句话常常就够了。
2. **像随口留的**，不像汇报。可以没头没尾。
3. **不索取**：绝不能说「你怎么不来」「我等你一天了」「你都不理我」。
   她不会催人。
4. **不编造他做过的事**：不能说「你上次送的」「你说的那个」——
   那是事实，由别处提供。你不知道就直接不提。
5. **不提天气与季节**（存档里有真实季节，编了会打架）。
6. 可以是：她今天做了什么、看见了什么、忽然想起的事、
   一句不带要求的挂念（「灶上给你留了」「路上看见…」）。
7. 不要用「今天」开头太多次，不要用网络用语、颜文字、表情记号。
8. **不要列举。** 不要「也……也……」「一是……二是……」这种排比。
   一条字条只说一件小事。

如果有「你们最近聊过的内容」，可以自然接一句，但**不要生硬引用**。"""


# 稍宽于 prompt 里的 25 字 —— 给标点和语气词留余地，
# 但不允许写成一段。实测踩过：不设闸时模型会写三句六十多字。
MAX_CHARS = 34


def _clean(text: str) -> str:
    """洗掉角色名前缀、表情记号、多余空行。"""
    t = (text or "").strip().strip('"“”')
    t = re.sub(r"^\s*(她|Shu)\s*[:：]\s*", "", t)
    t = re.sub(r"\$[hsula]", "", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t if 2 <= len(t) else ""


def _call(payload: dict) -> str:
    key = os.environ.get("MIND_AI_API_KEY", "").strip()
    if not key and KEYFILE.is_file():
        key = KEYFILE.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("没有 API 密钥")
    body = mind_llm.chat_body(payload, key, "https://api.deepseek.com/v1", 60)
    return body["choices"][0]["message"]["content"]


def write_note(ch, api_base: str = "", model: str = "deepseek-chat") -> Note | None:
    """让模型写一条留话。失败返回 None（不该因为留话失败影响聊天）。"""
    # 最近几轮值得一提的内容，让留话接得上
    recent = ""
    if ch.turns:
        lines = []
        for t in ch.turns[-4:]:
            who = "他" if t.role == "player" else "你"
            lines.append(f"  {who}：{t.text[:60]}")
        recent = "\n".join(lines)

    hint = ""
    if getattr(ch, "today_task", ""):
        hint += f"\n她手边在做的事：{ch.today_task}"
    if getattr(ch, "today_thought", ""):
        hint += f"\n她心里想着的事：{ch.today_thought}"
    mood = getattr(ch, "mood", None) or {}
    if mood.get("event"):
        hint += f"\n她今天的一件小事：{mood['event']}"

    # 时辰 —— 原来 prompt 里完全没有时间，于是凌晨能写出晚饭的话。
    #
    # 只说**时辰和此刻**，不给她日期（日期对字条没用，还容易被抄进正文）。
    now = datetime.now()
    h = now.hour
    if h < 5:
        period = "深夜"
        # ⚠️ 这里**不能**写「她本该睡着」—— `should_leave` 已经拦住了睡着的
        # 情况，能走到这里说明她**醒着**。写混了会自相矛盾。
        when_tip = ("深夜，她还醒着 —— 字条可以说夜深、说还没睡、说灯还亮着。"
                    "但**不要问「你怎么还不睡」**：她不知道你几点才看到这张字条。"
                    "语气可以很低，像不想惊动谁。")
    elif h < 9:
        period = "一早"
        when_tip = "清早。她可能刚起，字条是顺手留的，可以提天光、露水、早饭。"
    elif h < 12:
        period = "上午"
        when_tip = "上午，一天里做事的时候。字条可以说手上的活。"
    elif h < 14:
        period = "中午前后"
        when_tip = "中午前后。可以提吃饭、歇一歇。"
    elif h < 18:
        period = "下午"
        when_tip = "下午。可以提地里的活计、日头。"
    elif h < 22:
        period = "傍晚到夜里"
        when_tip = "傍晚到夜里。可以提收工、晚饭、留灯。"
    else:
        period = "夜里"
        when_tip = "夜里。适合安静的、收束一天的话。"

    parts = [f"给她写一张留话。现在是{period}（{now.strftime('%H:%M')}）。"]
    parts.append(f"**这张字条要符合上面的时辰。** {when_tip}")
    if hint:
        parts.append("可用作底子的素材（不必全用）：" + hint)
    if recent:
        parts.append("你们最近聊过的：\n" + recent)
    parts.append("直接输出那张字条的内容，不要加引号、不要加任何说明。")

    try:
        text = _call({
            "model": model,
            "temperature": 1.15,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "\n\n".join(parts)}],
        })
    except Exception:
        return None

    text = _clean(text)
    # 太长就再要一次，并把上次的长版本给它看 —— 比单纯重试有效
    if text and len(text) > MAX_CHARS:
        shorter = _call({
            "model": model,
            "temperature": 1.0,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                    f"下面这张字条太长了（{len(text)} 字），请压到 {MAX_CHARS} 字以内，"
                    f"只留最要紧的一件事。\n\n{text}\n\n"
                    "直接输出改后的字条，不要说明。"},
            ],
        })
        text2 = _clean(shorter)
        if text2 and len(text2) <= len(text):
            text = text2

    if not text:
        return None
    return Note(text=text,
                at=datetime.now().isoformat(timespec="seconds"))


if __name__ == "__main__":
    # 独立自测：造一个假角色，看能不能生成
    class _T:
        def __init__(self, role, text):
            self.role, self.text = role, text

    class _C:
        last_talk_date = "2026-10-13"
        last_talk_at = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
        today_task = "在试验田里间苗，腰有些酸"
        today_thought = "盘算着这块地明年该轮种什么"
        mood = {"event": "院子里那只花猫又来蹭饭，今天多添了半勺"}
        turns = [_T("player", "今天有点累。"), _T("her", "别站着了，屋里坐。")]

    ok, why = should_leave(_C())
    print(f"  该不该留: {ok}  ({why})")
    if ok:
        n = write_note(_C())
        print(f"  留话: {n.text if n else '生成失败'}")
