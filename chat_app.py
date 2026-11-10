#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「mind · 内在生活」—— 与这座镇子 mod **完全独立**的独立对话程序。

## 与 mod 的关系

**代码完全独立**，但**共用同一份人设与语料资产**：
    ../tools/prompts/mind_persona.json
    ../tools/corpus/mind_corpus.json
    ../tools/corpus/stage_examples.json

这样做的理由：人设是她这个人的定义，改一次两边都变，不会出现
「游戏里的她」和「窗口里的她」性格不一致。

## 为什么是本地网页而不是 tkinter

    * 能正常显示立绘（tkinter 做图片排版很别扭）
    * 滚动聊天记录、状态面板、输入框都很自然
    * 零第三方依赖：http.server 是标准库，浏览器人人都有

## 启动

    python chat_app.py
    （会自动打开浏览器窗口）

首次运行若环境变量 MIND_AI_API_KEY 未设置，程序会在窗口里询问。
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from server import ChatHandler  # noqa: E402

DEFAULT_PORT = 8731


def find_port(start: int, tries: int = 20) -> int:
    """找一个可用的端口。默认端口被占就往后试。

    ⚠️ 不能只用 SO_REUSEADDR + bind 判断 —— Windows 上这个选项
    允许绑定**已被占用**的端口，于是探测永远成功、两个进程抢一个口。
    所以先 connect 试一下：连得上就说明有人在服务。
    """
    import socket
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.35)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                continue                      # 有人在用
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"从 {start} 起找了 {tries} 个端口都被占用，请手动指定。")


def main() -> None:
    port = find_port(DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 68)
    print("  mind · 内在生活")
    print("=" * 68)
    print(f"  窗口地址: {url}")
    print("  关闭本窗口（或按 Ctrl+C）即退出程序。")
    print()

    # 启动时备份存档。每天最多一份，见 mind_backup.py 的说明。
    # 放在这里而不是 server 里，是因为**启动这一刻**是最干净的时点：
    # 还没有任何一轮对话在写盘，备下来的一定是完整状态。
    try:
        import mind_backup
        r = mind_backup.run()
        if r.get("ok") and not r.get("skipped"):
            print(f"  存档备份: {r['name']}（{r['files']} 个文件，"
                  f"{r['size'] / 1024:.1f} KB）")
            if r.get("removed"):
                print(f"            已清理 {r['removed']} 份过旧的备份")
        elif r.get("skipped"):
            print("  存档备份: 今天已备过，跳过")
        else:
            print(f"  存档备份: 未执行 —— {r.get('reason', '')}")
    except Exception as e:                       # 备份失败不该挡住启动
        print(f"  存档备份: 出错（不影响使用）—— {e}")

    # 记忆快照 —— 每天第一份。
    #
    # 和上面 mind_backup 的分工：
    #     mind_backup    备**整份 saves**（含 backups 自身），粒度粗、用途是灾备
    #     mind_snapshot  只备**会话**，能列出来、能挑一份回滚 —— 用途是误操作兜底
    #
    # 为什么需要后者：用户 2026-10-16 误触「重新开始」，一瞬间丢掉
    # 134 轮对话、6 条观察、2 个事件，只靠一个孤零零的 .bak 才救回来。
    # 而且那次之后「重新开始」也改成先自动打快照了。
    try:
        import mind_snapshot
        r = mind_snapshot.auto_daily()
        if r and r.get("ok"):
            s = r.get("stats") or {}
            print(f"  记忆快照: {r['name']}"
                  f"（{s.get('turns', 0)} 轮 / {s.get('events', 0)} 事件 / "
                  f"{s.get('observations', 0)} 观察）")
        elif r is None:
            print("  记忆快照: 今天已打过，跳过")
    except Exception as e:                       # 快照失败也不该挡住启动
        print(f"  记忆快照: 出错（不影响使用）—— {e}")

    # 静默失败登记簿 —— 上次运行有没有东西被咽下去。
    #
    # 起因：2026-10-16 一天之内三次「功能静默失效」，
    # 全都是 `except Exception: pass` 把真错误吞了：
    #     · 事件改写一直退回原文（Path 没导入）
    #     · 开场白的 focus 一直是空（_RUN_SEED 没导入）
    # 症状都是「不生效但没有任何报错」。
    #
    # 这里只**报**，不修 —— 修要靠人看日志。但至少不再是无声的。
    try:
        import mind_diag
        rows = mind_diag.tail(4)
        if rows:
            print("  静默失败: 上次运行有东西被咽下去了（详见 saves/diagnostics.log）")
            for ln in rows:
                if not ln.startswith("    "):    # 只看首行，traceback 太占地方
                    print(f"            {ln[:110]}")
        else:
            print("  静默失败: 无")
    except Exception:
        pass
    print()

    # 密钥状态 —— **问真正读取它的那个函数**。
    #
    # ## 原来这里是错的（2026-10-16 修）
    #
    #     key = os.environ.get("MIND_AI_API_KEY")
    #     if key: print("已从环境变量读取")
    #     else:   print("未设置 —— 程序会在窗口里询问你")
    #
    # 它只看环境变量，但实际的读取顺序是：
    #     `.apikey` 文件 -> `MIND_AI_API_KEY` -> 窗口里问
    # （见 `resolve_api_key`）
    #
    # 于是**每一次启动都谎报「未设置」**，即使 key 早就存好了。
    # 代价是你会以为自己没配、然后白填一遍。
    try:
        from server import resolve_api_key, KEY_FILE
        k = resolve_api_key()
        if k:
            where = (".apikey 文件" if KEY_FILE.is_file() and
                     KEY_FILE.read_text(encoding="utf-8").strip() == k
                     else "环境变量 MIND_AI_API_KEY")
            print(f"  API 密钥: 已就绪（来自{where}，{k[:9]}…{k[-4:]}）")
        else:
            print("  API 密钥: 未设置 —— 程序会在窗口里询问你")
    except Exception as e:
        print(f"  API 密钥: 检查时出错（不影响使用）—— {e}")
    print()

    ChatHandler.base_dir = HERE
    ChatHandler.port = port

    server = ThreadingHTTPServer(("127.0.0.1", port), ChatHandler)

    # 设 MIND_NO_BROWSER=1 就不自动打开浏览器。
    #
    # 为什么需要：重启服务时它会自己弹出一个窗口 —— 如果桌面上有人，
    # 那个窗口就当着人弹出来了。后台重启（部署、测试）必须能关掉它。
    if os.environ.get("MIND_NO_BROWSER"):
        print(f"  浏览器未自动打开（MIND_NO_BROWSER=1）：{url}")
    else:
        try:
            webbrowser.open(url)
        except Exception:
            print(f"  无法自动打开浏览器，请手动访问 {url}")

    # 日记巡检：道过晚安、安静 12 分钟才写（2026-10-29）
    try:
        import server as _srv
        _srv.watch_diary()
    except Exception as _e:
        print(f'  日记巡检未启动：{_e}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在退出…")
    finally:
        server.shutdown()
        server.server_close()

        # 退出时再备一次。
        #
        # 为什么不能只靠启动时那次：启动备的是**昨天的**状态。
        # 实测踩过 —— 启动时 6 轮，聊到 29 轮、加了收藏与承诺，
        # 这些一整天都不在备份里，要等明天启动才补上。
        # 万一今晚存档坏了，今天白做。
        #
        # 去重交给 mind_backup：内容和最近一份相同就跳过，
        # 所以反复开关程序不会堆出一堆一模一样的备份。
        try:
            import mind_backup
            r = mind_backup.run()
            if r.get("ok") and not r.get("skipped"):
                print(f"  退出前备份: {r['name']}（{r['files']} 个文件，"
                      f"{r['size'] / 1024:.1f} KB）")
            elif r.get("skipped"):
                print("  退出前备份: 无变化，跳过")
        except Exception as e:
            print(f"  退出前备份失败（不影响存档）—— {e}")


if __name__ == "__main__":
    main()
