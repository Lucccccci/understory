#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 桥接：把 NapCat（OneBot 11）收到的消息转给本机对话服务。

## 它做什么

    QQ 里你发消息
       ↓
    NapCat（挂在 QQNT 上，实现 OneBot 11 协议）
       ↓ 反向 HTTP 或正向 WebSocket
    本程序（bridge.py）
       ├─ 剥掉 CQ 码、判断是不是主人在说话
       ├─ POST 到 http://127.0.0.1:8731/api/qq
       └─ 拿到回复，经 OneBot API 发回 QQ
       ↓
    你收到她的回复

## 为什么接入判断放在这一层而不是服务端

「只回主人一个人」是**策略**，不是她的能力。
策略放在桥接层，服务端就保持通用 —— 将来想改成「群里也能聊」，
只改这里，人设与记忆一行都不用动。

## 启动

    python bridge.py

需要同时满足：
    * 本机对话服务在跑（桌面版快捷方式，或 chat_app.py）
    * NapCat 已登录并配好网络（反向 HTTP 或正向 WebSocket）
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from onebot import (  # noqa: E402
    OneBotClient,
    ReverseHttpReceiver,
    ForwardWebSocketReceiver,
    strip_cq_codes,
)

CONFIG_FILE = HERE / "bridge_config.json"

# NapCat 默认的 API 地址（正向 WebSocket 模式下用）
DEFAULT_API_BASE = "http://127.0.0.1:3000"


def load_config() -> dict:
    """读配置，并做基本校验。

    为什么要校验：配置文件是给人手工改的，很容易写出非法 JSON
    （我自己第一版就在注释字段后多打了个逗号）。
    与其让 json 报一句难懂的 `Expecting ',' delimiter`，
    不如直接指出第几行、可能是什么问题。
    """
    if not CONFIG_FILE.is_file():
        raise SystemExit(f"找不到 {CONFIG_FILE}")

    raw = CONFIG_FILE.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        lines = raw.splitlines()
        bad = lines[e.lineno - 1] if 0 < e.lineno <= len(lines) else ""
        hint = ""
        stripped = bad.strip()
        if stripped.endswith(",") or stripped.startswith('"_'):
            hint = ("\n提示：以下划线开头的字段是注释，它们之间也不能漏或多逗号。\n"
                    "      最稳妥的办法是只改引号里的值，不要动标点和结构。")
        raise SystemExit(
            f"bridge_config.json 格式错误（第 {e.lineno} 行）：{e.msg}\n"
            f"    该行内容：{bad.strip()[:80]}{hint}"
        )


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.behavior = cfg.get("behavior") or {}
        self.token = str(cfg.get("token") or "")

        self.owner_qq = int(cfg.get("owner_qq") or 0)
        self.chat_server = str(cfg.get("chat_server") or "http://127.0.0.1:8731").rstrip("/")

        # 发消息用哪个地址：正向 WS 模式下是 api_url，否则是反向 HTTP 时 NapCat 的 HTTP 服务
        ws_cfg = cfg.get("forward_ws") or {}
        api_base = str(ws_cfg.get("api_url") or DEFAULT_API_BASE)
        # API 的 token 和 WS 的 token 可以不一样（SnowLuma 就是分开给的）
        self.api_token = str(ws_cfg.get("api_token") or ws_cfg.get("token")
                             or self.token or "")
        self.client = OneBotClient(api_base, self.api_token)

        self._receivers: list = []
        # 机器人自己的 QQ 号。三种来源，优先级从高到低：
        #   1. 事件里带的 self_id（WebSocket 模式会有）
        #   2. 启动时 get_login_info 拿到的（_report_account 里存进来）
        #   3. 配置里的 self_qq（手填兜底，用于 get_login_info 失败时）
        self._self_id: int | None = None
        try:
            _sq = int(cfg.get("self_qq") or 0)
            if _sq:
                self._self_id = _sq
        except (TypeError, ValueError):
            pass

    # ── 启动 ──────────────────────────────────────────────
    def start(self) -> None:
        self._preflight()

        rev = self.cfg.get("reverse_http") or {}
        if rev.get("enabled"):
            r = ReverseHttpReceiver(
                host=str(rev.get("host") or "127.0.0.1"),
                port=int(rev.get("port") or 8732),
                on_event=self.on_event,
                token=self.token,
            )
            r.start()
            self._receivers.append(r)
            log(f"反向 HTTP 已监听 {rev.get('host')}:{rev.get('port')}"
                f"  → NapCat 的「HTTP 服务器」填 http://{rev.get('host')}:{rev.get('port')}")

        ws = self.cfg.get("forward_ws") or {}
        if ws.get("enabled"):
            w = ForwardWebSocketReceiver(
                url=str(ws.get("url") or "ws://127.0.0.1:3001"),
                on_event=self.on_event,
                token=str(ws.get("token") or self.token or ""),
            )
            w.start()
            self._receivers.append(w)
            log(f"正向 WebSocket 已连接 {ws.get('url')}")

        if not self._receivers:
            raise SystemExit("reverse_http 和 forward_ws 都关着 —— 没有收消息的途径。")

        log(f"只回应 QQ {self.owner_qq}。其他人的消息会被忽略。")
        self._report_account()
        log("等待消息中…（Ctrl+C 退出）")

    def _report_account(self) -> None:
        """报告机器人账号状态，让人肉眼确认设置符合预期。

        典型的安全设定是：**机器人账号只有一个好友（你自己）**。
        这样「不回复陌生人」不只是代码策略，而是结构上不存在陌生人。
        但代码仍会拦（白名单），属于双重保险。
        """
        try:
            info = self.client.get_login_info()
            data = info.get("data") or {}
            nick = data.get("nickname") or "?"
            uin = data.get("user_id") or "?"
            log(f"机器人账号：{nick}（{uin}）")
            # ★ 存下来。判断群里有没有 @ 我需要它。
            #
            # 原先只在收到 meta_event 时赋值 —— 但**反向 HTTP 模式不推 meta 事件**，
            # 于是 _self_id 永远是 None，群里 @ 她也永远不回（实测踩过）。
            # 这里既然已经拿到了，就存起来。
            try:
                if uin and str(uin) != "?":
                    self._self_id = int(uin)
            except (TypeError, ValueError):
                pass
        except Exception as e:
            log(f"（读不到账号信息：{e}）")
            return

        try:
            friends = self.client.get_friend_list()
        except Exception:
            friends = []

        if not friends:
            log("好友列表：读不到（不影响使用）")
            return

        log(f"好友列表：{len(friends)} 个")
        if len(friends) == 1:
            if str(friends[0].get("user_id")) == str(self.owner_qq):
                log("  ✓ 只有一个好友，就是你 —— 陌生人从结构上不存在")
            else:
                log("  !! 唯一的好友不是 owner_qq，请检查两个号有没有填反")
        else:
            others = [str(f.get("user_id")) for f in friends
                      if str(f.get("user_id")) != str(self.owner_qq)]
            log(f"  !! 除你之外还有 {len(others)} 个好友：{others[:5]}")
            log("     他们发消息会被白名单拦下，但建议确认这是否是你想要的设置")

    def _preflight(self) -> None:
        if not self.owner_qq:
            raise SystemExit(
                "bridge_config.json 里的 owner_qq 还没填。\n"
                "请填上你自己的 QQ 号 —— 不填的话就变成「谁都能跟她说话」，\n"
                "而她只会用主人的关系状态回应，对陌生人会显得过分亲昵。"
            )
        # 探一下对话服务是否在线
        try:
            import urllib.request
            with urllib.request.urlopen(f"{self.chat_server}/api/status", timeout=5) as r:
                st = json.loads(r.read().decode("utf-8"))
            has_key = st.get("has_key")
            log(f"对话服务在线（阶段 {st.get('stage_name')}，"
                f"{st.get('game_hearts')} 心，密钥 {'已设置' if has_key else '未设置'}）")
            if not has_key:
                log("!! 对话服务没读到 API 密钥 —— 请先在桌面窗口里设一次")
        except Exception as e:
            log(f"!! 连不上对话服务 {self.chat_server}：{e}")
            log("   请先启动桌面版（双击快捷方式），再跑本桥接。")

    # ── 事件处理 ──────────────────────────────────────────
    def on_event(self, event: dict) -> None:
        try:
            self._handle(event)
        except Exception as e:
            log(f"处理事件出错（已忽略）：{e}")

    def _handle(self, event: dict) -> None:
        if event.get("post_type") != "message":
            # 记录一下自己的 QQ 号，用于识别 @机器人
            if event.get("post_type") == "meta_event" and event.get("self_id"):
                self._self_id = int(event["self_id"])
            return

        msg_type = event.get("message_type")
        user_id = event.get("user_id")
        group_id = event.get("group_id")

        raw = event.get("raw_message") or event.get("message") or ""
        if not isinstance(raw, str):
            return

        text, ats = strip_cq_codes(raw)

        # 图片（2026-10-28）：把 CQ:image 里的图下载下来，转 data URL 一起发。
        # 失败就当没发图 —— 不能因为一张图把整条消息丢了。
        images = self._grab_images(raw)

        # ── 策略：只回主人 ──────────────────────────────
        if user_id != self.owner_qq:
            log(f"忽略非主人消息（来自 {user_id}）")
            return

        # 群消息：默认只有 @ 她才回
        if msg_type == "group":
            in_group = bool(self.behavior.get("reply_in_group_only_when_at", True))
            if in_group:
                me = self._self_id
                if not me:
                    # 拿不到自己的 QQ 号就判不了 @。
                    # 静默忽略过一次就够难查的了，这里明确说一句。
                    if not getattr(self, "_warned_self_id", False):
                        self._warned_self_id = True
                        log("!! 拿不到机器人自己的 QQ 号，群里无法判断有没有 @ 你。")
                        log("   （私聊不受影响。）可在 bridge_config.json 里手填 self_qq。")
                    return
                addressed = any(str(a.get("qq")) == str(me) for a in ats)
                if not addressed:
                    return

        if not text:
            return

        log(f"{'群' if msg_type == 'group' else '私聊'} {user_id}：{text[:60]}")

        # ── 控制指令（2026-11-08）──────────────────────────
        # 在本地就截住，**根本不发到 /api/qq** —— 她永远看不到你发 /status。
        if text.startswith("/"):
            import qq_cmd_impl
            got = qq_cmd_impl.handle(text, self.chat_server)
            if got is not None:
                back, _flags = got
                for i, seg in enumerate(self._split(back)):
                    if i:
                        time.sleep(0.3)
                    self._send(seg, user_id=user_id, group_id=group_id)
                return

        reply = self._ask(text, user_id=user_id, group_id=group_id,
                          images=images)
        # 测试档下，她的回复也要标出来，免得你以为这是真实档里的话
        try:
            import qq_cmd as _qc
            reply = _qc.mark(reply, _qc.load_flags()) if reply else reply
        except Exception:
            pass

        if not reply:
            err = self.behavior.get("reply_on_error") or ""
            if err:
                reply = err
            else:
                return

        # 像真人打字的节奏
        delay = self.behavior.get("typing_delay_ms") or [800, 2200]
        try:
            lo, hi = int(delay[0]), int(delay[1])
            time.sleep(random.randint(lo, max(lo, hi)) / 1000)
        except (TypeError, ValueError, IndexError):
            pass

        # 分条发（2026-10-28）：真人不会一口气发一大段。
        # 她把话分成几行，这几行就分几条消息发出去，中间隔一下。
        for i, seg in enumerate(self._split(reply)):
            if i:
                time.sleep(random.randint(400, 1100) / 1000)
            self._send(seg, user_id=user_id, group_id=group_id)

    # ── 与对话服务通信 ────────────────────────────────────
    def _ask(self, text: str, user_id, group_id,
             images: list | None = None) -> str | None:
        import urllib.error
        import urllib.request

        import qq_cmd
        flags = qq_cmd.load_flags()
        payload = {
            "text": text,
            "images": images or [],
            "user_id": user_id,
            "is_owner": user_id == self.owner_qq,
            # 测试档 / 假装网页（2026-11-08）
            "session": flags["session"],
            "as_web": bool(flags["home"]),
        }
        if group_id:
            payload["group_id"] = group_id

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.chat_server}/api/qq",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        # QQ 私聊 = 主存档（2026-11-08 修：不带这个头，服务把请求当"无头"，
        # 写入被改写到 test 档、再被身份守卫拒掉 —— 她的回复发了，记忆没留）
        req.add_header("X-Mind-Session", "desktop")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log(f"调用对话服务失败：{e}")
            return None

        if not result.get("ok"):
            log(f"对话服务返回错误：{result.get('error')}")
            return None

        if result.get("remembered"):
            log(f"（她记住了：{result['remembered']}）")

        reply = str(result.get("reply") or "").strip()

        limit = int(self.behavior.get("max_reply_chars") or 0)
        if limit and len(reply) > limit:
            reply = reply[:limit].rstrip() + "…"

        return reply

    @staticmethod
    def _grab_images(raw: str, limit: int = 3) -> list[str]:
        """从 CQ 码里取出图片，下载并转成 data URL。

        OneBot 11 的 image 段有几种形态：
            [CQ:image,file=xxx.image,url=https://...]
            [CQ:image,file=https://...]
        优先用 url=，没有就把 file= 当 URL 试一次。
        失败返回空列表 —— 图丢了也比整条消息丢了强。
        """
        import base64
        import re
        import urllib.request

        out: list[str] = []
        for m in re.finditer(r"\[CQ:image,([^\]]*)\]", raw or ""):
            if len(out) >= limit:
                break
            args = dict(
                kv.split("=", 1) for kv in m.group(1).split(",") if "=" in kv
            )
            url = (args.get("url") or args.get("file") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "mind-bridge"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    blob = r.read()
                if not blob:
                    continue
                # 从魔数猜类型，猜不到就用 jpeg
                if blob[:8].startswith(b"\x89PNG"):
                    mime = "image/png"
                elif blob[:3] == b"\xff\xd8\xff":
                    mime = "image/jpeg"
                elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                    mime = "image/webp"
                elif blob[:3] == b"GIF":
                    mime = "image/gif"
                else:
                    mime = "image/jpeg"
                out.append(
                    f"data:{mime};base64," + base64.b64encode(blob).decode()
                )
                log(f"收到图片 {len(blob)//1024} KB（{mime}）")
            except Exception as e:
                log(f"图片下载失败：{e}")
        return out

    @staticmethod
    def _split(text: str, max_parts: int = 3) -> list[str]:
        """把她的回复拆成几条消息。

        规则（宁可少拆，不要拆碎）：
            · 按换行拆 —— 她的换行本来就是语句断开的地方
            · 太短的（<6 字）并到前一条，不然会刷屏「嗯」「在呢」
            · 最多 max_parts 条，剩下的并进最后一条
        """
        parts = [x.strip() for x in (text or "").split("\n") if x.strip()]
        if len(parts) <= 1:
            return parts or [text]

        merged: list[str] = []
        for seg in parts:
            if merged and len(seg) < 6:
                merged[-1] = merged[-1] + "\n" + seg
            else:
                merged.append(seg)

        if len(merged) > max_parts:
            head = merged[:max_parts - 1]
            tail = "\n".join(merged[max_parts - 1:])
            merged = head + [tail]
        return merged

    def _send(self, text: str, user_id, group_id) -> None:
        try:
            if group_id:
                self.client.send_group(int(group_id), text)
                log(f"已回复到群 {group_id}")
            else:
                self.client.send_private(int(user_id), text)
                log(f"已回复 {user_id}")
            log(f"  她说：{text[:70]}")
        except Exception as e:
            log(f"发送失败：{e}")


def main() -> None:
    cfg = load_config()

    if not cfg.get("enabled", True):
        raise SystemExit(
            "bridge_config.json 里 enabled=false。\n"
            "确认 owner_qq 填好后，把 enabled 改成 true。"
        )

    print("=" * 68)
    print("  QQ 桥接 —— 让她在 QQ 里和你说话")
    print("=" * 68)

    bridge = Bridge(cfg)
    bridge.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("正在退出…")
        for r in bridge._receivers:
            try:
                r.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
