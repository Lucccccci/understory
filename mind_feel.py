"""让模型判断「他这句话让她心里怎么变」—— 取代词表。

## 为什么换掉词表

实测用户的 187 句真实输入：

    命中暖词    21 句  (11%)
    命中冷词     6 句  (3%)
    **都没命中  160 句  (85%)**

也就是说 **85% 的对话里，她的情绪完全不动** —— 只掉精力（而精力已经在谷底）。
用户反馈「心情不随对话走」，这就是根因。

词表抓不到他的表达，因为他不说「谢谢」「想你」这种标记词，
他说的是「我今天在矿洞里待了一整天」「我三天没来了，你想我没」
—— **情感藏在事情里，不在词里。**

## 换成 API 之后

多一次调用（约 1-2 秒），但如果**丢到后台线程**，你的回复不必等它：

    你的话 -> 她的回复（主链路，2-5 秒）
           -> 后台：判情绪 -> 调心情 -> 存盘

所以体感不变，而心情真的会跟着对话走。

## 判据设计

**只问一件事：他这句话让我心里怎么变。**

    不给 0-100 的分数  -> 模型给不准，而且会把「累」和「不高兴」混一起
    给三档 + 强度       -> 稳，而且够用

    暖    他关心你、夸你、想你了、说了让你心里软的话
    中性  只是陈述、报个事、闲聊
    冷    敷衍、不耐烦、疏远

强度用 1-3。1 = 轻（「今天还行」），3 = 重（「我今天特别难过」）。

## 为什么不让她自己吐情绪值

她在回复里已经写表情记号（`$u`）了 —— 那是**她表现出来的**，
而心情是**她心里怎么变**。两件事同源会耦合，一处错两处错。
单独一次轻量调用更干净。
"""

from __future__ import annotations

import mind_llm
import json
import re
import urllib.request

# 三档 -> 情绪的增量。范围参照原来的 SHIFT_WARM/SHIFT_COLD，
# 但允许更大的幅度（因为现在判得准了，可以给得实一点）。
SHIFT = {
    "暖": (3, 6),     # 强度 1->3 附近，2->4~5，3->6
    "中性": (-1, 1),  # 几乎不动
    "冷": (-6, -3),
}

_PROMPT = """判断下面这句话：她心里怎么变，以及**它值不值得被长期记住**。

句子：{text}

只输出一个 JSON，不要解释：

    {{"feel": "暖|中性|冷", "strength": 1, "salience": 0, "fact": "",
      "place": "", "task": "", "event": ""}}

## feel / strength

- 暖：关心她、夸她、说想她、谢谢她、让她心里软的话
- 冷：敷衍、不耐烦、嫌她烦、明显疏远
- 中性：陈述一件事、报个进展、闲聊、问个信息

strength：1=轻，2=一般，3=很重（中性填 1）。

**情感常在事情里，不在字面上。**
「我今天在矿洞里待了一整天」是中性；「我三天没来了，你想我没」是暖的。

## salience（0–3）—— 这条对**长期**有多重要

    0  寒暄、废话、角色扮演里的动作、情绪宣泄
    1  普通日常（今天吃了什么、去了哪、几点睡）
    2  关于他这个人的事：身体、家人、工作、经历、长期偏好、住过的地方
    3  关于你们关系的事：承诺、决定、说重了的话、头一回的事

**只有 2 和 3 才写 fact。** 拿不准就写 1 —— 宁可少记，不要瞎记。

## event（可选）—— 他这句话里，有没有一件**发生过的事**

有就给一句「事件」：**第三人称、过去式、一句话、15–24 字**，
写清楚发生了什么；**不要**括号动作、引号、昵称、语气词；
**不要写「昨天/今天/刚才」这类相对时间**（日期由记录带）。

只有这几类才给：
    · 头一回的事（第一次接吻、第一次一起做饭）
    · 两个人一起做了什么（一起去看了月光水母、一起补坛子）
    · 他做成/经历了什么值得记一笔的（接活被退稿、把答应她的事做完了）

日常寒暄、心情、提问 —— **留空**。拿不准就留空。

    他：今天有甲方找我拍片子，结果麦克风坏了
    event：「他接的活因为麦克风坏了没交成」

## fact（可选）—— 只能记**稳定的**关于他的事实

家人、身体、职业、爱好、住处、重要经历。
必须写成**完整的第三人称一句**（主语「他」，不超过 26 字）。
不写：当天状态、一次性的心情、梦、半句。

    他：我爸妈八岁就分开了          → fact「他八岁时父母就分开了」
    他：今天腰又酸了，累死了        → fact 留空（这是当天状态）
    他：我梦见你不在了              → fact 留空（梦不是事实）

## place / task（可选，没变化就留空）

如果**这一轮对话里她换了地方或换了手上的活**，给两个字段：

    "place": 八选一 —— 院子 / 堂屋 / 厨房 / 卧房 / 后廊 / 卫生间 / 储物角 / 外面
    **外面** = 出了院门（镇上去、地里去、跟他出门）
    "task":  12 字以内，她此刻手上在做的事

例：他说"去把碗洗了"、她答"（端着碗去了厨房）" -> {{"place": "厨房", "task": "洗碗"}}
**没提到就别填** —— 留空表示维持原样，不要凭空替她走动。

## fact

一句她该记住的事实，**20 字以内**，用「他…」开头。
写「他右膝做过手术」，不要写「我右边膝盖去年动过手术，下雨天会酸」——
那是原话，不是事实。不要括号，不要动作，不要引号。
"""


def judge(text: str, api_key: str, model: str = "deepseek-chat",
          api_base: str = "https://api.deepseek.com/v1",
          timeout: int = 20) -> tuple[str, int]:
    """判断一句话让她心里怎么变。失败返回 ("中性", 1)。

    Args:
        text: 他说的话。
        api_key: 密钥。
        model: 用哪个模型（默认对话模型就够，这是简单分类）。
        api_base: API 地址。
        timeout: 超时秒数。**故意设短** —— 这是后台任务，超时了就不调了。

    Returns:
        (档位, 强度)，如 ("暖", 2)。
    """
    t = (text or "").strip()
    if not t or not api_key:
        return "中性", 1

    payload = {
        "model": model,
        "temperature": 0.2,          # 分类任务，要稳不要花
        "max_tokens": 60,
        "messages": [{"role": "user", "content": _PROMPT.format(text=t[:400])}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        out = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return "中性", 1

    # 模型偶尔会带 markdown 代码块或前后废话，抠出第一个 JSON
    m = re.search(r"\{[^{}]*\}", out)
    if not m:
        return "中性", 1
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return "中性", 1

    feel = str(d.get("feel") or "中性").strip()
    if feel not in SHIFT:
        feel = "中性"
    try:
        strength = int(d.get("strength") or 1)
    except (TypeError, ValueError):
        strength = 1
    strength = max(1, min(3, strength))
    return feel, strength


def delta_for(feel: str, strength: int) -> int:
    """档位 + 强度 -> 情绪增量。

    暖 3~6 / 中性 -1~1 / 冷 -6~-3。
    强度 1/2/3 在区间里线性取点。
    """
    lo, hi = SHIFT.get(feel, (-1, 1))
    if lo == hi:
        return lo
    # strength 1 -> 区间 1/3 处，3 -> 满
    frac = {1: 0.34, 2: 0.67, 3: 1.0}.get(strength, 0.67)
    return int(round(lo + (hi - lo) * frac))


if __name__ == "__main__":
    import os
    from pathlib import Path
    kf = Path(__file__).resolve().parent / ".apikey"
    key = os.environ.get("MIND_AI_API_KEY") or (
        kf.read_text(encoding="utf-8").strip() if kf.is_file() else "")

    CASES = [
        ("我今天在矿洞里待了一整天。", "中性", "陈述一件事"),
        ("我三天没来了，你想我没。", "暖", "他在意她怎么想"),
        ("我喜欢你", "暖", "直白"),
        ("知道了", "中性", "敷衍（交给词的判据）"),
        ("随便吧", "冷", ""),
        ("这张图你看看，是我今早在地里拍的。", "暖", "分享"),
        ("嗯。", "中性", ""),
        ("你今天怎么了，一句话都不说。", "暖", "关心"),
    ]
    print("=" * 70)
    print("  情绪判据自测（真调 API）")
    print("=" * 70)
    ok = 0
    for t, want, why in CASES:
        feel, st = judge(t, key)
        mark = "✓" if feel == want else ("~" if feel == "中性" else "✗")
        if feel == want:
            ok += 1
        d = delta_for(feel, st)
        print(f"  {mark} {t[:26]:<28} -> {feel} (强度{st}, 情绪{d:+d})   {why}")
    print()
    print(f"  {ok}/{len(CASES)} 与预期一致")


def judge_ex(text: str, api_key: str, model: str = "deepseek-chat",
             api_base: str = "https://api.deepseek.com/v1",
             timeout: int = 20, her_reply: str = "",
             current_place: str = "") -> dict:
    """情绪 + 长期重要性，一次调用。

    Returns:
        {"feel": "暖|中性|冷", "strength": 1-3, "salience": 0-3, "fact": "..."}

    失败时返回中性且 salience=0 —— **绝不因为判不出来而写进记忆**。
    """
    out = {"feel": "中性", "strength": 1, "salience": 0, "fact": "",
           "place": "", "task": "", "event": ""}
    t = (text or "").strip()
    if not t or not api_key:
        return out

    # 2026-11-05：位置判定改成**看两边**。
    # 原来只喂他说的那句 —— 可她动没动，写在她自己的动作里
    # （「（端着碗去了厨房）」），只看他的话当然跟不上。
    scene = f"他：{t[:300]}"
    if her_reply:
        scene += f"\n她：{her_reply[:300]}"
    if current_place:
        scene += f"\n（她现在在：{current_place}）"

    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 140,
        "messages": [{"role": "user", "content": _PROMPT.format(text=scene)}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        raw = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return out

    m = re.search(r"\{[^{}]*\}", raw)
    if not m:
        return out
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return out

    feel = str(d.get("feel") or "").strip()
    if feel in ("暖", "中性", "冷"):
        out["feel"] = feel
    try:
        out["strength"] = max(1, min(3, int(d.get("strength") or 1)))
    except (TypeError, ValueError):
        pass
    try:
        out["salience"] = max(0, min(3, int(d.get("salience") or 0)))
    except (TypeError, ValueError):
        pass
    _ev = str(d.get("event") or "").strip().strip("。")
    # 相对时间一律剥掉（用户要求：只用日期，不写"昨天"）
    import re as _re
    _ev = _re.sub(r"(昨天|今天|明天|刚才|刚刚|上次|上回|前天|后天)", "", _ev)
    _ev = _ev.strip("，, 、")
    if 6 <= len(_ev) <= 40 and _ev.startswith("他"):
        out["event"] = _ev

    _pl = str(d.get("place") or "").strip()
    if _pl in ("院子", "堂屋", "厨房", "卧房", "后廊", "卫生间", "储物角", "外面"):
        out["place"] = _pl
    _tk = str(d.get("task") or "").strip().strip("。")
    if 2 <= len(_tk) <= 16:
        out["task"] = _tk

    fact = str(d.get("fact") or "").strip()
    fact = re.sub(r"[（(].*?[)）]", "", fact).strip()
    if out["salience"] >= 2 and 4 <= len(fact) <= 40:
        out["fact"] = fact
    else:
        out["salience"] = min(out["salience"], 1)
    return out
