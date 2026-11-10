"""把 SnowLuma 的「注入」接到我们这边（2026-11-09）

## SnowLuma 的接口（实测）

    POST /api/login               {"username","password"} → {"token": "..."}
    GET  /api/processes           Authorization: Bearer <token>
                                  → {"list": [{pid, name, injected, connected,
                                              loggedIn, account:{uin,name}}]}
    POST /api/processes/<pid>/load
    POST /api/processes/<pid>/unload

关键点：
  · 鉴权走 **Authorization: Bearer**，不是 cookie（cookie 会 401）
  · 可能有**两个** QQ.exe：一个是他自己的号，一个是她
    → 必须按 account.uin 选中目标，瞎注会注到他的号上

## 凭据放哪

`saves/_ops/snowluma.json`：{"base","username","password","target_uin"}
这个文件不进版本库、不进提示词，只是本机配置。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request as _u
from pathlib import Path

CONF = Path(__file__).with_name("saves") / "_ops" / "snowluma.json"

DEFAULT = {
    "base": "http://127.0.0.1:5099",
    "username": "admin",
    "password": "",
    "target_uin": 0,
}


def _qq_cfg() -> dict:
    """settings.json 的 qq 段（新位置）；旧的 saves/_ops 文件作兜底。"""
    try:
        cfg = json.loads((Path(__file__).with_name("settings.json"))
                         .read_text(encoding="utf-8"))
        return cfg.get("qq") or {}
    except Exception:
        return {}


def _snowluma_dir() -> Path:
    d = (_qq_cfg().get("snowluma") or {}).get("dir") or ""
    return Path(d) if d else Path(r"D:\shu-qq\snowluma")


def _port_open(host: str = "127.0.0.1", port: int = 5099) -> bool:
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.4)
        return sk.connect_ex((host, port)) == 0


def _ensure_snowluma(wait: int = 25) -> tuple[bool, str]:
    """SnowLuma 没在跑就把它拉起来。返回 (起来没有, 说明)。"""
    import subprocess
    import time

    if _port_open():
        return True, ""
    sl = _snowluma_dir()
    exe = sl / "node.exe"
    entry = sl / "index.mjs"
    if not (exe.is_file() and entry.is_file()):
        return False, f"SnowLuma 目录不在：{sl}"
    try:
        # DETACHED_PROCESS：别让它跟着我们死
        subprocess.Popen([str(exe), str(entry)], cwd=str(SNOWLUMA_DIR),
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)
    except Exception as e:
        return False, f"拉起失败：{e}"
    t0 = time.time()
    while time.time() - t0 < wait:
        if _port_open():
            return True, "SnowLuma 已启动"
        time.sleep(0.6)
    return False, "SnowLuma 起来了但端口没开"


def _open() -> bool:
    """当前能不能连上 SnowLuma。"""
    return _port_open()


def load_conf() -> dict:
    d = dict(DEFAULT)
    q = _qq_cfg()
    sl = q.get("snowluma") or {}
    if sl.get("base"):
        d["base"] = sl["base"]
    if sl.get("username"):
        d["username"] = sl["username"]
    if sl.get("password"):
        d["password"] = sl["password"]
    if q.get("bot_uin"):
        d["target_uin"] = int(q["bot_uin"])
    # 旧位置（saves/_ops/snowluma.json）作兜底
    try:
        d.update(json.loads(CONF.read_text(encoding="utf-8")))
    except Exception:
        pass
    return d


def save_conf(d: dict) -> None:
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _req(base: str, path: str, token: str = "", body: dict | None = None,
         timeout: int = 25) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = _u.Request(base.rstrip("/") + path, data=data, headers=headers,
                   method="POST" if body is not None else "GET")
    with _u.urlopen(r, timeout=timeout) as s:
        raw = s.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def login(conf: dict | None = None) -> str:
    c = conf or load_conf()
    d = _req(c["base"], "/api/login",
             body={"username": c["username"], "password": c["password"]})
    if not d.get("success"):
        raise RuntimeError(d.get("message") or "登录失败")
    return str(d.get("token") or "")


def _uin_of(p: dict) -> int:
    acc = p.get("account") or {}
    if isinstance(acc, str):
        try:
            acc = json.loads(acc)
        except Exception:
            acc = {}
    try:
        return int(acc.get("uin") or 0)
    except (TypeError, ValueError):
        return 0


def status() -> dict:
    """她那个 QQ 进程现在是什么状态。"""
    c = load_conf()
    out = {"ok": False, "online": False, "injected": False, "connected": False,
           "pid": 0, "error": "", "names": []}
    if not _open():
        out["error"] = "SnowLuma 没开"
        return out
    try:
        tok = login(c)
        d = _req(c["base"], "/api/processes", token=tok)
        procs = d.get("list") or []
        out["names"] = [f"{p.get('name')}({_uin_of(p)})" for p in procs]
        target = int(c.get("target_uin") or 0)
        me = next((p for p in procs if _uin_of(p) == target), None)
        if me is None:
            out["error"] = "没找到她的 QQ 进程"
            return out
        out.update(ok=True, pid=int(me.get("pid") or 0),
                   injected=bool(me.get("injected")),
                   connected=bool(me.get("connected")),
                   online=bool(me.get("loggedIn")))
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def inject() -> dict:
    """找到她的进程并注入。

    2026-11-10：SnowLuma 没开的话**先把它拉起来** ——
    左栏点一下就该接上，不该要求用户记得先开那个面板。
    """
    c = load_conf()
    ok, why = _ensure_snowluma()
    if not ok:
        return {"ok": False, "error": why}
    st = status()
    if not st.get("ok"):
        return st
    if st.get("injected"):
        st["note"] = "已经接上了"
        return st
    try:
        tok = login(c)
        _req(c["base"], f"/api/processes/{st['pid']}/load", token=tok,
             body={})
        st = status()
        st["note"] = "注入完成" if st.get("injected") else "注入后状态未变"
        return st
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"
        return st
