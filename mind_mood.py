#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她的心情 —— 她也有状态好和状态差的时候。

## 为什么需要这个

没有这一层的时候，她**无论何时何地都是同等热情**：凌晨三点和中午一样有精神，
你三天没来和天天来一样温和。这在人身上是不成立的，所以她会显得「不像活的」。

心情补的是这一块：**她有自己的状态，而且这个状态不由你决定。**

## 两个维度，不是一个数字

单独一个「心情值」不够用，因为**「累」和「不高兴」是两回事**：

    情绪 valence   0-100   坏 <--> 好
    精力 energy    0-100   想安静 <--> 想说话

组合出来的效果完全不同：

              情绪好              情绪差
    精力高    话多、爱逗你        话少、闷闷的
    精力低    温和、话短、家常    敷衍、走神、想一个人待着

比如「干了半天活，累但踏实」= 情绪 70 / 精力 25 —— 这是她最典型的状态。

## 刷新规则：一小时

    距上次 < 1 小时   -> 不改（同一次相处里她有连贯性）
    距上次 >= 1 小时  -> 重新掷一次心情，并抽一个「今天发生了什么」

**一小时之内不变**是要紧的：否则同一次聊天里她会精神分裂。

## 心情只影响语气，不碰关系

关系阶段始终由这座镇子存档决定（见 mind_character.STAGES）。
心情再差也不会掉好感 —— 它只是让她今天说话的样子不一样。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVENTS_FILE = HERE / "tools" / "corpus" / "mood_events.json"

# 距上次刷新超过这个时长才重掷心情
REFRESH_AFTER = timedelta(hours=1)

# 心情值的边界 —— 不能一路飙到满，也不能一路跌到底。
#
# ## 2026-10-16 抬高了下限
#
#     情绪  12 -> 16
#     精力   8 -> 12
#
# 理由：用户说「尽量减少心情对态度的影响」。
# 下限越低，触底那种极端状态（「很不好受」「很累」）出现的概率越大 ——
# 而那一档无论怎么写措辞，都容易演成冷淡。抬高下限 = 减少极端。
VALENCE_MIN, VALENCE_MAX = 16, 96
ENERGY_MIN, ENERGY_MAX = 12, 92

# 每轮对话对心情的影响幅度
SHIFT_WARM = (3, 6)      # 他说了贴心的话
SHIFT_COLD = (-6, -3)    # 他敷衍、冷淡
SHIFT_CHAT = (-4, -2)    # 聊久了会累（只掉精力）

# 精力的**恢复**。
#
# ## 为什么必须有（2026-10 加）
#
# 原来精力每轮无条件 -2~-4，而 ENERGY_MIN = 8、上限 100。
# 于是：**连聊 25-45 轮，精力必然触底** —— 没有恢复机制。
#
# 实测后果：用户聊了 51 轮之后，精力到了 8（触底），
# 于是 prompt 里出现「很累，想早点歇着；他若留你多聊，你会说改日」，
# 她就真的开始赶人。用户的原话是「为什么比此前更冷漠了」。
#
# 恢复的规则：**不是每一轮都累**。
# 短句来回（「嗯」「哦」「好」）几乎不耗神，只有长对话才累。
# 所以按他这句话的长度决定消耗。
# 精力：**不再长期往下漂**（2026-10-16 调）
#
# ## 原来错在哪
#
# 基线 47（三角分布均值），而「你说的超过 6 个字」就扣 2-4 点。
# 正常聊天基本都是长句 —— 于是她的精力**长期在下行**，
# 稳定停在「有些累」那一档。实测她自己聊到精力 26。
#
# 再叠加原来那句「动作和话都慢半拍」（长度指示），
# 就成了「她长期话少」。
#
# ## 现在
#
#     基线抬到 58 —— 多数日子精力是够的
#     消耗降到 -3~-1 —— 聊天本来就该是歇着的事，不是干活
#     短句回一点   —— 一口一口地聊，她是能缓过来的
#
# 用户的要求是「让她好心情变多一些」，精力和情绪两边一起抬。
ENERGY_COST_SHORT = 0        # 他说话很短 -> 不掉精力

# 触发「冷淡」判定的最短长度。
#
# 原来是 `len(t) <= 2` —— 于是「好」「嗯」「嘿嘿」全被当成冷淡。
# **但「嘿嘿」是开心的意思。** 而且角色卡还要求她自己说话短，
# 两套规则在打架。
#
# 现在只有**明确的敷衍词**才算冷，短本身不算。
COLD_MIN_LEN = 0         # 不再按长度判冷

# 心情重掷时怎么算新值 —— **基线 + 旧值 + 新随机值** 的三段加权。
#
#     只有「旧 + 新」两段是不够的：收敛到的是新值的均值，不是基线。
#     实测极端值漂不回来（情绪 95 连掷 200 次还是 76）。
#     加上基线那一项，才有回归力 —— 她不会一路飘高或一路沉底。
#
# 三个权重加起来是 1：
#     基线 0.34   比 1/3 略多一点点「她本来的性子」
#     旧值 0.28   比 1/3 略少 —— 免得上一段对话无限延长
#     新值 0.38   最大的一项 —— 睡一觉起来确实会变
DRIFT_BASE = 0.34
DRIFT_PREV = 0.28
DRIFT_NEW = 0.38

# 她的基线 —— **是三角分布的均值，不是 mode**。
#
# 踩过的：一开始按 mode 写，结果均值回归的落点对不上。
#
#     情绪  triangular(16, 96, 76) 的均值 ≈ 62.7
#     精力  triangular(12, 92, 62) 的均值 ≈ 55.3
#
# 均值回归要往**均值**拉，不是往峰值拉 —— 否则会系统性偏高。
#
# ## 2026-10-16 上调（用户：「让她好心情变多一些」）
#
#     情绪  59 -> 63   （range 也从 12-96 抬到 16-96）
#     精力  47 -> 55   （range 从 8-94 抬到 12-92）
#
# 理由：原来「像往常一样」和往下那三档经常中，往上那档很少中，
# 净效果是**她长期处在收着的状态**。抬上来之后
# 「心情很好」和「精力足」成了常见档，而不是偶发档。
BASELINE_VALENCE = 63
BASELINE_ENERGY = 55
# 生成新随机值时的峰值（决定分布形状，偏平静的一侧）
MODE_VALENCE = 76
MODE_ENERGY = 62


@dataclass
class Mood:
    """她此刻的状态。

    `event` 是「今天发生了什么」—— 从池子里抽的一句小事。
    它只写她自己的事，绝不涉及玩家、天气、季节（见 mood_events.json 的说明），
    因为这个事件是随机抽的，不知道当前关系阶段，也不能和存档里的天气打架。
    """

    valence: int = 62          # 情绪 0-100
    energy: int = 55           # 精力 0-100
    event: str = ""            # 今天发生了什么（虚构的小事）
    updated_at: str = ""       # 上次刷新时间（ISO）

    # ── 派生属性 ──────────────────────────────────────
    @property
    def tier(self) -> str:
        """精力档 —— 决定抽哪一组事件。"""
        # 阈值和其他地方统一（2026-10-19）：
        # 原来这里用 34/67，而 tone 用 22/34/70 —— 于是 67-69 分时
        # 「高精力」配上「精力如常」，她会一边喊累一边被安排重活。
        if self.energy < 40:
            return "低精力"
        if self.energy < 70:
            return "中精力"
        return "高精力"

    @property
    def valence_word(self) -> str:
        if self.valence >= 78:
            return "很好"
        if self.valence >= 58:
            return "不错"
        if self.valence >= 38:
            return "平常"
        if self.valence >= 22:
            return "有些低"
        return "不太好"

    @property
    def energy_word(self) -> str:
        if self.energy >= 70:
            return "有精神"
        if self.energy >= 40:
            return "还好"
        if self.energy >= 22:
            return "有点累"
        return "很累"

    @property
    def label(self) -> str:
        """给界面看的两字标签。"""
        # 原来 58 分以上全归「平和」—— 界面那个两字标签几乎不变，
        # 于是「她有起伏」在界面上看不见。现在顶部再加一档。
        v = self.valence
        low = self.energy < 40
        high = self.energy >= 70
        if v >= 88:
            return "兴致高" if high else "心里亮"
        if v >= 58:
            if high:
                return "兴致好"
            if low:
                return "懒懒的"
            return "平和"
        if v >= 38:
            # 语气那一档说的是「有些安静，但见他来了是高兴的」——
            # 标签不能写「闷」，那是更重的一档（<38 才是）。
            return "安静"
        return "提不起劲"

    @property
    def tone(self) -> str:
        """给她自己的语气指示 —— 这是真正进 prompt 的部分。

        ## 核心原则：心情改「气质」，不改「态度」

        用户 2026-10-16 的原话是「尽量减少心情对态度的影响」。
        这条是这一整段的设计准绳：

            **她对他在不在意 —— 那是恒定的，跟心情无关。**
            心情只决定这份在意**今天长什么样**。

                精力高 -> 想动手、想张罗、会主动递话
                精力低 -> 想歇着、想安静 —— 但**仍然愿意他在**

        所以下面每一档都只描述**气质与做法**，
        **绝不描述「话变短」「不想说话」「不想理人」**。

        ## 改过三版，两个坑

        **坑一：累被翻译成「赶人」。**

            原来「很累」那档写的是「他若留你多聊，你会说改日」——
            和角色卡的「不推拉、不设限、不逐客」直接矛盾。
            实测后果：用户连聊 51 轮、精力触底到 8，她就真的开始
            「不早了，回吧」。用户的原话是「为什么更冷漠了」。

        **坑二：指示几乎全是「收」。**

            20 种状态组合里 16 种带「变短 / 不想说话 / 慢」。
            加上精力基线偏低（47）而说话长了就掉精力 ——
            她长期停在「有些累，动作和话都慢半拍」那一档。
            再叠加角色卡原来那条 40 字上限，就成了「冷淡又简短」。

            **净效果是心情把她的热情一点点磨掉。**

        现在：情绪档只讲**心情的样子**，精力档只讲**今天的做法**，
        两档都不碰「说不说、说多少」。
        """
        v, e = self.valence, self.energy
        bits = []
        # 六档（2026-10-19 拆开顶部）。
        #
        # 原来 78-96 共用同一句 —— 19 分的差别她嘴里一个字都不差，
        # 于是「情绪在起伏」你根本看不出来。现在 >=88 单独一档。
        if v >= 88:
            bits.append("你今天心情格外好 —— 话比平时多，容易笑出声，"
                        "会想逗他一句，也愿意主动说点什么")
        elif v >= 78:
            bits.append("你今天心情很好，眉眼松快，容易笑，"
                        "会主动跟他说点有的没的")
        elif v >= 58:
            bits.append("你今天心绪平稳，像往常一样")
        elif v >= 38:
            bits.append("你今天心里有些安静，"
                        "但见他来了是高兴的，只是不主动逗他")
        elif v >= 22:
            bits.append("你今天心里闷着点事 —— "
                        "那件事跟他没关系，别让他以为是自己惹的")
        else:
            bits.append("你今天很不好受，不太想说自己怎么了；"
                        "但他真问起来，你可以漏一点给他，"
                        "全咽下去会让他以为自己做错了什么")

        if e >= 70:
            bits.append("精力足，坐不住，想动手做点什么、想张罗")
        elif e >= 40:
            bits.append("精力如常")
        elif e >= 22:
            bits.append("有些累，动作慢些，话会说得更省、更家常")
        else:
            bits.append("很累，懒得动 —— 那就别装精神，"
                        "可以直说累、可以让他陪你坐会儿")

        # ── 底线（每一档都带）────────────────────────────
        #
        # ⚠️ 这里**只讲原则，不写例句**。
        #
        # 上一版为了禁止她赶人，写成了「不说『改日』『你先回吧』这类话」——
        # 那等于**把那两句话放进了 prompt**，正是我自己立的规矩说的
        # 「模型会照着念」。**禁止某句话的方式不该是引用它。**
        #
        # 而且这一档无论怎么措辞，都最容易被读成「冷淡」。
        # 所以每一档都补一句底线，说清「心情改的是样子，不是在意」。
        bits.append("心情只改你今天的样子，不改你对他的在意 —— "
                    "任何状态下都不冷落他、不让他猜、不把人往外推")

        return "；".join(bits) + "。"

    def to_dict(self) -> dict:
        return {"valence": self.valence, "energy": self.energy,
                "event": self.event, "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Mood":
        if not isinstance(d, dict):
            return cls()
        def clamp(v, lo, hi, dflt):
            try:
                return max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                return dflt
        return cls(
            valence=clamp(d.get("valence"), VALENCE_MIN, VALENCE_MAX, 62),
            energy=clamp(d.get("energy"), ENERGY_MIN, ENERGY_MAX, 55),
            event=str(d.get("event", "")),
            updated_at=str(d.get("updated_at", "")),
        )


# ── 事件池 ───────────────────────────────────────────
_events_cache: dict | None = None


def load_events() -> dict:
    global _events_cache
    if _events_cache is None:
        if EVENTS_FILE.is_file():
            raw = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
            _events_cache = {k: v for k, v in raw.items()
                             if not k.startswith("_") and isinstance(v, list)}
        else:
            _events_cache = {}
    return _events_cache


# ── 刷新 ─────────────────────────────────────────────
def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# ── 精力：昼夜曲线 + 会话疲劳（2026-10-19 重做）────────────
#
# ## 为什么推翻旧做法
#
# 旧的是「按他这句话的字数」掉血：短句回血、长句掉血。用户的原话是
# 「精力受对话长度影响，太不符合现实」。确实不现实，而且三处都错：
#     ① 方向反了 —— 累的是说话的人，可这里算的是听的人
#     ② 代理错了 —— 真正累人的是**聊了多久**，不是单句多长
#     ③ 没有恢复 —— 一天里她不会因为你不说话而歇过来
#
# ## 现在的模型
#
#     精力 = 时辰曲线 + 当天随机 - 会话疲劳
#
# **每一刻按当前情况重新算，不累加。** 所以它不会贴栏杆：
# 到点自然低，白天自然高，聊久了自然掉，断开自然回。
#
# ## 疲劳为什么「拓宽」
#
# 用户要求「让她不容易累」。所以：每轮 -0.8、上限 -20 ——
# 要**连聊 25 轮**才见底（旧模型十几轮就到底）。

# 时辰 -> 精力基准。关键点之间线性插值。
BASE_CURVE = [
    (0, 28), (3, 15), (5, 22), (6, 40), (7, 58), (9, 70),
    (12, 78), (16, 85), (18, 72), (21, 45), (22, 32), (24, 28),
]

FATIGUE_PER_TURN = 0.8      # 每轮掉多少
FATIGUE_MAX = 20.0          # 疲劳上限（越小越不容易累）
SESSION_GAP_HOURS = 2.0     # 隔这么久就算「新的一段」，疲劳清零
ASLEEP_ENERGY = 80          # 睡着时精力（在睡就是在恢复）


def base_energy_at(dt: datetime) -> float:
    """按小时在 BASE_CURVE 上线性插值。"""
    h = dt.hour + dt.minute / 60.0
    for i in range(len(BASE_CURVE) - 1):
        h0, v0 = BASE_CURVE[i]
        h1, v1 = BASE_CURVE[i + 1]
        if h0 <= h <= h1:
            if h1 == h0:
                return v0
            return v0 + (v1 - v0) * (h - h0) / (h1 - h0)
    return BASE_CURVE[0][1]


def _daily_jitter(dt: datetime) -> float:
    """同一天同一时刻固定的 ±5 抖动 —— 别让她每天都一模一样。

    用日期做种子（**不用 random()**），这样刷新页面不会跳。
    """
    import random as _r
    rng = _r.Random(dt.strftime("%Y-%m-%d") + "|energy")
    return rng.uniform(-5.0, 5.0)


def fatigue_of(session_turns: int) -> float:
    """连聊了多少轮 -> 该扣多少精力。"""
    return min(FATIGUE_MAX, max(0, session_turns) * FATIGUE_PER_TURN)


def derive_energy(dt: datetime | None = None, session_turns: int = 0,
                  asleep: bool = False) -> int:
    """按「现在几点 + 连聊几轮 + 是否睡着」算出精力。"""
    dt = dt or datetime.now()
    if asleep:
        return max(ENERGY_MIN, min(ENERGY_MAX, ASLEEP_ENERGY))
    e = base_energy_at(dt) + _daily_jitter(dt) - fatigue_of(session_turns)
    return max(ENERGY_MIN, min(ENERGY_MAX, int(round(e))))


# ── 每日底色（2026-10-19 加）───────────────────────────
#
# 人不是每天都一样：有日子本来就在 52 附近（有点闷），有日子在 80。
# 这里按**日期**算出一个当天固定的底色 —— 不用存进存档，
# 从日期种子推出来，重启和换机都一致。
#
# 为什么不是存字段：存了就要迁移、要维护「跨天时重抽」。
# 从日期推，永远不可能和事实不一致。

# 范围 38-90（实测 60 天跨度 42-88，标准差 10.3）。
# 原来 45-82 太窄 —— 抽 10 天全落在 53-68，跨天看不出差别。
DAY_BASE_LOW = 38
DAY_BASE_HIGH = 90
DAY_BASE_MODE = 62

# 每轮对话之后，往当天底色回归这么多。
#
# 0.10 = 每轮拉回一成的差距。96 分时每轮 -3 左右，
# 抵得住一句 +5 的好话 —— 于是它**悬在底色上方而不是钉在 96**。
REVERT_TO_BASE = 0.10


def day_base_for(dt: datetime | None = None) -> int:
    """今天的心情底色。同一天永远相同。"""
    import random as _r
    dt = dt or datetime.now()
    rng = _r.Random(dt.strftime("%Y-%m-%d") + "|daybase")
    return int(round(rng.triangular(DAY_BASE_LOW, DAY_BASE_HIGH, DAY_BASE_MODE)))


def needs_refresh(mood: Mood, now: datetime | None = None) -> bool:
    """距上次刷新是否已超过一小时。"""
    now = now or datetime.now()
    last = _parse(mood.updated_at)
    if last is None:
        return True          # 从没刷过（首次运行）
    return now - last >= REFRESH_AFTER


def roll(now: datetime | None = None,
         prev: Mood | None = None) -> Mood:
    """掷一个新心情，并抽一条「今天发生了什么」。

    ## 为什么不用「纯随机」

    纯随机会出现「情绪 95 / 精力 90」这种亢奋状态 —— 那不是她。
    她的常态是**中低精力**：做过事，有点累，但心是稳的。

    所以用三角分布而非均匀分布，峰值落在偏平静的一侧。

    ## 2026-10 改：从「重掷」改成「漂移」

    原来这里是无条件取新随机值，结果：

        你花一小时把她从 45 聊到 80
        过了一小时 -> 直接变回随机数（可能是 45）
        **「你哄好她了」这件事不会积累。**

    也就是说「心情随对话改变」是真的，但**那个改变活不过一小时**。

    现在改成：新值 = 旧值和新随机值之间，**向随机值靠 DRIFT 比例**。

        她昨天气压低   -> 今天还是偏低，但没那么低   （性情的连续性）
        你昨天哄好了   -> 今天仍带着那份好         （你的行为留下了痕迹）
        长期没聊       -> 逐步回归她的基线         （不会一路飘走）

    `DRIFT = 0.55` 的意思是「向新值靠 55%，旧值留 45%」——
    既不是原地不动（那就失去了「新的一天」），也不是推倒重来。
    """
    now = now or datetime.now()

    # 情绪：多数时候偏好（她的底色是从容），但不是满
    # 中心从固定的 MODE_VALENCE 改成**当天底色** ——
    # 于是「今天本来就有点闷」会体现在每一次重掷里。
    #
    # ⚠️ 这一行曾经漏掉，而下面的混合公式已经在用 `_base` ->
    # 每次重掷都 NameError -> /api/chat 直接 500（界面显示 server error）。
    _base = day_base_for(now)
    v_new = int(random.triangular(VALENCE_MIN, VALENCE_MAX, _base))
    # 精力：多数时候偏中低（她总是在做事）
    e_new = int(random.triangular(ENERGY_MIN, ENERGY_MAX, MODE_ENERGY))
    v_new = max(VALENCE_MIN, min(VALENCE_MAX, v_new))
    e_new = max(ENERGY_MIN, min(ENERGY_MAX, e_new))

    if prev is None:
        valence, energy = v_new, e_new
    else:
        # 三段加权：**基线 + 旧值 + 新值**。
        #
        # 光用 `旧×(1-D) + 新×D` 是不够的 —— 那样收敛到的是
        # 「新值的均值」，而不是基线。实测：
        #
        #     从情绪 95 连掷 200 次 -> 76   （回不到基线）
        #     从情绪 15 连掷 200 次 -> 38
        #
        # 也就是说**极端值漂不回来**。加上基线这一项才有回归力：
        #
        #     极端高 -> 往基线拉；极端低 -> 也往基线拉
        #
        # 权重 0.34 / 0.28 / 0.38 的含义：
        #     比 1/3 略多给新值一点 —— 睡一觉起来确实会变
        #     比 1/3 略少给旧值一点 —— 免得上一段对话无限延长
        # 锚点从固定 baseline 换成当天底色
        valence = int(round(
            _base * DRIFT_BASE
            + prev.valence * DRIFT_PREV
            + v_new * DRIFT_NEW))
        energy = int(round(
            BASELINE_ENERGY * DRIFT_BASE
            + prev.energy * DRIFT_PREV
            + e_new * DRIFT_NEW))
        valence = max(VALENCE_MIN, min(VALENCE_MAX, valence))
        energy = max(ENERGY_MIN, min(ENERGY_MAX, energy))

    mood = Mood(valence=valence, energy=energy,
                updated_at=now.isoformat(timespec="seconds"))

    # 抽事件：与精力档匹配。
    # **事件照旧重抽，不继承** —— 那是「今天发生了什么」，
    # 该像天气一样换新的，而不是跟着心情一起漂。
    pool = load_events().get(mood.tier) or []
    if not prev or not prev.event:
        mood.event = random.choice(pool) if pool else ""
    else:
        # 避免连续两次抽到同一条
        choices = [e for e in pool if e != prev.event] or pool
        mood.event = random.choice(choices) if choices else ""
    return mood


def refresh_if_due(mood: Mood, now: datetime | None = None) -> tuple[Mood, bool]:
    """到时间就重掷。返回 (心情, 是否刷新过)。"""
    if needs_refresh(mood, now):
        return roll(now, prev=mood), True
    return mood, False


# ── 对话的影响 ───────────────────────────────────────
# 判据用词表，不需要额外调模型
_WARM = ("谢谢", "辛苦了", "想你", "喜欢", "好看", "陪你", "照顾", "担心",
         "别累", "歇", "注意身体", "一起吃", "给你", "帮你", "我在")
_COLD = ("随便", "无所谓", "不想说", "算了", "没空", "烦", "别管", "闭嘴",
         "无聊", "懒得", "知道了", "嗯。", "哦。")


def apply_turn(mood: Mood, user_text: str, her_reply: str = "",
               valence_delta: int | None = None,
               session_turns: int = 0,
               now: datetime | None = None,
               asleep: bool = False) -> tuple[Mood, str]:
    """一轮对话之后微调心情。返回 (心情, 变化说明)。

    ## 三条规则

    1. 他说了贴心的话   -> 情绪升
    2. 他敷衍、冷淡     -> 情绪降（**她该因为你冷淡而低落**，
                          否则心情就成了纯粹的讨好机制）
    3. 聊得久           -> 精力降（她会累）

    ## 2026-10 修的三处

    用户反馈「比此前更冷漠」，查出来是心情机制在正常工作 ——
    但设计上有三个洞，把那 51 轮对话推到了「情绪 30 / 精力 8（触底）」：

    **① 精力没有恢复机制。** 原来每轮无条件 -2~-4，上限 100、下限 8，
    连聊 25-45 轮必然触底。现在改成**按他这句话的长度**算消耗 ——
    「嗯」「哦」这种短句来回几乎不耗神，有时还回一点。

    **② 「好」被判成冷淡。** 原来是 `len(t) <= 2` -> cold，
    于是「好」「嗯」「嘿嘿」全算冷淡，情绪 -3~-6。
    **但「嘿嘿」是开心的意思。** 而且角色卡还要求她自己说话短 ——
    两套规则在打架。现在只有明确的敷衍词（「随便」「无所谓」）才算冷。

    **③ 触底时的语气指示在赶人。** 那句「他若留你多聊，你会说改日」
    和角色卡的「不推拉、不设限、不逐客」直接矛盾。改在 `tone` 里。
    """
    v, e = mood.valence, mood.energy
    notes = []
    t = (user_text or "").strip()

    # ## 情绪：优先用外部给的 delta（2026-10-17）
    #
    # 词表判据的实测命中率只有 11%（暖）/ 3%（冷），**85% 的话它判不出来** ——
    # 因为他的情感藏在事情里、不在字面上（「我今天在矿洞里待了一整天」）。
    #
    # 所以现在由 `mind_feel.judge` 调模型判，结果通过 `valence_delta` 传进来。
    # **词表退回成兜底**：API 挂了或没给 delta 时才用。
    if valence_delta is not None:
        if valence_delta:
            v += valence_delta
            notes.append(f"情绪{valence_delta:+d}")
    else:
        warm = any(w in t for w in _WARM)
        cold = any(w in t for w in _COLD)
        if warm and not cold:
            v += random.randint(*SHIFT_WARM)
            notes.append("情绪 +")
        elif cold:
            v += random.randint(*SHIFT_COLD)
            notes.append("情绪 -")

    # ── 精力（2026-10-19 重做）────────────────────────────
    #
    # 旧的是「按他这句话的字数」掉血。用户的原话：
    #     「精力受对话长度影响，太不符合现实」
    #
    # 现在改成**按当前情况重新算**：
    #     精力 = 时辰曲线 + 当天随机 - 会话疲劳
    #
    # 三件事都不累加，所以它不会贴在栏杆上：
    #     到点自然低、白天自然高、聊久了自然掉、断开自然回。
    #
    # session_turns 由 Character 算好传进来
    # （连续聊了几轮；断开 2 小时以上算新的一段）。
    e = derive_energy(now, session_turns, asleep)
    notes.append(f"精力={e}")

    # ── 往当天底色回归（2026-10-19 加）──────────────────
    #
    # 没有这一步，一句 +5 的好话会一直累加，直到撞在 96 上 ——
    # 撞住之后反馈环路就死了：你说再多，她也不再变好。
    #
    # 回归目标**不是**固定 baseline 63（那会把她整体压暗），
    # 而是**当天底色** —— 于是她是「围绕今天该有的样子浮动」，
    # 不是「被往下拉」。
    try:
        # ⚠️ **先夹紧，再回归。**
        #
        # 顺序错了会白做：96 分时一句 +5 让 v 变成 101，
        # 这时再按 (50-101)*0.1 回归只拉回 5 分，夹紧后仍是 96 —— 等于没动。
        # 先夹到 96，再 (50-96)*0.1 = -4.6，才真的落到 91。
        v = max(VALENCE_MIN, min(VALENCE_MAX, v))
        _base = day_base_for(now)
        v = int(round(v + (_base - v) * REVERT_TO_BASE))
    except Exception:
        pass

    mood.valence = max(VALENCE_MIN, min(VALENCE_MAX, v))
    mood.energy = max(ENERGY_MIN, min(ENERGY_MAX, e))
    return mood, " ".join(notes)


# ── 进 prompt ────────────────────────────────────────
def mood_block(mood: Mood) -> str:
    """拼成给模型看的一段。

    ## 写法要点

    **心情是底色，不是话题。** 所以这里明确要求：
    不要把心情说出来，让语气自己带出来。

    否则她会张口就是「我今天心情不好」—— 那是机器人汇报状态，
    不是人在说话。
    """
    lines = ["## 你今天的状态（**只影响语气，不影响你对他的态度**）", ""]
    lines.append(f"- {mood.tone}")
    if mood.event:
        lines.append(f"- 今天发生的事：{mood.event}")
    lines.append("")
    lines.append("**怎么用这三行 —— 这一条极其重要：**")
    lines.append("")
    lines.append("- **它是底色，不是话题。** 别把它讲出来，让人从你的语气里"
                 "感觉出来。（原来这里举了个例句，结果她真照着念 —— "
                 "所以只讲原则，不举例。）")
    lines.append("- **它改的是你今天的做法，不是你对他的态度。**"
                 "心情好的时候你会主动张罗；累的时候你话更省 —— "
                 "但**这两种情况下你都想他在**。")
    lines.append("- 所以：**任何心情下都不冷落他、不让他猜、不把他往外推。**"
                 "如果你今天状态差，就直说差，别用沉默让他自己琢磨。")
    lines.append("- 今天那件事可以自然地带一句，但不必每句都提，也不是非提不可。")
    return "\n".join(lines)
