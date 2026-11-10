"""每日事务合并 —— 一次调用产出三样东西。

## 为什么合并

原来跨天时各跑各的：

    _spawn_her_event  -> 1 次调用（她昨天的事）
    _spawn_digest     -> 1 次调用（昨天的大致经过）
    _spawn_gamelines  -> 1 次调用（8 条游戏台词）

**三次调用喂的几乎是同一份状态**。合并成一次之后：
    · 调用次数 3 -> 1
    · 延迟：跨天第一句不会被三次后台任务抢带宽（本来就异步，但省配额）
    · 三样东西出自同一遍阅读，**彼此不会打架**（摘要说「没提过」，台词却说「你昨天说的」）

周摘要不并进来 —— 它每 7 天才跑一次，而且输入是**已经压好的日摘要**，
性质不同。

## 失败怎么办

**退回三次单独调用。** 合并版的 prompt 很长，一次坏掉就全没了；
分开跑至少能保住两样。所以这不是「替换」，是「优先」。

## 谁该跑

三个都有各自的条件（昨天有没有摘要、今天有没有台词文件）。
都不需要就一次调用都不发 —— 重启服务不该触发任何模型调用。
"""
from __future__ import annotations

import mind_llm
import json
import re
import urllib.request
from datetime import date, timedelta

import mind_digest
import mind_lines
import mind_herday


def _needs(ch) -> tuple[bool, bool, bool]:
    """(要不要摘要, 要不要她的事, 要不要游戏台词)"""
    y = (date.today() - timedelta(days=1)).isoformat()

    need_digest = not any(
        str(d.get("day", ""))[:10] == y for d in (getattr(ch, "digests", None) or []))
    need_herday = not any(
        str(getattr(e, "day", ""))[:10] == y
        for e in (getattr(ch, "her_events", None) or []))

    # 游戏台词：文件今天的就不要重新生成
    # 起居（手边/心里）每天都要重生成一次 —— 词库抽的会撞车
    need_life = str(getattr(ch, "life_brief_day", "")) != date.today().isoformat()

    need_lines = True
    try:
        if mind_lines.OUT_FILE.is_file():
            old = json.loads(mind_lines.OUT_FILE.read_text(encoding="utf-8"))
            if str(old.get("generated_at", ""))[:10] == date.today().isoformat():
                need_lines = False
    except (OSError, json.JSONDecodeError):
        pass

    return need_digest, need_herday, need_lines, need_life


_PROMPT = """你在替「她」处理今天的三件事。**一次输出三样东西**。

## 她现在的样子

{state}

## 他昨天说的话（逐条）

{transcript}

## 输出要求

只输出**一个 JSON 对象**，不要别的文字：

{{
  "task": "……",
  "thought": "……",
  "place": "厨房",
  "trouble": "……",
  "taste_changes": ["咸：淡一点"],
  "tastes": ["吃：……", "喝：……", "忌口：……"],
  "digest": ["……", "……", "……", "……", "……"],
  "her_day": "……",
  "game_lines": [
    {{"text":"……","emotion":"$h","kind":"state","season":"any","weather":"any","tier":"any"}}
  ]
}}

### task（她今天手边在做的事，1 句）
- **16 字以内**，一件具体的小事，能看见动作或物件
- **必须和你昨天说的话有一点关系** —— 他提到累，她就做得慢一点；
  他提到冷，她可能在做与暖有关的事。没有关系就写她自己的季节活
- **三句里至多一句是田里的活**（下种、间苗、看墒、掐尖、收、翻土）。
  她是农事出身，田里的事一定有，但**不能句句都是田** ——
  那样她就不像在过日子，像在值班
- 写「把檐下的红薯翻个面」，不写「在做家务」

### thought（她今天心里想着的，1 句）
- **18 字以内**，半句话就够，**不要点题**
- 多半关于你，也可以是某件没办完的小事
- 写「他昨天那声叹气，还在我这儿」，不写「我担心他」

### place（她此刻在哪儿，只能填一个词）
- 只能从这八个里选：**院子 / 堂屋 / 厨房 / 卧房 / 后廊 / 卫生间 / 储物角 / 外面**
- **外面** = 出了院门（去镇上、下地、跟他出门）
- 要和她这会儿在做的事一致（做饭就在厨房，晒东西就在院子）

### trouble（她眼下的一件小麻烦，1 句）
- **20 字以内**，具体、不大、不影响你们说话
- 写「存种子的小罐子少了一只盖」，不写「最近有点烦」
- 隔几天可以换一件；不要每天都是新的

### tastes（她的口味，3 条）
- 每条 **12 字以内**，形如「吃：喜欢刚出锅烫嘴的」「喝：冬天也喝热的」
- **要稳定** —— 这是她一直以来的口味，不是今天的心情
- 三条分别写 吃 / 喝 / 忌口（或偏好）

### taste_changes（昨天聊出来的口味变化，0–2 条）
- 只有**他明确表达了口味**才写：说咸了、说太甜、说想吃某样、说吃不了某样
- 每条 **10 字以内**，写成「咸：淡一点」「甜：爱吃」这种「项目：方向」
- 没有就给空数组 —— **不要为了凑数编**


- ⚠️ **不要猜他此刻在干什么**（「他这会儿还睡着」「他还没起」这类）。
  你手上没有实时状态 —— 只写你知道的事（他说过的话、发生过的事）。
  要提他，就提**已经发生过的**，别写正在发生的。

### digest（昨天的大致经过，5 条）
- 每条 **30 字以内**，只写**发生了什么**：谁做了什么、说了什么要紧的事
- 不写情绪评价，不写「他很温柔」
- 用「他」称呼对方，用「你」称呼自己
- 挑要紧的：承诺、决定、身体、争执、约好的事、后来的变化

### her_day（昨天她那边的一件小事，1 句）
- **34 字以内**，要有小后果或小麻烦
- **只写她自己**，不要提到他
- 写「院角那畦葱太密，挖出来重栽了一排」，不写「今天去地里干活了」

### game_lines（游戏里的日常问候，8 条）
- 每条 **30 字以内**，**只写她说的话**，不许写动作、不许用括号
- 必须带一个表情：`$h` 高兴 / `$u` 若有所思 / `$s` 难过 / `$a` 不高兴 / `$l` 心里软
- `$l` 只允许出现在 tier=high 的那条上
- 分类：
  · **1 条 `kind=today`**：必须提到**他**或**你们**，用「他昨天说的话」当原料
  · **1 条 `kind=promise`**：关于他还没做到的事，口气要松、绝不催（没有就省略）
  · 其余 `kind=state`：她自己的日子、手上的活、心情
- 标签 `season`：any/spring/summer/fall/winter；
  `weather`：any/sunny/rainy/stormy/snowy；
  `tier`：any/low/mid/high
- 不要重复，不要套话
"""


def _parse(out: str) -> dict:
    t = (out or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        d = json.loads(t[i:j + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _digest_lines(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        s = str(x).strip().strip("　 \"'“”「」")
        if len(s) < 4:
            continue
        if len(s) > mind_digest.MAX_LINE_CHARS:
            s = s[:mind_digest.MAX_LINE_CHARS]
        if s not in out:
            out.append(s)
        if len(out) >= mind_digest.MAX_LINES:
            break
    return out


def _game_lines(raw, allow_love: bool) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for d in raw:
        v = mind_lines._clean_line(d, allow_love)
        if not v or v["text"] in seen:
            continue
        seen.add(v["text"])
        out.append(v)
        if len(out) >= mind_lines.MAX_LINES:
            break
    return mind_lines._fix_today(out)


def _call(ch, api_key: str, want_digest: bool, want_herday: bool,
          want_lines: bool, model: str, api_base: str, timeout: int) -> dict:
    """发一次请求，返回解析后的 dict（可能为空）。"""
    if not api_key:
        return {}

    y = (date.today() - timedelta(days=1)).isoformat()
    transcript = mind_digest._transcript(mind_digest._turns_of(ch, y)) or "（昨天没聊）"

    body = _PROMPT.format(
        state=mind_lines._state_text(ch, date.today().isoformat()),
        transcript=transcript[:6000],
    )
    payload = {
        "model": model,
        "temperature": 0.7,
        "max_tokens": 1600,
        "messages": [{"role": "user", "content": body}],
    }
    try:
        resp = mind_llm.chat_body(payload, api_key, api_base, timeout)
        return _parse((resp["choices"][0]["message"]["content"] or "").strip())
    except Exception:
        return {}


def run_once(ch, api_key: str, allow_love: bool = True) -> tuple[bool, str]:
    """跨天时一次生成三样。返回 (有没有改动, 说明)。"""
    need_d, need_h, need_l, need_life = _needs(ch)
    if not (need_d or need_h or need_l or need_life):
        return False, "今天都生成过了"

    y = (date.today() - timedelta(days=1)).isoformat()
    d = _call(ch, api_key, need_d, need_h, need_l,
              model="deepseek-chat",
              api_base="https://api.deepseek.com/v1",
              timeout=90)

    digest = _digest_lines(d.get("digest")) if need_d else []
    herday = ""
    if need_h:
        herday = mind_herday._clean(str(d.get("her_day") or "")) if d.get("her_day") else ""
    lines = _game_lines(d.get("game_lines"), allow_love) if need_l else []

    # 有一样空就整个退回去分开跑 —— 合并版坏了不该拖垮三样
    # 起居：由模型生成（2026-11-02 从词库抽改成 AI）
    _task = str(d.get("task") or "").strip().strip("。")
    _thought = str(d.get("thought") or "").strip().strip("。")
    _trouble = str(d.get("trouble") or "").strip().strip("。")
    _place = str(d.get("place") or "").strip()
    _PLACES = ("院子", "堂屋", "厨房", "卧房", "后廊", "卫生间", "储物角", "外面")
    _tastes = [str(x).strip() for x in (d.get("tastes") or [])
               if str(x).strip()][:3]
    _taste_changes = [str(x).strip() for x in (d.get("taste_changes") or [])
                      if str(x).strip()][:2]

    ok_any = (digest or herday or lines or _task or _thought
              or _trouble or _tastes)
    if not ok_any:
        return _fallback(ch, api_key, allow_love, need_d, need_h, need_l)

    done = []

    # 她今天的手边/心里（2026-11-02：从词库抽改成模型生成）
    # 值在上面的 _task / _thought 里已经取好了
    if 4 <= len(_task) <= 24:
        ch.today_task = _task
        done.append(f"手边「{_task[:12]}」")
    if 4 <= len(_thought) <= 28:
        ch.today_thought = _thought
        done.append(f"心里「{_thought[:12]}」")
    # 口味变化：从对话里长出来的，不同于每天抄卡的那三条
    for tc in _taste_changes:
        if 3 <= len(tc) <= 14:
            ch.taste_notes = [x for x in (ch.taste_notes or [])
                              if str(x.get("item", "")) != tc.split("：")[0]]
            ch.taste_notes.append({"item": tc.split("：")[0],
                                   "note": tc,
                                   "day": date.today().isoformat()})
            done.append(f"口味变化「{tc}」")
    if len(ch.taste_notes or []) > 20:
        ch.taste_notes = ch.taste_notes[-20:]

    if _place in _PLACES:
        ch.today_place = _place
        done.append(f"在{_place}")

    if 6 <= len(_trouble) <= 30:
        ch.today_trouble = _trouble
        done.append(f"小麻烦「{_trouble[:10]}」")
    if len(_tastes) >= 2:
        ch.today_tastes = _tastes
        done.append(f"口味 {len(_tastes)} 条")

    if _task or _thought or _trouble or _tastes:
        ch.life_brief_day = date.today().isoformat()

    if digest:
        ch.add_digest(y, digest)
        done.append(f"摘要 {len(digest)} 条")
        try:
            import mind_week
            wk_ok, wk_msg = mind_week.run_once(ch, api_key)
            if wk_ok:
                done.append(f"周摘要 {wk_msg}")
        except Exception:
            pass
    if herday:
        ch.note_her_event(herday, day=y)
        done.append(f"她的事「{herday[:14]}」")
    if lines:
        mind_lines.write(lines, meta={"day": date.today().isoformat(),
                                         "kinds": "merged"})
        done.append(f"台词 {len(lines)} 条")

    # 缺的那部分再单独补
    missing_d = need_d and not digest
    missing_h = need_h and not herday
    missing_l = need_l and not lines
    if missing_d or missing_h or missing_l:
        f_ok, f_msg = _fallback(ch, api_key, allow_love,
                                missing_d, missing_h, missing_l)
        if f_msg:
            done.append(f_msg)

    return True, " / ".join(done)


def _fallback(ch, api_key: str, allow_love: bool,
              need_d: bool, need_h: bool, need_l: bool) -> tuple[bool, str]:
    """分开跑（合并版失败时的退路）。"""
    msgs = []
    try:
        if need_d:
            ok, msg = mind_digest.run_once(ch, api_key)
            if ok:
                msgs.append(msg)
    except Exception:
        pass
    try:
        if need_h:
            ok, msg = mind_herday.run_once(ch, api_key,
                                          season=getattr(ch, "today_season", "") or "")
            if ok:
                msgs.append(msg)
    except Exception:
        pass
    try:
        if need_l:
            ok, msg = mind_lines.run_once(ch, api_key, allow_love=allow_love)
            if ok:
                msgs.append(msg)
    except Exception:
        pass
    if not msgs:
        return False, "合并与分开两条路都失败了"
    return True, "分开跑：" + " / ".join(msgs)
