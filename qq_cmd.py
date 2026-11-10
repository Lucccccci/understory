"""QQ 控制指令（2026-11-08）

QQ 只有一行字，网页上的面板在手机上看不见 —— 所以给一套 `/` 指令，
把那些"看不见的东西"搬到手机上。

## 规矩

  ① **指令不进记忆**：在 bridge 里截住，根本不会发到 /api/qq
  ② 只有主人能用
  ③ 输出是系统口吻，不是她的语气
  ④ 测试档 / 假装网页 是**粘性状态**，存在 saves/_ops/qq_flags.json

## 指令表

  /help                 这一页
  /status               现在几点 / 天气 / 她在哪 / 在做什么 / 心情精力
  /today                她的今天：手边、心里、小麻烦、口味
  /facts                她记得的事实（按权重取 5 条）
  /sees                 她认得的你（观察）
  /events               最近的几件事（带日期）
  /promises             你答应过还没做的
  /relation             阶段 / 天数 / 心数 / 累计轮次
  /recall <词>          翻旧账（3 条）
  /diary                最近一篇日记
  /house                她现在在房子的哪一处
  /re                   重说她上一句（抹掉那对问答再重跑）
  /undo                 撤回上一对问答
  /life                 她此刻换个说法
  /test                 切到测试档（输出都会带【测试】）
  /desktop              切回主存档
  /home                 假装你是在网页上说话（通道变 web）
  /home off             关掉这个假装
"""
from __future__ import annotations

import json
from pathlib import Path

FLAGS = Path(__file__).with_name("saves") / "_ops" / "qq_flags.json"

HELP = """【指令】
/status  状态快照
/today   她的今天
/facts   她记得的事实
/sees    她认得的你
/events  最近的事
/promises 还没做的承诺
/relation 关系与轮次
/recall 词  翻旧账
/diary   最近一篇日记
/house   她在哪间屋
/re      重说上一句
/undo    撤回上一轮
/life    换个说法
/test    切测试档（带【测试】）
/desktop 切回主存档
/home    假装在网页说话"""


def load_flags() -> dict:
    try:
        d = json.loads(FLAGS.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return {"session": d.get("session") or "desktop",
            "home": bool(d.get("home"))}


def save_flags(d: dict) -> None:
    FLAGS.parent.mkdir(parents=True, exist_ok=True)
    FLAGS.write_text(json.dumps({k: d[k] for k in ("session", "home")},
                                ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")


def is_test(flags: dict) -> bool:
    return flags.get("session") == "test"


def mark(text: str, flags: dict) -> str:
    """测试档下，所有输出都带【测试】。"""
    return ("【测试】" + text) if is_test(flags) else text
