"""她手上没做完的活 —— 跨天推进，做完就变成一件旧事。

## 为什么要有这个

一个人有连续性，很大程度是靠「手上还有没弄完的东西」。
她的 `today_task` 是今天此刻在干什么，一天一换、不留痕；
「那把椅子还差一条腿」却会一直挂在那儿，跟着日子往前挪。

**成本最低的「活人感」：一个列表 + 一条算术，0 次模型调用。**

## 推进是**确定性**的

    stage = min(total, floor(距开始天数 × 速度))

速度按季节：秋收农忙 0.25 / 天，平常 0.40，冬闲 0.60。

**为什么不用随机**：刷新页面就会重算一次，随机会让进度来回跳，
看着像 bug。用天数推，同一时刻永远同一个结果。

## 做完之后

    stage == total
        ->  写进 her_events（kind="done"）
        ->  从 projects 移除
        ->  隔几天补一件新的

两件事就这么串起来了：「椅子做完了」会变成她可以提起的一件旧事。
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

CORPUS = Path(__file__).resolve().parent / "tools" / "corpus" / "projects.json"

MAX_ACTIVE = 3          # 同时最多几件
NEW_AFTER = 4           # 做完之后隔几天补新的

# 季节 -> 每天推进多少
RATE_SEASON = {
    "春": 0.40,
    "夏": 0.35,
    "秋": 0.25,      # 农忙，顾不上
    "冬": 0.60,      # 冬闲
}
RATE_DEFAULT = 0.40


@dataclass
class Project:
    text: str                       # 院角那把旧竹椅
    what: str = ""                  # 还差一条腿
    stage: int = 0
    total: int = 3
    started: str = ""               # ISO 日期
    last_touched: str = ""

    # ── 派生 ──────────────────────────────────────────
    @property
    def done(self) -> bool:
        return self.stage >= self.total

    @property
    def percent(self) -> float:
        return 0.0 if self.total <= 0 else min(1.0, self.stage / self.total)

    def line(self) -> str:
        """进 prompt 的一行。做完的不再出现（会被移走）。"""
        head = f"{self.text}，{self.what}" if self.what else self.text
        touched = ""
        if self.last_touched:
            try:
                days = (date.today() -
                        date.fromisoformat(self.last_touched[:10])).days
                if days >= 1:
                    touched = f"，{'前天' if days == 2 else f'{days} 天前'}动过"
            except (ValueError, TypeError):
                pass
        return f"- {head}（{self.stage}/{self.total}{touched}）"

    # ── 序列化 ────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Project":
        return Project(
            text=str(d.get("text") or ""),
            what=str(d.get("what") or ""),
            stage=int(d.get("stage") or 0),
            total=max(1, int(d.get("total") or 3)),
            started=str(d.get("started") or ""),
            last_touched=str(d.get("last_touched") or ""),
        )


# ══════════════════════════════════════════════════════════
# 语料
# ══════════════════════════════════════════════════════════
def _load_pool() -> list[dict]:
    try:
        d = json.loads(CORPUS.read_text(encoding="utf-8"))
        items = d.get("items") if isinstance(d, dict) else d
        return [x for x in (items or []) if isinstance(x, dict) and x.get("text")]
    except (OSError, json.JSONDecodeError):
        return []


def pick_new(existing: list[Project], seed: str = "") -> Project | None:
    """从语料里挑一件**没在手上的**，做成新项目。"""
    pool = _load_pool()
    if not pool:
        return None
    have = {p.text for p in (existing or [])}
    pool = [x for x in pool if x["text"] not in have]
    if not pool:
        return None
    rng = random.Random(seed or datetime.now().strftime("%Y-%m-%d"))
    it = rng.choice(pool)
    today = date.today().isoformat()
    return Project(text=str(it["text"]),
                   what=str(it.get("what") or ""),
                   stage=0,
                   total=max(1, int(it.get("total") or 3)),
                   started=today,
                   last_touched="")


# ══════════════════════════════════════════════════════════
# 推进
# ══════════════════════════════════════════════════════════
def rate_for(season: str) -> float:
    return RATE_SEASON.get((season or "").strip(), RATE_DEFAULT)


def advance(p: Project, today: date, season: str = "") -> bool:
    """按天数重算进度。返回是否真的变了。"""
    if not p.started:
        p.started = today.isoformat()
        return False
    try:
        started = date.fromisoformat(p.started[:10])
    except (ValueError, TypeError):
        p.started = today.isoformat()
        return False

    days = max(0, (today - started).days)
    new = min(p.total, int(days * rate_for(season)))
    if new > p.stage:
        p.stage = new
        p.last_touched = today.isoformat()
        return True
    return False


def block(projects: list) -> str:
    """拼成给模型看的一段。"""
    items = [Project.from_dict(p) if isinstance(p, dict) else p
             for p in (projects or [])]
    items = [p for p in items if p.text and not p.done]
    if not items:
        return ""
    lines = ["## 你手上还没做完的东西", ""]
    lines += [p.line() for p in items]
    lines += [
        "",
        "这些是你自己的活，跟他没关系。",
        "- 可以提进度、可以嫌它烦，**但别每天说**。",
        "- 真做完的那天，可以主动提一句。",
    ]
    return "\n".join(lines)
