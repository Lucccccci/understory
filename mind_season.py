"""时令、天气、节日 —— 全部按**现实**走，不读游戏。

## 为什么和游戏脱钩（2026-10-27）

原来季节/天气/节日都从这座镇子存档读。问题是：

    · 存档只有你开游戏才更新 —— 你不玩，她那边的季节就冻住了
    · 游戏日一个月 28 天，和现实日期映射不上，节日必然算错
    · 他的农业等级、矿洞深度是**他的游戏进度**，不是她的时令

现在改成：她是活的，按现实日历过日子（人在当地）。

## 三个来源

    季节    现实月份（3-5 春 / 6-8 夏 / 9-11 秋 / 12-2 冬），再分初/仲/暮
    天气    Open-Meteo（当地，免费无需密钥），缓存 1 小时；失败就按季节给个大概
    节日    公历节日 + 农历节日（2026-2027 近似表）+ 几个节气

天气和节日**都只是背景**。她不会每天报天气 —— 只在话题碰上时才提。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent / "saves" / "weather_cache.json"
CACHE_TTL = 3600  # 一小时

# 当地
LAT, LON = 31.30, 120.62

SEASON_WORD = {3: "春", 4: "春", 5: "春",
               6: "夏", 7: "夏", 8: "夏",
               9: "秋", 10: "秋", 11: "秋",
               12: "冬", 1: "冬", 2: "冬"}

# 这个时节的活（她自己的，和游戏无关）
SEASON_WORK = {
    "春": ["该翻的地翻了，种子下得早些才赶得上",
           "春雨多，晾在外头的东西要留个心"],
    "夏": ["日头毒，活赶早不赶晚，晌午别在院里待着",
           "湿气重，坛子缸子都得看着，容易起霉"],
    "秋": ["趁好天晒最后一茬，过了这阵子就没日头了",
           "霜降前把地窖收拾出来，菜要下窖了"],
    "冬": ["外头湿冷，屋里火别断，被褥要常晒",
           "囤的菜见底了，得盘算着吃"],
}

# 天气只是背景 —— 这句每次都给，防止她天天播报
WEATHER_RULE = ("天气只是背景。他问起、或者话题碰上了才说一句；"
                "**不要每回都提要下雨还是要晴**。")

# Open-Meteo 的 weather_code -> 中文
_WCODE = [
    ((0,), "晴"),
    ((1, 2), "晴间多云"),
    ((3,), "阴"),
    ((45, 48), "有雾"),
    ((51, 53, 55, 56, 57), "小雨"),
    ((61, 63, 65, 66, 67), "雨"),
    ((71, 73, 75, 77), "雪"),
    ((80, 81, 82), "阵雨"),
    ((85, 86), "阵雪"),
    ((95, 96, 99), "雷雨"),
]

# 农历节日（2026–2027 近似表；再往后要补）
LUNAR_FESTIVALS = {
    "2026-02-17": "春节", "2026-03-03": "元宵",
    "2026-06-19": "端午", "2026-08-19": "七夕",
    "2026-09-25": "中秋", "2026-10-18": "重阳",
    "2027-02-06": "春节", "2027-02-20": "元宵",
    "2027-06-09": "端午", "2027-08-08": "七夕",
    "2027-09-15": "中秋", "2027-10-08": "重阳",
}

# 公历节日
SOLAR_FESTIVALS = {
    "01-01": "元旦", "05-01": "劳动节", "10-01": "国庆",
}

# 几个要紧的节气（近似日；她不会说得很精确）
SOLAR_TERMS = {
    "02-04": "立春", "03-05": "惊蛰", "04-05": "清明",
    "05-05": "立夏", "06-21": "夏至", "07-23": "大暑",
    "08-07": "立秋", "09-23": "秋分", "10-23": "霜降",
    "11-07": "立冬", "12-21": "冬至", "01-20": "大寒",
}


# ══════════════════════════════════════════════════════════
# 季节
# ══════════════════════════════════════════════════════════

def _city() -> tuple[float, float, str]:
    """读 settings.json 里的 city；没有就退回当地。"""
    try:
        cfg = json.loads((Path(__file__).with_name("settings.json"))
                         .read_text(encoding="utf-8"))
        c = cfg.get("city") or {}
        lat = float(c.get("lat") or 31.30)
        lon = float(c.get("lon") or 120.62)
        return lat, lon, str(c.get("name") or "")
    except Exception:
        return 31.30, 120.62, ""

def season_of(d: date | None = None) -> tuple[str, str]:
    """返回 (季节, 细分名)，例如 ("秋", "深秋")。"""
    d = d or date.today()
    # 从三月起算：base 0-2 春，3-5 夏，6-8 秋，9-11 冬
    base = (d.month - 3) % 12
    season = ("春", "夏", "秋", "冬")[base // 3]
    prefix = ("初", "仲", "暮")[base % 3]
    return season, prefix + season


# ══════════════════════════════════════════════════════════
# 天气（Open-Meteo，无需密钥）
# ══════════════════════════════════════════════════════════
def _guess_weather(d: date) -> str:
    """拿不到实时数据时，按季节给个大概 —— 当地的气候。"""
    m = d.month
    if m in (6, 7):
        return "梅雨季，潮得很，东西容易返霉"
    if m in (8, 9):
        return "还热，偶尔有台风过境"
    if m in (12, 1, 2):
        return "湿冷，屋里比外头还阴"
    if m in (3, 4, 5):
        return "回暖，雨水多了起来"
    if m in (10, 11):
        return "秋高气爽，早晚凉"
    return ""           # 真的没有把握就不写，界面显示「—」


def _cached() -> dict | None:
    try:
        d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if (datetime.now().timestamp() - float(d.get("ts") or 0)) < CACHE_TTL:
            return d
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _save_cache(d: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _code_to_cn(code: int) -> str:
    for codes, cn in _WCODE:
        if code in codes:
            return cn
    return ""


def weather_today(d: date | None = None) -> str:
    """今天的天气，一句话。拿不到就按季节给个大概，再不行给空串。"""
    d = d or date.today()
    # ⚠️ 空缓存要当作「没缓存」——否则一次失败会在 1 小时内一直返回空串，
    # 界面上就是天气栏只有图标和「—」（2026-11-02 实测）。
    c = _cached()
    if c and str(c.get("text") or "").strip():
        return str(c.get("text") or "")

    try:
        lat, lon, _cname = _city()
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}"
               f"&longitude={lon}&current=temperature_2m,weather_code"
               f"&timezone=Asia%2FShanghai")
        with urllib.request.urlopen(url, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = data.get("current") or {}
        code = int(cur.get("weather_code") or -1)
        temp = cur.get("temperature_2m")
        cn = _code_to_cn(code)
        if cn:
            text = f"{cn}"
            if isinstance(temp, (int, float)):
                text += f"，{round(temp)} 度上下"
            _save_cache({"ts": datetime.now().timestamp(), "text": text})
            return text
    except Exception:
        pass

    # ⚠️ 失败也要写缓存（哪怕是空串）。
    # 否则状态面板每 2 秒轮询一次，就会每 2 秒发一次带 6 秒超时的请求。
    g = _guess_weather(d)
    _save_cache({"ts": datetime.now().timestamp(), "text": g or ""})
    return g


# ══════════════════════════════════════════════════════════
# 节日
# ══════════════════════════════════════════════════════════
def festival_of(d: date | None = None) -> tuple[str, str]:
    """返回 (名字, 种类)。种类是「节日」或「节气」。"""
    d = d or date.today()
    iso = d.isoformat()
    if iso in LUNAR_FESTIVALS:
        return LUNAR_FESTIVALS[iso], "节日"
    mmdd = iso[5:]
    if mmdd in SOLAR_FESTIVALS:
        return SOLAR_FESTIVALS[mmdd], "节日"
    if mmdd in SOLAR_TERMS:
        return SOLAR_TERMS[mmdd], "节气"
    return "", ""


# ══════════════════════════════════════════════════════════
# 拼成 prompt
# ══════════════════════════════════════════════════════════
def block() -> str:
    """时令这一段。这是她自己的季节，不是他的农场。"""
    d = date.today()
    season, sub = season_of(d)
    lines = ["## 时令（你自己那边的）", ""]
    lines.append(f"现在是**{sub}**（{d.month} 月 {d.day} 日）。")

    w = weather_today(d)
    if w:
        lines.append(f"外头的天：{w}。")

    work = SEASON_WORK.get(season) or []
    if work:
        lines.append(f"这个时节的活：{work[0]}。")

    name, kind = festival_of(d)
    if name:
        if kind == "节气":
            lines.append(f"差不多是**{name}**前后。")
        else:
            lines.append(f"今天是**{name}**。")

    lines.append("")
    lines.append("**这些是你自己的时令，不是他的农场。**")
    lines.append("你说起季节，说的是你院子里的树、灶上的火、地窖里的菜 ——")
    lines.append("不是他的收成。")
    if name:
        lines.append("节日也好、节气也好，**不必主动提**；他提起来，你有话接就够了。")
    lines.append(WEATHER_RULE)
    return "\n".join(lines)


def facts() -> list[str]:
    """给开场白用的短句列表（比 block 更短）。"""
    d = date.today()
    season, sub = season_of(d)
    out = [f"现在是{sub}"]
    w = weather_today(d)
    if w:
        out.append(f"外头{w}")
    name, kind = festival_of(d)
    if name:
        out.append(f"{'今天是' if kind == '节日' else '差不多是'}{name}")
    return out
