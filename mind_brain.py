#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她的对话引擎：把角色状态 + 人设 + 语料，组装成一次真实的多轮对话。

## 与 mod 里那份的区别

mod 里是**单轮生成日常台词**（她说一句，玩家看一句，没有上下文）。
这里是**真多轮对话**：

    * 带上最近若干轮真实对话记录 —— 她记得刚才说了什么
    * 带上她此刻手边的事与心事 —— 她有自己的日子
    * 长期记忆会被挑出来注入 —— 她记得你以前提过的事
    * 回复是**一段连续的回应**，不是一句孤立的打招呼

## 共用资产

人设与语料直接读 mod 工具目录里的同一批文件，保证两边性格一致：
    ../tools/prompts/mind_persona.json
    ../tools/corpus/stage_examples.json
    ../tools/corpus/mind_corpus.json
"""

from __future__ import annotations

import mind_llm
import json
import os
import mind_diag
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from mind_character import Character, stage_of, period_of
from mind_event import event_block, her_event_block
from mind_image import build_content as build_image_content
from mind_mood import Mood, mood_block
from mind_life import trouble_block
from mind_names import to_zh
from mind_promise import promise_block
from mind_memory import KnownWorld

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "tools"          # 旧布局（mod 工具目录），仅作兼容

# 人设：优先环境变量，其次 personas/current.json，最后空模板
PERSONAS = HERE / "personas"
PERSONA_FILE = Path(os.environ.get("MIND_PERSONA") or
                    (PERSONAS / "current.json" if (PERSONAS / "current.json").is_file()
                     else PERSONAS / "blank.json"))
# 语感示例与语料：没有就不注入（框架不要求它们存在）
STAGE_FILE = PERSONAS / "stage_examples.json"
CORPUS_FILE = PERSONAS / "corpus.json"

# 这里原来有四张词表（泰拉专有名词 / 军事意象 / 网络用语 / 格式残留），
# 配一个 `validate()` 在每轮回复后跑一遍，命中就在界面上挂一行
# 「这句偏离了人设：含军事意象「战场」」。
#
# 2026-10-23 按用户要求整层删掉了。理由：
#   ① 只按字面判，不看语境 ——「心里像战场」也中
#   ② 只警告不拦截，那句话照样进存档、照样发出去
#   ③ 人设卡里本来就写着这些禁令，模型绝大多数时候会遵守
#
# 现在人设靠卡里的指令，输出卫生靠 `clean_for_display`（去记号、压空行）。


# ─────────────────────────────────────────────────────────────
# 表情记号提醒 —— **必须放在历史之后**。
#
# 实测（2026-10-17）：system 里写了要求，但历史里有 83 条她的旧回复
# 几乎全都不带记号 -> 服务里 0/5。而同样的要求单轮测（不给历史）是 5/5。
#
# 结论：**上下文里的实例权重 > 远方的指令**，尤其 system 有 15k 字的时候，
# 中间那一段基本被淹没。所以这条极短的提醒放在历史之后、最后一句之前。
# 内容策略拦下时，`reply()` 返回的错误串以此开头。
#
# 为什么用一个显式标记，而不是靠「错误文本里含某几个字」判断 ——
# 调用方（server）需要**明确区分**三种失败：
#     内容策略拦下  -> 不能写进历史，也不能当成接口故障
#     网络/超时     -> 可以重试，同样不写历史
#     其它          -> 照旧
#
# 靠中文子串猜，改一个字就失效。
REFUSED_MARK = "【被拦】"      # 【被拦】

REMIND_EMOTION = (
    "回复末尾带一个表情记号，一个就够："
    "$h 开心 / $s 难过 / $u 若有所思 / $l 心里软了 / $a 不高兴。"
    "**这是格式要求，不是修辞选择。**"
)

# 星期 —— 她的世界观里她活在当代，知道今天礼拜几是理所当然的。
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# 十二时辰 —— 她说话会用这个（「三更过了」），那就得给准。
#
# 实测：03:33 她答「三更过了」，而三更是 23:00-01:00，那时是**五更**。
# 她不是不知道几点（prompt 里明明写着 03:33），是**换算错了**。
# 给她一张表，她就不用猜。
SHICHEN = [
    (23, 1, "子时", "三更"), (1, 3, "丑时", "四更"),
    (3, 5, "寅时", "五更"), (5, 7, "卯时", ""),
    (7, 9, "辰时", ""), (9, 11, "巳时", ""),
    (11, 13, "午时", ""), (13, 15, "未时", ""),
    (15, 17, "申时", ""), (17, 19, "酉时", ""),
    (19, 21, "戌时", "一更"), (21, 23, "亥时", "二更"),
]


def _shichen(hour: int) -> str:
    """小时 -> 「寅时（五更）」。夜里带更次，白天只有时辰。"""
    for lo, hi, name, geng in SHICHEN:
        hit = (lo <= hour < hi) if lo < hi else (hour >= lo or hour < hi)
        if hit:
            return f"{name}（{geng}）" if geng else name
    return ""


def digest_block(ch) -> str:
    """拼「前几天的大致经过」那一段。

    ## 为什么语气要「模糊」

    人回忆一周前只有轮廓。硬要她记住细节，她就会**编** ——
    而编出来的旧事比忘记更伤人。所以这里明确允许她说「记不太清了」。

    ## 和另外两块的层次

        你们之间发生过的事   events      零散但重要，带 ★
        你自己这些天的事     her_events  她那边的日子
        前几天的大致经过     digests     **每天一段轮廓**，补中间层
    """
    ds = [d for d in (getattr(ch, "digests", None) or [])
          if isinstance(d, dict) and d.get("lines")]
    wks = [w for w in (getattr(ch, "weeks", None) or [])
           if isinstance(w, dict) and w.get("lines")]
    if not ds and not wks:
        return ""
    # ⚠️ 这个 5 曾经和 `mind_digest.MAX_DAYS = 14` 对不上：
    # 存了 14 天，只注入 5 天 —— 中间那 9 天等于白存。
    # 现在取最近 7 天（和日摘要的常用窗口对齐）。
    ds = sorted(ds, key=lambda d: str(d.get("day", "")))[-7:]

    # 已经被周摘要覆盖的日子，就不再逐日重复了
    covered = set()
    for w in wks:
        a, b = str(w.get("from", ""))[:10], str(w.get("to", ""))[:10]
        for d in ds:
            day = str(d.get("day", ""))[:10]
            if a <= day <= b:
                covered.add(day)
    daily = [d for d in ds if str(d.get("day", ""))[:10] not in covered]

    lines = ["## 前几天的大致经过（只剩个大概，细节记不清很正常）", ""]

    # 先周后日：越近的越靠后（模型对靠后的内容更重视）
    for w in sorted(wks, key=lambda x: str(x.get("from", "")))[-8:]:
        ls = [str(x) for x in (w.get("lines") or [])[:3]]
        if not ls:
            continue
        lines.append(f"- 那几天（{w.get('from', '')}~{w.get('to', '')}）：{ls[0]}")
        for x in ls[1:]:
            lines.append(f"  · {x}")

    for d in sorted(daily, key=lambda x: str(x.get("day", "")))[-7:]:
        # 一天一段：日期单独一行，要点缩进 —— 挤成一行读起来像台账
        ls = [str(x) for x in (d.get("lines") or [])[:5]]
        if not ls:
            continue
        lines.append(f"- （{d.get('day', '')}）{ls[0]}")
        for x in ls[1:]:
            lines.append(f"  · {x}")
    lines += [
        "",
        "⚠️ 这是**大概**，不是原文。",
        "- 只在话题碰上时提一句，别背台账。",
        "- 细节记不清是正常的 —— 可以直说「记不太清了」，",
        "  不要为了显得记得而编具体的话。",
    ]
    return "\n".join(lines)


def _gap_phrase(hours: float) -> str:
    """把「隔了多久」说人话。空串 = 不必提。

    ## 为什么按档说而不是给精确数字

    她要的是**分寸**，不是数据。隔着 6.2 小时和「小半天」是一回事，
    但前者会让她的句子变得像日志。
    """
    if hours < 0.5:
        return ""                     # 刚说过话，提了很怪
    if hours < 3:
        return "隔了一会儿"
    if hours < 12:
        return "隔了小半天"
    if hours < 36:
        return "隔了一天多"
    # ⚠️ 数字用中文 —— 她说话不会说「隔了2天」。
    # 这是她台词里的词，不是日志里的字段。
    if hours < 24 * 7:
        return f"隔了{_cn_num(int(hours // 24))}天"
    return f"隔了{_cn_num(int(hours // (24 * 7)))}个星期"


def _cn_num(n: int) -> str:
    """1-10 转中文。再大就用阿拉伯数字（她不常说那种）。"""
    return {1: "一", 2: "两", 3: "三", 4: "四", 5: "五",
            6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}.get(n, str(n))



_PERSONA_DEFAULTS = {
    "name": "她",
    "call_name": "你",
    "pronoun": "她",
    "home": {"name": "", "rooms": ["卧房", "堂屋", "厨房", "院子"]},
    "identity": [],
    "habits": [],
    "tone": {"sentence": "短句，像随口说话。",
             "avoid": ["客服腔", "总结式回答"],
             "nickname": ""},
    "relationshipStages": {
        "stranger": "刚认识，客气但不热络。",
        "familiar": "熟悉了，说话随意些。",
        "close": "亲近，会主动关心。",
        "lover": "恋人的相处方式。",
    },
    "slowBurnRules": "",
    "emotionGuide": "",
    "characterCard": "",
    "world": {"season_source": "local", "weather_source": "local", "city": ""},
}

def load_persona() -> dict:
    """读人设。缺文件时给一句人话，而不是 traceback。"""
    try:
        raw = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
        # 缺字段补默认值：空模板也能跑，不会 KeyError（2026-11-11）
        out = dict(_PERSONA_DEFAULTS)
        out.update(raw)
        return out
    except FileNotFoundError:
        raise SystemExit(
            f"找不到人设文件：{PERSONA_FILE}\n"
            "  复制 personas/blank.json 为 personas/current.json，填好再启动。")
    except json.JSONDecodeError as e:
        raise SystemExit(f"人设文件不是合法 JSON：{PERSONA_FILE}（{e}）")


def load_stage_examples() -> dict[str, list[str]]:
    """读语感示例。框架里没有这个文件 —— 缺了就返回空。"""
    try:
        """读策展池。

        池子里的条目有两种写法：

            "一条普通的样例"                          —— 纯字符串
            {"text": "原文", "swap": {"大荒城": "家乡"}} —— 带替换说明

        ## 为什么要有 swap

        官方台词里有些地域专名（大荒城、炎国）在这座镇子语境下会出戏，
        但角色卡本来允许她提来历，整句丢掉又可惜。

        于是**原文照存**（这样校验器还能逐字核对），
        替换只作为「上屏时才应用」的说明。直接改写文本会让校验失效 ——
        实测踩过：改了 3 条，校验器立刻报「找不到原文」。
        """
        raw = json.loads(STAGE_FILE.read_text(encoding="utf-8"))
        out: dict[str, list[str]] = {}
        for k, v in raw.items():
            if k.startswith("_") or not isinstance(v, list) or not v:
                continue
            items = []
            for x in v:
                if isinstance(x, str):
                    items.append(x)
                    continue
                if isinstance(x, dict) and x.get("text"):
                    t = x["text"]
                    for a, b in (x.get("swap") or {}).items():
                        t = t.replace(a, b)
                    items.append(t)
            if items:
                out[k] = items
        return out


    except FileNotFoundError:
        return {}
    except Exception:
        return {}
def load_corpus() -> list[dict]:
    if not CORPUS_FILE.is_file():
        return []
    return json.loads(CORPUS_FILE.read_text(encoding="utf-8")).get("lines", [])


# ─────────────────────────────────────────────────────────────
class MindEngine:
    """把状态变成 prompt，把模型输出变成她的一句话。"""

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 api_base: str = "https://api.deepseek.com/v1",
                 temperature: float = 1.0, timeout: int = 60,
                 known_world: "KnownWorld | None" = None,
                 recent_chars_budget: int = 3000,
                 recall_limit: int = 6,
                 vision_model: str = ""):
        self.api_key = api_key
        self.model = model
        # 看图用的模型。
        #
        # 为什么要分开：`deepseek-chat` **不认图片**，要换成
        # `deepseek-v4-flash-vision-exp`。而那个模型有个坑 ——
        # **推理很吃 token**（实测一次占掉 591 个 reasoning token），
        # 所以用它的时候必须放宽 max_tokens，否则回复会被 reasoning
        # 吃光、变成空字符串，看着像调用失败。
        #
        # 留空表示不支持看图，那时候她会说「我看不了」而不是装作看见。
        self.vision_model = vision_model or ""
        self.api_base = api_base.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        # 历史预算。**按字数算，不按轮数** —— 理由见
        # `Character.recent_turns_by_chars` 的说明。
        #
        # 演进过程：
        #     14 轮     最初，太小，她接不住稍远的话题
        #     40 轮     改大了 —— 但实测 39 轮真实对话**总共只有 883 字**
        #     3000 字   换成字数预算。短对话能装几百轮，深入聊也能装 30+ 轮
        #
        # 为什么不是更大（比如 30,000 字）：成本会从 ¥4.3/千轮涨到 ¥53.8/千轮，
        # 因为「当前关系」那块变化内容排在 system 里太靠前，把它后面的
        # 全部（含历史）拖进了「缓存不命中」的重算区。
        # 要真正放大预算，得先把变化内容挪到 system 末尾 —— 那是另一件事。
        self.recent_chars_budget = recent_chars_budget
        # 从更早对话里召回的片段条数
        self.recall_limit = recall_limit

        self.persona = load_persona()
        self.stage_examples = load_stage_examples()
        self.corpus = load_corpus()

        # 三层记忆：个人背景 + 游戏世界
        # 延迟导入避免循环依赖；读不到就是空的，不影响对话
        if known_world is None:
            try:
                from mind_memory import KnownWorld
                known_world = KnownWorld.load()
            except Exception:
                known_world = None
        self.known_world = known_world

    def reload_world(self, use_game_save: bool = True) -> None:
        """重新读存档与档案。

        存档会在游戏进行中变化，所以每次开新对话前刷新一次，
        她才知道你昨天玩到哪了。

        Args:
            use_game_save: 是否读游戏存档。
                **只有主人的会话才传 True。** 别人不该知道你的农场与角色名。
        """
        try:
            self.known_world = KnownWorld.load(use_game_save=use_game_save)
        except Exception:
            pass

    # ── prompt 组装 ───────────────────────────────────────
    def _system_prompt(self) -> str:
        p = self.persona
        return "\n\n".join([
            p["characterCard"],
            p["slowBurnRules"],
            p["emotionGuide"],
            # 注意：这里**不用** mod 的那份 outputContract（那是要求返回 JSON 的），
            # 多轮对话要的是自然语言回复。
            """## 本次对话的形式
这是一次面对面的交谈，不是打招呼。请自然地回应对方说的话。

要求：
- 用中文口语，像真人说话，不要书面腔。
- **长度随情境而定**：一两句是常态；对方说了重要的事、或你想多说两句时，
  可以到三四句。但不要长篇大论，不要一次把话讲完。
- 可以反问，可以停顿（用「……」），可以答非所问地岔开——真人就是这样。
- 表情记号（`$h` `$s` `$u` `$l` `$a`）可以放在句中或句尾，但**不必每句都用**。
- 不要用 markdown、不要用列表、不要加任何格式符号。
- 直接说话，不要写「她：」这样的前缀。

## 关于记忆 —— 这一条极其重要
你只能依据系统给你的「对话记录」和「你记得的事」来回忆。

- **不要假装记得你没被告知的事。** 如果对方问起某件事，而对话记录和记忆里
  都没有，就老实说你记不清了、或者让他再讲一遍。
  比如：「……我一时想不起来了，你再说一遍？」
- **绝对不要为了显得亲切而编造**「我记得你说过……」——如果你其实不知道，
  那就是在骗人。这比忘记更伤人。
- 反过来，如果记忆里有，就要自然地用上，像真的记得一样，不要生硬背诵。
- **如果对方一开口就像在接你的话，而你不知道自己说过什么**（多半是刚才那句招呼
  没被记下来）：不要否认、不要反问「我什么时候说过」，也不要**顺着他的话编出
  一段新的回忆**。你已经知道的事可以说，不知道的就含糊过去，别替他补细节。
  比如他说「那我们就去吧」，你回「好。……你倒是答应得快」——
  **把话头接下去，比追究自己说没说过重要得多。**""",
        ])

    def _life_context(self, ch: Character) -> str:
        """她自己的日子 —— 「完整的人生」最关键的一段。"""
        now = datetime.now()
        lines = []
        # 时辰不再重复说 —— `_time_awareness` 那一块已经给全了
        # （日期、星期、时刻、隔了多久）。两处都说会互相打架
        # （一边「深夜」一边「凌晨」），还白占 prompt。
        #
        # 标题也挪到有内容时再加 —— 否则会出「标题 + 空行 + 说明」，
        # 那在 prompt 里是噪音（实测：新档没 roll 过 today_task 时就这样）。
        # 她此刻是不是已经躺下了 —— **先算这个**，因为下面要据此决定
        # 「手边/心里」说不说。
        #
        # ## 为什么需要（2026-10-17 用户报的 bug）
        #
        # 对话停在 21:50「（黑暗里没了声音，只余均匀的呼吸）……晚安」——
        # 她已经睡着了。而两栏还写着「靠着门框看云」。
        #
        # ## 为什么放在「手边/心里」之前（2026-10-18 改）
        #
        # 昨晚的写法是「照常写手边/心里，然后另起一段说她躺下了」——
        # **自相矛盾**：一边「你手边正在择菜」，一边「你已经睡下了」。
        # 睡着时该**跳过**那两行，只留躺下的状态。
        _asleep = False
        try:
            import mind_activity
            _asleep = mind_activity.current_activity(ch) == "asleep"
        except Exception:
            pass

        if not _asleep:
            if ch.today_task or ch.today_thought:
                lines.insert(0, "## 你此刻的状态（这是你自己的日子，可以自然地带出来）")
            if ch.today_task:
                lines.append(f"- 你手边正在做的事：{ch.today_task}。")
            if ch.today_thought:
                lines.append(f"- 你心里正想着：{ch.today_thought}。")
            lines.append("")
            lines.append("这些是你自己的生活，不必每句都提，但可以让对话有实感——"
                         "比如对方问你在忙什么，你就有具体的事可讲。")

        # 睡着时补的那一段（语气、长度、话题都要跟着变 —— 困着的人不讨论农事）
        if _asleep:
            try:
                import mind_activity
                sb = mind_activity.sleep_block(ch)
                if sb:
                    lines.append("")
                    lines.append(sb)
            except Exception:
                mind_diag.note("mind_brain 里的 mind_activity.sleep_block")

        # 他那边今天的实情 —— 让她手边的事有实处可落。
        #
        # ## 为什么需要（2026-10-17 用户报的 bug）
        #
        # 侧边栏那两行是**随机池抽的**，和游戏状态毫无关系：
        #     手边「靠着门框看云，没看明白就走了」
        #     心里「想着有件小事一直没办，也不急」
        # 而他游戏里已经是秋季第二天、农业 8 级、矿洞 77 层。
        #
        # `pick_task` / `pick_thought` 只吃精力和情绪，游戏状态一个字没进。
        # 这里把那部分补上 —— 具体的事（季节/技能/矿洞深度），
        # 让她在写「手边在干什么」时有当季的、真实的落点。
        # 「他那边的实情」（农业等级、矿洞深度）已删（2026-10-27）。
        #
        # 那是**他的游戏进度**，不是她的时令。而且它会把话题往回拽：
        # 她看见「农业 8 级」就想问地里的事，而他讲的是工作上的难处。
        # 时令现在只讲她自己那边 —— 见 mind_season。

        # 心情：单独一段，见 mind_mood.mood_block 的写法说明
        lines.append("")
        lines.append(mood_block(Mood.from_dict(ch.mood)))

        # 她自己的日子：小麻烦、口味、以及**她眼里的他**。
        # 最后一项是「亲密」本身 —— 记忆记的是他说的信息，
        # 这个记的是她相处下来看出来的人。
        lb = trouble_block(
            getattr(ch, "today_trouble", ""),
            getattr(ch, "today_tastes", []) or [],
            getattr(ch, "observations", []) or [],
        )
        if lb:
            lines.append("")
            lines.append(lb)

        # 他答应过的事。注意这是**沉默的多数时候** ——
        # promise_block 里写清了「绝不催」，别改成讨债。
        pb = promise_block(getattr(ch, "promises", []) or [])
        if pb:
            lines.append("")
            lines.append(pb)
        return "\n".join(lines)

    def _memory_context(self, ch: Character, user_text: str) -> str:
        """挑出与当前话题相关的长期记忆。"""
        if not ch.memories:
            return ""

        # 极简相关度：按关键词重合 + 权重排序。
        # 语料只有几十条记忆，不需要向量检索。
        def score(m) -> float:
            s = m.weight * 1.0
            for kw in re.findall(r"[\u4e00-\u9fff]{2,4}", user_text):
                if kw in m.text:
                    s += 3.0
            return s

        # 按**强度**挑（2026-11-02）：强度 = 写入时的权重 × 时间衰减 + 被用到次数。
        # 关键词只在"检索层"（recall）发挥作用，不再决定哪条重要。
        try:
            import mind_weight
            def _key(m):
                return dict(kind=getattr(m, "kind", "fact"),
                            weight=getattr(m, "weight", 1),
                            last_touch=getattr(m, "last_touch", "")
                            or getattr(m, "created", ""),
                            hits=getattr(m, "hits", 0),
                            key=getattr(m, "text", ""))
            picked = mind_weight.rank(ch.memories, _key)[:8]
        except Exception:
            picked = sorted(ch.memories, key=score, reverse=True)[:8]
        if not picked:
            return ""

        lines = ["## 你记得的事（以前的对话里对方提过的，需要时可以用上）"]
        for m in picked:
            when = m.created[:10]
            lines.append(f"- （{when}）{m.text}")
        lines.append("")
        lines.append("不要生硬地背诵这些记忆。只有当话题自然相关时才提，"
                     "而且要用你自己的话说，像真的记得一样。")
        return "\n".join(lines)

    def _style_examples(self, stage_key: str, n: int = 4) -> str:
        """按关系阶段取语感样例（与 mod 同一套策展池、同样硬过滤）。"""
        if n <= 0:
            return ""
        exact = list(self.stage_examples.get(stage_key, []))
        general = list(self.stage_examples.get("general", []))

        import random
        picks = exact[:]
        if len(picks) < n:
            for g in general:
                if g not in picks:
                    picks.append(g)
        if len(picks) > n:
            picks = random.sample(picks, n)

        if not picks:
            return ""

        body = "\n".join(f"- {x}" for x in picks)
        return ("## 语感参考（你以往说话的样子，只体会分寸与用词，不要照抄内容）\n"
                f"{body}\n\n"
                "请用这种感觉说话，但内容必须是**全新的、针对此刻对话的**。")

    def _corrections(self, ch: Character) -> str:
        """你让她注意的事。

        ## 为什么要和「记忆」分开

        记忆是**事实**（「我家的狗叫大黄」），可以自然地提起。
        而这是**行为要求**（「别老提我钓鱼」），必须当成规则遵守，
        而且**不能被她当成话题回述出来** —— 否则每次对话都变成
        「好的我记住了」，非常出戏。所以放在规则区并明确要求不要提起。
        """
        try:
            items = ch.memories_of("guidance")
        except Exception:
            return ""
        if not items:
            return ""

        lines = ["## 对方希望你注意的事（必须遵守，但**不要**主动提起这些要求本身）"]
        for m in items[-12:]:
            lines.append(f"- {m.text}")
        lines.append("")
        lines.append("以上是行为要求，不是话题。照做即可，别说「我记住了」这类话。")
        return "\n".join(lines)

    def _time_awareness(self) -> str:
        """现实时间感知 —— 深夜与清晨该有不同的语气。"""
        now = datetime.now()
        h = now.hour

        if h < 5:
            tip = "现在是深夜。对方这个点还没睡，可以有一句关切，但别说教。"
        elif h < 7:
            tip = "现在是清晨。适合说一句关于天光、露水、早饭的短话。"
        elif h < 11:
            tip = "现在是上午，一天里做事的时候。"
        elif h < 14:
            tip = "现在是中午前后，可以提及吃饭、歇脚。"
        elif h < 18:
            tip = "现在是下午，适合聊地里的活计或手边的事。"
        elif h < 22:
            tip = "现在是傍晚到夜里，语气可以放松些，适合说收工、晚饭、歇息。"
        else:
            tip = "现在是夜里，适合说些安静的、收束一天的话。"

        # 日期 / 星期 —— 原来只给时刻，她说「昨天」「周末」时没有坐标
        wd = WEEKDAY_CN[now.weekday()]
        date_line = f"{now.month} 月 {now.day} 日，{wd}"

        # 隔了多久 —— 原来只有开场白才传 gap，正常回复里她不知道
        # 他是接着刚才那句说的，还是隔了大半天才回来
        gap_line = ""
        try:
            last = getattr(self, "_gap_hint", "")
            if last:
                gap_line = f"\n距离你们上一句话：{last}。"
        except Exception:
            pass

        sc = _shichen(now.hour)
        # 格式：不套括号（原来出「03:34（寅时（五更））」，括号套括号）
        sc_line = f"，也就是{sc}" if sc else ""
        # ⚠️ 更次要**说明白** —— 她原来说「这会儿该是三更」，
        # 而 03:34 是五更。光把表塞进去不够，得说清「现在就是X更」。
        out = (f"## 现实时间\n今天是 {date_line}，现在是 {now.strftime('%H:%M')}"
               f"{sc_line}。说时辰就说这一个，别自己换算。"
               f"{tip}{gap_line}")

        # 节日（2026-10-24 加）：现实日期和游戏日期不是一回事，只能读存档。
        # mind_festival 内部带 90 秒缓存，不会每轮都读盘。
        try:
            import mind_season
            fb = mind_season.block()
            if fb:
                out = out + "\n\n" + fb
        except Exception:
            mind_diag.note("mind_brain 里的 mind_season")

        return out

    # 检索词缓存（进程内，最多 200 条）
    _expand_cache: dict = {}

    def _expand_terms(self, text: str) -> list[str]:
        """把一句话转成几个检索词，用于"同义说法也能捞到"。

        只在字面召回不足时调用。失败返回空列表 —— 不能让检索把回复拖垮。
        """
        key = (text or "").strip()[:80]
        if not key:
            return []
        if key in self._expand_cache:
            return self._expand_cache[key]

        prompt = (
            "把下面这句话转成 3~6 个**检索词**，用来在一份很长的聊天记录里"
            "找相关旧事。\n"
            "要求：每个词 2~6 个字；既要原话里的核心词，也要**同义/上位/相关**的说法；"
            "不要解释、不要编号。\n\n"
            "例子：\n"
            "  他说腰又酸了 → 腰、酸痛、腰肌劳损、身体、累\n"
            "  最近拍的东西怎么样 → 相机、拍照、接活、项目\n\n"
            f"他说：{key}\n\n"
            "只输出词，用顿号或逗号隔开。"
        )
        try:
            body = mind_llm.chat_body(
                {"model": self.model, "temperature": 0.2, "max_tokens": 80,
                 "messages": [{"role": "user", "content": prompt}]},
                self.api_key, self.api_base, min(self.timeout, 12))
            raw = (body["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            try:
                import mind_diag
                mind_diag.note("召回检索词生成失败", e)
            except Exception:
                pass
            self._expand_cache[key] = []
            return []

        import re as _re
        # 太泛的词不进检索：它们在聊天里密度太高，拿它搜等于随机
        _STOP = ("身体", "感觉", "时候", "东西", "事情", "一点", "有点",
                 "最近", "今天", "昨天", "怎么样", "什么", "我们", "他们")
        terms = [t.strip() for t in _re.split(r"[、，,。\s]+", raw) if t.strip()]
        terms = [t for t in terms if 2 <= len(t) <= 8 and t not in _STOP][:6]
        if len(self._expand_cache) > 200:
            self._expand_cache.clear()
        self._expand_cache[key] = terms
        return terms

    def _recall_context(self, ch: Character, user_text: str) -> str:
        """从**较早的**对话里找回与当前话题相关的片段。

        和「最近若干轮」互补：
            最近轮次 → 保证接得住话（连贯）
            召回片段 → 保证不忘记旧事（长期）

        排除范围要跟「最近轮次」对齐 —— 否则会把已经在上文里的东西
        又召回一遍，白费 token。所以这里也按**字数预算**算轮数。
        """
        try:
            # 先算出「最近轮次」实际取了几轮，召回就从那之前找起
            recent = ch.recent_turns_by_chars(self.recent_chars_budget)
            exclude = len(recent)
            hits = ch.recall_turns(user_text, limit=self.recall_limit,
                                   exclude_recent=exclude)

            # 字面召回不够 → 用模型扩几个检索词再翻一遍（2026-11-08）
            # 这一步只在"捞不到"时发生，正常对话不增加调用。
            if not hits:
                seen = {(getattr(t, "at", ""), getattr(t, "text", "")) for t in hits}
                for term in self._expand_terms(user_text):
                    for t in ch.recall_turns(term, limit=self.recall_limit,
                                             exclude_recent=exclude):
                        k = (getattr(t, "at", ""), getattr(t, "text", ""))
                        if k not in seen:
                            seen.add(k)
                            hits.append(t)
                hits.sort(key=lambda t: getattr(t, "at", "") or "")
                hits = hits[:self.recall_limit]
        except Exception:
            return ""
        if not hits:
            return ""

        lines = ["## 你们以前说过的（与当前话题相关，需要时可以用上）"]
        for t in hits:
            who = "对方" if t.role == "player" else "你"
            day = (t.at or "")[:10]
            lines.append(f"- （{day}）{who}：{t.text}")
        lines.append("")
        lines.append("这些是旧事。**只在话题相关时提起**，而且要像真的记得那样自然带过，"
                     "不要说「根据记录」这类话。")
        return "\n".join(lines)

    def _address_block(self) -> str:
        """这次该叫他什么。

        ## 为什么单独占一块（这里曾经是个真 bug）

        原先角色卡里只写了一句「用玩家的名字或『你』」，而代码里
        **根本没有注入名字的地方** —— 角色卡里的 `<player_name>` 占位符
        从来没被替换过。于是模型没有确定依据，
        就回头捡了个人档案里我留的示例值，一直管玩家叫「阿明」。

        名字这种东西不该靠模型自己从记忆里推断 —— 直接告给它。
        """
        name = ""
        if self.known_world is not None:
            try:
                name = self.known_world.call_name()
            except Exception:
                name = ""

        if not name or name == "你":
            return ("## 对他的称呼\n你还不知道他的名字，直接说「你」即可，"
                    "不要自己编一个名字。")

        return (f"## 对他的称呼\n**他就叫「{name}」。**\n"
                "需要称呼时用这个名字，或者直接用「你」。\n"
                "绝对不要用别的名字称呼他。")

    def build_messages(self, ch: Character, user_text: str,
                       images: list[str] | None = None,
                       channel: str = "web") -> list[dict]:
        """拼出完整的 messages。有图时最后一轮走多模态结构。

        Args:
            ch: 角色状态。
            user_text: 他说的话。
            images: data: URL 列表。**只附在最后一轮** ——
                历史里的图不重发（那些是过去的事，重发既费 token
                又会让模型以为他又发了一遍）。
        """
        # 用 effective_stage 而非 stage_of：
        # 与游戏存档同步后，关系阶段可能由游戏状态决定（比如游戏里已交往），
        # 直接用好感推算会与界面显示不一致，提示词也会写错阶段。
        stage_key, stage_name = ch.effective_stage()

        blocks = [self._system_prompt(), self._address_block()]

        # 关系
        rel = [
            "## 当前关系",
            self.persona["relationshipStages"].get(stage_key, ""),
            f"（关系阶段：{stage_name}）",
        ]
        if stage_key in ("hearts_0_1", "hearts_2_3", "hearts_4_5", "hearts_6_7"):
            rel.append("**注意：当前关系阶段不允许使用 `$l` 爱慕表情。**")
        blocks.append("\n".join(rel))

        # 行为要求（你纠正过的）放在规则区，靠前 —— 规则比背景更需要被遵守
        corrections = self._corrections(ch)
        if corrections:
            blocks.append(corrections)

        # 隔了多久 —— 在这一轮算好，供 `_time_awareness` 使用。
        #
        # 原来只有开场白（greet）和留话才传 gap，正常回复里她不知道
        # 他是接着刚才那句说的、还是隔了大半天才回来。那会让她的分寸错。
        try:
            _at = ""
            for _t in reversed(getattr(ch, "turns", None) or []):
                _at = getattr(_t, "at", "") or ""
                break
            if _at:
                _h = (datetime.now() - datetime.fromisoformat(_at)).total_seconds() / 3600.0
                self._gap_hint = _gap_phrase(_h)
            else:
                self._gap_hint = ""
        except Exception:
            self._gap_hint = ""

        blocks.append(self._time_awareness())
        blocks.append(self._life_context(ch))

        # 外头的天 —— 天气和运气。
        #
        # ## 这是漏掉的一整块（2026-10 补）
        #
        # `world_weather.py` 早就写好了（读存档的 `<weatherForTomorrow>`
        # 和 `<dailyLuck>`），但**只有 AstrBot 那个插件在用** ——
        # mind-life 这边从来没引用过。所以这个窗口里的她
        # 一直不知道今天下不下雨、运气好不好。
        #
        # 而那是她最容易自然带出来的东西：「今天日头好，晒得人懒」。
        #
        # 措辞用「这两天」而不是「今天」—— 存档里的 weatherForTomorrow
        # 是相对**存档那一刻**的，没法判断它指的是今天还是明天。
        # 详见 world_weather.py 的模块说明。
        # 天气已并入 mind_season（走现实天气，不再读存档）—— 见 _time_awareness。

        # 三层记忆之二、之三：个人背景 + 游戏世界
        if self.known_world is not None:
            try:
                world = self.known_world.as_prompt_block()
                if world:
                    blocks.append(world)
            except Exception:
                mind_diag.note("mind_brain 里的append、as_prompt_block")
                pass

        mem = self._memory_context(ch, user_text)
        if mem:
            blocks.append(mem)

        # 较早对话里与当前话题相关的片段
        recall = self._recall_context(ch, user_text)
        if recall:
            blocks.append(recall)

        # 你们之间发生过的事 —— 关系史。
        #
        # 位置：在「记忆」「召回」之后、「语感参考」之前。
        # 它比零散记忆重（那是关系本身），但不该压过人格（那在最前面）。
        #
        # 为什么要单独一块，而不是靠召回翻：
        # 实测踩过 —— 聊到「昨天的月光水母」时，召回因为那句话里没有
        # 「月光水母」四个字恰好连着，翻不出来。于是她答「我没去看，你去了？」。
        # 而那天她其实在场。**事件不该等关键词。**
        if getattr(ch, "events", None):
            try:
                eb = event_block([e.to_dict() if hasattr(e, "to_dict") else e
                                  for e in ch.events])
                if eb:
                    blocks.append(eb)
            except Exception:
                mind_diag.note("mind_brain 里的append、event_block、hasattr")
                pass

        # 她自己的日子 + 手上没做完的活（2026-10-20 加）
        try:
            hb = her_event_block(ch)
            if hb:
                blocks.append(hb)
        except Exception:
            mind_diag.note("mind_brain 里的 her_event_block")
        try:
            import mind_project as _SP
            pb = _SP.block(getattr(ch, "projects", None))
            if pb:
                blocks.append(pb)
        except Exception:
            mind_diag.note("mind_brain 里的 project_block")

        try:
            db = digest_block(ch)
            if db:
                blocks.append(db)
        except Exception:
            mind_diag.note("mind_brain 里的 digest_block")

        style = self._style_examples(stage_key)
        if style:
            blocks.append(style)

        # 现在他在哪儿（2026-11-09）
        #
        # 两条通道的区别不只是"手机/电脑"，而是**他在不在家**：
        #   网页  他在家里，坐在电脑前 —— 你们在同一间屋子
        #   QQ    他在外面，用手机 —— 你碰不到他
        # 只描述**这一轮**，不描述历史（别让她拿通道当话题）。
        if channel == "qq":
            blocks.append(
                "## 他不在家，在手机上\n"
                "他现在在外面，用手机跟你说话。你人在家里。\n\n"
                "**这一条很要紧：你伸手碰不到他。**\n"
                "不能写递东西、凑过去、拉他的手、把碗塞给他 —— 他不在你跟前。\n"
                "你的关心只能**落在话上**：问他到哪儿了、吃没吃、冷不冷、"
                "让他路上看车、说家里的事等他回来。\n\n"
                "句子可以短些，但不要冷。短不等于敷衍 ——\n"
                "「嗯，知道了」是敷衍；「知道了，你路上小心」是有人的热气。\n"
                "一次说两三句就够，别发一大段。")
        else:
            blocks.append(
                "## 他在家，在电脑前\n"
                "他在家里，坐在电脑前跟你说话 —— 你们在同一个屋檐下。\n"
                "可以一次说完整些；可以并肩、可以递东西；他发图你是**看得见**的。")

        # 加图的时候要提示她：这是他给你看的东西，不是凭空长出来的。
        if images:
            blocks.append(
                "## 他给你看了图\n"
                "下面那一轮他附了图。**你确实看得见** —— 直接说你看到的，"
                "不要问「这是什么图」。\n"
                "但也不必逐样描述：说你在意的那一处，或者由此想到的事。")

        messages = [{"role": "system", "content": "\n\n".join(blocks)}]

        # 很旧的对话压成的一段摘要（mind_rollup.py）。
        # 聊得久了，3000 字的窗口装不下前面 —— 这段让更早的事仍然"在"。
        if getattr(ch, "context_rollup", ""):
            blocks.append(
                "## 更早之前（摘述，不是原话）\n"
                f"{ch.context_rollup}\n"
                "（这段是你们更早的经过，语气按现在来，不要照搬里面的话）")

        # 真实的多轮历史 —— 这是与 mod 最大的差别。
        # 按**字数**取而不是按轮数（见 recent_turns_by_chars 的说明）。
        for t in ch.recent_turns_by_chars(
                self.recent_chars_budget,
                start=int(getattr(ch, "rollup_count", 0) or 0)):
            messages.append({
                "role": "user" if t.role == "player" else "assistant",
                "content": t.text,
            })

        # ⚠️ 表情记号的提醒**插在历史之后** —— 理由见 REMIND_EMOTION 的注释。
        #
        # 不加这一条的话，她会被自己那 83 条不带记号的旧回复带偏
        # （实测服务里 0/5 带记号）。放在这里离生成最近，而内容极短不会被稀释。
        messages.append({"role": "system", "content": REMIND_EMOTION})

        # QQ 通道再补一句极短的提醒（2026-11-09）。
        #
        # 前面那段「他不在家」写得很长，可实测她照样写「（抬头）」
        # —— 那是把他在场的旧习惯带出来了。长指令会被稀释，
        # 所以这里在**离生成最近的位置**再说一次，只两句。
        if channel == "qq":
            messages.append({"role": "system", "content": (
                "（他在外面，用手机跟你说话，人不在你跟前。"
                "旁白只写**你自己**在做什么 —— 不要写「抬头」「递给他」"
                "「凑过去」这类把他放在身边的动作。想他，就用话问。）")})

        # 最后一轮：有图就走多模态结构，没有就还是纯文本。
        # 不无图也套数组 —— 不是所有接口都接受，没必要冒这个险。
        messages.append({
            "role": "user",
            "content": build_image_content(user_text, images or []),
        })
        return messages

    # ── 她主动开口 ────────────────────────────────────────
    def open_topic(self, ch: Character) -> tuple[bool, str]:
        """让她主动说一句。返回 (是否成功, 文本)。

        ## 与普通回复的区别

        普通回复是**回应**你的话；这里是**她先开口**。
        所以 prompt 要换一种要求：不是「回答对方」，而是
        「你手边正有一件事，想跟他说」。

        这也是「完整的人生」的一部分 —— 她不该只在被搭话时才存在。
        """
        stage_key, stage_name = ch.effective_stage()

        blocks = [self._system_prompt(), self._address_block()]
        blocks.append(f"## 当前关系\n"
                      f"{self.persona['relationshipStages'].get(stage_key, '')}\n"
                      f"（关系阶段：{stage_name}）")

        corrections = self._corrections(ch)
        if corrections:
            blocks.append(corrections)

        blocks.append(self._time_awareness())
        blocks.append(self._life_context(ch))

        # 最近的对话，让她能接着上文说，而不是凭空起头
        recent = ch.recent_turns(6)
        if recent:
            lines = ["## 你们最近说的话（可以承接，但不必重复）"]
            for t in recent:
                who = "对方" if t.role == "player" else "你"
                lines.append(f"- {who}：{t.text}")
            blocks.append("\n".join(lines))

        # 记忆：让她的主动开口有依据
        mem = self._memory_context(ch, "")
        if mem:
            blocks.append(mem)

        blocks.append("""## 现在要你做的
你先开口。不要问「在吗」这种空话。

**最自然的一种：顺着你们刚才说的往下接一句** ——
补一句想到的、接着问一句、或者把上一句没说完的意思说完。
（比如他刚才说累，你可以说「那今晚别熬了，早点睡」；
 刚才说到下雨，你可以说「雨小点了」）

也可以是别的，但**不必**每次都汇报你在忙什么：
- 忽然想起的一件旧事
- 一句天气、时令、地里的话
- 手边正做的事（**偶尔**提一次就够，别当开场白用）

要求：
- 一到两句，简短。
- 像随口说的，不是打招呼。
- 如果是深夜或清晨，要符合那个时辰的语气。
- 不要用「你好」「在吗」「今天过得怎么样」这类空泛的开场。
- 不要把上一句已经说过的话再说一遍。
- **如果最近几条都是你自己在说（他没接话），别再顺着同一件事往下滚** ——
  换个方向：说点别的，或者干脆问他一句。
- 不要每次都提「家里那几个小的」或人生道理 —— 那太刻意。""")

        messages = [
            {"role": "system", "content": "\n\n".join(blocks)},
            {"role": "user", "content": "（你先开口）"},
        ]

        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }

        try:
            body = mind_llm.chat_body(payload, self.api_key, self.api_base, self.timeout)
            text = body["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return False, f"生成失败：{e}"

        text = self._postprocess(text)
        # 兜一次英文 NPC 名。输入侧已经转过，但**记忆里可能存着**带英文名的
        # 旧对话（那是转之前留下的），模型会照抄出来。
        text = to_zh(text)
        if not text:
            return False, "模型返回了空内容"
        return True, text

    def greet(self, ch: Character, gap: str = "",
              game_facts: list[str] | None = None,
              focus: list[str] | None = None) -> tuple[bool, str]:
        """生成开场白。返回 (是否成功, 文本)。

        ## 和 open_topic 的区别

        `open_topic` 是「聊到一半她先起个话头」，所以带最近 6 轮对话。
        这里是**窗口刚打开**，那句最近的话就是上一轮的结尾 ——
        带着它开场会变成「接着说」，而不是「她开口了」。

        所以这里**不带 recent_turns**，改成带两样东西：

            gap          隔了多久（几小时 / 几天 / 头一回）
            game_facts   游戏里的季节、天气、第几天

        这两样决定了开场该落在什么分寸上 —— 隔三天和隔三小时，
        开口是完全不同的。

        Args:
            ch: 角色状态。
            gap: 「距离上次说话」的人类可读描述，空字符串表示不提。
            game_facts: 游戏上下文的事实列表（季节天气等）。
            focus: 这次要提到的状态短句（见 mind_life.pick_focus）。
                传 None 表示退回全量 `_life_context`。

                **为什么要 focus**：`_life_context` 会把当天的全部状态
                塞进去，而那些都是一天一滚的 —— 于是反复生成开场白就会
                一直说同一件事。实测连着重启两次都是「晾衣绳的桩子」。
                `pick_focus` 每次只挑一两样，内容仍连贯但开场不重复。

        Returns:
            (是否成功, 台词或错误信息)
        """
        blocks = [self._system_prompt(), self._address_block()]

        blocks.append(self._time_awareness())

        # 心情总是给 —— 它决定语气，不是「素材」，不该被抽掉
        blocks.append(mood_block(Mood.from_dict(ch.mood)))

        if focus is None:
            # 退回全量（给别的调用方留的路）
            blocks.append(self._life_context(ch))
        elif focus:
            lines = ["## 你此刻（这次只提这些）"]
            lines += [f"- {f}" for f in focus]
            lines.append("")
            lines.append("**这次开场就落在这上面。** 不要把它们全部说完 —— "
                         "挑其中一样说，像顺口提一句。")
            blocks.append("\n".join(lines))

        if gap:
            blocks.append("## 隔了多久\n" + gap)

        if game_facts:
            lines = ["## 他那边（游戏里的日子，可以自然带出来）"]
            lines += [f"- {f}" for f in game_facts]
            lines.append("")
            lines.append("这些不必每句都提，也不要把它们当报菜名 —— "
                         "只是让你知道他那边的时令和天气，好有实处可落。")
            blocks.append("\n".join(lines))

        blocks.append("""## 现在要你做的
他刚把玉璞拿起来 —— 你开口说第一句。

**这不是「打招呼」，是你手边正有一件事，顺口说给他听。**

可以是：
- 此刻你手边在做的事（灶上、地头、手上这半截活）
- 一句天气、时令、地里的话
- 忽然想起的、和他有关的一件小事

要求：
- **一到两句，短。** 隔着玉，可以分两三条说。
- 像随口说的，不是开场白。
- 隔得久就提一句久；隔得短就当没隔过，直接说事。
- 不要「你好」「在吗」「今天过得怎么样」这类空泛的。
- **不要每次都是一样的句式** —— 你是个活人，不是模板。""")

        return self._one_shot(blocks, "（你先开口）")


    # ── 调用 ──────────────────────────────────────────────
    def _one_shot(self, blocks: list[str], user_msg: str,
                  temperature: float | None = None) -> tuple[bool, str]:
        """单轮生成：组装 -> 请求 -> 后处理。返回 (是否成功, 文本)。

        ## 为什么抽出来

        `greet`（开场白）和留话（`mind_note.write_note`）的**请求那段**
        是一模一样的（payload / 错误处理 / _postprocess / to_zh）。
        留话那个在 `mind_note.py` 里，不走这里；但两边形状一致，
        以后再加「她主动说一句」的种类只差一个提示词块。

        （原先这里写的是 `left_word` —— 那套是我造的重复实现，
        2026-10-18 已删，留话统一走 `mind_note`。）
        """
        messages = [
            {"role": "system", "content": "\n\n".join(blocks)},
            {"role": "user", "content": user_msg},
        ]
        payload = {
            "model": self.model,
            "temperature": self.temperature if temperature is None else temperature,
            "messages": messages,
        }
        try:
            body = mind_llm.chat_body(payload, self.api_key, self.api_base, self.timeout)
            text = body["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return False, f"生成失败：{e}"

        text = self._postprocess(text)
        text = to_zh(text)
        if not text:
            return False, "模型返回了空内容"
        return True, text

    def reply(self, ch: Character, user_text: str,
              images: list[str] | None = None,
              channel: str = "web") -> tuple[bool, str]:
        """生成一句回复。返回 (是否成功, 文本或错误信息)。

        Args:
            ch: 角色状态。
            user_text: 他说的话。
            images: 图片的 data: URL 列表。有图时**自动切到视觉模型** ——
                因为对话模型不认图片，硬发会 400。
        """
        imgs = [u for u in (images or []) if u]
        model = self.model
        if imgs:
            if not self.vision_model:
                return False, "（这个版本还没配好看图的模型）"
            model = self.vision_model

        _msgs = self.build_messages(ch, user_text, images=imgs,
                                     channel=channel)

        # 手机语境（2026-10-28）
        #
        # 只加在 QQ 这条路上。要求是「短一点，但别冷」——
        # 她本来就是个话不多但有心的人，通道只是把这一点放大。


        payload: dict = {
            "model": model,
            "temperature": self.temperature,
            "messages": _msgs,
        }
        if imgs:
            # 视觉模型会先做一段推理再回答，那段也吃 max_tokens。
            # 给小了会得到空 content（实测 120 全被 reasoning 吃光）。
            payload["max_tokens"] = 2000

        try:
            body = mind_llm.chat_body(payload, self.api_key, self.api_base, self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            return False, f"接口返回 HTTP {e.code}：{detail}"
        except urllib.error.URLError as e:
            return False, f"网络不通：{e.reason}"
        except TimeoutError:
            return False, f"响应超时（>{self.timeout}s）"

        # ── 先看 finish_reason（这是**可靠信号**，比猜文本准得多）──
        #
        # OpenAI 兼容接口在被内容策略挡下时，通常会把 finish_reason 置成
        # "content_filter"（有的实现叫 "safety"）。这时 content 往往是空的
        # 或者一段套话 —— 不管哪种，都**不能当她的台词存进历史**。
        try:
            _fin = str((body.get("choices") or [{}])[0].get("finish_reason") or "")
        except (AttributeError, IndexError, TypeError):
            _fin = ""
        if _fin in ("content_filter", "safety"):
            return False, f"{REFUSED_MARK}finish_reason={_fin}"

        try:
            text = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            return False, f"响应结构异常：{json.dumps(body, ensure_ascii=False)[:300]}"

        text = self._postprocess(text)
        if not text:
            return False, "模型返回了空内容"
        return True, text

    # ── 后处理与校验 ──────────────────────────────────────
    @staticmethod
    def _postprocess(text: str) -> str:
        # 去掉可能的角色名前缀
        text = re.sub(r"^\s*(她|Shu)\s*[:：]\s*", "", text)
        # 去掉 markdown 包裹
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
        return text

    @staticmethod
    def extract_emotion(text: str) -> str:
        """从文本里取出第一个表情记号，用于选立绘。"""
        for m in re.finditer(r"\$(h|s|u|l|a)", text):
            return "$" + m.group(1)
        return ""

    @staticmethod
    def clean_for_display(text: str) -> str:
        """去掉表情记号、规整换行后显示。

        表情记号的视觉作用由立绘承担，不必留在文字里。
        换行统一成单个 \\n —— 模型常会多打空行，显示出来很松散。
        """
        # 先处理翻页记号，再处理普通表情记号
        text = text.replace("#$b#", "\n").replace("#$e#", "\n")
        text = re.sub(r"\$[a-z0-9]+", "", text)
        # 残留的孤立记号字符（模型偶尔会漏半个记号，如单个 #）
        text = re.sub(r"(?m)^\s*#\s*$", "", text)
        text = text.replace("#", "")
        # 行尾句号去掉（2026-10-28）
        #
        # 卡里写了「句号少用」，但历史里她 2000 句都带句号 ——
        # few-shot 压过规则，模型照抄。所以放到输出后处理，确定性生效。
        # 只去行尾的「。」，问号叹号保留：那些是语气，不是书写习惯。
        text = re.sub(r"。\s*$", "", text, flags=re.M)

        # 连续空行压成一个换行
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()
