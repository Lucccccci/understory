"""把很旧的对话压成一段摘要（2026-11-08）

## 为什么

现在提示词只带**最近 3000 字**的历史 —— 聊一个月下来，前面全被截掉。
AstrBot 那边用的是 `llm_compress`：按 token 预算，把旧段压成摘要，
只保留最近一小段原文。这里照它的思路做，但更简单：

    保留    最近 3000 字原文（不动）
    压缩    更早的部分，攒够一批就折叠进 `context_rollup`
    注入    摘要作为一条 system 块，放在历史之前

## 为什么要"攒够一批"

每轮都压一次 = 每轮多一次调用。攒到 200 轮压一次，
一次调用覆盖 200 轮 —— 成本摊薄到接近零。

## 摘要里要留什么

不是"发生了什么"的流水账，是**还接得上的东西**：
关系进展、他的近况与性情、许过的承诺、闹过的别扭、反复出现的细节。
丢掉的应该是寒暄和过程，不是信息。
"""
from __future__ import annotations

import mind_llm

BATCH_TURNS = 200        # 攒够多少轮压一次
MAX_BATCH = 600          # 单次最多压多少轮（防止第一次一口气吞掉几千轮）
KEEP_TURNS = 120         # 最近这么多轮永远不压（原文留给窗口）
MAX_ROLLUP_CHARS = 700   # 摘要硬上限 —— 它是背景，不该比人设卡还长

_PROMPT = """你在替一个角色整理「很早以前的对话」的摘要。

## 已有的摘要（可能为空）

{old}

## 这次要并进去的旧对话（按时间顺序）

{new}

## 怎么写

把新内容并进摘要，输出**一份新的摘要**，不超过 {limit} 字。

必须留住：
- 关系走到了哪一步（认识了多久、说过什么要紧的话）
- 他的近况：身体、工作、家人、住过的地方、正在做的事
- 他许过的承诺、她说过的重话
- 反复出现的小细节（他习惯、她习惯、猫、屋子）

可以丢：寒暄、语气词、重复的日常、已经过去的心情。

**不写相对时间**（昨天/今天），要写就写「早些时候」「那阵子」。
**用陈述句，不要分点，不要标题。** 读起来像一段旁白。

直接输出摘要正文。
"""


def _fmt(turns) -> str:
    out = []
    for t in turns:
        who = "他" if getattr(t, "role", "") == "player" else "她"
        out.append(f"{who}：{str(getattr(t, 'text', ''))[:120]}")
    return "\n".join(out)


def due(ch) -> bool:
    """够不够压一次。"""
    total = len(getattr(ch, "turns", None) or [])
    done = int(getattr(ch, "rollup_count", 0) or 0)
    if total - done < BATCH_TURNS + KEEP_TURNS:
        return False
    return True


def rollup_if_due(ch, api_key: str, api_base: str = "https://api.deepseek.com/v1",
                  model: str = "deepseek-chat", timeout: int = 60):
    """够就压一次。返回 (有没有压, 说明)。**只改 context_rollup / rollup_count。**
    """
    if not due(ch):
        return False, "还没到"
    turns = list(getattr(ch, "turns", None) or [])
    done = int(getattr(ch, "rollup_count", 0) or 0)
    end = len(turns) - KEEP_TURNS
    # 单次封顶：第一次跑时积压可能上千轮，一次调用塞不下
    end = min(end, done + MAX_BATCH)
    if end <= done:
        return False, "没有可压的"

    batch = turns[done:end]
    old = str(getattr(ch, "context_rollup", "") or "")
    prompt = _PROMPT.format(old=old or "（空）", new=_fmt(batch),
                            limit=MAX_ROLLUP_CHARS)
    payload = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        body = mind_llm.chat_body(payload, api_key, api_base, timeout)
        text = (body["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return False, f"调用失败：{e}"

    text = text.lstrip("#").strip()
    if len(text) < 40:
        return False, "摘要太短，没采用"
    ch.context_rollup = text[:MAX_ROLLUP_CHARS * 2]
    ch.rollup_count = end
    return True, f"压了 {len(batch)} 轮，摘要 {len(ch.context_rollup)} 字"
