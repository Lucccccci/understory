"""记忆快照 —— 让「她记得什么」变成可以存档、可以回滚的东西。

## 为什么需要

用户 2026-10-16 **误触**了设置面板底部的「重新开始」，一瞬间丢掉了
134 轮对话、6 条观察、2 个事件。虽然 `.bak` 救回来了，但那次暴露了两个洞：

    ① 那个按钮的代价和摩擦完全不成比例（一次点击 = 几个月记忆）
    ② 只有**一个** .bak，而且每次重置都会覆盖它

## 和游戏存档的同构

用户提的类比很准 —— 记忆缺的正是游戏存档早就有的两样东西：

    游戏存档              现在的记忆
    ─────────────────    ─────────────────
    睡觉时写（累加）      每轮写（累加）      ✅ 都有
    可以存很多个槽位       只有一个            ❌ 缺
    可以读回任意一个       不能回滚            ❌ 缺

这个模块补的正是后两样。

## 存什么

**整个 `saves/sessions/` 目录的副本**，不是单个文件。

理由：
    · 一个会话就几 KB 到几十 KB，整目录也就几百 KB
    · 单文件回滚容易漏（比如只想回滚桌面档，但观察其实存在同一文件里）
    · 和 mind_backup.py 的分工清楚 —— 那个备整份 saves（含 backups 自身），
      这个只备会话

## 什么时候自动打

    · 服务启动时（一天最多一次）
    · **危险操作之前** —— 尤其是「重新开始」

第二条是关键：**让危险操作变得可逆**。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "saves" / "sessions"
SNAP_DIR = HERE / "saves" / "snapshots"

# 自动快照最多留这么多份。手动打的不会被自动清理删掉。
KEEP_AUTO = 30
AUTO_PREFIX = "auto-"
MANUAL_PREFIX = "manual-"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _session_files() -> list[Path]:
    """要进快照的文件。跳过 .bak / .tmp —— 那些是中间产物。"""
    if not SESSIONS_DIR.is_dir():
        return []
    return [p for p in SESSIONS_DIR.glob("*.json")
            if not p.name.endswith((".tmp", ".bak"))]


def _stats_of(files: list[Path]) -> dict:
    """快照里有什么。

    **只统计 desktop**（你和她真正的那份对话），其它会话单独记在
    `other_turns` 里。原来是把目录下所有 json 一起相加 ——
    于是测试档、一堆空的 qq 档、甚至备份文件全都算进了"轮次"，
    界面上的数字看着比实际大得多（2026-11-03 修）。
    """
    turns = events = obs = 0
    other = 0
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        n = int(d.get("total_turns") or 0) or len(d.get("turns") or [])
        if p.stem == "desktop":
            turns += n
            events += len(d.get("events") or [])
            obs += len(d.get("observations") or [])
        else:
            other += n
    return {"turns": turns, "events": events, "observations": obs,
            "other_turns": other}


def take(kind: str = "manual", note: str = "") -> dict:
    """打一个快照。

    Args:
        kind: "manual"（手动）或 "auto"（自动）。自动的会被清理，手动的不会。
        note: 说明，用来以后认出来「这份是什么」。

    Returns:
        {"ok": bool, "name": str, "stats": dict, "error": str}
    """
    files = _session_files()
    if not files:
        return {"ok": False, "error": "没有会话文件可备"}

    prefix = MANUAL_PREFIX if kind == "manual" else AUTO_PREFIX
    name = f"{prefix}{_stamp()}"
    dst = SNAP_DIR / name
    # 同一秒打两次就加后缀，别覆盖
    n = 1
    while dst.exists():
        n += 1
        dst = SNAP_DIR / f"{name}_{n}"

    try:
        dst.mkdir(parents=True, exist_ok=True)
        for p in files:
            shutil.copy2(p, dst / p.name)
        stats = _stats_of(files)
        (dst / "_meta.json").write_text(
            json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                        "kind": kind, "note": note, "stats": stats},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"写快照失败：{e}"}

    _prune()
    return {"ok": True, "name": dst.name, "stats": stats}


def list_all() -> list[dict]:
    """列出所有快照，新的在前。"""
    if not SNAP_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(SNAP_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = {}
        mp = d / "_meta.json"
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        files = [p for p in d.glob("*.json") if p.name != "_meta.json"]
        out.append({
            "name": d.name,
            "at": meta.get("at", ""),
            "kind": meta.get("kind", "auto"),
            "note": meta.get("note", ""),
            "stats": (meta.get("stats")
                      if isinstance(meta.get("stats"), dict)
                      and "other_turns" in meta["stats"]
                      else _stats_of(files)),
            "files": len(files),
            "size": sum(p.stat().st_size for p in d.glob("*") if p.is_file()),
        })
    return out


def restore(name: str, backup_current: bool = True) -> dict:
    """回滚到某个快照。

    Args:
        name: 快照目录名（`list_all()` 里的 `name`）。
        backup_current: 回滚前先给**当前**状态打一份 auto 快照 ——
            防「回滚错了，想再回来」的时候发现回不去了。

    Returns:
        {"ok": bool, "restored": int, "error": str}
    """
    src = SNAP_DIR / name
    if not src.is_dir():
        return {"ok": False, "error": f"没有这个快照：{name}"}

    snap_files = [p for p in src.glob("*.json") if p.name != "_meta.json"]
    if not snap_files:
        return {"ok": False, "error": "这个快照里没有会话文件"}

    if backup_current:
        take("auto", note=f"回滚到 {name} 之前")

    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        for p in snap_files:
            shutil.copy2(p, SESSIONS_DIR / p.name)
    except OSError as e:
        return {"ok": False, "error": f"回滚失败：{e}"}

    return {"ok": True, "restored": len(snap_files)}


def delete(name: str) -> dict:
    """删一个快照。"""
    d = SNAP_DIR / name
    if not d.is_dir():
        return {"ok": False, "error": "没有这个快照"}
    try:
        shutil.rmtree(d)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _prune() -> int:
    """只清理**自动**快照，手动的保留。"""
    autos = sorted([d for d in SNAP_DIR.iterdir()
                    if d.is_dir() and d.name.startswith(AUTO_PREFIX)],
                   reverse=True)
    n = 0
    for d in autos[KEEP_AUTO:]:
        try:
            shutil.rmtree(d)
            n += 1
        except OSError:
            pass
    return n


def auto_daily() -> dict | None:
    """今天还没打过 auto 快照的话，打一份。

    服务启动时调。`take` 里的时间戳精确到秒，所以这里要看**日期**。
    """
    today = datetime.now().strftime("%Y%m%d")
    if SNAP_DIR.is_dir():
        for d in SNAP_DIR.iterdir():
            if d.is_dir() and d.name.startswith(AUTO_PREFIX + today):
                return None
    return take("auto", note="每日自动")


if __name__ == "__main__":
    print("=" * 60)
    print("  记忆快照")
    print("=" * 60)
    print(f"  会话目录 {SESSIONS_DIR}")
    print(f"  快照目录 {SNAP_DIR}")
    print()

    files = _session_files()
    print(f"  当前会话 {len(files)} 个:")
    for p in files:
        print(f"    {p.name:<28} {p.stat().st_size:>8,} B")
    print()

    r = take("manual", note="自测")
    print(f"  打快照 -> {r}")

    items = list_all()
    print(f"  现有快照 {len(items)} 个:")
    for it in items[:6]:
        s = it["stats"]
        print(f"    {it['name']:<26} {it['kind']:<7} "
              f"{s.get('turns',0):>4} 轮 / {s.get('events',0)} 事件 / "
              f"{s.get('observations',0)} 观察   {it['note']}")
    print()
    print(f"  自动快照上限 {KEEP_AUTO}（手动的不会被清理）")
