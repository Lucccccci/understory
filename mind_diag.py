"""静默失败的登记簿。

## 为什么要这个东西

2026-10-16 一天之内踩到**三次**同一个坑：

    ① mind_event.summarize    `Path` 没导入 -> 事件改写静默退回原文
    ② server 开场白           `_RUN_SEED` 没导入 -> focus 静默为空
                             （症状：开场白又变回「老说同一件事」）
    ③ server 返回时间戳       `datetime` 没导入 -> HTTP 500（这个至少报了）

**①② 都被 `except Exception: pass` 吞了。** 扫了一遍整个项目：
134 个 except 块里有 **74 个不引用异常、块体为空**。

## 但「都改掉」是错的

那些 except 大多是**对的**：

    · 读存档失败 -> 用默认值（读不到不该让她变哑巴）
    · 探测可选依赖 -> 没有就降级
    · 清理旧文件 -> 删不掉就算了
    · 读配置 -> 坏了用默认

**去掉它们会让程序一有风吹草动就崩。**

真正要修的不是「捕获」，是「**捕获之后没人知道**」。
所以这个模块只做一件事：**把静默捕获记下来**，
让「功能不生效」和「什么都没发生」不再长得一样。

## 怎么用

    import mind_diag

    try:
        ...
    except Exception:
        mind_diag.note("同步游戏关系")     # 一行，不改控制流

或者当装饰器：

    @mind_diag.guard("读存档")
    def read_game_context(...):
        ...

## 记在哪

    saves/diagnostics.log

每行是 `时间 | 位置 | 次数 | 异常`。
**同样的失败只记第一次和计数** —— 否则一个每轮都失败的地方
会把日志刷爆，反而看不见。
"""

from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "saves" / "diagnostics.log"

# 同一个位置的失败记一次就够。之后只累计次数。
_seen: dict[str, int] = {}
# 本次运行总共吞了多少次 —— 启动日志里报一下
_total = 0


def note(what: str, exc: BaseException | None = None) -> None:
    """记一次静默失败。**不要**在这里抛异常 —— 它的用途就是兜住。

    ## 绝不冒泡

    这一行被插进 8 个 except 块的开头。要是它自己抛了，
    「兜底」就变成了「新的崩溃源」—— 那比原来更糟。
    所以整个函数体都在 try 里，任何问题都咽掉。

    Args:
        what: 在做什么。写得具体点，「同步游戏关系」比「sync」有用得多。
        exc: 异常对象。不给就用当前正在处理的异常（`sys.exc_info`）。
    """
    global _total
    try:
        _total += 1

        if exc is None:
            exc = __import__("sys").exc_info()[1]
        if exc is None:
            return

        tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        # 找调用方那一帧 —— note() 自己的帧没意义
        where = "?"
        for fr in reversed(tb):
            if fr.filename.endswith("mind_diag.py"):
                continue
            where = f"{Path(fr.filename).name}:{fr.lineno}"
            break

        key = f"{where} | {what}"
        _seen[key] = _seen.get(key, 0) + 1
        if _seen[key] != 1:
            return                  # 只写第一次，之后靠 summary() 报次数

        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} | "
                    f"{where} | {what} | {type(exc).__name__}: {exc}\n")
            for line in traceback.format_exception(type(exc), exc, exc.__traceback__)[1:]:
                f.write("    " + line.rstrip() + "\n")
    except BaseException:
        # 连记都记不了，那就只能算了。**绝不能让它冒泡。**
        pass


def guard(what: str, default=None):
    """装饰器版：失败就返回 default，并记一笔。

        @guard("读存档", default=None)
        def read_game_context(...): ...
    """
    def deco(fn):
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:
                note(what, e)
                return default
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        return wrapper
    return deco


def summary() -> dict:
    """本次运行吞了些什么。启动时打印，让人知道有事被咽下去了。"""
    return {"total": _total, "kinds": len(_seen),
            "items": sorted(_seen.items(), key=lambda x: -x[1])}


def tail(n: int = 20) -> list[str]:
    """读日志尾部 —— 界面或命令行里看。"""
    if not LOG.is_file():
        return []
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:]


if __name__ == "__main__":
    print("=" * 66)
    print("  静默失败登记簿")
    print("=" * 66)
    print(f"  日志 {LOG}")
    print(f"  存在 {LOG.is_file()}"
          + (f"  （{LOG.stat().st_size:,} B）" if LOG.is_file() else ""))
    print()
    rows = tail(30)
    if not rows:
        print("  （还没有记录 —— 这是好事）")
    else:
        for ln in rows:
            print("  " + ln)
