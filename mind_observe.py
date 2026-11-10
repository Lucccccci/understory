"""她眼里的他 —— 观察抽取（2026-10-20 重做：加证据门槛）。

## 为什么重做

用户报「判定太过宽松」。查了一下，26 小时攒了 8 条，其中 5 条站不住：

    他会先关心别人            <- 泛化，几乎没有信息量
    他安心了就会轻声叫名字      <- 关系瞬间，不是性格
    他听不出关心会先愣一下      <- 单次反应
    他把怕失去说成想做得更好    <- 心理推断
    他趁黑说真心话，天亮不认    <- 推断 + 有点臆测

只有 3 条是真的行为模式。

## 根因：五处都太松

    ① 窗口只有 10 轮         <- 大约五分钟的对话，永远只看「最近这点事」
    ② 每 30 分钟产 1 条      <- 频率高到没有筛选压力
    ③ 没有任何证据门槛        <- 一次行为 = 一条永久性格
    ④ 不分类型              <- 瞬间 / 推断 / 模式 混在一个列表
    ⑤ 不去重不淘汰           <- 「他会先关心别人」和「他照顾人时忘了自己」同义

## 现在怎么判

**一条观察要进 `observations`（显示在她眼里），得在不同日期的至少 3 次里被看见。**

    type        trait（跨天重复）/ moment（一次性）/ interpretation（推断）
    days        模型必须指出它在哪几天出现过 —— 而且那些日子必须真的在窗口里
    evidence    模型必须原样抄一句原文 —— 抄不出来就说明在编

三条都是**可以机械校验**的，不靠模型自觉。

    窗口    最多 120 轮，且尽量覆盖 3 个不同日期（只看最近 10 轮看不出重复）
    冷却    2 小时（原来 30 分钟 —— 证据是攒出来的，不需要高频重试）
    候选池  只看见过 1-2 天的留在这里，**不显示给用户**

## 为什么候选池不显示

用户说「不要」。看见「她还在怀疑你」会破坏感觉 ——
她自己都不确定的事，不该摆在你面前。

## 和正则的关系

正则（`mind_life.observe`）已经删了，这里全部走模型。
"""
from __future__ import annotations

import mind_llm
import json
import re
import urllib.request
from datetime import datetime, timedelta

MAX_OBSERVATIONS = 12

# 上次尝试之后至少再聊这么多轮才再试
MIN_NEW_TURNS = 10

# 两次尝试之间至少隔这么久。
#
# 从 30 分钟改成 2 小时：证据是**跨天**攒出来的，高频重试只会
# 把同一段对话反复判一遍，除了烧钱没有用。
TRY_COOLDOWN = timedelta(hours=2)

# 窗口：往回取多少轮。够长才看得见「重复」。
WINDOW_MAX_TURNS = 120

# 窗口至少要覆盖这么多天。不够就继续往回取。
WINDOW_MIN_DAYS = 3

# 至少在不同日期出现这么多次，才升格成一条观察。
PROMOTE_DAYS = 2

# 两条观察「算同一件」的相似度阈值（字符二元组的 Jaccard）
SIM_MERGE = 0.55

# 候选池上限（不显示，但也不能无限长）
MAX_CANDIDATES = 20


# ══════════════════════════════════════════════════════════
# 窗口
# ══════════════════════════════════════════════════════════
def _day_of(turn) -> str:
    """轮次属于哪一天（MM-DD）。取不到就空串。"""
    at = getattr(turn, "at", "") or ""
    return at[5:10] if len(at) >= 10 else ""


def _window(ch) -> list:
    """往回取对话，尽量凑够 WINDOW_MIN_DAYS 天。

    ## 为什么不是简单的 turns[-N:]

    原来的窗口是最近 10 轮 —— 大约五分钟。而「他是个什么样的人」
    恰恰要看他**反复**怎么做，五分钟看不出来。

    这里往回走，直到**轮数够了 且 天数够了**，或者走到头。
    """
    turns = getattr(ch, "turns", None) or []
    if not turns:
        return []

    picked: list = []
    days: set[str] = set()
    for t in reversed(turns):
        picked.append(t)
        d = _day_of(t)
        if d:
            days.add(d)
        if len(picked) >= WINDOW_MAX_TURNS and len(days) >= WINDOW_MIN_DAYS:
            break
    picked.reverse()
    return picked


def _recent_text(ch) -> str:
    """把窗口按天分段 —— **必须带日期**，否则模型没法指出「哪几天」。"""
    picked = _window(ch)
    if not picked:
        return "（没有）"

    out: list[str] = []
    cur = None
    for t in picked:
        d = _day_of(t) or "（未知）"
        if d != cur:
            out.append(f"\n【{d}】")
            cur = d
        who = "他" if getattr(t, "role", "") == "player" else "你"
        txt = str(getattr(t, "text", "")).replace("\n", " ")[:110]
        out.append(f"{who}：{txt}")
    return "\n".join(out).strip()


def _window_days(ch) -> set[str]:
    return {d for d in (_day_of(t) for t in _window(ch)) if d}


def _by_day(ch) -> dict:
    """窗口按天分组：{"10-19": [turn, ...]}"""
    out: dict = {}
    for t in _window(ch):
        d = _day_of(t)
        if d:
            out.setdefault(d, []).append(t)
    return out


def _his_text(turns) -> str:
    """只取**他那侧**的话（role == player）。

    ## 为什么要限定

    一条「他是个什么样的人」的判断，必须由**他自己做过的事**支撑。
    她的旁白（「他愣了一下」）是她自己的描写，拿它当证据等于自证。
    """
    return _norm("\n".join(str(getattr(t, "text", "") or "")
                            for t in (turns or [])
                            if getattr(t, "role", "") == "player"))


def _known_text(ch) -> str:
    """已经确认的观察 —— 不要重复。"""
    obs = getattr(ch, "observations", None) or []
    return "\n".join(f"- {x}" for x in obs) if obs else "（还没有）"


def _pending_text(ch) -> str:
    """起疑但还没确认的候选。

    ## ⚠️ 这一段是「**鼓励**重复」的，跟上面相反

    第一版把它和「已知」混在一起，并且统一说了「不要重复」。
    结果：模型每次都换个新的说，候选**永远攒不到第三天**，
    升格机制等于死的。

    人的认识不是这样 —— 你看见他第二次、第三次还是那样，
    那才叫「我摸清他了」。所以这里要明说：又看见了，就再报一次。
    """
    cand = getattr(ch, "observation_candidates", None) or []
    lines = []
    for c in cand:
        if not isinstance(c, dict):
            continue
        t = c.get("text")
        if not t:
            continue
        days = "、".join(c.get("days") or [])
        lines.append(f"- {t}（已在 {days} 看见过）")
    return "\n".join(lines) if lines else "（没有）"


# ══════════════════════════════════════════════════════════
# 提示词
# ══════════════════════════════════════════════════════════
_PROMPT = """你在替一个角色判断：从这些天里，她**反复看见**的他是怎么样的人。

## 她已经确认的（**不要重复**）

{known}

## 她起疑、但还没确认的（**如果这次又看见了，就再报一次**）

{pending}

⚠️ 第二段是**希望你重复**的 —— 「又看见一次」正是确认它的方式。
如果这次又对上了，照样输出它，`days` 里把这次的日子也加上。

## 最近这些天（每段前面是日期）

{recent}

## 判定的门槛（很严，宁可没有）

只有同时满足下面三条，才算一条新观察：

1. **跨天重复**：同一个行为，在**至少两个不同的日期**里都出现过。
   只出现过一次的，不算 —— 那可能只是那天他累了、或者那天情况特别。
2. **是行为，不是解释**。
   写「他累了不吭声，问起来才说」；
   不写「他把怕失去说成想做得更好」—— 后者是解释，不是看见的。
3. **不是关系瞬间**。
   写「他照顾人的时候，自己那份总是忘了」；
   不写「他安心了就会轻声叫名字」—— 后者是那一刻发生的事，不是他是什么人。
4. **要写「怎么做」，不写「是什么样的人」。**
   ✅ 他累了不吭声，问起来才说
   ✅ 他答应完会当天就做
   ❌ 他总把关心落到具体小事上      <- 这是概括，看一百次也还是概括
   ❌ 他很体贴 / 他很可靠 / 他很在意我
   **判据**：这句话能不能让你在下次看见时**认出来**？
   概括不能，具体的行为能。

以下一律**不算**：
- 一次性的反应、随口说的一句话
- 对他的心理分析、动机猜测
- 你们之间发生过的事（那属于「一起做过的事」，另有一套记录）
- 关于他的信息（名字、家人、爱好、住在哪）

## 输出格式

必须输出一行 JSON，不要别的文字、不要代码块：

{{"type":"trait","verdict":"add","merge_into":"","text":"他……","days":["10-19","10-20"],"evidence":"原样抄一句对话"}}

- verdict 只能填这五个之一，**这一步比 text 本身更要紧**：
    add      真的是一条新观察，收下
    merge    和「她已经确认的」里某一条是同一件事 → merge_into 抄那一条
             （别新增，等于"又看见一次"）
    conflict 和已确认的某条**互相矛盾** → merge_into 抄那一条，
             进待确认区，等再看见一次才替换
    fact     这其实是一个**事实**（身体、工作、家里几口、住哪、养什么），
             不归观察管 → 交给记忆，不进这栏
    reject   是概括（「他很守约」）、是一次性的瞬间、或者拿不准 → 不收
  **拿不准就 reject。** 宁可少一条，不要多一条假的。

- type：trait（跨天重复的行为）/ moment（一次性）/ interpretation（推断）
- text：20 字以内，必须以「他」开头
- days：这个行为在**哪几天**出现过 —— 只能用上面标出来的日期
- evidence：**每天各抄一句**，只能用**他自己说的话或做的事**（「他：」那种行），
  不能用她的旁白。抄不出来就说明这天没有证据，那天就不该写进 days。

```
{{"type":"trait","text":"他答应的事当天就动手做",
 "days":["10-19","10-20"],
 "evidence":{{"10-19":"好呢（走过去开始收拾木柴）",
             "10-20":"说好的事我今天做完了"}}}}
```

**即使不够格也照样输出**，把 type 写成 moment 或 interpretation 就行 ——
我要知道你为什么判它不够格。

如果连一条候选都没有，输出：{{"type":"none"}}
"""


# ══════════════════════════════════════════════════════════
# 校验
# ══════════════════════════════════════════════════════════
_PUNCT = re.compile(r"[\s，。！？、…—·「」『』（）()\"'“”‘’,.!?~～:：;；]+")


def _norm(s: str) -> str:
    return _PUNCT.sub("", s or "")


def _bigrams(s: str) -> set[str]:
    n = _norm(s)
    return {n[i:i + 2] for i in range(len(n) - 1)}


def _sim(a: str, b: str) -> float:
    """字符二元组的 Jaccard。短中文串上比编辑距离稳。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _parse(out: str) -> dict | None:
    """从模型输出里挖出那个 JSON。"""
    t = (out or "").strip()
    if not t:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(t[i:j + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _validate(d: dict, ch) -> tuple[dict | None, str]:
    """机械校验。返回 (可入库的候选, 为什么不行)。"""
    typ = str(d.get("type") or "").strip().lower()
    if typ == "none":
        return None, "模型说没有"
    if typ != "trait":
        return None, f"不够格（{typ or '未标类型'}）"

    text = str(d.get("text") or "").strip().strip("　 \"'“”「」『』")
    if not text.startswith("他"):
        return None, "不是「他……」的句式"

    # 2026-11-06：语义判断交给模型（verdict），代码只做结构检查。
    # 「是不是概括 / 是不是重复 / 是不是矛盾」不再用正则硬拦。
    verdict = str(d.get("verdict") or "add").strip().lower()
    if verdict not in ("add", "merge", "conflict", "fact", "reject"):
        verdict = "add"
    if not (5 <= len(text) <= 26):
        return None, f"长度 {len(text)} 不合（5-26 字）"

    # 总结式的开头一律挡掉：「他总把关心落到小事上」这种话
    # 看一百次也还是概括，不构成「认得出他」。
    if re.match(r"^他(总|一直|很|非常|特别|挺|太)", text):
        return None, "是概括，不是具体行为"

    # days：必须真的在窗口里出现过
    raw_days = d.get("days") or []
    if isinstance(raw_days, str):
        raw_days = [raw_days]
    have = _window_days(ch)
    days = []
    for x in raw_days:
        x = str(x).strip()[-5:]          # 允许它写 2026-10-19
        if x in have and x not in days:
            days.append(x)
    if len(days) < 2:
        return None, f"跨天证据不足（有效日期 {len(days)} 个）"

    # ── evidence：**每一天各一条**，而且必须出自他那侧 ──
    #
    # 原来只要「任意 6 个字在整个窗口里找得到」就过 —— 窗口有 6500 字，
    # 随便抄一句不相干的话都能凑上。实测 8 次全部通过，等于没筛。
    #
    # 现在：days 里每一天都要有一条，而且必须在**那一天的他说的话**里。
    # 关于他的判断，不能靠她的旁白来支撑 —— 那是自证。
    ev = d.get("evidence")
    if isinstance(ev, str):                     # 宽容：偶尔还会输出字符串
        ev = {"": ev}
    if not isinstance(ev, dict) or not ev:
        return None, "没给证据"

    per_day = _by_day(ch)
    quotes: dict = {}
    for day in days:
        q = ev.get(day)
        if not q:                               # 允许它把日期写成 2026-10-19
            for k2, v2 in ev.items():
                if str(k2).strip()[-5:] == day:
                    q = v2
                    break
        q = str(q or "").strip()
        if not q:
            return None, f"{day} 那天没给证据"
        qn = _norm(q)
        if len(qn) < 4:
            return None, f"{day} 的证据太短"

        pool = _his_text(per_day.get(day) or [])

        # 2026-11-02：从「逐字出现」放宽到「关键词重合 ≥ 60%」。
        #
        # 原来要求一个 6 字片段原样出现在当天他的话里 —— 模型只要把
        # 「我右膝动过手术」写成「右膝盖做过手术」，整条观察就被丢掉。
        # 实测候选池长期只有 1 条，这就是主因。
        #
        # 现在按 3-gram 重合率算：意思对得上就能过，胡编的仍然过不去
        # （胡编的句子和他那天的原话几乎不会有三字重合）。
        # 短证据用 2-gram：模型把「右膝动过手术」写成「右膝盖做过手术」时，
        # 3-gram 只有 50% 重合会被误杀；2-gram 能到 80%，而胡编的仍是 0%。
        k = 2 if len(qn) <= 10 else 3
        if k < 2:
            return None, f"{day} 的证据太短"
        grams = {qn[i:i + k] for i in range(len(qn) - k + 1)}
        hit_n = sum(1 for g in grams if g in pool)
        ratio = hit_n / max(1, len(grams))
        if ratio < 0.6:
            return None, f"{day} 的证据和原话差太远（重合 {ratio:.0%}）"
        quotes[day] = q[:60]

    return {"text": text, "days": days, "evidence": quotes,
            "verdict": verdict,
            "merge_into": str(d.get("merge_into") or "").strip()}, ""


# ══════════════════════════════════════════════════════════
# 入库
# ══════════════════════════════════════════════════════════
def _trim_candidates(ch) -> None:
    cand = getattr(ch, "observation_candidates", None) or []
    if len(cand) > MAX_CANDIDATES:
        cand.sort(key=lambda c: str(c.get("last") or ""))
        ch.observation_candidates = cand[-MAX_CANDIDATES:]


def _record(ch, cand: dict, now_iso: str) -> tuple[bool, str]:
    """把一条候选并进去。够天的直接升格成观察。"""
    text = cand["text"]
    days = set(cand["days"])

    # ① 已经是观察了 —— 只刷新确认时间，不新增
    for o in (getattr(ch, "observations", None) or []):
        if _sim(text, o) >= SIM_MERGE:
            ch.observation_meta[o] = now_iso
            return True, f"已确认过：{o}"

    # ② 命中已有候选 —— 并天，够了就升格
    for c in (getattr(ch, "observation_candidates", None) or []):
        if _sim(text, str(c.get("text") or "")) >= SIM_MERGE:
            dset = set(c.get("days") or []) | days
            c["days"] = sorted(dset)
            c["n"] = int(c.get("n") or 0) + 1
            c["last"] = now_iso
            if len(dset) >= PROMOTE_DAYS:
                ch.observation_candidates.remove(c)
                ch.add_observation(str(c.get("text") or text))
                return True, f"升格（{len(dset)} 天）：{c.get('text')}"
            return True, f"候选 {len(dset)}/{PROMOTE_DAYS} 天：{c.get('text')}"

    # ③ 全新
    if len(days) >= PROMOTE_DAYS:
        ch.add_observation(text)
        return True, f"升格（{len(days)} 天）：{text}"

    ch.observation_candidates.append({
        "text": text, "days": sorted(days),
        "first": now_iso, "last": now_iso, "n": 1,
    })
    _trim_candidates(ch)
    return True, f"新候选 {len(days)}/{PROMOTE_DAYS} 天：{text}"


# ══════════════════════════════════════════════════════════
# 冷却
# ══════════════════════════════════════════════════════════
def should_try(ch, now: datetime | None = None) -> tuple[bool, str]:
    """现在该不该试一次模型判据。返回 (是否, 原因)。"""
    now = now or datetime.now()
    turns = getattr(ch, "turns", None) or []

    last_at = getattr(ch, "observation_tried_at", "") or ""
    if last_at:
        try:
            if now - datetime.fromisoformat(last_at) < TRY_COOLDOWN:
                return False, "刚试过"
        except (ValueError, TypeError):
            pass

    last_turn = int(getattr(ch, "observation_tried_turn", 0) or 0)
    if len(turns) - last_turn < MIN_NEW_TURNS:
        return False, f"新对话不够（{len(turns) - last_turn} 轮）"

    if not turns:
        return False, "还没聊过"
    return True, "可以试"


# ══════════════════════════════════════════════════════════
# 抽取
# ══════════════════════════════════════════════════════════
def extract(ch, api_key: str, model: str = "deepseek-chat",
            api_base: str = "https://api.deepseek.com/v1",
            timeout: int = 30) -> tuple[dict | None, str]:
    """让模型看出一段**可能**的观察。返回 (候选, 为什么不行)。"""
    if not api_key:
        return None, "没有 API 密钥"
    if not (getattr(ch, "turns", None) or []):
        return None, "还没聊过"

    prompt = _PROMPT.format(known=_known_text(ch),
                            pending=_pending_text(ch),
                            recent=_recent_text(ch))
    payload = {
        "model": model,
        "temperature": 0.3,        # 判定任务，越低越稳
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        out = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return None, f"调用失败：{type(e).__name__}"

    d = _parse(out)
    if not d:
        return None, f"输出不是 JSON：{out[:40]!r}"
    return _validate(d, ch)


def run_once(ch, api_key: str) -> tuple[bool, str]:
    """跑一次完整流程。返回 (有没有改动, 说明)。

    ## 为什么说明总是有内容

    不管成没成都要记 `observation_tried_at` —— 否则「看不出新的」时
    会每轮重试。所以调用方要在**返回 True 时存盘**，
    而不是只在「新增了观察」时存盘（那是原来的 bug）。
    """
    ok, why = should_try(ch)
    if not ok:
        return False, why

    now = datetime.now()
    ch.observation_tried_at = now.isoformat(timespec="seconds")
    ch.observation_tried_turn = len(getattr(ch, "turns", None) or [])

    cand, reason = extract(ch, api_key)
    if not cand:
        return True, f"没有新的（{reason}）"

    now_iso = now.isoformat(timespec="seconds")
    verdict = cand.get("verdict", "add")
    into = cand.get("merge_into", "")

    # 模型给的处理意见（2026-11-06）：代码不再自己判语义
    if verdict == "reject":
        return True, f"不收（{cand['text']}）"

    if verdict == "fact":
        # 其实是事实 → 交给记忆，不进观察
        try:
            ch.remember(cand["text"], kind="fact", weight=2)
            return True, f"转成事实：{cand['text']}"
        except Exception:
            return True, "转事实失败"

    if verdict == "merge" and into:
        for o in (getattr(ch, "observations", None) or []):
            if _sim(into, o) >= 0.5 or into in o or o in into:
                ch.observation_meta[o] = now_iso
                return True, f"又看见一次：{o}"
        return _record(ch, cand, now_iso)

    if verdict == "conflict":
        pending = getattr(ch, "observation_pending", None)
        if pending is None:
            pending = []
            ch.observation_pending = pending
        for x in pending:
            if _sim(str(x.get("text")), cand["text"]) >= 0.5:
                x["days"] = sorted(set(x.get("days") or []) | set(cand["days"]))
                x["n"] = int(x.get("n") or 0) + 1
                x["last"] = now_iso
                # 两次确认 → 替换掉矛盾的那条
                if int(x["n"]) >= 2:
                    old = str(x.get("vs") or "")
                    for o in list(getattr(ch, "observations", None) or []):
                        if old and (_sim(old, o) >= 0.5 or old in o):
                            ch.observations.remove(o)
                            ch.observation_meta.pop(o, None)
                    ch.add_observation(str(x["text"]))
                    pending.remove(x)
                    return True, f"改判（矛盾消解）：{x['text']}"
                return True, f"再等等看：{cand['text']}"
        pending.append({"text": cand["text"], "days": list(cand["days"]),
                        "n": 1, "last": now_iso, "vs": into})
        return True, f"记下矛盾待确认：{cand['text']}"

    return _record(ch, cand, now_iso)
