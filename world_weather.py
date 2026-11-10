"""【已停用 2026-10-27】与游戏脱钩后不再调用。

时令/天气/节日统一走 `mind_season.py`（现实日历）。
这个文件保留着是为了将来真要接回游戏时有参照。
"""
from __future__ import annotations

import re
from pathlib import Path

SAVE_ROOT = Path.home() / "AppData" / "Roaming" / "the valleyValley" / "Saves"

# 存档里的天气代号
_WEATHER = {
    "Sun": "晴", "Sunny": "晴",
    "Rain": "雨", "Rainy": "雨",
    "Storm": "雷雨", "Lightning": "雷雨",
    "Snow": "雪", "Snowy": "雪",
    "Wind": "刮风", "Windy": "刮风",
    "Fog": "雾", "Foggy": "雾",
    "GreenRain": "绿雨",
    "Festival": "节日",
    "Wedding": "婚礼",
}

_SEASON = {"spring": "春", "summer": "夏", "fall": "秋", "winter": "冬"}


def _latest_save() -> Path | None:
    """最近写过的存档主文件。"""
    if not SAVE_ROOT.is_dir():
        return None
    dirs = [d for d in SAVE_ROOT.iterdir() if d.is_dir()]
    if not dirs:
        return None
    d = max(dirs, key=lambda x: x.stat().st_mtime)
    main = d / d.name            # 主文件没有后缀，名字等于目录名
    return main if main.is_file() else None


def _grab(text: str, tag: str) -> str:
    """取 `<tag>值</tag>` 里的值。"""
    m = re.search(r"<" + re.escape(tag) + r">([^<]{0,80})</" + re.escape(tag) + r">", text)
    return m.group(1).strip() if m else ""


def read_weather() -> dict:
    """读天气和运气。

    Returns:
        dict，键：weather（中文）、weather_raw、luck（float）、luck_word（中文）、
        season、day、year。读不到就返回空 dict。
    """
    p = _latest_save()
    if p is None:
        return {}
    try:
        # 存档很大（6 MB 上下），但只读一次、只做几次正则，够快
        t = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    w_raw = _grab(t, "weatherForTomorrow")
    luck_raw = _grab(t, "dailyLuck")

    out: dict = {}
    if w_raw:
        out["weather_raw"] = w_raw
        out["weather"] = _WEATHER.get(w_raw, "")
    if luck_raw:
        try:
            out["luck"] = float(luck_raw)
        except ValueError:
            pass
    for k, tag in (("season", "currentSeason"), ("day", "dayOfMonthForSaveGame"),
                   ("year", "yearForSaveGame")):
        v = _grab(t, tag)
        if v:
            out[k] = v
    if out.get("season") in _SEASON:
        out["season_cn"] = _SEASON[out["season"]]
    return out


def as_facts() -> list[str]:
    """整理成给模型看的事实列表。

    ⚠️ 措辞故意含糊（「这两天」而不是「今天」）—— 理由见模块开头的说明。
    """
    d = read_weather()
    if not d:
        return []

    facts: list[str] = []

    w = d.get("weather")
    if w:
        facts.append(f"这两天是{w}天")

    luck = d.get("luck")
    if isinstance(luck, float):
        if luck >= 0.07:
            facts.append("运气正好（下矿、钓鱼都顺）")
        elif luck >= 0.02:
            facts.append("运气还行")
        elif luck > -0.02:
            facts.append("运气平平")
        elif luck > -0.07:
            facts.append("运气差些")
        else:
            facts.append("运气很差（今天别下矿）")

    return facts


if __name__ == "__main__":
    print("  存档目录:", SAVE_ROOT)
    p = _latest_save()
    print("  最新存档:", p.name if p else "(没有)")
    print()
    d = read_weather()
    print("  原始读取:", d)
    print()
    for f in as_facts():
        print("  - " + f)
