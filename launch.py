#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器：已在运行就把窗口调到前面，没运行才启动。

## 为什么需要这个

快捷方式会被反复双击。如果每次都起一个新实例：
    * 第二个实例会因端口被占而报错退出
    * 更糟的是，两个实例各有一份内存中的角色状态，可能互相覆盖存档

所以先探测端口：活着就只打开浏览器（把已有的窗口切到前面），
死了才真正启动。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "chat_app.py")

# 与 chat_app.py 的默认端口保持一致
DEFAULT_PORT = 8731
PORT_RANGE = 20


def find_running_port() -> int | None:
    """在可能的端口范围内找一个已在监听的实例。"""
    for offset in range(PORT_RANGE):
        port = DEFAULT_PORT + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return port
    return None


def main() -> None:
    port = find_running_port()

    if port is not None:
        url = f"http://127.0.0.1:{port}/"
        print(f"她已经在运行，打开窗口：{url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return

    # 没在运行 —— 交给 chat_app.py 自己找端口并启动
    print("正在启动…")
    kwargs = {}
    if os.name == "nt":
        # 不要再弹出一个新控制台窗口
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    subprocess.Popen([sys.executable, APP], cwd=HERE, **kwargs)

    # 等它就绪，然后开浏览器（chat_app.py 也会自己开一次，重复开无害）
    for _ in range(40):
        time.sleep(0.25)
        port = find_running_port()
        if port is not None:
            print(f"已启动：http://127.0.0.1:{port}/")
            return
    print("启动超时，请手动检查 chat_app.py 是否能运行。")


if __name__ == "__main__":
    main()
