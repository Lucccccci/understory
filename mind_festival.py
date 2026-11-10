"""【已停用 2026-10-27】与游戏脱钩后不再调用。

时令/天气/节日统一走 `mind_season.py`（现实日历）。
这个文件保留着是为了将来真要接回游戏时有参照。
"""
from __future__ import annotations

import time

# 这座镇子 1.6 的固定节日：(季节, 日) -> 名字
FESTIVALS = {
    ("spring", 13): "蛋蛋节",
    ("spring", 15): "沙漠节",
    ("spring", 16): "沙漠节",
    ("spring", 17): "沙漠节",
    ("spring", 24): "花舞节",
    ("summer", 11): "夏威夷宴会",
    ("summer", 28): "月光水母节",
    ("fall", 16): "这座镇子展览会",
    ("fall", 27): "万灵节",
    ("winter", 8): "冰雪节",
    ("winter", 15): "夜市",
    ("winter", 16): "夜市",
    ("winter", 17): "夜市",
    ("winter", 25): "冬星节",
}

SEASON_CN = {"spring": "春", "summer": "夏", "fall": "秋", "winter": "冬"}
CN2EN = {"春": "spring", "夏": "summer", "秋": "fall", "冬": "winter"}

_CACHE_TTL = 90.0
_cache: tuple[float, tuple[str, int]] = (0.0, ("", 0))


def _game_day() -> tuple[str, int]:
    """读存档拿 (季节, 日)。读不到就返回空。带缓存。"""
    global _cache
    now = time.time()
    ts, val = _cache
    if now - ts < _CACHE_TTL and (val[0] or val[1]):
        return val
    season, day = "", 0
    try:
        import server as _srv
        from world_save import read_game_context
        ctx = read_game_context(_srv.resolve_save_path())
        if ctx is not None and getattr(ctx, "found", False):
            # ⚠️ 实测：`GameContext.season` 给的是**中文**（"秋"），
            # 日期字段叫 `day` 不是 `day_of_month`。
            raw = str(getattr(ctx, "season", "") or "")
            season = CN2EN.get(raw, raw.lower())
            try:
                day = int(getattr(ctx, "day", 0) or 0)
            except (TypeError, ValueError):
                day = 0
    except Exception:
        pass
    _cache = (now, (season, day))
    return season, day


def today_name() -> str:
    """今天的节日名。不是节日返回空串。"""
    season, day = _game_day()
    if not season or not day:
        return ""
    return FESTIVALS.get((season, day), "")


def block() -> str:
    """拼成一段进 prompt。不是节日返回空串。"""
    name = today_name()
    if not name:
        return ""
    season, day = _game_day()
    return (
        "## 今天\n\n"
        f"- 今天是**{name}**（{SEASON_CN.get(season, '')}{day}）。\n"
        "你知道今天是什么日子。但**不要一开口就提** —— 他要是提起来，"
        "你有话接；他自己不提，你也不必提醒。"
    )
