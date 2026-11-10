"""指令的实现（2026-11-08）

每个指令都是一次 HTTP 调用 + 一段排版。
只读的指令**不写存档、不调模型**；/re /undo /life 才会动。
"""
from __future__ import annotations

import json
from datetime import datetime
import urllib.request as _u

import qq_cmd


def _req(base: str, path: str, session: str, body: dict | None = None,
         timeout: int = 40) -> dict:
    url = base.rstrip("/") + path
    data = None
    headers = {"X-Mind-Session": session}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = _u.Request(url, data=data, headers=headers,
                   method="POST" if body is not None else "GET")
    with _u.urlopen(r, timeout=timeout) as s:
        return json.loads(s.read().decode("utf-8"))


def _pct(st: dict) -> str:
    m = st.get("mood") or {}
    feel = m.get("label") or "—"
    return feel


def handle(text: str, base: str) -> tuple[str, dict] | None:
    """处理一条指令。返回 (要发回去的文字, 新的 flags)；不是指令返回 None。"""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    flags = qq_cmd.load_flags()
    parts = raw[1:].split()
    if not parts:
        return qq_cmd.mark(qq_cmd.HELP, flags), flags
    cmd = parts[0].lower()
    arg = " ".join(parts[1:]).strip()
    sess = flags["session"]

    try:
        if cmd in ("help", "h", "?"):
            return qq_cmd.mark(qq_cmd.HELP, flags), flags

        if cmd == "status":
            st = _req(base, "/api/status", sess)
            days = st.get("days") or {}
            now = datetime.now().strftime("%H:%M")
            season = days.get("season") or ""
            weather = days.get("weather") or ""
            head = " · ".join(x for x in (now, season, weather) if x)
            lines = [head,
                     f"她在{days.get('place') or '—'}，{st.get('task') or '没在忙什么'}",
                     f"心情 {_pct(st)}（{st.get('mood', {}).get('valence', '?')}）"
                     f" · 精力 {st.get('mood', {}).get('energy', '?')}"]
            if days.get("trouble"):
                lines.append(f"小麻烦：{days['trouble']}")
            return qq_cmd.mark("\n".join(lines), flags), flags

        if cmd == "today":
            st = _req(base, "/api/status", sess)
            days = st.get("days") or {}
            lines = [f"手边：{st.get('task') or '—'}",
                     f"心里：{st.get('thought') or '—'}"]
            if days.get("trouble"):
                lines.append(f"小麻烦：{days['trouble']}")
            tastes = days.get("tastes") or []
            if isinstance(tastes, list) and tastes:
                lines.append("口味：" + "；".join(str(x)[:24] for x in tastes[:3]))
            elif tastes:
                lines.append(f"口味：{tastes}")
            return qq_cmd.mark("\n".join(lines), flags), flags

        if cmd in ("facts", "mem"):
            d = _req(base, "/api/memory", sess, {"op": "list"})
            ms = d.get("memories") or d.get("items") or []
            ms = sorted(ms, key=lambda m: -float(m.get("weight") or 0))[:5]
            if not ms:
                return qq_cmd.mark("（她还没记住什么）", flags), flags
            body = "\n".join(f"★{m.get('weight')} {str(m.get('text'))[:40]}"
                             for m in ms)
            return qq_cmd.mark("她记得的：\n" + body, flags), flags

        if cmd in ("sees", "obs"):
            d = _req(base, "/api/status", sess)
            obs = d.get("observations") or (d.get("days") or {}).get("observations") or []
            if not obs:
                return qq_cmd.mark("（还看不出你是什么人）", flags), flags
            return qq_cmd.mark("她认得的你：\n" + "\n".join(f"· {o}" for o in obs),
                               flags), flags

        if cmd in ("events", "ev"):
            d = _req(base, "/api/status", sess)
            ev = d.get("events") or (d.get("days") or {}).get("events") or []
            if not ev:
                return qq_cmd.mark("（还没什么值得记的事）", flags), flags
            body = "\n".join(f"{str(e.get('day', ''))[:10]} {str(e.get('text'))[:36]}"
                             for e in ev[-5:])
            return qq_cmd.mark("最近的事：\n" + body, flags), flags

        if cmd in ("promises", "promise"):
            d = _req(base, "/api/status", sess)
            ps = d.get("promises") or (d.get("days") or {}).get("promises") or []
            if not ps:
                return qq_cmd.mark("（没有未兑现的承诺）", flags), flags
            body = "\n".join(f"· {str(p.get('text') if isinstance(p, dict) else p)[:40]}"
                             for p in ps[:6])
            return qq_cmd.mark("你答应过的：\n" + body, flags), flags

        if cmd in ("relation", "rel"):
            st = _req(base, "/api/status", sess)
            first = str(st.get("first_met") or "")[:10]
            days_n = ""
            if first:
                try:
                    days_n = f" · 在一起 {(datetime.now().date() - datetime.fromisoformat(first).date()).days} 天"
                except ValueError:
                    days_n = ""
            return qq_cmd.mark(
                f"{st.get('stage_name') or '—'}{days_n}\n"
                f"累计 {st.get('total_turns') or 0} 轮 · 心 {st.get('game_hearts') or '—'}",
                flags), flags

        if cmd == "recall":
            if not arg:
                return qq_cmd.mark("用法：/recall 关键词", flags), flags
            d = _req(base, "/api/recall", sess, {"query": arg, "limit": 3})
            hits = d.get("hits") or d.get("turns") or []
            if not hits:
                return qq_cmd.mark(f"（没翻到关于「{arg}」的旧话）", flags), flags
            body = "\n".join(
                f"{str(h.get('at', ''))[:10]} {'他' if h.get('role') == 'player' else '她'}："
                f"{str(h.get('text'))[:34]}" for h in hits)
            return qq_cmd.mark(body, flags), flags

        if cmd == "diary":
            d = _req(base, "/api/diary", sess)
            ents = d.get("entries") or []
            if not ents:
                return qq_cmd.mark("（还没有日记）", flags), flags
            e = ents[-1]
            return qq_cmd.mark(f"{str(e.get('day'))[:10]}\n{str(e.get('text'))[:220]}",
                               flags), flags

        if cmd == "house":
            st = _req(base, "/api/status", sess)
            days = st.get("days") or {}
            return qq_cmd.mark(f"她在家里的：{days.get('place') or '—'}", flags), flags

        if cmd in ("re", "undo"):
            _req(base, "/api/rewind", sess,
                 {"op": "regen" if cmd == "re" else "undo"})
            return qq_cmd.mark("已重说上一句" if cmd == "re" else "已撤回上一轮",
                               flags), flags

        if cmd == "life":
            _req(base, "/api/regenerate", sess, {"what": "life"}, timeout=120)
            st = _req(base, "/api/status", sess)
            return qq_cmd.mark(f"手边：{st.get('task') or '—'}\n"
                               f"心里：{st.get('thought') or '—'}", flags), flags

        if cmd == "test":
            flags["session"] = "test"
            qq_cmd.save_flags(flags)
            return qq_cmd.mark("已切到测试档", flags), flags

        if cmd in ("desktop", "main"):
            flags["session"] = "desktop"
            qq_cmd.save_flags(flags)
            return "已切回主存档", flags

        if cmd == "home":
            if arg.lower() in ("off", "0", "false"):
                flags["home"] = False
                qq_cmd.save_flags(flags)
                return qq_cmd.mark("已关掉「假装在网页」", flags), flags
            flags["home"] = True
            qq_cmd.save_flags(flags)
            return qq_cmd.mark("现在按网页通道说话", flags), flags

        return qq_cmd.mark(f"没有这条指令：/{cmd}\n/help 看全部", flags), flags

    except Exception as e:
        return qq_cmd.mark(f"指令失败：{type(e).__name__}: {e}", flags), flags
