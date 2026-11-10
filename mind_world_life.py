"""【已停用 2026-10-27】与游戏脱钩后不再调用。

时令/天气/节日统一走 `mind_season.py`（现实日历）。
这个文件保留着是为了将来真要接回游戏时有参照。
"""
from __future__ import annotations

# 季节 -> 这个季节地里的事。挑一条。
SEASON_WORK = {
    "春": [
        "地刚翻过，春天的种下去得快",
        "春雨多，地里的活得赶在雨前头做完",
    ],
    "夏": [
        "夏天的日头毒，地里的活得赶早做",
        "天热，浇水的次数比春天多",
    ],
    "秋": [
        "秋天的东西长得快，一天一个样",
        "该收了，秋天的收成得赶在冷下来之前进仓",
    ],
    "冬": [
        "冬天地里没活，都在屋里收拾东西",
        "地冻着，只能等开春",
    ],
}

# 农业等级 -> 他操心的事。挑一条。
FARM_LEVEL = [
    (8, ["地里的活他摸熟了，就是在琢磨怎么多收一点",
         "他那块地打理得比镇上多数人都细"]),
    (5, ["地里的活还在一件件学，有时候得问人",
         "他这几季下来，总算摸出些门道"]),
    (0, ["地里的活还生，时不时要出岔子",
         "他种地还在摸索，收成时好时坏"]),
]

# 矿洞深度 -> 具体的话
MINE_DEPTH = [
    (100, ["矿洞到过一百层往下了，那底下不好走"]),
    (70, ["矿洞到七十多层了，再往下得带够东西"]),
    (30, ["矿洞下到三十来层，越往下越不好走"]),
    (0, []),
]


def facts(ctx) -> list[str]:
    """从 GameContext 推导 1-3 条她这边能落到的事。

    Args:
        ctx: `world_save.GameContext`。

    Returns:
        短句列表，可能为空（读不到存档时）。
    """
    if ctx is None or not getattr(ctx, "found", False):
        return []

    out: list[str] = []

    # ① 季节 -> 地里的活
    season = getattr(ctx, "season", "") or ""
    if season in SEASON_WORK:
        out.append(SEASON_WORK[season][0])

    # ② 农业等级 -> 他操心什么
    lv = (getattr(ctx, "levels", None) or {}).get("农", "0")
    try:
        n = int(lv)
    except (TypeError, ValueError):
        n = 0
    for need, lines in FARM_LEVEL:
        if n >= need:
            out.append(lines[0])
            break

    # ③ 矿洞深度
    deep = int(getattr(ctx, "deepest_mine_level", 0) or 0)
    for need, lines in MINE_DEPTH:
        if deep >= need:
            out.extend(lines)
            break

    return out[:3]


def as_block(ctx) -> str:
    """拼成 prompt 的一段。读不到就返回空串。"""
    fs = facts(ctx)
    if not fs:
        return ""
    # 2026-10-26 改：原来的标题是「你自己的话里可以落到这些上面」，
    # 那是在**鼓励**她把游戏状态往自己的话里塞 —— 于是他的真实难处
    # 常被顶成「农场怎么样」。现在降级成背景：接得上，但不许拿它反问。
    lines = ["## 他那边的近况（**背景，不是话题**）"]
    lines += [f"- {f}" for f in fs]
    lines.append("")
    lines.append("这些只当**背景**。他聊到农事、矿洞，你接得上就够了。")
    lines.append("**不要拿这些反过来问他**（「你地里的活怎么样」「矿洞下到哪了」）——")
    lines.append("也不要为了用上它们，把话题往那儿带。他说别的事时，这些就是沉默的。")
    return "\n".join(lines)
