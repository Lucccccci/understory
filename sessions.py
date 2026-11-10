#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话会话管理：按「和谁说话」分开存记忆。

## 为什么需要这个

桌面版只有一个用户（你），所以状态是单份的。
接进 QQ 之后会有多个来源（不同 QQ 号、群、桌面窗口），
如果共用一份状态，就会出现：
    * 你朋友说的话被记成你说的
    * 她在你朋友面前表现得像和你交往 9 心一样

所以每个来源一份独立存档：`saves/sessions/<键>.json`。

## 「关系」不隔离

注意一个刻意的设计：**关系阶段来自游戏存档，那是全局的**。
所以：

    * 你（主人）→ 关系取你存档里的（交往 9 心）
    * 其他人   → 没有对应存档，一律从「初识」开始

这让人设保持自洽：她对你和对陌生人的态度本来就应该不同。

## ⚠️ 两种风格的教训（2026-10-17）

这个模块原来**混着两种风格**：`store` 是唯一入口，但有些辅助函数
是 `SessionStore` 的方法、有些是模块级函数。写代码时看不出区别，
于是我连续踩了三次：

    写成 session_store.get_session_lock(...)   -> AttributeError，对话 500
    写成 session_store.make_test_baseline()    -> AttributeError，端点 500

**所以现在的规矩：凡是操作 `store` 的东西，一律是它的方法。**
只有真正与实例无关的（`_safe_name`、`get_session_lock` 的锁表）
留在模块级。
"""

from __future__ import annotations

import json
import re
import sys
import shutil
import threading
from datetime import datetime
from pathlib import Path

from mind_character import Character

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "saves" / "sessions"
LEGACY_FILE = HERE / "saves" / "legacy.json"

# 桌面窗口用的固定键
DESKTOP_KEY = "desktop"

# **AI 自己的测试档**（2026-10-19 加）。
#
#     desktop  用户的真实记忆，永远不许被测试碰
#     test     用户自己的调试档（界面上那个「测试档」开关）
#     probe    AI 的测试档
#
# 为什么要分出来：先前 AI 用 `test` 做验证，把用户的调试档写脏两次。
# 「测试用测试档」这条规矩缺了主语 —— **谁的**测试档。
SCRATCH_KEY = "probe"

# 测试档的基线 —— 从真实档复制、清掉对话轮次的那一份。
# 「回到基线」就是把它拷回 test.json。见 `make_test_baseline` 的说明。
BASELINE_FILE = HERE / "saves" / "test_baseline.json"

_SAFE = re.compile(r"[^0-9A-Za-z_\-]")


def _safe_name(key: str) -> str:
    """把任意来源标识压成安全文件名。

    QQ 号本来就是数字，但群号、频道 ID 可能带奇怪字符。
    """
    cleaned = _SAFE.sub("_", key.strip())[:64]
    return cleaned or "unknown"


class SessionStore:
    """会话存档的读写。

    刻意不做缓存 —— 每个会话的状态就几 KB，直接读写文件更简单，
    也避免多个来源并发时内存与磁盘不一致。
    """

    def __init__(self, directory: Path = SESSIONS_DIR):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── 基本读写 ────────────────────────────────────────
    def path_of(self, key: str) -> Path:
        return self.dir / f"{_safe_name(key)}.json"

    def load(self, key: str) -> Character:
        p = self.path_of(key)

        # 兼容旧的单份存档：桌面会话第一次用新结构时，把旧档迁过来
        if not p.is_file() and key == DESKTOP_KEY and LEGACY_FILE.is_file():
            try:
                LEGACY_FILE.replace(p)
            except OSError:
                pass

        # probe 档：第一次用时从基线复制一份。
        # 空 Character 也能跑，但它没有观察/承诺/事件，
        # 测不出真实路径（那些正是最容易坏的地方）。
        if not p.is_file() and key == SCRATCH_KEY and BASELINE_FILE.is_file():
            try:
                shutil.copy2(BASELINE_FILE, p)
            except OSError:
                pass

        ch = Character.load(str(p))
        # 打上「这个角色是从哪个会话读出来的」—— 写的时候要核对。
        # 用普通属性而不是 dataclass 字段：它**不该被序列化**，
        # 每次 load 重新算，也不该出现在存档 JSON 里。
        try:
            ch._origin_key = key
        except Exception:
            pass
        return ch

    def save(self, key: str, ch: Character, force: bool = False) -> None:
        """把角色存回指定会话。**写之前先拦一种情况：用空档盖掉有内容的档。**

        ## 为什么这道闸必须有

        实测发生过两次（2026-10-18 凌晨）：

            test.json    377 轮 -> 0 轮
            desktop.json 377 轮 -> 0 轮

        而 `first_met` 变成了基线的值 —— 说明是用「只有她、没有对话」的
        那份角色把真实档盖掉了。**没有任何正常流程会这么做。**

        代价是不对称的：空档写进去毫无价值，被盖掉的 377 轮只能靠备份捞。

        ## 拦的判据刻意窄

        **只拦**「盘上已有 turns > 0，而这次要写的是 0 轮」。

        别的减少（重做一轮少两条、重生成）都合法，不能拦 ——
        拦了会误伤，而误伤会让人把这道闸删掉，那就彻底没有保护了。

        Args:
            key: 会话键。
            ch: 要写的角色。
            force: 真要清空时才传 True。见模块说明的「逃逸口」。

        Raises:
            RuntimeError: 当这次写入会用空档盖掉有内容的档。
        """
        target = self.path_of(key)

        # ── 身份校验（2026-10-18 加，因为「内容校验」拦不住跨会话覆盖）──
        self._check_identity(key, ch, target, force)

        if not force and not (ch.turns or []):
            existing = self._turns_on_disk(target)
            if existing > 0:
                msg = (f"[存档守卫] 拒绝写入：{key} 盘上有 {existing} 轮，"
                       f"而这次要写 0 轮。可能是空档覆盖。已拦下，未写盘。")
                print(msg, file=sys.stderr, flush=True)
                try:
                    import mind_diag
                    mind_diag.note(f"存档守卫拦下一次空档覆盖（{key}，"
                                  f"盘上 {existing} 轮）",
                                  RuntimeError(msg))
                except Exception:
                    pass
                raise RuntimeError(msg)
        ch.save(str(target))

    def _check_identity(self, key: str, ch: Character, target: Path,
                        force: bool) -> None:
        """确认「这个角色」确实是「这个会话」的。不过就抛。

        ## 两道判据

            ① 出生会话   ch._origin_key 必须 == key
            ② 出生时间   盘上已有 first_met 且轮次 > 0 时，必须 == ch.first_met

        ## 为什么要有②（① 已经够了）

        ① 依赖 `load()` 打过标签。如果有代码用 `Character.load(path)` 直接
        读（绕开 store），① 就失效。② 只看数据本身，**绕不过去** ——
        同一个角色的 first_met 永远不会变。

        ## 为什么记调用者

        光说「拒了一次写」没用。要知道**是哪一行代码**想这么干，
        否则下次还是查半天（这次就是查了半天）。
        """
        import traceback

        problems: list[str] = []

        # ① 出生会话
        origin = getattr(ch, "_origin_key", None)
        if not force and origin and origin != key:
            problems.append(f"角色出生在会话 {origin!r}，却要写进 {key!r}")

        # ② 出生时间
        disk = self._meta_on_disk(target)
        disk_turns, disk_first = disk
        if disk_turns > 0 and disk_first and ch.first_met and disk_first != ch.first_met:
            problems.append(
                f"盘上 first_met={disk_first}（{disk_turns} 轮），"
                f"手里 first_met={ch.first_met} —— 不是同一个角色")

        if not problems:
            return

        # 找到调用者（跳过本方法自己、save、以及 save_character）
        where = "?"
        try:
            frames = traceback.extract_stack()
            for fr in reversed(frames[:-1]):
                if fr.name in ("_check_identity", "save"):
                    continue
                where = f"{Path(fr.filename).name}:{fr.name}:{fr.lineno}"
                break
        except Exception:
            pass

        msg = (f"[存档守卫] 拒绝写入 {key}：{'；'.join(problems)}。"
               f"调用者 {where}")
        print(msg, file=sys.stderr, flush=True)
        try:
            import mind_diag
            mind_diag.note(msg, RuntimeError(msg))
        except Exception:
            pass
        raise RuntimeError(msg)

    @staticmethod
    def _meta_on_disk(path: Path) -> tuple[int, str]:
        """盘上那份的 (轮次, first_met)。读不出来返回 (0, "")。"""
        import json as _json
        if not path.is_file():
            return 0, ""
        try:
            d = _json.loads(path.read_text(encoding="utf-8"))
            return len(d.get("turns") or []), str(d.get("first_met") or "")
        except (OSError, ValueError):
            return 0, ""

    @staticmethod
    def _turns_on_disk(path: Path) -> int:
        """盘上那份有几轮。文件不存在/读不出来都返回 0（= 不拦）。"""
        import json as _json
        if not path.is_file():
            return 0
        try:
            d = _json.loads(path.read_text(encoding="utf-8"))
            return len(d.get("turns") or [])
        except (OSError, ValueError):
            # 读不出来就别拦 —— 那种情况 Character.load 有自己的隔离逻辑，
            # 这里再插一手会让「坏档被隔离」的路径变得不可预测。
            return 0

    def exists(self, key: str) -> bool:
        return self.path_of(key).is_file()

    def list_keys(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    # ── 测试档基线 ──────────────────────────────────────
    #
    # ## 为什么需要
    #
    # 测试档原来是「随手用、随手清」，于是每次状态都不同：
    # 有时带着真实档的全部记忆，有时空，有时只剩三轮。
    # **测出来的结论不可复现** —— 上一次成立、这一次不成立，
    # 而且分不清是代码变了还是环境变了。
    #
    # 实测为此浪费过时间：在测试档里得到「她聊得来当代话题」的结论，
    # 但那一版的测试档其实带着真实档的 169 轮底子，
    # 所以那个结论**证明不了卡片改动有效**。
    #
    # ## 基线是什么
    #
    #     保留   观察、事件、承诺、收藏、记忆、心情
    #            —— 这些是「她是谁」，去掉就换了个人
    #     清掉   对话轮次、开场白
    #            —— 这是「这次的上下文」，不清就会互相污染
    #
    # 于是测试档 = **一个有真实阅历的她，但今天刚开始跟你说话**。
    # 测卡片改动时，看到的就是卡片的效果，不是历史的惯性。

    def make_test_baseline(self) -> dict:
        """从**真实档**重建测试基线。"""
        real = self.path_of(DESKTOP_KEY)
        if not real.is_file():
            return {"ok": False, "error": "真实档不存在，做不了基线"}

        try:
            d = json.loads(real.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "error": f"真实档读不出来：{e}"}

        d["turns"] = []
        d["total_turns"] = 0
        d["first_met"] = datetime.now().isoformat(timespec="seconds")
        d["last_talk_at"] = ""
        d["last_talk_date"] = ""
        d["greeting"] = ""
        d["greeting_seed"] = ""

        BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        return {"ok": True, "kept": {
            "observations": len(d.get("observations") or []),
            "events": len(d.get("events") or []),
            "promises": len(d.get("promises") or []),
            "favorites": len(d.get("favorites") or []),
            "memories": len(d.get("memories") or []),
        }}

    def reset_to_baseline(self) -> dict:
        """把测试档恢复成基线。基线不存在就先按真实档做一份。"""
        if not BASELINE_FILE.is_file():
            r = self.make_test_baseline()
            if not r.get("ok"):
                return r

        dst = self.path_of("test")
        try:
            shutil.copy2(BASELINE_FILE, dst)
        except OSError as e:
            return {"ok": False, "error": str(e)}

        d = json.loads(dst.read_text(encoding="utf-8"))
        return {"ok": True, "turns": len(d.get("turns") or []),
                "observations": len(d.get("observations") or [])}


# ── 会话级锁（2026-10-17 加）────────────────────────────
#
# ## 要解决什么
#
# `_api_chat` 的流程是「读盘 -> 改十几次 -> 写盘」，中间夹着一次
# LLM 调用（2-5 秒），**而全程没有锁**。两个请求同时进来就会：
#
#     请求 A 读盘 -> N 轮        请求 B 读盘 -> N 轮
#     A 加一轮 -> N+1            B 加一轮 -> N+1
#     A 写盘 -> N+1              B 写盘 -> N+1     ← A 那一轮没了
#
# 表现是「我明明说了，她不记得」。
#
# ## 为什么粒度是「会话」而不是「全局」
#
# 全局锁会让桌面的请求阻塞 QQ 的 —— 而一次聊天要占 2-5 秒以上。
# 不同会话之间本来就不共享任何状态，没理由互相等。
#
# ## 用在哪里
#
# **必须包住整个「读-改-写」，不是只包住 save。**
# 只包 save 是没用的 —— 丢失更新发生在读的时候。
#
# ## ⚠️ 它在模块级，不在类里
#
# 锁表是所有实例共用的，和具体目录无关。调用时是 `get_session_lock(k)`，
# **不是** `store.get_session_lock(k)` —— 后者会 AttributeError。
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def get_session_lock(key: str) -> threading.Lock:
    """取某个会话的锁。没有就建一个。

    返回值给调用方 `with` 用：
        with get_session_lock(session_key):
            ch = get_character(session_key)
            ...
            save_character(session_key, ch)
    """
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _locks[key] = lk
        return lk


# 模块级单例，供 server.py 用
store = SessionStore()
