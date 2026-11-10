#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneBot 11 适配层：把 QQ 消息接进来，把她的回复发回去。

## 支持两种接入方式

| 方式 | 谁主动 | 收消息 | 发消息 |
|---|---|---|---|
| 反向 HTTP | NapCat 连我们 | NapCat POST 到我们 | 我们 POST 到 NapCat 的 HTTP API |
| 正向 WebSocket | 我们连 NapCat | 我们连 WebSocket 收 | 我们 POST 到 NapCat 的 HTTP API |

两种都实现，是因为 NapCat 的网络配置有多种组合，
使用者不必为了配合我们的代码去改 NapCat 的设置。

## 为什么单独一个文件

收发消息是「协议层」，和「她怎么说话」无关。
分开之后，将来想换成别的 OneBot 实现（或者别的聊天平台），
只要改这一层，人设与记忆完全不用动。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# OneBot 消息段里的 CQ 码，形如 [CQ:at,qq=123456]
CQ_CODE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


# ─────────────────────────────────────────────────────────────
def strip_cq_codes(text: str) -> tuple[str, list[dict]]:
    """剥掉 CQ 码，返回 (纯文本, @到的人列表)。

    群里 @ 机器人时消息里会带 [CQ:at,qq=...]，
    直接丢给模型会变成噪音。所以先剥掉，同时把 @ 的人记下来
    —— 用来判断「是不是在跟她说话」。
    """
    ats: list[dict] = []

    def repl(m: re.Match) -> str:
        kind = m.group(1)
        if kind == "at":
            params = {}
            for kv in m.group(2).lstrip(",").split(","):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    params[k.strip()] = v.strip()
            ats.append(params)
            return " "        # @ 某人替换成空格，免得词粘在一起
        if kind == "face":
            return ""         # QQ 表情直接丢掉
        if kind == "image":
            return "［图片］"
        if kind in ("reply",):
            return ""         # 引用回复的元数据
        return ""

    plain = CQ_CODE.sub(repl, text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain, ats


class OneBotClient:
    """负责「发消息」这一半。走 HTTP API，两种接入方式都一样。"""

    def __init__(self, api_base: str, token: str = "", timeout: int = 15):
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, action: str, payload: dict) -> dict:
        url = f"{self.api_base}/{action}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {"status": "ok", "raw": body}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"OneBot API {action} 返回 HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"连不上 OneBot API（{url}）：{e.reason}") from e

    def send_private(self, user_id: int, text: str) -> dict:
        return self._post("send_private_msg", {"user_id": user_id, "message": text})

    def send_group(self, group_id: int, text: str) -> dict:
        return self._post("send_group_msg", {"group_id": group_id, "message": text})

    def get_login_info(self) -> dict:
        return self._post("get_login_info", {})

    def get_friend_list(self) -> list:
        """取好友列表。用于确认机器人账号的好友数符合预期。

        标准 OneBot 的 get_friend_list 是 GET，但多数实现也接受 POST，
        所以这里用 POST；解析失败就返回空列表，不影响主流程。
        """
        try:
            result = self._post("get_friend_list", {})
        except Exception:
            return []
        data = result.get("data")
        return data if isinstance(data, list) else []


# ─────────────────────────────────────────────────────────────
class ReverseHttpReceiver:
    """反向 HTTP：起一个本地服务，等 NapCat 把事件 POST 过来。

    NapCat 的「HTTP 服务器」配置里，地址填本机的 host:port，
    并勾选「上报」事件。它会在收到 QQ 消息时把事件发到这里。
    """

    def __init__(self, host: str, port: int, on_event, token: str = ""):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.token = token
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _reply(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):  # noqa: N802
                # NapCat 可能任何路径都上报，所以不校验路径
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"

                # 校验 token（NapCat 若设了会带 Authorization）
                if receiver.token:
                    auth = self.headers.get("Authorization") or ""
                    if auth not in (f"Bearer {receiver.token}", f"Token {receiver.token}"):
                        return self._reply(401, {"status": "bad_token"})

                try:
                    event = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._reply(400, {"status": "bad_json"})

                # 先回 200，别让 NapCat 等 —— 她的回复是异步发的
                self._reply(200, {"status": "ok"})
                receiver.on_event(event)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


# ─────────────────────────────────────────────────────────────
class ForwardWebSocketReceiver:
    """正向 WebSocket：由我们主动连到 NapCat 收事件。

    标准库里没有 WebSocket 客户端，所以这里手写一个最小实现 ——
    只用得到「握手 + 读文本帧」，不值得为此引入第三方依赖。

    若 NapCat 侧用的是 WebSocket **客户端**模式（它连我们），
    那就该用 reverse_http，不是这个。
    """

    def __init__(self, url: str, on_event, token: str = ""):
        self.url = url
        self.on_event = on_event
        self.token = token
        self._sock = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        import socket
        import time
        from urllib.parse import urlparse

        while self._running:
            try:
                u = urlparse(self.url)
                host = u.hostname or "127.0.0.1"
                port = u.port or 3001
                path = u.path or "/"

                sock = socket.create_connection((host, port), timeout=10)
                self._sock = sock

                key = "dGhlIHNhbXBsZSBub25jZQ=="
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                )
                if self.token:
                    req += f"Authorization: Bearer {self.token}\r\n"
                sock.sendall((req + "\r\n").encode())

                # 读握手响应
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("握手时连接被关闭")
                    buf += chunk
                if b" 101 " not in buf.split(b"\r\n")[0]:
                    raise ConnectionError(f"WebSocket 握手失败：{buf.split(b'\r\n')[0]!r}")

                sock.settimeout(None)
                self._read_loop(sock)

            except Exception:
                if not self._running:
                    return
                time.sleep(3)      # 断线重连

    def _read_loop(self, sock) -> None:
        """读 WebSocket 帧。只处理文本帧，其余按长度跳过。"""
        while self._running:
            header = self._recv_exact(sock, 2)
            if not header:
                return
            b1, b2 = header[0], header[1]
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F

            if length == 126:
                ext = self._recv_exact(sock, 2)
                length = int.from_bytes(ext, "big")
            elif length == 127:
                ext = self._recv_exact(sock, 8)
                length = int.from_bytes(ext, "big")

            mask = self._recv_exact(sock, 4) if masked else b""
            payload = self._recv_exact(sock, length) if length else b""

            if masked and mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:            # close
                return
            if opcode == 0x9:            # ping → 回 pong 略；直接忽略
                continue
            if opcode != 0x1:            # 只要文本帧
                continue

            try:
                event = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            self.on_event(event)

    @staticmethod
    def _recv_exact(sock, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf
