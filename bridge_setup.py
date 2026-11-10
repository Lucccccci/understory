#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 桥接配置向导 —— 跑一下就好，不用手改 JSON。

    python bridge_setup.py

## 为什么需要它

`bridge_config.json` 里有一堆以下划线开头的注释字段，手改很容易：
    * 漏逗号 / 多逗号（JSON 直接报错，但报错信息不好懂）
    * 把注释字段也改了
    * 数字填成字符串（"123456" 和 123456 在某些地方行为不同）
    * 两个号填反（这个最要命 —— 她会回应错误的人）

这个向导只问你**该问的**，其余一律不动，写之前还会备份。

## 它做什么

    1. 问你大号 QQ（只有这个号能跟她说话）
    2. 问你机器人小号 QQ（用于核对有没有填反）
    3. 检查桌面服务在不在跑
    4. 备份旧配置 -> 写新配置
    5. 自检：把每一项念给你听
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "bridge_config.json"
CHAT_SERVER = "http://127.0.0.1:8731"


# ─────────────────────────────────────────────────────────────
def ask_qq(prompt: str, allow_blank: bool = False) -> int | None:
    """问一个 QQ 号。QQ 号是 5-12 位纯数字。"""
    while True:
        raw = input(prompt).strip().replace(" ", "").replace("　", "")
        if not raw and allow_blank:
            return None
        if not raw:
            print("    （不能留空）")
            continue
        if not re.fullmatch(r"\d{5,12}", raw):
            print("    （QQ 号应当是 5-12 位纯数字，再试一次）")
            continue
        return int(raw)


def check_server() -> tuple[bool, str]:
    """看桌面服务在不在。它不在的话 QQ 收不到回复。"""
    try:
        with urllib.request.urlopen(f"{CHAT_SERVER}/api/status", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        stage = d.get("stage_name") or "?"
        hearts = d.get("game_hearts")
        has_key = d.get("has_key")
        bits = [f"阶段 {stage}"]
        if hearts is not None:
            bits.append(f"{hearts} 心")
        bits.append("密钥 已设置" if has_key else "密钥 未设置")
        return True, "，".join(bits)
    except Exception as e:
        return False, str(e)


def load_existing() -> dict:
    if CONFIG.is_file():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def backup() -> Path | None:
    """写之前先备份。改坏了能退回来。"""
    if not CONFIG.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = CONFIG.with_name(f"bridge_config.{stamp}.json.bak")
    shutil.copy2(CONFIG, dst)
    return dst


def main() -> None:
    print("=" * 68)
    print("  QQ 桥接配置向导")
    print("=" * 68)
    print()
    print("  这个向导只改 bridge_config.json，不动别的文件。")
    print("  写之前会自动备份。")
    print()

    old = load_existing()
    old_owner = old.get("owner_qq") or 0
    was_enabled = bool(old.get("enabled"))
    if old_owner:
        print(f"  检测到已有配置：owner_qq = {old_owner}"
              f"（{'已启用' if was_enabled else '未启用'}）")
        keep = input("  要保留这个值吗？[Y/n] ").strip().lower()
        if keep in ("", "y", "yes", "是"):
            owner = int(old_owner)
            print(f"  沿用 {owner}")
        else:
            owner = ask_qq("  你的大号 QQ（只有这个号能跟她说话）：")
    else:
        owner = ask_qq("  你的大号 QQ（只有这个号能跟她说话）：")

    print()
    print("  接下来问机器人小号 —— 用来核对有没有两个号填反。")
    print("  （不想填就回车跳过，不影响使用）")
    bot = ask_qq("  机器人小号 QQ（登录 NapCat 的那个）：", allow_blank=True)

    if bot and bot == owner:
        print()
        print("  !! 两个号填成一样了。")
        print("     机器人和你必须是两个不同的号 —— 否则她会被自己的消息触发。")
        return

    # ── 检查桌面服务 ────────────────────────────────
    print()
    print("  正在检查桌面服务…")
    ok, msg = check_server()
    if ok:
        print(f"  ✓ 桌面服务在线（{msg}）")
    else:
        print(f"  !! 连不上桌面服务：{msg}")
        print("     这不影响写配置，但**用之前必须先启动桌面窗口** ——")
        print("     QQ 里的消息要靠它来生成回复。")
        go = input("     仍然继续写配置吗？[Y/n] ").strip().lower()
        if go not in ("", "y", "yes", "是"):
            print("     已取消。")
            return

    # ── 写配置 ──────────────────────────────────────
    bak = backup()
    if bak:
        print()
        print(f"  已备份原配置 -> {bak.name}")

    # 只动该动的字段，其余保持原样
    cfg = dict(old) if old else {}
    cfg.setdefault("_说明", "OneBot 11 桥接配置。改完需要重启 bridge.py。只有 owner_qq 是必填的。")
    cfg["owner_qq"] = owner
    cfg["_owner_qq说明"] = "你自己的 QQ 号（纯数字）。只有这个号能跟她说话。"
    cfg["enabled"] = True
    cfg["_enabled说明"] = "填好 owner_qq 后改成 true。"
    cfg.setdefault("chat_server", CHAT_SERVER)
    cfg.setdefault("_chat_server说明", "本机对话服务地址。桌面版跑着的时候它才在线。")
    cfg.setdefault("token", "")
    cfg.setdefault("_token说明", "OneBot 的 access_token。NapCat 里设了就要填一样，没设就留空。")

    # 反向 HTTP：最简单，默认开
    rh = dict(cfg.get("reverse_http") or {})
    rh.update({
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8732,
        "_说明": "反向 HTTP：NapCat 主动把消息 POST 到我们这里。"
                 "NapCat 的「HTTP 服务器」地址填 http://127.0.0.1:8732",
    })
    cfg["reverse_http"] = rh

    fw = dict(cfg.get("forward_ws") or {})
    fw.update({
        "enabled": False,
        "url": "ws://127.0.0.1:3001",
        "api_url": "http://127.0.0.1:3000",
        "_说明": "正向 WebSocket：我们主动连 NapCat。"
                 "url=收事件的 WS 地址，api_url=发消息的 HTTP 地址",
    })
    cfg["forward_ws"] = fw

    bh = dict(cfg.get("behavior") or {})
    bh.setdefault("reply_in_group_only_when_at", True)
    bh.setdefault("max_reply_chars", 500)
    bh.setdefault("typing_delay_ms", [800, 2200])
    bh.setdefault("reply_on_error", "（她好像走神了，没听清。再说一遍？）")
    bh["_说明"] = ("reply_in_group_only_when_at: 群里只有 @ 她才回。"
                   "typing_delay_ms: 发送前随机等待，像真人打字。")
    cfg["behavior"] = bh

    if bot:
        # 手填兜底：万一 NapCat 的 get_login_info 读不到，
        # 群里判断「有没有 @ 我」需要这个号（实测踩过）
        cfg["self_qq"] = bot
        cfg["_self_qq说明"] = ("机器人自己的 QQ。用于判断群里有没有 @ 她。"
                               "正常会自动读到，这里是兜底。")

    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

    # ── 自检 ────────────────────────────────────────
    print()
    print("=" * 68)
    print("  配置已写入 —— 请核对")
    print("=" * 68)
    chk = json.loads(CONFIG.read_text(encoding="utf-8"))
    print(f"  文件            {CONFIG}")
    print(f"  你（主人）       {chk['owner_qq']}")
    if bot:
        print(f"  机器人小号       {bot}")
    print(f"  启用            {chk['enabled']}")
    print(f"  对话服务         {chk['chat_server']}")
    print(f"  接收方式         反向 HTTP :{chk['reverse_http']['port']}")
    print(f"  打字延迟         {chk['behavior']['typing_delay_ms']} 毫秒")
    print()
    print("  下一步：")
    print("    1) 装 NapCat（用可视化管理工具最省事）")
    print("    2) 在 NapCat 里登录**小号**")
    print("    3) NapCat 里加一个「HTTP 服务器」，地址填 "
          f"http://127.0.0.1:{chk['reverse_http']['port']}，勾上「上报事件」")
    print("    4) 先启动桌面窗口，再跑 python bridge.py")
    print()
    print("  详细步骤见 QQ接入说明.md")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  已取消。")
        sys.exit(1)
