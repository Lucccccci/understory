#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取这座镇子存档，抽出「她应该知道的事」。

## 为什么需要这个

「完整的人生」不只是聊天记录。她生活在这座镇子，就应该知道你农场什么情况、
和镇上谁走得近、现在是第几年春天。否则她只是个会说话的窗口，不是那个世界里的人。

## 存档格式（实测确认）

    位置: %APPDATA%\\the valleyValley\\Saves\\<农场名>_<ID>\\<农场名>_<ID>
    格式: 明文 XML（UTF-8 with BOM，以 <?xml 开头）

单文件 5 MB 左右，用正则抽取所需字段比整个 XML 解析快得多，
而且不会因为某个字段结构变化就整体失败。

## 设计原则

**只抽取，不推断。** 读到什么就是什么，读不到就不提。
绝不让「不知道」变成「编一个」——那是幻觉的源头。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mind_names import to_zh

SAVE_ROOT = Path.home() / "AppData" / "Roaming" / "the valleyValley" / "Saves"

SEASON_CN = {"0": "春", "1": "夏", "2": "秋", "3": "冬",
             "spring": "春", "summer": "夏", "fall": "秋", "winter": "冬"}

FARM_TYPE_CN = {
    "Standard": "标准农场", "Riverland": "河流农场", "Forest": "森林农场",
    "Hills": "山顶农场", "Wilderness": "荒野农场", "FourCorners": "四角农场",
    "Beach": "海滩农场", "MeadowlandsFarm": "草甸农场",
}

# 睡觉地点的中文。存档里存的是地图内部名。
#
# 只列常见的 —— 认不出来时原样显示（好过瞎猜），
# 因为 `as_facts` 里是 `SLEEP_LOCATION_CN.get(x, x)`。
SLEEP_LOCATION_CN = {
    "FarmHouse": "自己屋里",
    "Farm": "农场（打地铺？）",
    "Cabin": "小木屋",
    "IslandFarmHouse": "姜岛那间屋子",
    "BathHouse_Pool": "澡堂",
    "Mine": "矿洞里",
    "UndergroundMine": "矿洞里",
    "Hospital": "诊所",
    "Saloon": "酒吧楼上",
    "ManorHouse": "镇长家",
    "ScienceHouse": "木匠家",
    "AnimalShop": "牧场",
    "FishShop": "鱼店",
    "SeedShop": "杂货铺",
    "AdventureGuild": "探险家公会",
    "WizardHouse": "法师塔",
    "LeahHouse": "莉亚家",
    "JoshHouse": "乔治家",
    "ElliottHouse": "艾利欧特的小屋",
    "Trailer": "帕姆的拖车",
    "Tent": "帐篷",
    "Shed": "工棚",
    "Barn": "畜棚",
    "Coop": "鸡舍",
}

# 技能名
LV_NAME = {"农": "耕种", "矿": "采矿", "战": "战斗",
           "钓": "钓鱼", "觅": "觅食"}

# 她可能真正在乎的镇上人（避免把无关 NPC 全塞进上下文）
NOTABLE_NPCS = {
    "Abigail", "Alex", "Elliott", "Emily", "Haley", "Harvey", "Leah",
    "Maru", "Penny", "Sam", "Sebastian", "Shane", "Caroline", "Clint",
    "Demetrius", "Evelyn", "George", "Gus", "Jodi", "Kent", "Lewis",
    "Linus", "Marnie", "Pam", "Pierre", "Robin", "Willy", "Wizard",
    "Jas", "Vincent", "Leo", "Sandy", "Krobus", "Dwarf",
    # SVE 常见人物
    "Sophia", "Victor", "Olivia", "Andy", "Susan", "Claire", "Martin",
    "Scarlett", "Lance", "Magnus", "Morgan", "Apples",
}


@dataclass
class MindRelation:
    """游戏存档里，玩家与她的关系。"""
    found: bool = False
    npc_name: str = ""          # 存档里的键，通常是 "Shu"
    points: int = 0
    hearts: int = 0             # points // 250
    status: str = ""            # Friendly / Dating / Engaged / Married / Divorced
    gift_this_week: int = 0
    talked_today: bool = False
    def as_fact(self) -> str:
        if not self.found:
            return ""
        return f"在这座镇子里，你和她的关系是「{self.status}」，好感 {self.hearts} 心（{self.points} 点）"


@dataclass
class GameContext:
    """从存档读到的、可以被她知道的游戏世界状态。"""
    found: bool = False
    save_name: str = ""
    read_at: str = ""

    player_name: str = ""
    farm_name: str = ""
    favorite_thing: str = ""
    gender: str = ""
    farm_type: str = ""
    money: int = 0
    house_level: int = 0

    # ── 他的身体状态（2026-10 加）──────────────────────
    #
    # 这几样**本来就在存档里，只是一直没读**。加它们的理由：
    #
    #   「你今天累成什么样了」是最好聊的东西之一 ——
    #   比「今天天气怎么样」具体得多，而且是**关于他**的。
    #
    # ⚠️ 但它们是**睡觉前那一刻**的值，不是「此刻」。
    #    所以措辞上要说「昨天睡下时」而不是「你现在」——
    #    `as_facts` 里就是这么写的。
    stamina: float = 0.0
    max_stamina: float = 0.0
    health: float = 0.0
    max_health: float = 0.0

    # 累计数据 —— 用来判断「他这一天有多拼」
    total_money_earned: int = 0
    deepest_mine_level: int = 0
    # 昨晚睡在哪。**不是「他此刻在哪」** —— 存档里没有那个。
    last_sleep_location: str = ""
    levels: dict = field(default_factory=dict)   # 农/矿/战/钓/觅

    year: int = 0
    season: str = ""
    day: int = 0
    hours_played: float = 0.0

    spouse: str = ""
    friends: list[tuple[str, int, str]] = field(default_factory=list)

    # 玩家与她在游戏里的关系
    shu: MindRelation = field(default_factory=MindRelation)

    # ── 摘要（给 prompt 用）────────────────────────────
    def as_facts(self, max_friends: int = 6) -> list[str]:
        """整理成「她应该知道的事实」列表。读不到的项自动省略。"""
        if not self.found:
            return []

        facts: list[str] = []

        if self.farm_name:
            facts.append(f"对方的农场叫「{self.farm_name}」")
        if self.farm_type:
            facts.append(f"农场的类型是{self.farm_type}")
        if self.year and self.season and self.day:
            facts.append(f"眼下是第 {self.year} 年{self.season}季第 {self.day} 天")
        if self.house_level:
            desc = {0: "一间小木屋", 1: "刚翻修过", 2: "已经扩建过一次",
                    3: "房子挺大了"}.get(self.house_level)
            if desc:
                facts.append(f"住处{desc}")

        # 关系近的镇上人。
        # 阈值实测调过：原本设 1000 点（4 心），但正常存档早期根本到不了，
        # 结果这条事实永远为空。400 点（约 1.6 心）更实际。
        close = [f for f in self.friends if f[1] >= 400][:max_friends]
        if close:
            # 英文名转中文 —— 存档里存的是 "Linus"，不转的话她会说
            # 「Linus 那边摆摊的多」这种中英混搭（实测踩过）。
            names = "、".join(f"{to_zh(n)}（{h} 心）" for n, _, h in close)
            facts.append(f"和镇上这些人走得比较近：{names}")

        if self.hours_played >= 1:
            facts.append(f"在这片地方已经忙了大约 {self.hours_played:.0f} 小时")
        if self.spouse:
            facts.append(f"配偶是 {to_zh(self.spouse)}")

        # ── 他的身体状态 ────────────────────────────────
        #
        # ⚠️ 措辞要紧：这些是**睡觉前那一刻**的值，不是「此刻」。
        #    写成「你现在体力 40」会出事 —— 他可能已经睡醒满血了。
        #    所以一律带上「昨晚睡下时」这个时间限定。
        if self.max_stamina:
            left = self.stamina / self.max_stamina
            # 只说「还剩多少」，不说数字 —— 数字像体检报告
            if left <= 0.12:
                facts.append("他昨晚睡下时体力几乎见底了")
            elif left <= 0.3:
                facts.append("他昨晚睡下时体力剩得不多")
            elif left >= 0.9:
                facts.append("他昨晚睡下时体力还满着（那天没怎么累着）")
        if self.max_health and self.health < self.max_health * 0.55:
            facts.append("他昨晚睡下时身上还带着伤")

        if self.last_sleep_location:
            where = SLEEP_LOCATION_CN.get(
                self.last_sleep_location, self.last_sleep_location)
            facts.append(f"他昨晚是在{where}歇下的")

        lv = {k: v for k, v in (self.levels or {}).items() if v}
        if lv:
            # 只说最高的两项，列全了像属性面板
            top = sorted(lv.items(), key=lambda x: -int(x[1] or 0))[:2]
            desc = "、".join(f"{LV_NAME.get(k, k)} {v} 级" for k, v in top
                             if str(v).isdigit())
            if desc:
                facts.append(f"他眼下最在行的是{desc}")

        return facts


def _find_latest_save() -> Path | None:
    """找最近玩过的存档。"""
    if not SAVE_ROOT.is_dir():
        return None

    best: tuple[float, Path] | None = None
    for d in SAVE_ROOT.iterdir():
        if not d.is_dir():
            continue
        main = d / d.name
        if not main.is_file():
            continue
        try:
            mtime = main.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, main)
    return best[1] if best else None


def _parse_friends(txt: str) -> list[tuple[str, int, str]]:
    """解析 friendshipData。

    结构形如：
        <item><key><string>Lewis</string></key><value><Friendship>
            <Points>208</Points>...<Status>Friendly</Status>...</Friendship></value></item>
    """
    out: list[tuple[str, int, str]] = []
    pattern = re.compile(
        r"<key><string>([^<]+)</string></key>\s*<value><Friendship>"
        r".*?<Points>(-?\d+)</Points>"
        r".*?<Status>([^<]*)</Status>",
        re.S,
    )
    for m in pattern.finditer(txt):
        name, points, status = m.group(1), int(m.group(2)), m.group(3)
        if name not in NOTABLE_NPCS:
            continue
        hearts = points // 250
        out.append((name, points, f"{hearts}"))
    out.sort(key=lambda x: -x[1])
    return out


def list_saves() -> list[dict]:
    """列出所有存档，供界面选择。按最近玩过排序。"""
    out: list[dict] = []
    if not SAVE_ROOT.is_dir():
        return out

    for d in SAVE_ROOT.iterdir():
        if not d.is_dir():
            continue
        main = d / d.name
        if not main.is_file():
            continue
        try:
            st = main.stat()
        except OSError:
            continue

        # 农场名与 ID 从目录名拆（形如 瓦伦丁_449518466）
        label = d.name
        farm, _, sid = d.name.rpartition("_")
        if farm:
            label = f"{farm}（{sid}）"

        out.append({
            "name": d.name,
            "label": label,
            "farm": farm or d.name,
            "mtime": st.st_mtime,
            "played": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    out.sort(key=lambda x: -x["mtime"])
    return out


def _parse_shu_relation(txt: str, npc_keys: tuple[str, ...] = ("Shu",)) -> MindRelation:
    """从 friendshipData 里找她的关系。

    ⚠️ 她的存档键是 **Shu**（原 mod 的内部名），不是中文「她」。
    改成 [CP] ShuYan 的话键会变成 NPC_.Shu_Shu，所以这里支持多个候选键。
    """
    rel = MindRelation()
    for npc in npc_keys:
        # 精确匹配 <string>Shu</string>，避免误匹配到别的 NPC
        pat = re.compile(
            r"<key><string>" + re.escape(npc) + r"</string></key>\s*"
            r"<value><Friendship>(.*?)</Friendship></value>",
            re.S,
        )
        m = pat.search(txt)
        if not m:
            continue

        body = m.group(1)
        pts = re.search(r"<Points>(-?\d+)</Points>", body)
        st = re.search(r"<Status>([^<]*)</Status>", body)
        gtw = re.search(r"<GiftsThisWeek>(\d+)</GiftsThisWeek>", body)
        ttd = re.search(r"<TalkedToToday>(true|false)</TalkedToToday>", body)

        rel.found = True
        rel.npc_name = npc
        rel.points = int(pts.group(1)) if pts else 0
        rel.hearts = rel.points // 250
        rel.status = st.group(1) if st else "Friendly"
        rel.gift_this_week = int(gtw.group(1)) if gtw else 0
        rel.talked_today = (ttd.group(1) == "true") if ttd else False
        return rel
    return rel


def read_game_context(save_file: Path | None = None) -> GameContext:
    """读存档。任何一步失败都返回 found=False，绝不抛异常。

    这是刻意的：读不到存档不该让她变成哑巴，只是少知道一些事。
    """
    ctx = GameContext(read_at=datetime.now().isoformat(timespec="seconds"))

    try:
        path = save_file or _find_latest_save()
        if path is None or not path.is_file():
            return ctx

        ctx.save_name = path.parent.name
        txt = path.read_text(encoding="utf-8-sig", errors="replace")

        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", txt)
            return m.group(1).strip() if m else ""

        ctx.player_name = grab("name")
        ctx.farm_name = grab("farmName")
        ctx.favorite_thing = grab("favoriteThing")
        ctx.gender = grab("gender")
        ctx.farm_type = FARM_TYPE_CN.get(grab("whichFarm"), grab("whichFarm"))

        money = grab("money")
        ctx.money = int(money) if money.isdigit() else 0

        lvl = grab("houseUpgradeLevel")
        ctx.house_level = int(lvl) if lvl.isdigit() else 0

        y = grab("yearForSaveGame")
        ctx.year = int(y) if y.isdigit() else 0
        s = grab("seasonForSaveGame")
        ctx.season = SEASON_CN.get(s, s)
        d = grab("dayOfMonthForSaveGame")
        ctx.day = int(d) if d.isdigit() else 0

        ms = grab("millisecondsPlayed")
        ctx.hours_played = int(ms) / 3_600_000 if ms.isdigit() else 0.0

        # ── 身体状态 ──────────────────────────────────
        #
        # `stamina` / `maxStamina` 全文各只 1 处，直接 grab 就行。
        #
        # `health` 全文有 3513 处（怪物、门、物品都有），不能直接 grab。
        # 玩家的那一对有个**独有的特征**：`<health>` 紧跟 `<maxHealth>`。
        # 查过全文，符合这个模式的只有两处：
        #
        #     位置 73867      health=120  maxHealth=120   <- 玩家
        #     位置 777736     health=392  maxHealth=200   <- 一个门
        #
        # 靠 `maxStamina` 附近搜是不行的 —— 实测玩家血量在它后面
        # **1510 字**处，一开始只搜 600 字，结果读成 0。
        def num(tag: str) -> float:
            v = grab(tag)
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        ctx.stamina = num("stamina")
        ctx.max_stamina = num("maxStamina")

        for m in re.finditer(
                r"<health>([^<]*)</health><maxHealth>([^<]*)</maxHealth>", txt):
            try:
                hp, mhp = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            # 血上限是 100~250 之间（等级加成后最多 250 上下）。
            # 那个门是 200，也在范围内 —— 但它是全文**最后一个**匹配，
            # 而玩家数据在存档前部（7 万 vs 77 万）。
            # 取第一个合法的就够了。
            if 50 <= mhp <= 400 and 0 <= hp <= 500:
                ctx.health, ctx.max_health = hp, mhp
                break

        tme = grab("totalMoneyEarned")
        ctx.total_money_earned = int(tme) if tme.isdigit() else 0
        dml = grab("deepestMineLevel")
        ctx.deepest_mine_level = int(dml) if dml.isdigit() else 0
        ctx.last_sleep_location = grab("lastSleepLocation")
        ctx.levels = {
            "农": grab("farmingLevel"), "矿": grab("miningLevel"),
            "战": grab("combatLevel"), "钓": grab("fishingLevel"),
            "觅": grab("foragingLevel"),
        }

        ctx.spouse = grab("spouse")
        ctx.friends = _parse_friends(txt)
        ctx.shu = _parse_shu_relation(txt)

        ctx.found = bool(ctx.player_name)
        return ctx

    except Exception:
        # 存档格式变了、文件被占用、权限问题 —— 一律当作「读不到」
        return ctx


if __name__ == "__main__":
    c = read_game_context()
    if not c.found:
        print("没读到存档")
    else:
        print(f"存档: {c.save_name}")
        print(f"玩家: {c.player_name}   农场: {c.farm_name}   最爱: {c.favorite_thing}")
        print(f"进度: 第 {c.year} 年{c.season}季第 {c.day} 天   已玩 {c.hours_played:.1f} 小时")
        print(f"金钱: {c.money}   房屋等级: {c.house_level}")
        print(f"好友: {c.friends[:8]}")
        print()
        print("给她的事实：")
        for f in c.as_facts():
            print("  ·", f)
