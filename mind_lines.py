"""游戏台词生成 —— 把聊天里的「她」搬到这座镇子的对话框里。

## 这个模块在整条链路的哪一环

    mind-life（聊天）          本模块                 SMAPI mod
    记忆/心情/观察/摘要  ──>  每天生成 8 条台词  ──>  按当下状态挑一条贴上
                                写 game_lines.json      （不联网、不需要密钥）

**游戏侧只读不生成。** 所以：不卡帧、不用联网、不用在游戏里配密钥，
而且说的话和聊天里是**同一个脑子**出来的 —— 不会变成两个人。

## 为什么不做「实时生成」

1. 这座镇子是单线程，交互路径上发网络请求会卡帧
2. 每次问候都调一次模型，成本高且容易失败
3. 它只接管**日常问候**（送礼、花束、事件一律不碰），
   而日常问候需要的是「她最近的状态」，不是「这一秒的上下文」

## 8 条怎么分配

    1 条 today     提到你们最近的事（昨天/前天）
    1 条 promise   关于他还没做到的事 —— **口气要松，不许催**
    6 条 state     她自己的日子、手上的活、心情

`today` 那条是「她还记得」的证据。没有它，玩家看到的全是她的日常，
不会觉得她知道你们聊过什么。

## 三档标签

    season    any / spring / summer / fall / winter
    weather   any / sunny / rainy / stormy / snowy
    tier      any / low / mid / high      （<4 心 / 4-7 / >=8）

mod 按当下状态挑，挑不到就回退原版台词。
"""
from __future__ import annotations

import mind_llm
import json
import os
import re
import urllib.request
from datetime import date, datetime
from pathlib import Path

OUT_FILE = Path(__file__).resolve().parent / "saves" / "game_lines.json"

# 除了自己的 saves/，再往 mod 目录写一份 —— mod 侧默认就在那儿找。
#
# 为什么写两个地方：`saves/` 是权威副本（可备份、可给人看）；
# mod 目录那份是**拿来就能用**的。改路径不用重编 C#，
# 想换位置就设环境变量 MIND_MOD_DIR。
MOD_MIRROR = Path(os.environ.get("MIND_MOD_DIR") or
                  r"C:\Steamlab\steamapps\common\the valley\Mods"
                  r"\[CP] ShuAIDialogue")

MAX_LINES = 8
MAX_CHARS = 30
MIN_CHARS = 4

# 合法的表情记号。$l 只在亲密阶段允许（和 mod 侧校验器同一套规则）。
EMOTIONS = ("$h", "$u", "$s", "$a", "$l")

_PROMPT = """你在替「她」写这座镇子游戏里的日常问候台词。

## 她现在的样子

{state}

## 要求

生成 {n} 条，输出**一个 JSON 数组**，不要别的文字。

每条的格式：

{{"text":"说的话","emotion":"$h","kind":"state","season":"any","weather":"any","tier":"any"}}

### text
- **{max_chars} 字以内**（游戏对话框一行就这么宽）
- **只写她说的话。不要写动作** —— 游戏里没有动作描写，
  别用括号，别写「（顿了顿）」这种东西
- 说人话，不要套话、不要排比

### emotion
`$h` 高兴 / `$u` 若有所思 / `$s` 难过 / `$a` 不高兴 / `$l` 心里软
**`$l` 只允许出现在 tier=high 的那条上**，别的档位用了会被丢弃。

### kind（一共 {n} 条）
- **1 条 `today`**：必须提到**他**或**你们**，用「你们最近在聊天里说的话」
  当原料。这是「她还记得」的证据 —— 写她自己的事不算。
  ✅「昨晚你说的那个事，我记着」
  ❌「昨晚猫把布条扒散了」（那是她自己的事，属于 state）
- **{promise_n} 条 `promise`**：关于他还没做到的事。**口气要松，绝不许催**
  —— 写「上次说的那个……忘了也不要紧」，不写「你怎么还没」。
  如果上面没有「他答应过、还没做到的」，就**不要写这一条**。
- **{state_n} 条 `state`**：她自己的日子、手上的活、心情。

### 三个标签（决定这句话什么条件下出现）
- `season`：any / spring / summer / fall / winter
- `weather`：any / sunny / rainy / stormy / snowy
- `tier`：any / low / mid / high（好感低 / 中 / 高）
拿不准就写 any。**不要 8 条全是 any** —— 至少有几条带具体条件。

直接输出 JSON 数组。
"""


# ══════════════════════════════════════════════════════════
# 状态采集
# ══════════════════════════════════════════════════════════
def _state_text(ch, today: str = "") -> str:
    """把「她现在的样子」拼成一段 —— 只挑对台词有用的。"""
    out = []
    try:
        from mind_mood import Mood
        m = Mood.from_dict(getattr(ch, "mood", {}) or {})
        out.append(f"心情：{m.valence_word}（{m.valence}），精力：{m.energy_word}")
    except Exception:
        pass

    if getattr(ch, "today_task", ""):
        out.append(f"她手边：{ch.today_task}")
    if getattr(ch, "today_thought", ""):
        out.append(f"她心里：{ch.today_thought}")

    hers = [e for e in (getattr(ch, "her_events", None) or [])][-3:]
    if hers:
        out.append("她这些天的事：")
        out += [f"  - （{e.day}）{e.text}" for e in hers]

    projs = getattr(ch, "projects", None) or []
    if projs:
        out.append("她手上没做完的：")
        for p in projs:
            t = p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
            w = p.get("what", "") if isinstance(p, dict) else getattr(p, "what", "")
            s = p.get("stage", 0) if isinstance(p, dict) else getattr(p, "stage", 0)
            n = p.get("total", 3) if isinstance(p, dict) else getattr(p, "total", 3)
            out.append(f"  - {t}，{w}（{s}/{n}）")

    unkept = [p for p in (getattr(ch, "promises", None) or [])
              if not getattr(p, "kept", False)]
    if unkept:
        out.append("他答应过、还没做到的：")
        out += [f"  - {p.text[:40]}" for p in unkept[:3]]

    ds = getattr(ch, "digests", None) or []
    if ds:
        d = ds[-1]
        out.append(f"最近那天的经过（{d.get('day', '')}）：")
        out += [f"  - {x}" for x in (d.get("lines") or [])[:4]]

    # 你们最近在聊天里说的话 —— `today` 那条台词的原料。
    # 不给这个，模型就会拿「她自己的事」冒充「你们的事」。
    turns = [t for t in (getattr(ch, "turns", None) or [])][-6:]
    if turns:
        out.append("你们最近在聊天里说的话：")
        for t in turns:
            who = "他" if getattr(t, "role", "") == "player" else "她"
            txt = str(getattr(t, "text", "") or "").replace("\n", " ")[:50]
            out.append(f"  - {who}：{txt}")

    tn = getattr(ch, "taste_notes", None) or []
    if tn:
        out.append("她从对话里记下的口味变化（生成口味时要遵守）：")
        out += [f"  - {x.get('note', '')}" for x in tn[-6:]]

    obs = getattr(ch, "observations", None) or []
    if obs:
        out.append("她眼里的他：")
        out += [f"  - {o}" for o in obs[:3]]

    if today:
        out.append(f"今天是 {today}。")
    return "\n".join(out) if out else "（没有额外信息）"


# ══════════════════════════════════════════════════════════
# 解析与校验
# ══════════════════════════════════════════════════════════
_BAD = re.compile(r"[（）()\[\]【】\n`]")
_ACTION = re.compile(r"[（(]\s*[^）)]{1,20}[）)]")


def _clean_line(d: dict, allow_love: bool) -> dict | None:
    """校验一条。不合格返回 None（宁可不给，也不放一句崩坏的）。"""
    if not isinstance(d, dict):
        return None
    text = str(d.get("text") or "").strip()
    text = text.strip("　 \"'“”「」『』")
    if not (MIN_CHARS <= len(text) <= MAX_CHARS + 4):
        return None
    # 不许有动作描写/括号/换行
    if _BAD.search(text) or _ACTION.search(text):
        return None

    emo = str(d.get("emotion") or "").strip()
    if emo not in EMOTIONS:
        emo = "$h"
    if emo == "$l" and not allow_love:
        emo = "$h"

    kind = str(d.get("kind") or "state").strip().lower()
    if kind not in ("state", "today", "promise"):
        kind = "state"

    def tag(key, allowed):
        v = str(d.get(key) or "any").strip().lower()
        return v if v in allowed else "any"

    return {
        "text": text,
        "emotion": emo,
        "kind": kind,
        "season": tag("season", ("any", "spring", "summer", "fall", "winter")),
        "weather": tag("weather", ("any", "sunny", "rainy", "stormy", "snowy")),
        "tier": tag("tier", ("any", "low", "mid", "high")),
    }


def _parse(out: str) -> list:
    t = (out or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(t[i:j + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    return arr if isinstance(arr, list) else []


# ══════════════════════════════════════════════════════════
# 生成
# ══════════════════════════════════════════════════════════
def generate(ch, api_key: str, allow_love: bool = True,
             model: str = "deepseek-chat",
             api_base: str = "https://api.deepseek.com/v1",
             timeout: int = 60) -> list:
    """生成一批台词。失败返回空列表（调用方保留旧文件）。"""
    if not api_key:
        return []

    unkept = [p for p in (getattr(ch, "promises", None) or [])
              if not getattr(p, "kept", False)]
    promise_n = 1 if unkept else 0
    prompt = _PROMPT.format(
        state=_state_text(ch, date.today().isoformat()),
        n=MAX_LINES,
        promise_n=promise_n,
        state_n=MAX_LINES - 1 - promise_n,
        max_chars=MAX_CHARS,
    )
    payload = {
        "model": model,
        "temperature": 1.0,     # 台词要有点变化，别八条一个调
        "max_tokens": 900,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        out = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return []

    lines, seen = [], set()
    for d in _parse(out):
        v = _clean_line(d, allow_love)
        if not v:
            continue
        if v["text"] in seen:
            continue
        seen.add(v["text"])
        lines.append(v)
        if len(lines) >= MAX_LINES:
            break
    return _fix_today(lines)


def _fix_today(lines: list) -> list:
    """把 `today` 那条校准到「真的提到他」的那句上。

    ## 为什么需要后处理

    提示词已经说了「today 必须提到他或你们」，但实测模型会把
    她自己的事（「猫又把布条扒了」）标成 today ——
    那玩家就看不到「她还记得」的证据。

    这里做一件机器能判的事：**today 那条必须出现「你 / 他 / 你们」**。
    不满足就找一条满足的换上来；一条都没有就把 today 降级成 state。
    """
    import re as _re
    if not lines:
        return lines
    MARK = _re.compile(r"[你他]|咱")
    today = [x for x in lines if x.get("kind") == "today"]
    if today and MARK.search(today[0]["text"]):
        return lines

    # 找一条提到他的、非 today 的
    cand = [x for x in lines if x.get("kind") != "today" and MARK.search(x["text"])]
    if not cand:
        for x in today:
            x["kind"] = "state"
        return lines

    # 挑最像「记得」的那条：含「昨天/那天/上次/说」优先
    def score(x):
        t = x["text"]
        return sum(1 for k in ("昨天", "那天", "上次", "说的", "答应", "记着") if k in t)

    pick = max(cand, key=score)
    pick["kind"] = "today"
    for x in today:
        if x is not pick:
            x["kind"] = "state"
    return lines


def write(lines: list, meta: dict | None = None) -> bool:
    """写进 game_lines.json。mod 从这儿读。"""
    if not lines:
        return False
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "version": 1,
        "meta": meta or {},
        "lines": lines,
    }
    tmp = OUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(OUT_FILE)

    # 顺手写一份到 mod 目录（拿不到就跳过，不影响主流程）
    try:
        if MOD_MIRROR.is_dir():
            (MOD_MIRROR / "game_lines.json").write_text(
                OUT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    return True


def run_once(ch, api_key: str, allow_love: bool = True,
             force: bool = False) -> tuple[bool, str]:
    """生成并写盘。返回 (有没有改动, 说明)。

    同一天只生成一次（除非 force）—— 否则每次跨天检查都会重跑。
    """
    today = date.today().isoformat()
    if not force and OUT_FILE.is_file():
        try:
            old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            if str(old.get("generated_at", ""))[:10] == today:
                return False, f"{today} 已经生成过"
        except (OSError, json.JSONDecodeError):
            pass

    lines = generate(ch, api_key, allow_love=allow_love)
    if not lines:
        return False, "生成失败（保留旧文件）"

    # `$l`（爱慕）只能出现在 tier=high 上 —— 低心数说出 8 心的话会崩人设。
    # 这里在写盘前统一收紧，mod 侧就不用再判了。
    for x in lines:
        if x["emotion"] == "$l" and x.get("tier") != "high":
            x["emotion"] = "$h"

    kinds = {}
    for x in lines:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    write(lines, meta={"day": today, "kinds": kinds})
    return True, f"{len(lines)} 条 {kinds}"
