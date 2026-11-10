"""NPC 名字转中文。

## 为什么需要它

这座镇子存档里 NPC 名是**英文**，而 `as_facts()` 直接把它们塞进 prompt：

    和镇上这些人走得比较近：Linus（2 心）、Demetrius（2 心）、Robin（1 心）

模型看到英文名，就照着说英文 —— 实测出现过：

    她：嗯，Tuesdays 那边摆摊的多。
    她：……这味道，是 Jodi 家的？

中文对话里蹦英文名，很出戏。

## 两处都要做

    输入侧  把存档事实里的英文名换成中文，模型看到的就是中文
    输出侧  万一它还是打了英文（比如从记忆里翻出的旧名字），上屏前再兜一次

只做输入侧不够 —— 记忆里存着的旧对话可能带着英文名，
模型会照抄。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES_FILE = HERE / "tools" / "corpus" / "npc_names_zh.json"

_names: dict[str, str] | None = None
_pattern: re.Pattern | None = None


def _load() -> dict[str, str]:
    global _names, _pattern
    if _names is not None:
        return _names

    raw = json.loads(NAMES_FILE.read_text(encoding="utf-8")) if NAMES_FILE.is_file() else {}
    # 只取「英文名 -> 中文名」这种纯映射，跳过 _ 开头的说明字段
    _names = {k: v for k, v in raw.items()
              if not k.startswith("_") and isinstance(v, str)}

    if _names:
        # 长的名字排前面（Sebastian 要在 Sam 之前匹配，否则会被切开）
        keys = sorted(_names, key=len, reverse=True)
        # 用词边界，避免 "Sam" 命中 "Samantha" 这类
        _pattern = re.compile(
            r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k in keys) + r")(?![A-Za-z])")
    else:
        _pattern = None
    return _names


def to_zh(text: str) -> str:
    """把文本里的英文 NPC 名换成中文。认不出的保持原样。"""
    if not text:
        return text
    _load()
    if _pattern is None:
        return text
    names = _names or {}
    return _pattern.sub(lambda m: names.get(m.group(1), m.group(1)), text)


def known(name: str) -> bool:
    _load()
    return name in (_names or {})
