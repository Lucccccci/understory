"""统一的模型调用口 —— 全项目只有这一处发 HTTP。

## 为什么要有这个文件（2026-10-27）

改之前，**13 个地方各自给模型打电话**：各自拼 payload、各自设超时、
各自 `try/except`。后果是：

    · 想加重试 → 要改 13 处，必然漏
    · 想换模型 / 走代理 → 同上
    · 想知道花了多少钱 → 没有任何一处记录
    · 某处失败了 → 那天的东西就永久缺失，只有一行日志

对照 AstrBot：它只有一个 `Provider.text_chat()`。

## 现在这一个入口负责四件事

    ① 发请求（唯一一处 urllib）
    ② 失败重试（网络错误 / 5xx / 429，指数退避）
    ③ 用量记账（saves/usage.json，按天累计 in/out token 和调用次数）
    ④ 并发闸门（同时最多 N 个请求，其余排队）

## 为什么重试放在这里而不是调用方

调用方关心的是「拿到文字没有」。重试是传输层的事。
放在这里，13 个调用点同时获得重试能力。

## 记账为什么要落文件

进程会重启。只记内存等于不记。按天累加，一天一行。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

USAGE_FILE = Path(__file__).resolve().parent / "saves" / "usage.json"

# 同时最多几个请求。这座镇子那边的台词生成也会走这里，
# 跨天时「每日事务 + 观察」可能撞在一起。
MAX_CONCURRENT = 3

# 重试用多久
RETRIES = 2
BACKOFF = (1.2, 3.0)

_gate = threading.BoundedSemaphore(MAX_CONCURRENT)
_usage_lock = threading.Lock()


# ══════════════════════════════════════════════════════════
# 记账
# ══════════════════════════════════════════════════════════
def _record(usage: dict | None, ok: bool = True) -> None:
    """把这一次的 token 用量累加到当天。**绝不抛异常**。"""
    try:
        today = date.today().isoformat()
        with _usage_lock:
            data = {}
            if USAGE_FILE.is_file():
                try:
                    data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
            days = data.setdefault("days", {})
            row = days.setdefault(today, {"in": 0, "out": 0, "calls": 0})
            row["calls"] = int(row.get("calls") or 0) + 1
            if usage:
                row["in"] = int(row.get("in") or 0) + int(
                    usage.get("prompt_tokens") or 0)
                row["out"] = int(row.get("out") or 0) + int(
                    usage.get("completion_tokens") or 0)
            # 只留 30 天，别让这个文件长成怪物
            if len(days) > 30:
                for k in sorted(days)[:-30]:
                    days.pop(k, None)
            tmp = USAGE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(USAGE_FILE)
    except Exception:
        pass


def usage_today() -> dict:
    """今天的用量（给状态面板用）。"""
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        return data.get("days", {}).get(date.today().isoformat(), {}) or {}
    except (OSError, json.JSONDecodeError):
        return {}


# ══════════════════════════════════════════════════════════
# 调用
# ══════════════════════════════════════════════════════════
def chat_body(payload: dict, api_key: str, api_base: str,
              timeout: float = 60) -> dict:
    """POST 一次 /chat/completions，返回解析后的 body。

    失败会重试；最终仍失败就抛异常（调用方的 try/except 照旧生效）。
    """
    if not api_key:
        raise ValueError("没有 API 密钥")

    url = (api_base or "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    last: Exception | None = None

    for attempt in range(RETRIES + 1):
        with _gate:
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {api_key}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8")
                body = json.loads(raw)
                _record(body.get("usage"))
                return body
            except urllib.error.HTTPError as e:
                last = e
                # 4xx（除了 429）重试没用：密钥错、参数错，重试还是错
                if e.code != 429 and 400 <= e.code < 500:
                    _record(None)
                    raise
            except Exception as e:      # 网络、超时、解析
                last = e

        if attempt < RETRIES:
            time.sleep(BACKOFF[attempt] if attempt < len(BACKOFF) else 3.0)

    _record(None)
    raise last if last else RuntimeError("调用失败")
