#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存档自动备份 —— 让攒下来的东西不会因为一次意外全丢。

## 为什么需要它（而且该最先做）

`saves/sessions/*.json` 里装着你们全部的往来：对话、记忆、心事、心情。
聊几个月之后那是几 MB 的、**无法重建**的东西。

而这个目录**此前没有任何保护**：文件损坏、误删、写盘时断电，
任何一样发生就是全没了。

## 备份策略

用 zip 而不是复制目录，理由是：

    * 一次备份就是一个文件，好管理、好拷走
    * 压缩后很小（对话文本压得很好）
    * 恢复时解开就行，不需要专用工具

**每天最多一份**：启动时检查，当天已经备过就跳过。
理由是存档一天内的变化有限，而备份太密会让「翻回某一天」变得困难 ——
你要的是每天一个清楚的快照，不是每小时一个。

保留最近 KEEP 份，更旧的自动清掉。

## 恢复怎么做

备份是普通 zip，里面是 `sessions/` 那一套文件。
要恢复就把 zip 里的内容解到 `saves/` 下覆盖 —— 不需要跑这个程序。
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAVE_DIR = HERE / "saves"
BACKUP_DIR = HERE / "backups"

# 保留多少份。每天一份的话，20 份约等于三周。
KEEP = 20


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d")


def _today_backup() -> Path:
    return BACKUP_DIR / f"mind-{_stamp()}.zip"


def list_backups() -> list[dict]:
    """列出已有备份，新的在前。"""
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    # ⚠️ 按**修改时间**排，不能按文件名排。
    # 字符串排序下 "mind-20261013-2.zip" < "mind-20261013.zip"
    # （'-' 的字符序小于 '.'），于是「最近一份」会取错 —— 实测踩过，
    # 导致去重判断拿旧备份比对，永远认为内容变了。
    for p in sorted(BACKUP_DIR.glob("mind-*.zip"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime)
                             .strftime("%Y-%m-%d %H:%M"),
        })
    return out


def _collect() -> list[Path]:
    """要备份的文件。没有可备份的就返回空。"""
    if not SAVE_DIR.is_dir():
        return []
    files = [p for p in SAVE_DIR.rglob("*") if p.is_file()]
    # 跳过临时文件与旧备份。
    # `.bak` 是存档自己的历史副本（几 MB 的冗余），
    # `.tmp` 是上次写坏的残留 —— 两个都不该进备份。
    return [p for p in files
            if not p.name.endswith((".tmp", ".bak"))]


def _same_as_latest(files: list[Path]) -> bool:
    """当前存档是否和最近一份备份内容相同。

    比对的是**每个文件的字节内容**（sha256），不是时间戳 ——
    存档每次保存都会刷新 mtime，比时间会永远认为「变了」。

    读不到的备份（损坏/被占）一律当作「不同」，宁可多备一份。
    """
    items = list_backups()
    if not items:
        return False
    latest = BACKUP_DIR / items[0]["name"]

    def rel(f: Path) -> str:
        r"""相对路径，**统一用正斜杠**。

        ⚠️ Windows 上 str(Path) 给的是反斜杠，而 zip 里存的是正斜杠。
        不归一化的话两边永远不相等，去重就永远失效 —— 实测踩过：
        连跑两次 run() 都会各存一份，看起来像「内容总在变」。
        """
        return f.relative_to(SAVE_DIR.parent).as_posix()

    try:
        with zipfile.ZipFile(latest) as z:
            if sorted(z.namelist()) != sorted(rel(f) for f in files):
                return False
            for f in files:
                if hashlib.sha256(z.read(rel(f))).digest() != \
                   hashlib.sha256(f.read_bytes()).digest():
                    return False
        return True
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def _prune() -> int:
    """删掉超出 KEEP 的旧备份。返回删了几个。"""
    olds = sorted(BACKUP_DIR.glob("mind-*.zip"),
                  key=lambda x: x.stat().st_mtime, reverse=True)[KEEP:]
    n = 0
    for p in olds:
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def run(force: bool = False) -> dict:
    """做一次备份。返回结果说明。

    Args:
        force: 忽略「今天已备过」，强制再备一份（给手动备份用）
    """
    files = _collect()
    if not files:
        return {"ok": False, "reason": "还没有存档可备份"}

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = _today_backup()

    if target.is_file() and not force:
        # 「今天已备过」并不等于「不用备了」—— 实测踩过：
        # 启动时备了一份（6 轮），之后聊到 29 轮、加了收藏与承诺，
        # 这些**一整天都不在备份里**，要等明天启动才补上。
        # 所以这里再比一次内容：一样就跳过，不一样就存成今天第 N 份。
        if _same_as_latest(files):
            return {"ok": True, "skipped": True, "name": target.name,
                    "reason": "内容和最近一份备份相同"}
        for i in range(1, 100):
            cand = BACKUP_DIR / f"mind-{_stamp()}-{i}.zip"
            if not cand.is_file():
                target = cand
                break

    # 手动强制备份时，如果当天的已在，改成一个带序号的别名
    if target.is_file() and force:
        for i in range(1, 100):
            cand = BACKUP_DIR / f"mind-{_stamp()}-{i}.zip"
            if not cand.is_file():
                target = cand
                break

    # 先写临时文件再改名 —— 中途失败不会留下半个坏备份
    tmp = target.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                # 存相对 saves/ 的路径，恢复时直接解到 saves/ 就行
                z.write(f, f.relative_to(SAVE_DIR.parent).as_posix())
        tmp.replace(target)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "reason": f"备份失败：{e}"}

    removed = _prune()
    size = target.stat().st_size
    return {
        "ok": True, "skipped": False, "name": target.name,
        "size": size, "files": len(files), "removed": removed,
    }


def restore(name: str, overwrite: bool = False) -> dict:
    """从备份恢复。

    ## 为什么不自动备份前先自动恢复

    恢复是**破坏性**操作（会盖掉现在的存档），所以：
      * 默认不覆盖已存在的文件
      * 调用方（界面）必须先跟用户确认
    """
    src = BACKUP_DIR / name
    if not src.is_file():
        return {"ok": False, "reason": "找不到这个备份"}

    # 安全：zip 里不能有跳出 saves/ 的路径
    with zipfile.ZipFile(src) as z:
        for member in z.namelist():
            dest = (SAVE_DIR.parent / member).resolve()
            if not str(dest).startswith(str(SAVE_DIR.parent.resolve())):
                return {"ok": False, "reason": f"备份里有异常路径：{member}"}
        if not overwrite:
            clash = [m for m in z.namelist()
                     if (SAVE_DIR.parent / m).is_file()]
            if clash:
                return {"ok": False, "need_confirm": True,
                        "reason": f"会覆盖 {len(clash)} 个现有文件"}

        z.extractall(SAVE_DIR.parent)

    return {"ok": True, "name": name}


def info() -> dict:
    """给界面用：备份数量、最近一次、目录位置。"""
    items = list_backups()
    return {
        "dir": str(BACKUP_DIR),
        "count": len(items),
        "latest": items[0] if items else None,
        "items": items[:8],
        "keep": KEEP,
    }


if __name__ == "__main__":
    import json
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(json.dumps(info(), ensure_ascii=False, indent=2))
