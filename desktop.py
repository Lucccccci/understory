"""桌面窗口启动器（2026-11-10）

同一个网页，套一个系统窗口 —— 没有地址栏、没有标签页，像一个 App。

    python desktop.py            开窗口
    python desktop.py --browser  用默认浏览器打开
    python desktop.py --headless 只跑服务

## 三种窗口来源（自动挑，按顺序）

  1. pywebview       装了就用它 —— 最像原生 App（可置顶、可做托盘）
  2. Edge / Chrome 的 --app 模式
                    没装 pywebview 也能有"无地址栏窗口"，
                    代价：窗口后面还有一个浏览器进程
  3. 默认浏览器      前面两条都不行时兜底

服务跑在子进程里；端口从子进程的输出里读（不再假设 8731 ——
find_port 在别的实例占着 8731 时会往后挪）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 放在临时目录里：AppData 下新建目录被权限挡住（WinError 5），
# 而 %TEMP% 可写，且实测 Edge 在那里建 profile 渲染正常。
import tempfile as _tempfile

_BASE = Path(_tempfile.gettempdir()) / "mind-app"
WIN_CONF = _BASE / "window.json"
DEFAULT_SIZE = (1180, 760)
PORT_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def _load_win() -> dict:
    try:
        return json.loads(WIN_CONF.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_win(d: dict) -> None:
    try:
        WIN_CONF.parent.mkdir(parents=True, exist_ok=True)
        WIN_CONF.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    except OSError:
        pass


def _alive(port: int, timeout: float = 0.6) -> bool:
    try:
        # 必须带会话头：不带的话服务会把它当"无头请求"，
        # 写入被改写到 test 档、再被身份守卫拒掉 —— diagnostics 里一串红字
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/status",
            headers={"X-Mind-Session": "desktop"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class Server:
    """起一个 chat_app 子进程，并从它的输出里抓端口。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self._lines: list[str] = []

    def start(self) -> None:
        env = dict(os.environ)
        env["MIND_NO_BROWSER"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "chat_app.py")],
            cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._lines.append(line.rstrip())
            if self.port is None:
                m = PORT_RE.search(line)
                if m:
                    self.port = int(m.group(1))
            print("[mind]", line.rstrip(), flush=True)

    def wait(self, seconds: int = 45) -> bool:
        t0 = time.time()
        while time.time() - t0 < seconds:
            if self.port and _alive(self.port):
                return True
            if self.proc and self.proc.poll() is not None:
                return False
            time.sleep(0.25)
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def find_browser() -> str | None:
    if sys.platform == "win32":
        # 用注册表里的 Edge/Chrome 也行，这里先看常见路径
        for p in BROWSERS:
            if os.path.isfile(p):
                return p
        return None
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    for name in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _profile_busy(prof: str) -> bool:
    """有没有已经跑着的浏览器在用这个 profile 目录。

    同一个 --user-data-dir 被占用时，Edge 会把 URL 交给旧实例、
    新窗口留白 —— 这就是"开出来是一块白"的原因。
    """
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return False
    return prof.lower() in out.lower()


def open_app_window(url: str, w: int, h: int) -> bool:
    """用 Edge/Chrome 的 --app 模式开一个无地址栏窗口。"""
    exe = find_browser()
    if not exe:
        return False
    # ⚠️ profile 不能放在项目目录里：工作区带「低完整性」标签，
    # Edge 在那种目录下建 profile 会渲染失败 —— 窗口一片空白。
    # 也不能放 %LOCALAPPDATA%：这个环境里建目录被拒（WinError 5）。
    # %TEMP% 两条都不犯。
    prof = str(_BASE / "appprofile")
    Path(prof).mkdir(parents=True, exist_ok=True)
    if _profile_busy(prof):
        # 该 profile 已被占用 → 换一个临时目录，保证窗口能开出来
        import tempfile
        prof = tempfile.mkdtemp(prefix="mindapp_")
        print("[mind] 已有窗口在用这个 profile，这次用临时目录开。", flush=True)
    args = [exe, f"--app={url}", f"--window-size={w},{h}",
            f"--user-data-dir={prof}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate"]
    # 关键：不要用 Popen 直接起 Edge —— 它会继承本进程的令牌。
    # 如果本进程是管理员，Edge 就是管理员实例，之后用户正常打开浏览器
    # 会一直看到「现有实例正在以提升的权限运行」的弹窗。
    # 改走 explorer + 快捷方式：explorer 是普通权限，拉起的 Edge 也是。
    try:
        lnk = Path(tempfile.gettempdir()) / f"mind_app_{os.getpid()}.lnk"
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
            + str(lnk).replace("'", "''") + "');"
            "$s.TargetPath='" + exe.replace("'", "''") + "';"
            "$s.Arguments='" + " ".join(args[1:]).replace("'", "''") + "';"
            "$s.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=20)
        if lnk.is_file():
            subprocess.run(["explorer.exe", str(lnk)], capture_output=True,
                           timeout=10)
            return True
    except Exception:
        pass

    # 兜底：直接起（可能仍是提权实例，但至少能开）
    try:
        subprocess.Popen(args)
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="mind-app 启动器")
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开")
    ap.add_argument("--headless", action="store_true", help="只跑服务")
    args = ap.parse_args()

    srv = Server()
    srv.start()
    print("[mind] 正在启动服务…", flush=True)
    if not srv.wait():
        print("[mind] 服务没起来，看看上面的报错。", flush=True)
        srv.stop()
        return 1

    url = f"http://127.0.0.1:{srv.port}/"
    print(f"[mind] 服务就绪：{url}", flush=True)

    if args.headless:
        print("[mind] headless，Ctrl+C 退出。", flush=True)
        try:
            if srv.proc:
                srv.proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()
        return 0

    if args.browser:
        webbrowser.open(url)
        try:
            if srv.proc:
                srv.proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()
        return 0

    conf = _load_win()
    w, h = int(conf.get("w", DEFAULT_SIZE[0])), int(conf.get("h", DEFAULT_SIZE[1]))

    # ① pywebview
    try:
        import webview  # type: ignore
    except ImportError:
        webview = None

    if webview is not None:
        win = webview.create_window("mind-app", url, width=w, height=h,
                                    min_size=(760, 520), text_select=True)
        win.events.closed += lambda: _save_win(
            {"w": getattr(win, "width", w), "h": getattr(win, "height", h)})
        try:
            webview.start(debug=False)
        finally:
            srv.stop()
        return 0

    # ② Edge/Chrome --app
    if open_app_window(url, w, h):
        print("[mind] 已用浏览器应用模式开窗（关窗口后按 Ctrl+C 结束）。", flush=True)
        try:
            if srv.proc:
                srv.proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()
        return 0

    # ③ 兜底
    print("[mind] 没找到可用的窗口环境，改用默认浏览器。", flush=True)
    webbrowser.open(url)
    try:
        if srv.proc:
            srv.proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
