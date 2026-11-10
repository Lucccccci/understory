#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 HTTP 服务：给对话窗口提供界面与接口。

设计取舍：
    * 用标准库的 http.server，零第三方依赖
    * 只监听 127.0.0.1，不对外网开放
    * 立绘从 mod 素材里实时裁切+放大，不预先生成文件

## 关于 API 密钥

优先读环境变量 MIND_AI_API_KEY。若未设置，窗口会询问，并允许**只保存在本机**
（mind-life/.apikey，已在 .gitignore 里排除）。
独立程序无法像 mod 那样强制只走环境变量——那对普通使用太麻烦。
但绝不写进代码、绝不随任何东西分发。
"""

from __future__ import annotations

import collections
import io
import json
import os
import random
import re
from datetime import datetime
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import mind_backup
import mind_diag
import mind_image
import mind_life
import mind_note
from dataclasses import asdict

from mind_image import VISION_MODEL

from mind_mood import Mood
from mind_brain import MindEngine
from mind_character import Character, stage_of, next_stage
from world_save import list_saves, read_game_context, SAVE_ROOT
from sessions import store as session_store, DESKTOP_KEY
from sessions import get_session_lock

HERE = Path(__file__).resolve().parent
SAVE_DIR = HERE / "saves"
SAVE_FILE = SAVE_DIR / "legacy.json"
KEY_FILE = HERE / ".apikey"
SETTINGS_FILE = HERE / "settings.json"
KEY_ENV = "MIND_AI_API_KEY"

# 美术素材
PORTRAIT_SRC = HERE.parent / "_source_shu" / "assets" / "Image" / "shu.png"
# 从官方立绘生成的高清脸（见 make_official_face.py）
FACE_DIR = HERE / "assets" / "face"
MIND_FACE_DIR = HERE / "assets" / "face_shu_big"
# 背景音乐
BGM_DIR = HERE / "assets" / "bgm"
# 背景纹理（由 make_bg_texture.py 从 mod 图块集抽取去色而成）
BG_DIR = HERE / "assets" / "bg"
# 玩家头像（由 make_farmer_avatar.py 从游戏贴图合成）
AVATAR_DIR = HERE / "assets" / "avatar"

# 表情记号 -> (官方高清脸 / mod 表情变体文件名)
# 无表情时用官方立绘裁出的高清脸 —— 它是 1464×1464 裁的，比 mod 的 64×64 清楚得多
EMOTION_TO_FILE = {
    "":   "face_default.png",
    "$h": "expr_happy.png",
    "$s": "expr_sad.png",
    "$u": "expr_unique.png",
    "$l": "expr_love.png",
    "$a": "expr_angry.png",
}

# 兼容旧接口：立绘索引 -> 表情键
INDEX_TO_FILE = {
    0: "face_default.png",
    1: "expr_happy.png",
    2: "expr_sad.png",
    3: "expr_unique.png",
    4: "expr_love.png",
    5: "expr_angry.png",
}

# 旧接口用的索引表（保留，因为 QQ 桥接与旧前端都还按其约定传值）
EMOTION_TO_INDEX = {
    "": 0, "$h": 1, "$s": 2, "$u": 3, "$l": 4, "$a": 5,
}

# 立绘文件名 -> mod 立绘表里的索引。
# 用于「只画风统一地用 mod 立绘」时把表情文件名映射回去。
EMOTION_INDEX_BY_FACE = {
    "face_default.png": 0,
    "expr_happy.png": 1,
    "expr_sad.png": 2,
    "expr_unique.png": 3,
    "expr_love.png": 4,
    "expr_angry.png": 5,
    "expr_neutral.png": 0,
}


def emotion_to_face(emotion: str) -> str:
    """表情记号 -> 立绘文件名（不含扩展名）。

    前端现在按**文件名**取图，所以接口统一返回文件名而不是索引。
    （先前返回索引，前端把它当文件名用会 404 —— 静默失败。）
    """
    fname = EMOTION_TO_FILE.get(emotion or "", "face_default.png")
    return fname[:-4] if fname.endswith(".png") else fname

DEFAULT_SETTINGS = {
    # 用哪个存档。"latest" 表示最近玩过的那个。
    "save_name": "latest",
    # 是否把游戏里的关系同步进来（她是否知道你们在游戏里的关系）
    "use_game_relation": True,
    # 立绘来源：
    #   "mod"      = 只用 mod 自带的像素立绘（**默认**）
    #   "official" = 无表情时用官方高清脸，有表情才切 mod 版
    #
    # 为什么默认 mod：实测两种画风混用时，无表情走高清、有表情走像素，
    # 同一场对话里来回切换，视觉上很跳。统一用一套反而舒服。
    # 官方高清脸（1464×1464 裁的）仍然保留，想用就切过去。
    "portrait_source": "mod",
    # 配色主题：light（宣纸）/ moon（月华）/ ink（墨夜）/ candle（烛火）
    "theme": "light",
    # 背景音乐
    "bgm_enabled": False,
    "bgm_track": "mind_theme",
    "bgm_volume": 0.35,
}

# 可选曲目：文件名 -> 显示名
#   mind_theme  = 官方「她的试验田」BGM（她的主题）
#   mod_*      = [CP] 她Shu 随包的两首
BGM_TRACKS = {
    "mind_theme":    "她的试验田（官方）",
    "mod_shubgm":   "麓阳书院（mod）",
    "mod_dahuang":  "赴大荒（mod）",
}

_engine: MindEngine | None = None
_settings: dict | None = None


# ─────────────────────────────────────────────────────────────
def load_settings() -> dict:
    global _settings
    if _settings is not None:
        return _settings
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.is_file():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    _settings = s
    return s


def save_settings() -> None:
    if _settings is None:
        return
    try:
        SETTINGS_FILE.write_text(
            json.dumps(_settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def resolve_save_path():
    """按设置决定读哪个存档。返回 Path 或 None。"""
    name = load_settings().get("save_name", "latest")
    if name and name != "latest":
        p = SAVE_ROOT / name / name
        if p.is_file():
            return p
        # 选中的存档没了（被删/改名），退回最近的那个
        load_settings()["save_name"] = "latest"

    saves = list_saves()
    if not saves:
        return None
    return SAVE_ROOT / saves[0]["name"] / saves[0]["name"]


# ─────────────────────────────────────────────────────────────
def resolve_api_key() -> str | None:
    v = os.environ.get(KEY_ENV)
    if v and v.strip():
        return v.strip()
    if KEY_FILE.is_file():
        v = KEY_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    return None


def save_api_key(key: str) -> None:
    KEY_FILE.write_text(key.strip(), encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass


# 当前请求用哪个会话键。
#
# ## 为什么要这么绕
#
# `get_character()` 是模块级函数，payload 方法也都不收会话参数 ——
# 要支持「测试档」得一路传下去，改十几个签名。
#
# 所以改成一个**请求级的变量**：ChatHandler 在处理请求前从 HTTP 头
# （`X-Mind-Session`）读一次写进来，`get_character()` 读它。
#
# **只允许 desktop 和 test** —— 白名单是必要的：
# 否则前端传个 `qq_123` 就能读到别人的会话。
#
# ## ⚠️ 这里原来是模块级全局，是个并发炸弹（2026-10-17 修）
#
# 原来写的是：
#
#     _active_session = DESKTOP_KEY
#     def set_active_session(key):
#         global _active_session
#         _active_session = ...
#
# 而注释里还写着「服务是单线程 HTTPServer，不存在并发串档的问题」——
# **那个假设是错的**，实际用的是 `ThreadingHTTPServer`（见 chat_app.py）。
#
# 后果实测发生过：线程 A 正在处理桌面窗口的请求，线程 B（测试脚本）
# 带着 `test` 头进来，把全局变量改成 test ——
# **线程 A 后半程就写进了测试档**。反过来，桌面的写请求也会盖掉测试档。
#
# 更糟的一种：真实档被测试的话写进去，而且看起来完全正常 ——
# 用户的原话是「你是不是没用测试档测试？」，
# 而那时真实档里已经混进了「我三天没来了，你想我没。」这种测试问句。
#
# 修法：**换成线程局部**。每个请求线程有自己的会话键，互不干扰。
#
# ## 三个会话键的分工（2026-10-19 加 `probe`）
#
#     desktop  用户的真实记忆。**永远不许被测试碰**
#     test     用户自己的调试档（界面上那个「测试档」开关）
#     probe    **AI 自己的测试档**。所有自动化验证用这个
#
# 为什么要单独分一个 `probe`：之前 AI 用 `test` 做验证，于是
# 用户的调试档被反复写脏 —— 一天里被覆盖过两次。
# 「测试用测试档」这条规矩不够，因为**谁的测试档**没说清。
#
# ⚠️ 白名单是**硬性**的：不在集合里的键会**兜底成 desktop**
# （见下面 `set_active_session`）。所以新增一个会话键，
# 必须同时加到这里，否则请求会落到真实档上 —— 那比报错危险得多。
_ALLOWED_SESSIONS = {DESKTOP_KEY, "test", "probe"}
_session_ctx = threading.local()


def set_active_session(key: str) -> str:
    """设置**当前线程**的会话键。不在白名单里就退回桌面档。

    ## 第二道防线：没带头的请求不许写真实档（2026-10-17 加）

    起因是实测发生过一次污染 —— 测试脚本的话写进了真实档，
    用户的原话是「你是不是没用测试档测试？」。
    根因是 `_active_session` 原来是模块级全局（见上面的说明），
    已经改成线程局部了。

    但只修并发还不够：**如果哪天又出别的岔子，代价是真实记忆被写脏。**
    而那个代价是不对称的 —— 测试档脏了无所谓，真实档脏了是几个月的东西。

    所以这里再兜一层：

        · HTTP 头明确给了 `test`        -> test 档（正常）
        · HTTP 头明确给了 `desktop`     -> 桌面档（正常，前端会带）
        · **什么头都没给**              -> 退回桌面档，但**标记为「无头」**
          任何写入都会被 `_guard_write()` 拦下来

    拦下来不是静默丢弃，会打到 stderr —— 免得又变成「看不出哪里错了」。
    """
    k = (key or "").strip()
    if not k:
        # 无头请求：允许读（很多查询类接口不带也无妨），但要标记
        _session_ctx.key = DESKTOP_KEY
        _session_ctx.headerless = True
        return _session_ctx.key
    _session_ctx.headerless = False
    _session_ctx.key = k if k in _ALLOWED_SESSIONS else DESKTOP_KEY
    return _session_ctx.key


def _guard_write(session_key: str) -> str:
    """写入前的最后一道闸。返回**实际该写哪个档**。

    无头请求想写真实档 -> 改写到测试档，并留痕。

    为什么改写到测试档而不是直接拒绝：拒绝会让调用方拿到一个
    静默的半成功状态（内存里改了、盘上没改），更难查。
    写到测试档至少是有痕迹的，而且不会毁真实记忆。
    """
    if (session_key == DESKTOP_KEY
            and getattr(_session_ctx, "headerless", False)):
        print("[会话守卫] 无头请求试图写真实档 —— 已改写到测试档。"
              "调用方应该带上 X-Mind-Session 头。",
              file=sys.stderr, flush=True)
        return "test"
    return session_key


def current_session() -> str:
    """当前线程的会话键。**所有地方都该用这个**，别读全局变量。

    历史：原来是个模块级全局 `_active_session`，多线程下会串档
    （实测污染过真实档）。改成 threading.local 之后，
    为了不手改 14 处引用，试过用模块级 `__getattr__` 做动态别名 ——
    **那个方案在模块自己的代码里不生效**，报 NameError。
    所以还是显式调用，看得见。
    """
    return getattr(_session_ctx, "key", DESKTOP_KEY)


def get_character(session_key: str = "") -> Character:
    """按会话键取角色状态。

    桌面窗口用 "desktop"，QQ 私聊用 "qq_<QQ号>"，群聊用 "qqgroup_<群号>"。
    每个会话一份独立存档，互不污染记忆。

    Args:
        session_key: 留空则用**当前请求**的会话键（见 `_active_session`）。
    """
    return session_store.load(session_key or current_session())


def save_character(session_key: str, ch: Character) -> None:
    """存当前会话。

    ⚠️ 走 `_guard_write` —— 无头请求不许写真实档（见那函数的说明）。
    所有存档写入都必须经过这里，否则那道闸就漏了。

    ## 为什么这里要吞掉「空档覆盖」那个异常

    `SessionStore.save` 里有道闸：盘上有 N 轮、而这次要写 0 轮时**拒绝写**。
    那道闸是对的，但它抛 RuntimeError —— 让它一路抛到 HTTP 层的话，
    用户点一下就 500，**看起来像程序坏了**，而实际是一次被拦下的错误写入。

    所以这里：拦下的情况**记日志、不抛**。内存里的角色还在，
    对话照常继续，只是这一次没有落盘。
    """
    try:
        session_store.save(_guard_write(session_key), ch)
    except RuntimeError as e:
        # 已经 print 到 stderr、已记进 diagnostics；这里只需不炸
        print(f"[保存跳过] {session_key}: {e}", file=sys.stderr, flush=True)


# ── 语音合成（转发给独立的语音服务）──────────────────────
#
# 语音服务是 GPT-SoVITS（tools/GPT-SoVITS-win/.../serve_voice.py），
# 默认在 127.0.0.1:9880。它不在这个进程里 —— 那边要加载 3.5 GB 模型。
VOICE_URL = "http://127.0.0.1:9880"
_tts_cache: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()
_tts_cache_lock = threading.Lock()
_TTS_CACHE_MAX = 300


def _tts_synth(text: str) -> bytes | None:
    """合成一句（发给语音服务），带 LRU 缓存。失败返回 None。

    ## 为什么要缓存

    「点两次播放键」是很自然的动作（第一次没听清）。没有缓存的话
    每次都重新合成（1-3 秒）—— 而她的常用句子会反复出现。

    300 条按平均 8 KB 算约 2.4 MB，可以忽略。
    """
    with _tts_cache_lock:
        hit = _tts_cache.get(text)
        if hit is not None:
            _tts_cache.move_to_end(text)
            return hit

    import urllib.parse
    import urllib.request
    try:
        q = urllib.parse.urlencode({
            "text": text,
            "text_lang": "zh",
            # 参考音频和文本用语音服务自己的默认值 —— 那边是唯一权威
        })
        req = urllib.request.Request(f"{VOICE_URL}/tts?{q}")
        with urllib.request.urlopen(req, timeout=120) as r:
            wav = r.read()
        if len(wav) < 2000 or not wav[:4] == b"RIFF":
            return None
    except Exception as e:
        mind_diag.note("调语音服务合成", e)
        return None

    with _tts_cache_lock:
        _tts_cache[text] = wav
        _tts_cache.move_to_end(text)
        while len(_tts_cache) > _TTS_CACHE_MAX:
            _tts_cache.popitem(last=False)
    return wav


def _tts_synth_turn(text: str) -> bytes | None:
    """合成**一整条回复**：拆解 -> 逐段合成 -> 拼成一条。

    ## 为什么在服务端拼而不是前端

    前端逐段播时，段与段之间有 `<audio>` 元素的切换缝，
    加上模型自带的首尾静音，听起来一顿一顿（用户实测反馈）。
    样本级拼接没有缝。

    ## 缓存分两层

        外层 /api/tts 的文本级缓存   —— 同一条重复点，秒回
        内层 _tts_synth 的句子缓存   —— 不同条里的同一句复用

    第二层很实用：「嗯。」「睡吧。」这类短句在她 94 条回复里反复出现。

    ## 不裁静音

    见 `_api_tts` 的说明 —— 用户明确要求不裁。
    """
    import mind_speech

    parsed = mind_speech.parse(text)
    say_segs = [s for s in parsed.segs if s.kind == "say"]
    if not say_segs:
        return None                     # 整条都是动作，没得念

    pieces: list = []
    for s in parsed.segs:
        if s.kind == "pause":
            pieces.append(("pause", s.pause))
            continue
        wav = _tts_synth(s.text)
        if not wav:
            # 一段失败不让整条废掉 —— 跳过它，念其余的
            mind_diag.note(f"合成失败，跳过一段（{len(s.text)} 字）")
            continue
        pieces.append(("say", wav))

    if not any(k == "say" for k, _ in pieces):
        return None
    return mind_speech.splice(pieces)


# ── 情绪判断（2026-10-17 换成模型）────────────────────────
#
# ## 为什么换
#
# 词表判据实测命中率：暖 11% / 冷 3% / **都没中 85%**。
# 原因是他的情感藏在事情里，不在字面上：
#
#     「我今天在矿洞里待了一整天」   陈述，但里头有累
#     「我三天没来了，你想我没」     问句，但里头有在意
#
# 用户反馈「心情不随对话走」就是这个 —— 85% 的轮次她心里纹丝不动。
#
# ## 为什么在后台
#
# 判一次 1-2 秒，而它只影响「下一轮的语气描述」，不影响这一轮的回复。
# 所以丢给线程，用户不必等。
#
# ## 为什么重新读档
#
# 后台跑的时候主链路可能正在写同一个会话。直接改内存里的对象再存盘，
# 会**盖掉主链路刚写的东西**（今天的线程隔离问题就是这一类）。
# 所以后台：拿锁 -> 重新 load -> 改 -> save。

def _spawn_feel_judge(session_key: str, user_text: str,
                      her_reply: str, api_key: str) -> None:
    """起一个线程判情绪。失败静默（心情不动，不影响任何事）。"""
    import threading

    def work() -> None:
        try:
            import mind_feel
            # 一次调用，两个用途：情绪变化 + **这条值不值得长期记住**
            # （2026-11-02：记忆权重交给模型判，不再靠正则看句式）
            # 带上她的回话和当前位置 —— 判定要看两边（2026-11-05）
            try:
                _p0 = getattr(get_character(session_key), "today_place", "")
            except Exception:
                _p0 = ""
            r = mind_feel.judge_ex(user_text, api_key,
                                  her_reply=her_reply, current_place=_p0)
            feel, strength = r["feel"], r["strength"]
            delta = mind_feel.delta_for(feel, strength)
            # 注意：place/task 也是这次调用的产物 —— 不能因为"心情没动、也没记住事"
            # 就提前 return，否则换了地方也写不回去（2026-11-04 实测踩到）。
            if (not delta and not r.get("fact")
                    and not r.get("place") and not r.get("task")):
                return
            with get_session_lock(session_key):
                ch = get_character(session_key)
                m_before = Mood.from_dict(ch.mood)
                ch.note_turn_mood(user_text, her_reply, valence_delta=delta)

                # 长期记忆：只有模型觉得重要（salience ≥ 2）才写
                # 对话里换了地方/换了活 -> 跟着改（2026-11-04）
                moved = ""
                if r.get("place") and r["place"] != getattr(ch, "today_place", ""):
                    ch.today_place = r["place"]
                    moved = r["place"]
                if r.get("task") and r["task"] != getattr(ch, "today_task", ""):
                    ch.today_task = r["task"]
                    moved = (moved + " " if moved else "") + r["task"]

                # 事件：不再存他的原话，存模型改写过的「发生了什么」
                # （2026-11-06 —— 原来 detect() 把原话裁一段直接存，
                #   事件栏看起来就是聊天记录）
                took_event = ""
                if r.get("event"):
                    ev = ch.add_event_manual(str(r["event"]))
                    if ev is not None:
                        took_event = str(r["event"])

                wrote = ""
                if r.get("fact"):
                    if ch.remember(r["fact"], kind="fact",
                                   weight=int(r["salience"] or 2)):
                        wrote = r["fact"]

                m_after = Mood.from_dict(ch.mood)
                # 旧对话压缩（2026-11-08）：到点才真的调用，平时只查一个数
                rolled = ""
                try:
                    import mind_rollup
                    ok_r, msg_r = mind_rollup.rollup_if_due(ch, api_key)
                    if ok_r:
                        rolled = msg_r
                except Exception:
                    pass

                if moved:
                    print(f"[在手边] {moved}", flush=True)
                if rolled:
                    print(f"[摘要] {rolled}", flush=True)
                if took_event:
                    print(f"[事件] {took_event}", flush=True)
                if (m_after.valence != m_before.valence or wrote or moved
                        or took_event or rolled):
                    save_character(session_key, ch)
                    print(f"[心情] {feel}(强度{strength}) -> 情绪 "
                          f"{m_before.valence} -> {m_after.valence}",
                          flush=True)
                if wrote:
                    print(f"[记忆] 权重{r['salience']}：{wrote}", flush=True)
        except Exception:
            pass          # 心情判断失败不该影响任何东西

    t = threading.Thread(target=work, name="feel-judge", daemon=True)
    t.start()


# ── 被拦下的回复：一律不写进历史（2026-10-20 加）─────────────
#
# ## 为什么要单独判
#
# 模型被内容策略挡下时不一定报错，有两种形态：
#
#     ① 接口报错 / finish_reason=content_filter   <- 可靠信号，mind_brain 已标
#     ② 软拒绝：HTTP 200，内容是「作为 AI，我不能…」
#
# 两种都**不能进 history**。原因不是洁癖 —— 是这句话会变成她的「先例」：
# 下一轮她看到自己这么说过，就会继续这么说话，越滚越像。
# 这个坑我们踩过（83 轮没带情绪记号，模型就再也不带记号了）。
#
# ## 判据要窄
#
# 只认「AI 腔」。**她自己的拒绝必须原样保留** ——
# 「今天不」「别闹」「这个不说了」是她的台词，不是故障。
# 把她的拒绝也拦掉，等于把她这个人删了。
_REFUSAL_RE = re.compile(
    r"作为(一个)?\s*(AI|人工智能|语言模型|助手|智能助理)"
    r"|我是(一个)?\s*(AI|人工智能|语言模型|智能助理)"
    r"|(很|非常)?抱歉[，,]\s*.{0,14}?(无法|不能|没办法)"
    r"|我(无法|不能)(协助|提供|生成|回答|讨论)"
    r"|不(能|可以|便)(讨论|提供|生成).{0,10}(内容|话题|信息)"
    r"|(该|这个)内容(可能)?(涉及|违反)"
)


def _looks_like_refusal(text: str) -> bool:
    """这段回复像不像「模型的拒绝」，而不是「她的话」。"""
    t = (text or "").strip()
    if not t:
        return False
    # 太长的一般是她在说话；套话式的拒绝通常短
    if len(t) > 120:
        return False
    return bool(_REFUSAL_RE.search(t))

    """她睡着的那一刻，写一篇日记。

    ## 为什么是「睡着时」而不是零点

    你聊到十二点半，她说着说着睡过去了 —— 那才是她一天的结束。
    零点只是日历翻页，和她的感受无关。

    ## 会不会重复写

    `mind_diary.write_one` 自己查当天写过没有，写过就跳过。

    ## 失败不影响任何东西

    日记是给你读的，**不进 prompt**。写不出来就是今天没有，她不会因此少记什么。
    """
    import threading

    def work() -> None:
        try:
            import mind_diary
            ok, msg = mind_diary.write_one(ch, session_key, api_key)
            if ok:
                print(f"[日记] {msg}", flush=True)
        except Exception as e:
            print(f"[日记] 出错：{e!r}"[:180], flush=True)
            mind_diag.note("后台写日记", e)

    threading.Thread(target=work, name="diary", daemon=True).start()


def watch_diary(session_key: str = "desktop") -> None:
    """后台巡检：**道过晚安、又安静了 12 分钟**，才写今天的日记。

    ## 为什么不用「她睡着」判定

    睡着是「深夜 + 静默」，和「今天聊完了」不是一回事 ——
    实测会在你还没说晚安的时候就写，把一天提前结了。

    ## 为什么需要巡检

    你道完晚安就把窗口关了，之后没有任何请求。
    只有定时看一眼，才知道「这条对话真的停了」。

    ## 一天只写一篇

    `mind_diary.write_one` 自己查当天写过没有，重复调用无害。
    """
    import threading
    import time
    from datetime import datetime

    def loop() -> None:
        while True:
            time.sleep(240)          # 每 4 分钟
            try:
                import mind_diary
                with get_session_lock(session_key):
                    ch = get_character(session_key)
                    turns = ch.turns or []
                    if not turns:
                        continue
                    last = turns[-1]
                    if getattr(last, "role", "") != "player":
                        continue
                    if not mind_diary.is_goodnight(getattr(last, "text", "")):
                        continue
                    try:
                        gap = (datetime.now()
                               - datetime.fromisoformat(str(last.at)[:19])).total_seconds()
                    except (ValueError, TypeError):
                        continue
                    # 静默要够久（2026-11-04）：12 分钟时你常还没睡，
                    # 或者刚说完晚安又聊起来，日记就写在半截上。
                    # 现在要 35 分钟没有新消息，而且只在夜里写。
                    if gap < 35 * 60:
                        continue
                    _h = datetime.now().hour
                    if not (_h >= 21 or _h < 5):
                        continue
                        continue
                    ok, msg = mind_diary.write_one(ch, session_key,
                                                  resolve_api_key() or "")
                    if ok:
                        print(f"[日记] {msg}", flush=True)
            except Exception as e:
                print(f"[日记巡检] {e!r}"[:160], flush=True)
                mind_diag.note("日记巡检", e)

    threading.Thread(target=loop, name="diary-watch", daemon=True).start()


def _is_asleep(ch) -> bool:
    try:
        import mind_activity
        return mind_activity.current_activity(ch) == "asleep"
    except Exception:
        return False


def _spawn_diary(ch, session_key: str, api_key: str) -> None:
    """她睡着的那一刻，写一篇日记。

    为什么是「睡着时」而不是零点：你聊到十二点半，她说着说着睡过去了 ——
    那才是她一天的结束。零点只是日历翻页。

    重复调用无害：`mind_diary.write_one` 自己查当天写过没有。

    日记**不进 prompt**，写不出来就是今天没有，她不会因此少记什么。
    """
    import threading

    def work() -> None:
        try:
            import mind_diary
            ok, msg = mind_diary.write_one(ch, session_key, api_key)
            if ok:
                print(f"[日记] {msg}", flush=True)
        except Exception as e:
            print(f"[日记] 出错：{e!r}"[:180], flush=True)
            mind_diag.note("后台写日记", e)

    threading.Thread(target=work, name="diary", daemon=True).start()


def _spawn_daily(session_key: str, api_key: str) -> None:
    """跨天时一次性处理每日事务（摘要 / 她的日子 / 游戏台词）。

    ## 为什么合并

    原来三个独立线程、三次模型调用，喂的几乎是同一份状态。
    合并后一次搞定，而且三样出自同一遍阅读，不会互相打架
    （摘要说「没提过」，台词却说「你昨天说的」那种矛盾）。

    ## 失败退回分开跑

    `mind_daily.run_once` 内部处理：合并版解析不出来就调三个单独模块。

    ## 只在主人会话上跑

    游戏台词只对「这一个玩家」有意义；QQ/群会话没有对应的那局游戏。
    摘要和她的日子其实对任何会话都有意义 —— 所以这两样仍然各会话独立。
    """
    import threading

    def work() -> None:
        try:
            import mind_daily
            from sessions import DESKTOP_KEY

            print("[每日] 后台开始", flush=True)
            with get_session_lock(session_key):
                ch = get_character(session_key)
                stage_key, _ = ch.effective_stage()
                allow_love = stage_key in ("hearts_8_plus", "dating",
                                           "engaged", "married")
                changed, msg = mind_daily.run_once(ch, api_key, allow_love=allow_love)
                print(f"[每日] changed={changed} {msg}", flush=True)
                if changed:
                    save_character(session_key, ch)
        except Exception as e:
            print(f"[每日] 出错：{e!r}"[:200], flush=True)
            mind_diag.note("后台每日事务", e)

    threading.Thread(target=work, name="daily", daemon=True).start()


def _spawn_gamelines(session_key: str, api_key: str) -> None:
    """跨天时生成一批**游戏内台词**，写进 saves/game_lines.json。

    ## 为什么只在主人会话上生成

    这座镇子里那个 NPC 是「这一个玩家」的她。别的会话（QQ、群）说的
    不是同一局游戏，给它们生成台词没有意义。

    ## 为什么在后台

    一次调用要 10-30 秒。放在聊天路径上会把当天第一句拖死。

    ## 失败不影响任何东西

    生成失败就**保留旧文件** —— 游戏里继续说昨天那批，
    或者实在没有就回退原版台词（mod 侧本来就这么设计）。
    """
    import threading
    from sessions import DESKTOP_KEY

    if session_key != DESKTOP_KEY:
        return

    def work() -> None:
        try:
            import mind_lines
            print("[游戏台词] 后台开始", flush=True)
            with get_session_lock(session_key):
                ch = get_character(session_key)
                stage_key, _ = ch.effective_stage()
                allow_love = stage_key in ("hearts_8_plus", "dating",
                                           "engaged", "married")
                changed, msg = mind_lines.run_once(
                    ch, api_key, allow_love=allow_love)
                print(f"[游戏台词] changed={changed} {msg}", flush=True)
        except Exception as e:
            print(f"[游戏台词] 出错：{e!r}"[:200], flush=True)
            mind_diag.note("后台生成游戏台词", e)

    threading.Thread(target=work, name="gamelines", daemon=True).start()


def _spawn_digest(session_key: str, api_key: str) -> None:
    """跨天时把**昨天**的对话压成几条要点。

    ## 为什么单独一个线程

    `refresh_day()` 在聊天路径上，同步调模型会把当天第一句拖慢。
    它和 `_spawn_her_event` 是两件事（一个摘要、一个写她自己的日子），
    各自失败互不影响 —— 所以不合并。

    ## 失败静默

    摘要失败就当没有：`digests` 少一天，其它照旧。
    但要进 diagnostics，不能又是一次「静默失败」。
    """
    import threading

    def work() -> None:
        try:
            import mind_digest
            print("[摘要] 后台开始", flush=True)
            with get_session_lock(session_key):
                ch = get_character(session_key)
                changed, msg = mind_digest.run_once(ch, api_key)
                print(f"[摘要] changed={changed} {msg}", flush=True)
                if changed:
                    save_character(session_key, ch)
        except Exception as e:
            print(f"[摘要] 出错：{e!r}"[:200], flush=True)
            mind_diag.note("后台生成每日摘要", e)

    threading.Thread(target=work, name="digest", daemon=True).start()


def _spawn_her_event(session_key: str, api_key: str) -> None:
    """跨天时给她补一条「昨天她那边发生的事」。

    ## 为什么在后台

    `refresh_day()` 在聊天路径上。同步调模型会把**当天第一句话**
    拖慢三秒 —— 而那句话恰恰是最重要的开场。

    ## 为什么先记下季节

    生成要知道「现在是什么季节」才好写。季节在 `refresh_day` 里
    从游戏存档读出来、存进了 `ch.today_season`。后台线程重新 load
    之后直接用它，不重复读存档。

    ## 失败静默

    这一类是锦上添花。模型挂了就退到语料（`mind_herday.run_once`
    里已经处理），语料也没了就当没发生 —— 但**要进 diagnostics**。
    """
    import threading

    def work() -> None:
        try:
            import mind_herday
            print("[她的日子] 后台开始", flush=True)
            with get_session_lock(session_key):
                ch = get_character(session_key)
                season = getattr(ch, "today_season", "") or ""
                changed, msg = mind_herday.run_once(
                    ch, api_key, season=season)
                # ⚠️ 不管成没成都打一行 —— 否则失败时这里完全是黑箱，
                # 只能看到「没生成」，看不出是不该生成还是出错了。
                print(f"[她的日子] changed={changed} {msg}", flush=True)
                if changed:
                    save_character(session_key, ch)
        except Exception as e:
            print(f"[她的日子] 出错：{e!r}"[:200], flush=True)
            mind_diag.note("后台生成她自己的事", e)

    threading.Thread(target=work, name="her-day", daemon=True).start()


def _spawn_observe(session_key: str, api_key: str) -> None:
    """后台跑一次「她眼里的他」的模型判据。

    ## 和 `_spawn_feel_judge` 是同一个套路

        主链路  你的话 -> 她的回复（2-5 秒）-> 立刻返回
        后台    判出新的观察 -> 存盘

    ## 为什么要重新读档

    后台跑的时候主链路可能正在写同一个会话。直接改内存再存盘
    会盖掉主链路刚写的东西。所以：拿锁 -> 重新 load -> 改 -> save。

    ## 失败静默

    观察是锦上添花。它失败不该让任何东西坏掉 ——
    但**要记进 diagnostics**，不能又是一次「静默失败」。
    """
    import threading

    def work() -> None:
        try:
            import mind_observe
            with get_session_lock(session_key):
                ch = get_character(session_key)
                ok, why = mind_observe.should_try(ch)
                if not ok:
                    return
                # run_once 现在返回 (有没有改动, 说明)。
                #
                # ⚠️ 必须在 **changed** 时存盘，而不是「新增了观察」时。
                # 因为 `observation_tried_at` 也要落盘 —— 否则「看不出新的」
                # 会被当成「没试过」，下一轮又试，等于每轮一次模型调用。
                changed, msg = mind_observe.run_once(ch, api_key)
                if changed:
                    save_character(session_key, ch)
                    print(f"[观察] {msg}", flush=True)
        except Exception as e:
            mind_diag.note("后台观察判断", e)

    threading.Thread(target=work, name="observe-judge", daemon=True).start()


def get_engine(api_key: str | None = None) -> MindEngine | None:
    global _engine
    if _engine is None:
        key = api_key or resolve_api_key()
        if not key:
            return None
        # 看图用的模型要和对话模型分开 —— `deepseek-chat` 不认图片。
        # 详见 mind_image.py 的模块说明和 MindEngine.__init__ 里的注释。
        _engine = MindEngine(api_key=key, vision_model=VISION_MODEL)
    return _engine


# ─────────────────────────────────────────────────────────────
class ChatHandler(BaseHTTPRequestHandler):
    base_dir: str = ""
    port: int = 0

    # 静默默认日志（我们自己打有用的）
    def log_message(self, fmt, *args):
        pass

    # ── 工具 ──────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ── 路由 ──────────────────────────────────────────────
    def _apply_session_header(self) -> None:
        """从 `X-Mind-Session` 头决定这一轮用哪个会话档。

        统一在这里设一次，后面所有 `get_character()` / `save_character()`
        都跟着走 —— 见 `_active_session` 的说明。
        """
        set_active_session(self.headers.get("X-Mind-Session", ""))

    def do_GET(self):  # noqa: N802
        try:
            self._apply_session_header()
            path = self.path.split("?", 1)[0]

            if path in ("/", "/index.html"):
                # 信息不全 → 先给填写页（2026-11-10）
                if _needs_setup():
                    return self._serve_file(HERE / "web" / "setup.html",
                                            "text/html; charset=utf-8")
                return self._serve_index()
            if path == "/api/setup":
                return self._json(_setup_page_state())
            # QQ 链路状态（2026-11-09）：左栏那个「接上 QQ」按钮读它
            if path == "/api/qq_link":
                return self._api_qq_link()
            # 左栏的星图（2026-11-10）
            if path == "/api/star_map":
                return self._api_star_map()
            # 星图点开某天：把那天说过的几句取出来（2026-11-10）
            if path == "/api/day_turns":
                return self._api_day_turns()
            if path == "/app.js":
                return self._serve_file(HERE / "web" / "app.js", "application/javascript; charset=utf-8")
            if path == "/style.css":
                return self._serve_file(HERE / "web" / "style.css", "text/css; charset=utf-8")
            # 图标（2026-11-02）
            if path in ("/icon.svg", "/favicon.svg"):
                return self._serve_file(HERE / "web" / "icon.svg",
                                        "image/svg+xml; charset=utf-8")
            if path == "/icon-192.png":
                return self._serve_file(HERE / "web" / "icon-192.png", "image/png")
            if path == "/icon-512.png":
                return self._serve_file(HERE / "web" / "icon-512.png", "image/png")
            if path == "/apple-touch-icon.png":
                return self._serve_file(HERE / "web" / "apple-touch-icon.png", "image/png")
            # ⚠️ 静态文件是**逐个列白名单**的，不是目录托管。
            # 所以加一个新的 js/css 必须在这里补一行 ——
            # 忘了补的症状是 404，而页面看起来「什么都没发生」
            # （实测踩过：voice.js 没加，播放键根本不出现，控制台才看得到 404）。
            # 语音在 v1.0 移除（voice.js 已删）
            if path == "/rewind.js":
                return self._serve_file(HERE / "web" / "rewind.js", "application/javascript; charset=utf-8")
            if path.startswith("/portrait/"):
                return self._serve_portrait(path.rsplit("/", 1)[-1])
            if path.startswith("/bgm/"):
                return self._serve_bgm(path.rsplit("/", 1)[-1])
            if path.startswith("/bg/"):
                return self._serve_bg(path.rsplit("/", 1)[-1])
            if path.startswith("/avatar/"):
                return self._serve_avatar(path.rsplit("/", 1)[-1])
            if path == "/glass":
                return self._serve_file(HERE / "web" / "glass_samples.html",
                                        "text/html; charset=utf-8")
            if path == "/diary":
                return self._serve_file(Path(self.base_dir) / "web" / "diary.html", "text/html; charset=utf-8")
            if path == "/api/diary":
                return self._api_diary()
            if path == "/api/status":
                return self._json(self._status_payload())
            if path == "/api/history":
                return self._json(self._history_payload())
            if path == "/api/saves":
                return self._json(self._saves_payload())
            if path == "/api/backup":
                return self._json(mind_backup.info())
            if path == "/api/favorites":
                return self._json(self._favorites_payload())
            if path == "/api/promises":
                return self._json(self._promises_payload())

            return self._json({"error": "not found"}, 404)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "server error"}, 500)

    def do_POST(self):  # noqa: N802
        try:
            self._apply_session_header()
            path = self.path.split("?", 1)[0]

            if path == "/api/setup":
                data = self._read_json()
                ok, msg = _apply_setup(data)
                return self._json({"ok": ok, "msg": msg})
            if path == "/api/chat":
                return self._api_chat()
            if path == "/api/key":
                return self._api_key()
            if path == "/api/reset":
                return self._api_reset()
            if path == "/api/settings":
                return self._api_settings()
            if path == "/api/regenerate":
                return self._api_regenerate()
            if path == "/api/diary":
                return self._api_diary()
            if path == "/api/qq_link":
                return self._api_qq_link()
            if path == "/api/qq":
                return self._api_qq()
            if path == "/api/memory":
                return self._api_memory()
            if path == "/api/recall":
                return self._api_recall()
            if path == "/api/snapshot":
                return self._api_snapshot()
            if path == "/api/test_baseline":
                return self._api_test_baseline()
            if path == "/api/rewind":
                return self._api_rewind()
            # 语音在 v1.0 移除（/api/tts 不再暴露）
            if path == "/api/correct":
                return self._api_correct()
            if path == "/api/open_topic":
                return self._api_open_topic()
            if path == "/api/backup":
                return self._api_backup()
            if path == "/api/promises":
                return self._api_promise()
            if path == "/api/notes_seen":
                return self._api_notes_seen()
            if path == "/api/favorite":
                return self._api_favorite()
            if path == "/api/unfavorite":
                return self._api_unfavorite()
            if path == "/api/favorite_note":
                return self._api_favorite_note()

            return self._json({"error": "not found"}, 404)
        except Exception:
            traceback.print_exc()
            return self._json({"error": "server error"}, 500)

    # ── 静态文件与立绘 ────────────────────────────────────
    def _serve_index(self) -> None:
        """发首页，并把主题**直接写进 HTML**。

        为什么要在服务端注入：如果等 JS 跑完再套主题，页面会先按默认色
        画一遍再跳成你选的那套 —— 开页时闪一下白/闪一下色。
        直接从服务端写 data-theme，浏览器第一次绘制就是对的。
        """
        try:
            html = (HERE / "web" / "index.html").read_text(encoding="utf-8")
        except OSError:
            return self._send(500, b"index missing", "text/plain; charset=utf-8")

        theme = str(load_settings().get("theme", "light"))
        if theme not in ("light", "moon", "ink", "candle"):
            theme = "light"
        html = html.replace("{{THEME}}", theme)
        return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_file(self, path: Path, ctype: str) -> None:
        """发静态文件。

        ## 为什么要显式禁缓存（2026-10-22 加）

        原来没有任何缓存头，浏览器会按 Last-Modified 做**启发式缓存** ——
        改完 `app.js` 刷新页面还是旧版。实测踩过：加了「手写事件」按钮，
        用户那边完全看不到，以为没做。

        这些都是本地开发用的文件，禁掉缓存代价为零。
        """
        if not path.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        try:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_avatar(self, name: str) -> None:
        """提供玩家头像（自己游戏里的小人，见 make_farmer_avatar.py）。

        如果还没生成（没解包过游戏贴图），就回一张占位图 ——
        让前端不必处理 404。
        """
        base = name[:-4] if name.endswith(".png") else name
        if not re.fullmatch(r"[a-z_]{2,24}", base):
            return self._send(400, b"bad avatar name", "text/plain; charset=utf-8")

        f = AVATAR_DIR / f"{base}.png"
        if f.is_file():
            return self._serve_file(f, "image/png")

        # 占位：一个中性的圆点图案
        try:
            from PIL import Image, ImageDraw
            S = 128
            im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.ellipse([4, 4, S - 4, S - 4], fill=(211, 203, 184, 255))
            d.ellipse([42, 34, 86, 78], fill=(250, 248, 242, 255))
            d.ellipse([26, 78, 102, 150], fill=(250, 248, 242, 255))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return self._send(200, buf.getvalue(), "image/png")
        except ImportError:
            return self._send(404, b"avatar missing", "text/plain; charset=utf-8")

    def _serve_bg(self, name: str) -> None:
        """提供背景纹理图。

        纹理来自 mod 自带的图块集，由 make_bg_texture.py 抽块、去色后平铺而成。
        **为什么用 mod 图块**：背景若用别的画风素材，又会变成「立绘混搭」那种别扭；
        用她自己场景里的图块，画风天然一致。

        同样走白名单 —— 不接受任意文件名。
        """
        base = name[:-4] if name.endswith(".png") else name
        if not re.fullmatch(r"[a-z_]{2,24}", base):
            return self._send(400, b"bad bg name", "text/plain; charset=utf-8")
        f = BG_DIR / f"{base}.png"
        if not f.is_file():
            return self._send(404, b"bg not found", "text/plain; charset=utf-8")
        return self._serve_file(f, "image/png")

    def _serve_bgm(self, name: str) -> None:
        """提供背景音乐文件。

        只允许白名单里的曲目名 —— 不接受任意文件名，
        否则就成了「读取任意文件」的漏洞。
        """
        base = name[:-4] if name.endswith((".mp3", ".ogg")) else name
        if base not in BGM_TRACKS:
            return self._send(404, b"unknown track", "text/plain; charset=utf-8")

        for ext, ctype in ((".mp3", "audio/mpeg"), (".ogg", "audio/ogg")):
            f = BGM_DIR / f"{base}{ext}"
            if f.is_file():
                return self._serve_file(f, ctype)
        return self._send(404, b"track file missing", "text/plain; charset=utf-8")

    def _serve_portrait(self, spec: str) -> None:
        """spec 形如 "face_default" / "expr_happy" / "0" / "0.png" / "face_default@2"。

        优先用官方立绘生成的高清脸（assets/face/，来自 1464×1464 原图），
        取不到才回退到 mod 的 64×64 素材。

        `@n` 后缀表示再放大 n 倍（一般不需要 —— 素材本身已是 384px，
        CSS 缩放即可；保留它只为兼容旧链接）。
        """
        # 先剥掉 @n 后缀
        scale = 1
        m_scale = re.match(r"^(?P<base>.+?)@(?P<n>\d+)$", spec)
        if m_scale:
            spec = m_scale.group("base")
            scale = max(1, min(int(m_scale.group("n")), 6))

        base = spec[:-4] if spec.endswith(".png") else spec

        # 安全校验：只允许简单文件名，防目录穿越
        if "/" in base or "\\" in base or ".." in base:
            return self._send(400, b"bad portrait spec", "text/plain; charset=utf-8")

        # 索引形式 -> 文件名（QQ 桥接与旧前端走这条）
        if base.isdigit():
            base = INDEX_TO_FILE.get(int(base), "face_default.png")[:-4]

        # 第三个来源（2026-11-04）：从社区 mod 的立绘表切出来的那 14 张。
        # 先看它 —— 选了 "her" 就整批用这一套，不再回落。
        if load_settings().get("portrait_source") == "her":
            target = MIND_FACE_DIR / (base + ".png")
            if target.is_file():
                if scale == 1:
                    return self._serve_file(target, "image/png")
                try:
                    from PIL import Image
                    im = Image.open(target).convert("RGBA")
                    im = im.resize((im.width * scale, im.height * scale),
                                   Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="PNG")
                    return self._send(200, buf.getvalue(), "image/png")
                except Exception:
                    return self._serve_file(target, "image/png")

        # 立绘来源开关。
        # 默认 "mod"：只用 mod 的像素立绘，保证画风统一。
        # "official"：无表情时用官方高清脸（face_default），有表情才回落到 mod 版。
        official_on = load_settings().get("portrait_source", "mod") == "official"
        if official_on and base == "face_default":
            target = FACE_DIR / "face_default.png"
            if target.is_file():
                if scale == 1:
                    return self._serve_file(target, "image/png")
                try:
                    from PIL import Image
                    im = Image.open(target).convert("RGBA")
                    im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="PNG")
                    return self._send(200, buf.getvalue(), "image/png")
                except Exception:
                    return self._serve_file(target, "image/png")

        # mod 的像素立绘（默认走这条）
        idx = EMOTION_INDEX_BY_FACE.get(base + ".png", 0)
        return self._serve_mod_portrait(idx, scale)

    def _serve_mod_portrait(self, idx: int, scale: int = 1) -> None:
        """mod 立绘（高清，不像素化）。

        优先发 assets/face/ 里预生成的那批（make_official_face.py 产出），
        取不到才回落到原始立绘表实时裁切。
        """
        fname = INDEX_TO_FILE.get(idx, "face_default.png")
        cand = FACE_DIR / fname
        if cand.is_file():
            return self._serve_file(cand, "image/png")

        if not PORTRAIT_SRC.is_file():
            return self._send(404, b"portrait source missing", "text/plain; charset=utf-8")
        try:
            from PIL import Image
            sheet = Image.open(PORTRAIT_SRC).convert("RGBA")
            cols = sheet.width // 64
            if idx < 0 or idx >= cols * (sheet.height // 64):
                return self._send(404, b"portrait index out of range", "text/plain; charset=utf-8")
            r, c = divmod(idx, cols)
            crop = sheet.crop((c * 64, r * 64, c * 64 + 64, r * 64 + 64))
            if scale != 1:
                crop = crop.resize((64 * scale, 64 * scale), Image.LANCZOS)
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            return self._send(200, buf.getvalue(), "image/png")
        except ImportError:
            return self._serve_file(PORTRAIT_SRC, "image/png")

    def _favorites_payload(self) -> dict:
        """列出收藏。按收藏时间倒序（新的在前）。"""
        ch = get_character()
        return {
            "count": len(ch.favorites),
            "items": [asdict(f) for f in ch.favorites],
        }

    def _api_favorite(self) -> None:
        """收藏一句她说的话。

        `context` 是**你上一句说了什么** —— 只存孤句的话，
        半年后翻出来完全想不起当时在聊什么。
        """
        data = self._read_json()
        text = (data.get("text") or "").strip()
        if not text:
            return self._json({"ok": False, "error": "没指定要收藏哪句"})

        # 洗掉表情记号（$h/$s/$u/$l/$a）与多余空白。
        #
        # 收藏是**给人看的**，不该出现内部记号。实测踩过：
        # 直接存原始文本，收藏里会留着「……你自己不会看。$l」这样的尾巴。
        # 立绘表情要单独留一份，因为它还要用来在收藏页显示同一张脸。
        emo = (data.get("emotion") or "").strip()
        if not emo:
            m = re.search(r"\$[hsula]", text)
            emo = m.group(0).lstrip("$") if m else ""
        clean = re.sub(r"\$[hsula]", "", text)
        clean = re.sub(r"[ \t]+\n", "\n", clean)
        clean = re.sub(r"\n{2,}", "\n", clean).strip()

        ch = get_character()
        # 当前心情与关系阶段，一并记下当时的场景
        mood = Mood.from_dict(ch.mood)
        stage_key, stage_name = ch.effective_stage()
        fav = ch.add_favorite(
            clean,
            context=(data.get("context") or "").strip(),
            mood=mood.label,
            stage=stage_name,
            emotion=emo,
            note=(data.get("note") or "").strip(),
        )
        save_character(current_session(), ch)
        return self._json({"ok": True, "favorite": asdict(fav),
                           "count": len(ch.favorites)})

    def _api_unfavorite(self) -> None:
        data = self._read_json()
        ch = get_character()
        ok = ch.remove_favorite(at=(data.get("at") or "").strip(),
                                text=(data.get("text") or "").strip())
        if ok:
            save_character(current_session(), ch)
        return self._json({"ok": ok, "count": len(ch.favorites)})

    def _api_favorite_note(self) -> None:
        """改备注 —— 不动台词本身。那是她说过的话，不该被改写。"""
        data = self._read_json()
        ch = get_character()
        ok = ch.edit_favorite(at=(data.get("at") or "").strip(),
                              note=(data.get("note") or "").strip())
        if ok:
            save_character(current_session(), ch)
        return self._json({"ok": ok})

    # 防止同时起多个生成线程（status 会被轮询）
    _note_lock = threading.Lock()
    _note_busy = False
    # 开场白同理 —— 它在 status 里触发，也会被轮询重复调到
    _greeting_lock = threading.Lock()
    _greeting_busy = False

    def _promises_payload(self) -> dict:
        ch = get_character()
        items = []
        for p in ch.promises:
            d = asdict(p)
            d["overdue"] = p.overdue
            items.append(d)
        # 没做到的排前面，新的在前
        items.sort(key=lambda x: (x["kept"], not x["overdue"], x["at"]),
                   reverse=False)
        return {"count": len(items),
                "live": sum(1 for x in items if not x["kept"]),
                "items": items}

    def _api_promise(self) -> None:
        """标记「这件事他做到了」，或者干脆忘掉这条。"""
        data = self._read_json()
        ch = get_character()
        op = str(data.get("op") or "keep")
        text = (data.get("text") or "").strip()
        at = (data.get("at") or "").strip()
        idx = data.get("index")
        if op == "forget":
            ok = ch.forget_promise(text=text, at=at)
        else:
            ok = ch.mark_promise_kept(text=text, at=at)
        # 留一行日志：手机/网页上"点了没反应"的时候，先看这句有没有出现
        print(f"[答应的事] op={op} at={at!r} text={text[:24]!r} -> {ok}",
              flush=True)
        # 兜底：老前端只发 at，而迁移进来的条目 at 为空（2026-11-08）
        if not ok and isinstance(idx, int) and 0 <= idx < len(ch.promises):
            pr = ch.promises[idx]
            if op == "forget":
                ch.promises.remove(pr)
                ok = True
            else:
                pr.kept = True
                pr.kept_at = datetime.now().isoformat(timespec="seconds")
                ok = True
        if ok:
            save_character(current_session(), ch)
        return self._json({"ok": ok})

    def _maybe_spawn_note(self, session_key: str) -> None:
        """后台判断并生成一条留话。

        为什么要后台：生成要走模型，几秒钟。放在 status 里会卡住整个页面。
        """
        ch = get_character()
        ok, why = mind_note.should_leave(ch)
        if not ok:
            return
        with ChatHandler._note_lock:
            if ChatHandler._note_busy:
                return
            ChatHandler._note_busy = True

        def work():
            try:
                n = mind_note.write_note(ch)
                if n:
                    ch.add_note(n)
                    # 同上：新线程里 current_session() 会退化成 desktop
                    save_character(session_key, ch)
            except Exception:
                traceback.print_exc()
            finally:
                with ChatHandler._note_lock:
                    ChatHandler._note_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _maybe_spawn_greeting(self, ch, session_key: str) -> None:
        """后台生成开场白。

        ## 为什么不在请求里生成

        `_status_payload()` 是**每次轮询**都会调的，一次模型调用几秒钟，
        放在里面会让整个窗口卡住。

        ## 什么时候生成

        只在「本次运行还没生成过」时（`greeting_stale()`）。
        存下来之后，同一次运行里刷新页面不会变 —— 变了会露馅。

        ## 生成要什么上下文

            隔了多久       从 last_talk_at 算
            她此刻的状态   greet() 内部从 Character 取（心情、手边的活、心事）
            游戏里的日子   季节、天气、第几天 —— 让开场有实处可落
        """
        if not ch.greeting_stale():
            return
        engine = get_engine()
        if engine is None:
            return          # 没配密钥，等配好了再生成
        with ChatHandler._greeting_lock:
            if ChatHandler._greeting_busy:
                return
            ChatHandler._greeting_busy = True

        # 在这里把上下文准备好，别把 Character 带进线程里乱动
        gap = ""
        try:
            if ch.last_talk_at:
                last = datetime.fromisoformat(ch.last_talk_at)
                delta = datetime.now() - last
                mins = int(delta.total_seconds() // 60)
                if mins < 60:
                    gap = f"上次说话是 {mins} 分钟前。"
                elif mins < 60 * 24:
                    gap = f"上次说话是 {mins // 60} 小时前。"
                else:
                    gap = f"上次说话是 {mins // (60 * 24)} 天前 —— 隔得有些久了。"
            else:
                gap = "你们还没说过话 —— 这是窗口第一次打开。"
        except Exception:
            gap = ""

        # 开场白用的事实改成**现实时令**（2026-10-27 与游戏脱钩）。
        # 原来读存档：你不开游戏，季节/天数就冻在最后一次存档那天。
        facts: list[str] = []
        try:
            import mind_season
            facts = mind_season.facts()
        except Exception:
            facts = []

        def work():
            try:
                # 每次只挑一两样她当天的状态 —— 不然反复生成开场白
                # 就会一直说同一件事（实测：连着两次都是「晾衣绳的桩子」）。
                #
                # 种子里带 time.time()：**同一个进程里每次调用都要不同**，
                # 因为开场白一次运行只生成一次，那一次就该是随机的一份。
                #
                # ⚠️ 这里原来写的是 `_RUN_SEED + str(time.time())`，
                # 而 `_RUN_SEED` 定义在 mind_character.py、server.py 没导入
                # 它（我写这段时漏了）—— 于是每次生成的线程里都抛
                # NameError，被 except 吞掉，focus 永远是空列表。
                # 症状：开场白又变回「老说同一件事」，但看不出哪里错了。
                focus = []
                try:
                    focus = mind_life.pick_focus(ch, str(random.random()))
                except Exception:
                    traceback.print_exc()
                ok, text = engine.greet(ch, gap=gap, game_facts=facts,
                                        focus=focus)
                if ok:
                    ch.set_greeting(text)
                    # ⚠️ 用参数里的 session_key，**不能用 current_session()**。
                    # 这里是新线程，threading.local 里没有请求上下文，
                    # current_session() 会退回 DESKTOP_KEY —— 于是 test 会话
                    # 生成的开场白被写进真实档（2026-10-18 实测证实）。
                    save_character(session_key, ch)
            except Exception:
                traceback.print_exc()
            finally:
                with ChatHandler._greeting_lock:
                    ChatHandler._greeting_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _api_notes_seen(self) -> None:
        """把留话标记为已看（前端展示过之后调）。"""
        ch = get_character()
        cnt = ch.mark_notes_seen()
        if cnt:
            save_character(current_session(), ch)
        return self._json({"ok": True, "marked": cnt})

    def _api_backup(self) -> None:
        """手动备份一次。

        启动时会自动备（每天一份），这个接口是给「我想现在就备一份」用的 ——
        比如刚聊完一段重要的。
        """
        data = {}
        try:
            data = self._read_json()
        except Exception:
            pass
        r = mind_backup.run(force=bool(data.get("force")))
        return self._json({"ok": r.get("ok", False), "result": r,
                           "info": mind_backup.info()})

    def _status_payload(self) -> dict:
        ch = get_character()

        # 不再从游戏同步关系（2026-10-27）：好感度维持现状。
        #
        # 原来的逻辑是「每次开面板都按存档刷一遍阶段」—— 存档读不到就可能
        # 退回「初识」。现在关系阶段是聊天档里的固定值，和游戏无关。

        # 心情：到点就重掷，**并且立刻存回**。
        # 只刷不存会导致每次请求都重新掷一次（见 mind_character.status 的注释）。
        try:
            _, mood_changed = ch.refresh_mood()
            if mood_changed:
                save_character(current_session(), ch)
        except Exception:
            mind_diag.note("server 里的refresh_mood、save_character")
            pass

        st = ch.status()

        # 她在睡 / 醒 —— 界面上那两栏要和对话一致。
        #
        # ⚠️ **只替换显示，不改存档**（2026-10-18 修的真故障）
        #
        # 原来是 `mind_activity.apply_to(ch)`，它直接改 `today_task` /
        # `today_thought`。而它只在「推断为睡着」时执行，**醒来没人改回来** ——
        # `refresh_day()` 只在跨天时重抽，today_key 早就是今天了。
        # 结果昨晚她躺下时写的两句，在界面上粘了一整天。
        # 用户的原话：「心里想的事又没有及时更新」。
        #
        # 现在：存档永远是当天的真实状态，这里只覆盖**返回给界面的那份**。
        try:
            import mind_activity
            _ov = mind_activity.display_override(ch)
            if _ov:
                st["task"], st["thought"] = _ov
        except Exception:
            mind_diag.note("_status_payload 里的 mind_activity")

        st["has_key"] = resolve_api_key() is not None
        # 她给你留的话（还没看的）。
        # 注意这里**只读不生成** —— 生成要走一次模型调用，几秒钟，
        # 放在这里会把打开窗口卡住。真正的生成丢给后台线程。
        # ⚠️ 返回**全部**留话（带 seen 标志），不是只返回未读的。
        #
        # 原来这里是 `ch.pending_notes()` —— 只给未读。而前端的设计是
        # 「点开 -> 标已读 -> 摊开内容」。于是展开的瞬间服务端已经没有数据，
        # 前端再一过滤就空了，框直接隐藏。
        #
        # 用户报的症状正是：「看到提醒但是打不开」。
        #
        # 已读只决定**徽标还提不提醒**，不该决定内容能不能看。
        st["notes"] = [x.to_dict() for x in ch.notes][-20:]
        self._maybe_spawn_note(current_session())
        st["portrait"] = emotion_to_face("")

        # 开场问候语：**实时生成**，但不在这个请求里生成。
        #
        # 这里每个页面轮询都会调到，直接调模型会把窗口卡住几秒。
        # 所以：先拿缓存，没有就起后台线程生成，下次轮询就有了。
        # （和留话的 _maybe_spawn_note 是同一个模式）
        st["greeting"] = ch.refresh_greeting()
        self._maybe_spawn_greeting(ch, current_session())

        # 三层记忆的加载状态，让界面能显示「她知道些什么」
        #
        # 注意：这里**直接读存档**，不走 engine。
        # engine 只在有 API 密钥时才创建，而界面在设密钥之前就该显示正确关系——
        # 否则用户第一次打开会看到「初识 0 心」，与游戏里的实际情况不符。
        kw = None
        engine = get_engine()
        if engine is not None:
            kw = engine.known_world
        else:
            try:
                from mind_memory import KnownWorld
                kw = KnownWorld.load(use_game_save=False)
            except Exception:
                kw = None

        rel = kw.game.shu if (kw and kw.game_loaded) else None
        # ── 「你们的日子」（2026-10-27 与游戏脱钩后新加）──
        #
        # 原来是游戏状态：好感 10 心 / 农场名 / 镇上交好。
        # 现在不读游戏了，那些全是空的 —— 换成真的会变的数字。
        try:
            from datetime import date as _date, datetime as _dt, timedelta as _td
            _fm = getattr(ch, "first_met", "") or ""
            try:
                _together = (_date.today()
                             - _dt.fromisoformat(_fm[:19]).date()).days + 1
            except (ValueError, TypeError):
                _together = 0
            _dates = {t.at[:10] for t in (ch.turns or [])}
            _since = (_date.today() - _td(days=29)).isoformat()
            _active30 = len([x for x in _dates if x >= _since])
            _prom = len([x for x in (ch.promises or [])
                         if not getattr(x, "kept", False)])
            _sub, _wx = "", ""
            try:
                import mind_season
                _, _sub = mind_season.season_of()     # 仲秋
                _wx = mind_season.weather_today()     # 阴，16 度上下（缓存 1 小时）
            except Exception:
                pass
            st["days"] = {
                "together": _together,
                # 累计轮次（不受 3000 窗口裁剪影响）
                "turns": (getattr(ch, "total_turns", 0)
                          or len(ch.turns or [])),
                "window": len(ch.turns or []),
                "active30": _active30,
                "mem": len(ch.memories or []),
                "obs": len(ch.observations or []),
                "evt": len(ch.events or []),
                "prom": _prom,
                "dig": len(ch.digests or []),
                "wk": len(ch.weeks or []),
                "season": _sub,
                "weather": _wx,
                # 最近记得的三件事（2026-11-05）：按**强度**取，
                # 强度和提示词里挑记忆用的是同一套算法 —— 界面上看到的
                # 就是她此刻真正常想起的那几条，不是另算一遍。
                "qq": _qq_state(self.base_dir),
                "mem_top": _top_memories(ch, 3),
                "trouble": getattr(ch, "today_trouble", "") or "",
                "tastes": list(getattr(ch, "today_tastes", []) or []),
                # 优先级：睡着 > 她最后一句的动作 > 存档里的位置
                "place": _resolve_place(ch),
            }
        except Exception:
            mind_diag.note("_status_payload 里的 days 统计")
            st["days"] = {}

        try:
            import mind_llm
            st["usage"] = mind_llm.usage_today()
        except Exception:
            st["usage"] = {}

        st["world"] = {
            "profile": bool(kw and kw.profile_loaded),
            "save": bool(kw and kw.game_loaded),
            "call_name": kw.call_name() if kw else "",
            "farm": kw.game.farm_name if (kw and kw.game_loaded) else "",
            "progress": (f"第 {kw.game.year} 年{kw.game.season}季第 {kw.game.day} 天"
                         if (kw and kw.game_loaded and kw.game.year) else ""),
            "friends": len([f for f in kw.game.friends if f[1] >= 400])
                       if (kw and kw.game_loaded) else 0,
            "save_name": kw.game.save_name if (kw and kw.game_loaded) else "",
            # 游戏里的她关系
            "mind_found": bool(rel and rel.found),
            "mind_status": rel.status if (rel and rel.found) else "",
            "mind_hearts": rel.hearts if (rel and rel.found) else 0,
            "mind_key": rel.npc_name if (rel and rel.found) else "",
        }
        return st

    def _history_payload(self) -> dict:
        ch = get_character()
        items = []
        for t in ch.recent_turns(60):
            items.append({
                "role": t.role,
                "text": MindEngine.clean_for_display(t.text),
                "emotion": t.emotion,
                # 立绘文件名，便于前端恢复历史最后一句时的神情
                "portrait": emotion_to_face(t.emotion),
                "at": t.at,
            })
        return {"turns": items, "status": self._status_payload()}

    def _saves_payload(self) -> dict:
        s = load_settings()
        saves = list_saves()
        return {
            "saves": saves,
            "selected": s.get("save_name", "latest"),
            "use_game_relation": bool(s.get("use_game_relation", True)),
            "portrait_source": s.get("portrait_source", "mod"),
            "theme": s.get("theme", "light"),
            "bgm_enabled": bool(s.get("bgm_enabled", False)),
            "bgm_track": s.get("bgm_track", "mind_theme"),
            "bgm_volume": float(s.get("bgm_volume", 0.35)),
            # 只列出磁盘上真实存在的曲目
            "bgm_tracks": [
                {"id": tid, "name": tname}
                for tid, tname in BGM_TRACKS.items()
                if any((BGM_DIR / f"{tid}{ext}").is_file()
                       for ext in (".mp3", ".ogg"))
            ],
        }

    def _api_settings(self) -> None:
        data = self._read_json()
        s = load_settings()

        if "save_name" in data:
            name = str(data["save_name"])
            # latest 或某个真实存在的存档目录名
            if name == "latest" or any(x["name"] == name for x in list_saves()):
                s["save_name"] = name

        if "use_game_relation" in data:
            s["use_game_relation"] = bool(data["use_game_relation"])

        if "theme" in data:
            th = str(data["theme"])
            if th in ("light", "moon", "ink", "candle"):
                s["theme"] = th

        if "portrait_source" in data:
            src = str(data["portrait_source"])
            if src in ("mod", "official"):
                s["portrait_source"] = src

        if "bgm_enabled" in data:
            s["bgm_enabled"] = bool(data["bgm_enabled"])

        if "bgm_track" in data:
            track = str(data["bgm_track"])
            if track in BGM_TRACKS:
                s["bgm_track"] = track

        if "bgm_volume" in data:
            try:
                v = float(data["bgm_volume"])
                s["bgm_volume"] = max(0.0, min(1.0, v))
            except (TypeError, ValueError):
                pass

        save_settings()

        # 换了存档就刷新世界与关系。
        # 注意：这里只处理桌面会话 —— 设置是全局的，
        # 但「关系」只属于桌面窗口（QQ 会话各按自己的会话键单独同步）。
        global _engine
        if _engine is not None:
            _engine.reload_world()
        if s.get("use_game_relation", True) and _engine is not None and _engine.known_world:
            try:
                ch = get_character()
                ch.sync_from_game(_engine.known_world.game)
                save_character(current_session(), ch)
            except Exception as e:
                # 原来这里是裸 pass —— 一个 NameError 被静默吞掉，
                # 导致设置看似保存了、接口却返回异常。现在至少记进日志。
                log_line = f"设置保存后同步关系失败：{e}"
                try:
                    print(f"[警告] {log_line}", flush=True)
                except Exception:
                    pass

        return self._json({"ok": True, "settings": s,
                           "status": self._status_payload()})

    def _api_key(self) -> None:
        data = self._read_json()
        key = (data.get("key") or "").strip()
        if not key:
            return self._json({"ok": False, "error": "密钥为空"})
        if not key.startswith("sk-"):
            return self._json({"ok": False, "error": "密钥格式不对（应以 sk- 开头）"})

        save_api_key(key)
        global _engine
        _engine = None      # 换 key 后重建引擎
        if get_engine() is None:
            return self._json({"ok": False, "error": "保存后初始化失败"})
        return self._json({"ok": True})

    def _api_reset(self) -> None:
        """「重新开始」—— 清空当前会话。

        ## ⚠️ 这个接口改过（2026-10-16）

        原来它**直接把存档移走**：

            p.replace(p.with_suffix(".json.bak"))

        后果是用户误触一次就丢掉 134 轮对话、6 条观察、2 个事件，
        而且下次保存会把全新的空档写下来 —— **看起来就像记忆凭空消失了**。
        靠一个孤零零的 .bak 才救回来。

        现在改成：**先打一份快照，再清空。**
        而且用 `copy` 而不是 `move` —— 移走之后不留文件的做法，
        会让「清空」和「文件丢了」在磁盘上长得一模一样，查不出区别。
        """
        data = self._read_json()
        if not data.get("confirm"):
            return self._json({"ok": False, "error": "需要确认"})

        session_key = current_session()
        p = session_store.path_of(session_key)

        # 先打快照 —— 让这次操作可逆
        snap = None
        try:
            import mind_snapshot
            r = mind_snapshot.take("auto", note=f"重新开始 {session_key} 之前")
            if r.get("ok"):
                snap = r.get("name")
        except Exception:
            traceback.print_exc()

        try:
            if p.is_file():
                p.unlink()          # 删掉，下次 load 会建全新的
        except OSError as e:
            return self._json({"ok": False, "error": f"清空失败：{e}"})

        return self._json({"ok": True, "session": session_key,
                           "snapshot": snap,
                           "note": "清空前已自动打快照，可在「记忆快照」里回滚"})

    def _run_chat(self, user_text: str, session_key: str,
                  use_game_relation: bool = True,
                  images: list[str] | None = None,
                  channel: str = "web") -> dict:
        """跑一轮对话 —— **加会话锁的薄包装**。

        ## 为什么要有这一层（2026-10-17 加）

        `_run_chat_locked` 的流程是「读盘 -> 改十几次 -> 写盘」，
        中间夹着一次 LLM 调用（2-5 秒）。**没有锁的话两个并发请求会互相盖：**

            请求 A 读盘 -> N 轮        请求 B 读盘 -> N 轮
            A 加一轮 -> N+1            B 加一轮 -> N+1
            A 写盘 -> N+1              B 写盘 -> N+1     ← A 那一轮没了

        表现是「我明明说了，她不记得」。

        ## 锁的粒度是「会话」

        不同会话不共享任何状态，没理由互相等 ——
        桌面的请求不该阻塞 QQ 的。见 `sessions.get_session_lock`。

        ## 代价

        同一个会话不能并发，而一次聊天占 2-5 秒以上。
        但这不是损失：一个人的对话本来就是顺序的，
        前端 `busy` 也禁止了连发，QQ 消息同样顺序到达。
        """
        # ⚠️ `get_session_lock` 是 sessions.py 的**模块级函数**，不是
        # `SessionStore` 的方法。写成 `session_store.get_session_lock(...)`
        # 会 AttributeError -> 每次对话 500。踩过。
        with get_session_lock(session_key):
            return self._run_chat_locked(user_text, session_key,
                                         use_game_relation, images, channel=channel)

    def _run_chat_locked(self, user_text: str, session_key: str,
                  use_game_relation: bool = True,
                  images: list[str] | None = None,
                  channel: str = "web") -> dict:
        """跑一轮对话。桌面窗口与 QQ 桥接都走这里，保证行为一致。

        Args:
            user_text: 他说的话。
            session_key: 会话键（桌面窗口是 desktop，QQ 是 qq_<号>）。
            use_game_relation: 是否套用游戏里的关系。**只有主人**该为 True。
            images: 图片的 data: URL 列表。有图时 engine 会自动切到视觉模型。
        """
        engine = get_engine()
        if engine is None:
            return {"ok": False, "need_key": True, "error": "还没有设置 API 密钥"}

        # 每次对话前重读存档：你刚玩过游戏的话，她就该知道进展了。
        # 读不到也不影响，只是少知道一些事。
        #
        # 关键：**只有主人的会话才读存档**。
        # 否则她会对陌生人说出你的农场名、角色名、和镇上谁交好。
        engine.reload_world(use_game_save=False)

        ch = get_character(session_key)
        # 跨天信号来自**读档那一次**（`Character.load` 里已经调过
        # refresh_day，结果记在 `_refreshed`）。这里再调一次只会拿到 False。
        _new_day = getattr(ch, "_refreshed", False) or ch.refresh_day()
        if _new_day:
            _k = resolve_api_key() or ""
            _spawn_daily(session_key, _k)
        # 心情：距上次超过一小时才重掷（见 mind_mood.py）
        mood, mood_changed = ch.refresh_mood()

        # 关系同步。注意：**只有主人会话才同步游戏关系**。
        # 别人的会话没有对应的存档，硬套主人的关系会让她对陌生人也像交往中。
        sync_note = ""
        if use_game_relation and engine.known_world:
            try:
                changed, sync_note = ch.sync_from_game(engine.known_world.game)
            except Exception:
                sync_note = ""

        # 人设词表校验已移除（2026-10-23，按用户要求）。
        #
        # 原来这里会算 allow_love，然后把回复过一遍四张词表
        # （泰拉专有名词 / 军事意象 / 网络用语 / 格式残留），
        # 命中就在界面上挂一行「这句偏离了人设：含军事意象「战场」」。
        #
        # 为什么去掉：
        #   ① 它只按**字面**判，不看语境 ——「心里像战场」也会中
        #   ② 它**只警告不拦截**，那句话照样进存档、照样发出去
        #   ③ 人设卡里那条禁令本来就写着，模型绝大多数时候会遵守
        #
        # 现在人设靠卡里的指令 + 后面那层「被拦就不进历史」兜底。
        ok, text = engine.reply(ch, user_text, images=images, channel=channel)
        if not ok:
            # 【被拦】= 内容策略挡下。**这轮什么都不写进存档**。
            if text.startswith("【被拦】"):
                try:
                    mind_diag.note("内容策略拦下（未存盘）",
                                  RuntimeError(text[:160]))
                except Exception:
                    pass
                print(f"[被拦] {text[:80]}", flush=True)
                return {"ok": False,
                        "error": "这句模型没接（不是她的问题），"
                                 "没有写进记忆。换个说法再试。"}
            return {"ok": False, "error": text}

        # 软拒绝：接口没报错，但回的是套话。同样**不进历史** ——
        # 进了她就会学着这么说（见 _REFUSAL_RE 上面那段说明）。
        if _looks_like_refusal(text):
            try:
                mind_diag.note("软拒绝（未存盘）", RuntimeError(text[:160]))
            except Exception:
                pass
            print(f"[软拒绝] 已拦下：{text[:60]}", flush=True)
            return {"ok": False,
                    "error": "这句模型没接（不是她的问题），"
                             "没有写进记忆。换个说法再试。"}

        emotion = engine.extract_emotion(text)

        # 存进历史的玩家那一轮要**带上「他发了图」这个事实** ——
        # 否则下一轮她回头看，会看到一句没头没尾的话
        # （「你看这个」），而不知道当时有图。
        stored = user_text
        if images:
            n = len(images)
            stored = (user_text + f"\n（他发了 {n} 张图）").strip()
        # 日记不在这里触发（2026-10-29 改）。
        #
        # 原来靠「她睡着」判定（深夜 + 静默 20 分钟）—— 实测会在
        # 你还没说晚安的时候就写，等于把一天提前结了。
        # 现在改成：**你道过晚安 + 静默 12 分钟**，由后台巡检触发。

        # channel 只做记录（web / qq），**不进提示词**（2026-11-08 用户要求：
        # 记下来可以，但不要让她拿"你是不是在QQ上找过我"当话题）
        ch.add_turn("player", stored, channel=channel)
        ch.add_turn("her", text, emotion, channel=channel)
        ch.mark_talked()
        # 心情微调：贴心的话让她好一点，敷衍让她低一点。
        # 注意**不动关系** —— 关系始终由这座镇子存档决定。
        # 心情：原来在这里同步调 `note_turn_mood`（词表判据）。
        # 现在改成后台用模型判 —— 见 `_spawn_feel_judge`。
        # **这里不再动心情**，否则会和后台那次互相盖。
        try:
            _spawn_feel_judge(session_key, user_text, text,
                              resolve_api_key() or "")
        except Exception:
            mind_diag.note("起情绪判断线程")

        # 她可能刚好睡着 —— 睡着那一刻写日记（写一次，重复调用无害）
        if _is_asleep(ch):
            _spawn_diary(ch, session_key, resolve_api_key() or "")
        # 从你这句里认承诺（「明天给你带点鱼」这类）。
        # 认错了会显得她自作多情，所以 mind_promise.detect 刻意偏保守。
        try:
            # 显式传 "player" —— 只记他答应的，不记她自己说的
            ch.note_promise(user_text, role="player")
        except Exception:
            mind_diag.note("server 里的note_promise")
            pass
        # 她眼里的他：从他的话里看出「他是怎样的人」。
        # 与记忆系统分工：记忆记信息，这个记行为模式（见 mind_life.observe）。
        #
        # 这里只走**正则快路径**（不花钱）。它只能覆盖 10 种固定说法，
        # 而且上限原来是 6 条，攒满就再也不会动 ——
        # 用户报的「『她眼里的我』好几天没更新」就是这个。
        # 慢路径（模型判）在下面：后台、带冷却，补正则够不到的那些。
        try:
            ch.note_observation(user_text)
        except Exception:
            mind_diag.note("server 里的note_observation")
            pass
        # 慢路径：让模型从最近对话里看出**一条新的**观察。
        #
        # ## 为什么要后台
        #
        # 一次模型调用 1-3 秒，而它只影响「她以后怎么看他」，
        # 不影响这一轮的回复。让用户等它没道理。
        #
        # ## 为什么要冷却
        #
        # 观察是慢慢攒的东西。每轮一条，一周几百条且都差不多 ——
        # 那不叫认识。冷却参数见 mind_observe.py。
        try:
            _spawn_observe(session_key, resolve_api_key() or "")
        except Exception:
            mind_diag.note("起观察判断线程")
        # 你们之间**发生过的事** —— 关系史。
        #
        # 和上面几件的分工：
        #     记忆   他说的信息
        #     观察   她看出来的性格
        #     承诺   他答应的事
        #     事件   **你们一起做过什么**（见 mind_event.py）
        #
        # 判据刻意保守 —— 66 句真实对话里大约收 2 条。
        # 误判的代价是她在意一件你根本没当回事的事，那比漏记更糟。
        try:
            # 2026-11-06 停用：原来这里是 mind_event.detect() 把他的话**裁一段**
            # 直接存成事件，于是事件栏全是转录片段。
            # 现在事件改由 judge_ex 的 event 字段产出（模型改写、第三人称、
            # 不带相对时间），在后台线程里写。
            pass
        except Exception:
            mind_diag.note("server 里的note_event")
            pass

        # 注意：**不在这里加好感**。
        # 关系完全由这座镇子存档决定，窗口只负责聊天与记忆。

        # QQ 上**不写事实记忆**（2026-10-28 用户要求）。
        #
        # 那条路是正则匹配「我是…」「我喜欢…」这类句式，在随口聊的
        # 手机语境里最容易误记 —— 记错一条，她会在几天后当真话讲出来。
        # 事件（发生过的）仍然记，只停掉「事实」这一种。
        # 事实记忆改由**模型判**（2026-11-02）：在 _spawn_feel_judge 里，
        # 同一句话顺带判断 salience，≥2 才写。正则那条路停用 ——
        # 它只看句式不看内容，抓进来一堆「我爱你（凑到耳朵边」这种东西。
        # QQ 上本来就不写（手机语境容易误记）。
        remembered = None
        save_character(session_key, ch)

        return {
            "ok": True,
            "reply": engine.clean_for_display(text),
            "raw": text,
            # 本地时间戳，**前端要用它**。
            #
            # ## 为什么必须给（2026-10-16 修）
            #
            # 原来不返回时间戳，前端只能自己造：
            #
            #     addBubble('me', text, new Date().toISOString())
            #
            # 而 `toISOString()` 按定义输出 **UTC**。于是同一段对话里：
            #
            #     历史消息（从服务端读的）  "2026-10-16T23:38:21"       本地
            #     刚发的那条（前端造的）    "2026-10-16T14:45:37.123Z" UTC
            #     显示出来                  23:38 和 14:45 —— 差 9 小时
            #
            # 前端也已经能认时区了（`hhmm()`），但**从源头给对更干净** ——
            # 两种格式混在同一个列表里，迟早还会咬人。
            "at": datetime.now().isoformat(timespec="seconds"),
            "emotion": emotion,
            "portrait": emotion_to_face(emotion),
            "mood": {"label": mood.label, "tone": mood.tone,
                     "event": mood.event, "valence": mood.valence,
                     "energy": mood.energy, "tier": mood.tier,
                     "changed": mood_changed},
            "remembered": remembered,
            "sync_note": sync_note,
            "status": ch.status(),
        }

    def _api_memory(self) -> None:
        """记忆的增删改 —— 「她的记忆面板」的后端。

        请求体按 op 区分（2026-10-19 起支持 kind）：

            {"op": "list"}

            {"op": "add",    "text": "...", "kind": "fact"|"guidance"}
            {"op": "edit",   "kind": "memory",  "index": 0, "text": "..."}
            {"op": "delete", "kind": "memory",  "index": 0}

            {"op": "edit",   "kind": "observation", "index": 0, "text": "..."}
            {"op": "delete", "kind": "observation", "index": 0}
            {"op": "edit",   "kind": "event", "index": 0, "text": "..."}
            {"op": "delete", "kind": "event", "index": 0}
            {"op": "star",   "kind": "event", "index": 0, "first": true}
            {"op": "delete", "kind": "promise", "at": "..."}
            {"op": "edit",   "kind": "promise", "at": "...", "text": "..."}

        kind 缺省 = memory（保持旧调用行为）。

        为什么这个接口重要：没有它，你无法验证她是真的记得还是编的。
        记忆是整件事最核心的能力，不该是个黑盒。
        """
        data = self._read_json()
        op = str(data.get("op") or "list")
        session_key = current_session()

        ch = get_character(session_key)
        changed = False

        # kind 缺省是 memory —— 前端旧调用不带 kind，保持原行为不变
        kind = str(data.get("kind") or "").strip()
        try:
            idx = int(data.get("index", -1))
        except (TypeError, ValueError):
            idx = -1
        text = str(data.get("text") or "").strip()
        at = str(data.get("at") or "").strip()

        if op == "add":
            if kind == "event":
                # 手动记一件事件（面板上的「＋ 记一件」）
                changed = bool(ch.add_event_manual(
                    text, first=bool(data.get("first"))))
            else:
                k = "guidance" if data.get("kind") == "guidance" else "fact"
                if text:
                    changed = ch.remember(text, kind=k, source="manual")

        elif op == "edit":
            # ⚠️ 分支顺序要紧：`op == "edit"` 是**兜底**，所有 edit 都先进这里。
            # 我第一版把 her_event/project 写成了后面另一个
            # `elif op == "edit" and kind in (...)` —— 永远走不到；
            # 而且 kind="her_event" 会掉进最后的 edit_memory，**改错一条记忆**。
            if kind == "her_event":
                changed = ch.edit_her_event(idx, text)
            elif kind == "project":
                changed = ch.edit_project(
                    idx, text=text,
                    what=data.get("what"),
                    stage=(int(data["stage"]) if "stage" in data else None))
                # 进度到顶 = 做完了：立刻写进她的旧事并移除，
                # 不等明天跨天（否则你点了「完成」，面板上还挂一天）
                if 0 <= idx < len(ch.projects):
                    pr = ch.projects[idx]
                    st = (pr.get("stage", 0) if isinstance(pr, dict)
                          else getattr(pr, "stage", 0))
                    tt = (pr.get("total", 3) if isinstance(pr, dict)
                          else getattr(pr, "total", 3))
                    if st >= tt:
                        ch.complete_project(idx)
                        changed = True
            elif kind == "observation":
                changed = ch.edit_observation(idx, text)
            elif kind == "event":
                changed = ch.edit_event(idx, text)
            elif kind == "promise":
                changed = ch.edit_promise(at, text)
            elif idx >= 0 and text:
                changed = ch.edit_memory(idx, text)

        elif op == "star":
            # 手动标 / 取消「头一回」。★ 永不被上限裁掉，见 _trim_events
            if kind == "event":
                changed = ch.set_event_first(
                    idx, bool(data.get("first", True)))

        elif op == "delete":
            if kind == "her_event":
                changed = ch.remove_her_event(idx)
            elif kind == "project":
                changed = ch.remove_project(idx)
            elif kind == "observation":
                changed = ch.remove_observation(idx)
            elif kind == "event":
                changed = ch.remove_event(idx)
            elif kind == "promise":
                changed = ch.forget_promise(at=at, text=text)
            elif idx >= 0:
                changed = ch.forget(idx)

        if changed:
            save_character(session_key, ch)

        return self._json({
            "ok": True,
            "changed": changed,
            "session": session_key,
            "memories": ch.memory_list(),
            # ── 只读的部分 ────────────────────────────────
            #
            # 这个面板原来只列 memories。但她的「记得」有四种东西，
            # 只看得到一种的话，你没法判断她到底记不记得某件事 ——
            # 而「她是真的记得还是编的」正是这个程序最核心的能力。
            #
            # 所以把另外三种也摊出来。它们**只读** —— 要改就改 memories
            # （那是唯一有增删改语义的一类）。
            "observations": list(ch.observations or []),
            # 她自己的过去 + 手上没做完的活（2026-10-20 加）
            "her_events": [
                {"text": e.text, "day": e.day, "kind": e.kind}
                for e in (getattr(ch, "her_events", None) or [])
            ],
            "projects": [
                {"text": (p.get("text", "") if isinstance(p, dict)
                          else getattr(p, "text", "")),
                 "what": (p.get("what", "") if isinstance(p, dict)
                          else getattr(p, "what", "")),
                 "stage": (p.get("stage", 0) if isinstance(p, dict)
                           else getattr(p, "stage", 0)),
                 "total": (p.get("total", 3) if isinstance(p, dict)
                           else getattr(p, "total", 3)),
                 "started": (p.get("started", "") if isinstance(p, dict)
                             else getattr(p, "started", ""))}
                for p in (getattr(ch, "projects", None) or [])
            ],
            # 每条观察是什么时候看出来的 —— 让人一眼看出「多久没更新了」。
            # 旧存档没有这个字段，给空 dict 即可。
            "observation_meta": dict(getattr(ch, "observation_meta", {}) or {}),
            # 你们之间发生过的事 —— 关系史（★ 标记的是「头一回」）
            "events": [
                {"text": getattr(e, "text", ""),
                 "kind": getattr(e, "kind", "together"),
                 "day": getattr(e, "day", "")}
                for e in (ch.events or [])
            ],
            # Promise 的字段名是 at / due / kept / said_by ——
            # 不是 made / done（我第一次写错了，接口直接 500）。
            "promises": [
                {"text": p.text,
                 "at": getattr(p, "at", ""),
                 "due": getattr(p, "due", ""),
                 "kept": bool(getattr(p, "kept", False)),
                 "said_by": getattr(p, "said_by", "")}
                for p in (ch.promises or [])
            ],
            "stats": {
                "turns": len(ch.turns or []),
                "total_turns": ch.total_turns,
                "first_met": ch.first_met,
                "last_talk_at": ch.last_talk_at,
            },
        })

    # ── 语音（2026-10-17 加）──────────────────────────
    #
    # 语音服务是独立的（GPT-SoVITS，默认 127.0.0.1:9880），
    # 所以这里是**转发**：收到一句文本 -> 调它 -> 把 wav 转回去。
    #
    # 为什么不直接让浏览器调 9880：
    #     ① 那个服务没开 CORS，跨端口会被浏览器拦掉
    #     ② 端口/参考音频的配置该集中在一处
    #     ③ 以后要换后端（IndexTTS 之类），前端不用动

    def _api_rewind(self) -> None:
        """重做最后一轮。

        请求体：
            {"op": "regen"}                 她的回复重新生成
            {"op": "edit", "text": "..."}   改了你的话再重跑

        ## 为什么两件事合成一个端点

        它们底下是同一个操作：**先把最后那对问答从记忆里抹掉，再重跑一遍。**

        不抹掉的话她会看到重复的输入 ——
            实测症状：连着问两次同一个问题，她回「这话你今天是第二回说了」。

        见 `Character.drop_last_exchange`。
        """
        data = self._read_json()
        op = str(data.get("op") or "regen")
        session_key = current_session()

        with get_session_lock(session_key):
            ch = get_character(session_key)

            # 取回「我上一句说了什么」—— 重跑要用
            last_user = ""
            for t in reversed(ch.turns or []):
                if t.role == "player":
                    last_user = t.text or ""
                    break
            if not last_user:
                return self._json({"ok": False, "error": "没有可重做的对话"})

            text = str(data.get("text") or "").strip() if op == "edit" else last_user
            if not text:
                return self._json({"ok": False, "error": "不能改成空的"})

            dropped = ch.drop_last_exchange()
            save_character(session_key, ch)

        # 出了锁再跑 —— 重跑要调模型（2-5 秒），
        # 而 `_run_chat` 自己会重新加锁，不能嵌套。
        result = self._run_chat(text, session_key,
                                use_game_relation=bool(
                                    load_settings().get("use_game_relation", True)))
        return self._json({**result, "rewound": dropped,
                           "mode": "edit" if op == "edit" else "regen"})

    def _api_tts(self) -> None:
        """合成**一整条回复**，返回一条 audio/wav。

        请求体：{"text": "她的回复原文"}

        ## 为什么收整条而不是一句（2026-10-17 改）

        原来前端逐段来取、逐段播（`<audio>` 一个接一个）。
        用户反馈「停顿太多太夸张」—— 查出来主因是**段与段的接缝**：
        每段自带首尾静音，加上元素切换，听起来一顿一顿。

        现在服务端拆解 + 逐段合成 + **样本级拼成一条**：
        相邻段直接接上，模型自带的呼吸自然连在一起，中间不留缝。

        ⚠️ **不裁静音。** 试过裁（每段能省 0.4-0.6s），用户否了 ——
        那是模型自己的呼吸，裁完语气更硬。要减的是我额外加的那道停顿，
        而那个由 `mind_speech` 的 PAUSE_* 参数控制。
        """
        data = self._read_json()
        text = (str(data.get("text") or "")).strip()
        if not text:
            return self._json({"ok": False, "error": "没有可念的文字"})

        wav = _tts_synth_turn(text)
        if not wav:
            return self._json({
                "ok": False,
                "error": "语音服务没响应 —— 9880 端口开着吗？",
            })
        self._send(200, wav, "audio/wav")

    def _api_test_baseline(self) -> None:
        """测试档基线：打基线 / 回到基线。

        请求体：
            {"op": "make"}   从真实档重建基线
            {"op": "reset"}  把测试档恢复成基线（默认）

        ## 为什么需要

        测试档原来是「随手用、随手清」，每次状态都不同，
        **测出来的结论不可复现**。今天为此浪费过时间：
        在测试档里得到「她聊得来当代话题」的结论，
        但那一版的测试档其实带着真实档的 169 轮底子，
        所以那个结论证明不了卡片改动有效。

        基线 = 一个**只有她、没有这段对话**的起点。
        """
        data = self._read_json()
        op = str(data.get("op") or "reset")
        if op == "make":
            r = session_store.make_test_baseline()
        else:
            r = session_store.reset_to_baseline()
        return self._json(r)

    def _api_snapshot(self) -> None:
        """记忆快照 —— 列 / 打 / 回滚 / 删。

        请求体按 op 区分：
            {"op": "list"}
            {"op": "take",    "note": "..."}
            {"op": "restore", "name": "auto-20261016-221400"}
            {"op": "delete",  "name": "..."}

        ## 为什么要有这个

        用户 2026-10-16 误触了「重新开始」，一瞬间丢掉 134 轮对话。
        靠一个孤零零的 .bak 救回来。而游戏存档早就有「多槽位 + 读档」，
        记忆一直缺这一半 —— 见 mind_snapshot.py 的模块说明。
        """
        import mind_snapshot

        data = self._read_json()
        op = str(data.get("op") or "list")

        if op == "take":
            note = str(data.get("note") or "").strip()[:60]
            return self._json(mind_snapshot.take("manual", note=note))

        if op == "restore":
            name = str(data.get("name") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "要指定哪一份"})
            r = mind_snapshot.restore(name)
            # 回滚之后内存里那份是旧的 —— 下次请求会重新 load，不用管
            return self._json(r)

        if op == "delete":
            name = str(data.get("name") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "要指定哪一份"})
            return self._json(mind_snapshot.delete(name))

        return self._json({"ok": True, "snapshots": mind_snapshot.list_all()})

    def _api_recall(self) -> None:
        """试一次召回：给我一个词，看她能翻出哪些旧对话。

        ## 为什么值得单独开一个接口

        「她记不记得那件事」现在只能靠聊天试探 —— 你问一句，看她接不接得上。
        但接不上有两种可能：**她忘了**，或者**召回的判据没匹配上**。
        这两件事的修法完全不同，而从对话里分不出来。

        有了这个接口，你可以直接试：

            查「小满」  -> 翻了 3 条   => 记得，只是平时没问到
            查「小满」  -> 翻了 0 条   => 召回的判据漏了，该调

        请求体：{"query": "小满", "limit": 6}
        """
        data = self._read_json()
        query = str(data.get("query") or "").strip()
        if not query:
            return self._json({"ok": False, "error": "给个词才能试"})
        try:
            limit = int(data.get("limit") or 6)
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(20, limit))

        ch = get_character(current_session())
        try:
            hits = ch.recall_turns(query, limit=limit,
                                   exclude_recent=0)   # 试召回时不排除近期
        except Exception as e:
            return self._json({"ok": False, "error": f"召回出错：{e}"})

        return self._json({
            "ok": True,
            "query": query,
            "count": len(hits),
            "hits": [
                {"role": t.role, "text": t.text, "at": (t.at or "")[:16]}
                for t in hits
            ],
            "note": ("没翻到。要么她真没听过这个词，"
                     "要么召回的判据（连续二字重合 + 稀有度门槛 0.25）没匹配上 —— "
                     "两种情况的修法不同。") if not hits else "",
        })

    def _api_correct(self) -> None:
        """「你能纠正她」的接口。

        把一句话记成**行为要求**（kind=guidance）：她以后照做，
        但不会把这条要求当话题回述出来（否则每次都说「好的我记住了」，很出戏）。
        """
        data = self._read_json()
        text = str(data.get("text") or "").strip()
        if not text:
            return self._json({"ok": False, "error": "没有内容"})
        if len(text) > 120:
            return self._json({"ok": False, "error": "太长了（上限 120 字）"})

        session_key = current_session()
        ch = get_character(session_key)
        added = ch.correct(text)
        save_character(session_key, ch)

        return self._json({
            "ok": True,
            "added": added,
            "text": text,
            "memories": ch.memory_list(),
        })

    def _api_open_topic(self) -> None:
        """让她主动开口。"""
        data = self._read_json()
        session_key = current_session()

        engine = get_engine()
        if engine is None:
            return self._json({"ok": False, "need_key": True,
                               "error": "还没有设置 API 密钥"})

        use_relation = (current_session() == DESKTOP_KEY
                        and load_settings().get("use_game_relation", True))
        engine.reload_world(use_game_save=False)

        ch = get_character(session_key)
        if getattr(ch, "_refreshed", False) or ch.refresh_day():
            _k2 = resolve_api_key() or ""
            _spawn_daily(session_key, _k2)
        if use_relation and engine.known_world:
            try:
                ch.sync_from_game(engine.known_world.game)
            except Exception:
                mind_diag.note("server 里的sync_from_game")
                pass

        ok, text = engine.open_topic(ch)
        if not ok:
            return self._json({"ok": False, "error": text})

        emotion = engine.extract_emotion(text)
        ch.add_turn("her", text, emotion)
        ch.mark_talked()
        save_character(session_key, ch)

        return self._json({
            "ok": True,
            "reply": engine.clean_for_display(text),
            "emotion": emotion,
            "portrait": emotion_to_face(emotion),
            "status": ch.status(),
        })


    def _api_diary(self) -> None:
        """日记：读列表 / 收藏。**不进 prompt**，纯本地阅读。"""
        import mind_diary
        key = current_session()
        if self.command == "GET":
            return self._json({"ok": True, "entries": mind_diary.load(key)})
        data = self._read_json()
        day = str(data.get("day") or "")[:10]
        starred = mind_diary.toggle_star(key, day) if day else False
        return self._json({"ok": bool(day), "starred": starred,
                           "entries": mind_diary.load(key)})


    def _api_regenerate(self) -> None:
        """重新生成「今天的起居」或者「今天的日记」（2026-11-07）。

        只重写展示字段，**绝不碰 turns**。
        用户点按钮触发，所以必须当场返回结果，不能丢后台。
        """
        import mind_daily
        import mind_diary
        key = current_session()
        what = str(self._read_json().get("what") or "").strip()

        if what == "life":
            ch = get_character(key)
            before = len(ch.turns or [])
            ch.life_brief_day = ""      # 强制重新生成
            ok, msg = mind_daily.run_once(ch, resolve_api_key())
            if len(ch.turns or []) != before:
                return self._json({"ok": False, "error": "轮次被改动，已中止"})
            if ok:
                save_character(key, ch)
            return self._json({"ok": bool(ok), "msg": str(msg)[:120],
                               "task": getattr(ch, "today_task", ""),
                               "thought": getattr(ch, "today_thought", "")})

        if what == "diary":
            ch = get_character(key)
            today = mind_diary.logical_today().isoformat()
            keep = [x for x in mind_diary.load(key)
                    if str(x.get("day"))[:10] != today]
            mind_diary.save(key, keep)
            ok, msg = mind_diary.write_one(ch, key, resolve_api_key())
            ents = mind_diary.load(key)
            return self._json({"ok": bool(ok), "msg": str(msg)[:120],
                               "entries": ents})

        return self._json({"ok": False, "error": "what 只能是 life / diary"})

    def _touch_qq_state(self, user_id, is_owner: bool, channel: str) -> None:
        """记一下 QQ 桥接最近的活动（2026-11-05）。

        只是给左栏那块显示用的：最近一次什么时候来的、一共多少次。
        失败不影响任何事 —— 它纯展示。
        """
        try:
            f = Path(self.base_dir) / "saves" / "qq_state.json"
            st = {}
            if f.is_file():
                st = json.loads(f.read_text(encoding="utf-8"))
            st["last_at"] = datetime.now().isoformat(timespec="seconds")
            st["last_user"] = str(user_id or "")
            st["last_owner"] = bool(is_owner)
            st["last_channel"] = channel
            st["count"] = int(st.get("count") or 0) + 1
            f.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass




    def _api_day_turns(self) -> None:
        """某一天的对话（给星图点开用）。只读，最多 6 条。"""
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        day = (q.get("day") or [""])[0][:10]
        if not day:
            return self._json({"ok": False, "error": "缺 day"})
        ch = get_character()
        out = []
        for t in (getattr(ch, "turns", None) or []):
            at = str(getattr(t, "at", "") or "")
            if at[:10] != day:
                continue
            out.append({"role": getattr(t, "role", ""),
                        "text": str(getattr(t, "text", ""))[:80]})
        return self._json({"ok": True, "day": day, "turns": out[-6:]})

    def _api_star_map(self) -> None:
        """给左栏星图用的紧凑数据（2026-11-10）。

        只读。每条给：文本（截断）、类别、日期、强度。
        前端按日期排成一条星带，越旧越暗 —— 让"遗忘"这件事看得见。
        """
        ch = get_character()
        stars = []

        mem = list(getattr(ch, "memories", None) or [])
        top_w = max([float(getattr(m, "weight", 1) or 1) for m in mem] or [1])
        for m in mem:
            at = (getattr(m, "last_touch", "") or getattr(m, "created", "") or "")[:10]
            w = float(getattr(m, "weight", 1) or 1) / max(top_w, 1)
            stars.append({"t": str(getattr(m, "text", ""))[:60],
                          "k": "mem", "at": at,
                          "w": round(max(0.25, w), 3)})

        for e in (getattr(ch, "events", None) or []):
            stars.append({"t": str(getattr(e, "text", ""))[:60], "k": "evt",
                          "at": str(getattr(e, "day", "") or "")[:10], "w": 0.8})

        for pr in (getattr(ch, "promises", None) or []):
            if getattr(pr, "kept", False):
                continue
            stars.append({"t": str(getattr(pr, "text", ""))[:60], "k": "prom",
                          "at": str(getattr(pr, "at", "") or "")[:10], "w": 1.0})

        stars = [x for x in stars if x["at"]]
        stars.sort(key=lambda x: x["at"])
        return self._json({"stars": stars, "count": len(stars)})

    def _api_qq_link(self) -> None:
        """QQ 链路的开关（2026-11-09）。

        GET  看状态：她的 QQ 进程在不在、注没注入
        POST {"action": "inject"} 去注入（走 SnowLuma 的接口）

        为什么放在我们这边：手机/网页上只要一个按钮，
        不用去开 5099 那个面板。
        """
        import mind_qqlink
        if self.command == "GET":
            return self._json(mind_qqlink.status())
        act = str(self._read_json().get("action") or "inject").strip()
        if act == "status":
            return self._json(mind_qqlink.status())
        if act != "inject":
            return self._json({"ok": False, "error": "action 只能是 inject / status"})
        st = mind_qqlink.inject()
        if st.get("injected"):
            st["bridge"] = _start_bridge()
        return self._json(st)

    def _api_qq(self) -> None:
        """QQ 桥接调用的接口。

        请求体：
            {
              "text": "消息内容",
              "user_id": 123456,          # 谁发的
              "group_id": 987654,         # 群消息才有
              "is_owner": true            # 是否是主人（由桥接判断）
            }

        ## 会话键的规则

            qq_<QQ号>            私聊，每人一份记忆
            qqgroup_<群号>       群聊共用一份（群里的对话是公共的）

        ## 关系规则

        **只有主人**才会同步游戏存档里的关系。
        别人没有对应存档，一律从「初识」开始 —— 否则她会对陌生人
        也表现得像交往 9 心一样，那是人设崩坏。
        """
        data = self._read_json()
        text = (data.get("text") or "").strip()
        if not text and not (data.get("images") or []):
            return self._json({"ok": False, "error": "空消息"})
        if not text:
            text = "（他发了张图）"
        if len(text) > 1000:
            return self._json({"ok": False, "error": "消息过长"})

        group_id = data.get("group_id")
        user_id = data.get("user_id") or "unknown"
        is_owner = bool(data.get("is_owner"))

        # 第一道闸：**不是主人，直接不理**（2026-11-08 用户要求）。
        # bridge 那边已经先拦过了；这里再拦一次 —— 桥是外部程序，
        # 不能把"谁有资格跟她说话"这件事只押在它身上。
        # 连 qq_state 都不记：不然左栏会显示"刚有人来找过"。
        if not is_owner:
            return self._json({"ok": False, "ignored": True,
                               "error": "只回应主人"})

        self._touch_qq_state(user_id, is_owner, "group" if group_id else "private")

        # 主人的私聊**共用主存档**（2026-10-28 用户要求）：
        # 你在 QQ 上和她说的话，回到网页打开还是同一份记忆。
        # 群聊仍然分开 —— 群里的对话是公共的，不该写进你的私人档案。
        from sessions import DESKTOP_KEY as _DK
        # 桥可以指定落到哪个档（/test 切测试档，2026-11-08）
        want = str(data.get("session") or "").strip()
        if is_owner and not group_id:
            session_key = want if want in ("desktop", "test") else _DK
        else:
            session_key = f"qqgroup_{group_id}" if group_id else f"qq_{user_id}"

        # 显式把会话定成算出来的那个 key（2026-11-08）。
        # 不设的话，桥接不带 X-Mind-Session 时会被当成"无头请求"，
        # 写入被改写到 test 档、再被身份守卫拒掉 —— 整轮记忆丢失。
        set_active_session(session_key)

        # 只有主人套用游戏里的关系
        use_relation = is_owner and load_settings().get("use_game_relation", True)

        # 图片（2026-10-28）：桥接把 QQ 的图下载下来转成 data URL 传进来，
        # 这里原样交给对话 —— 看图的模型已经配好了（mind_image.VISION_MODEL）。
        imgs = [str(x) for x in (data.get("images") or []) if x]
        # /home：让这一轮按网页通道对待（2026-11-08）
        chan = "web" if data.get("as_web") else "qq"
        result = self._run_chat(text, session_key, use_relation,
                                images=imgs or None, channel=chan)
        result["session"] = session_key
        return self._json(result)

    def _api_chat(self) -> None:
        data = self._read_json()
        user_text = (data.get("text") or "").strip()
        # 图片：前端可能传 data: URL 或裸 base64，normalize 会整理好。
        # 非法/过大的**静默丢掉** —— 输入侧丢了顶多少看一张图，
        # 不该让整轮对话失败。
        images = mind_image.normalize(data.get("images"))
        if not user_text and not images:
            return self._json({"ok": False, "error": "没有输入内容"})
        if len(user_text) > 1000:
            return self._json({"ok": False, "error": "一次说太多了（上限 1000 字）"})
        # 只发图不说话时补一句，否则她不知道该怎么接
        if images and not user_text:
            user_text = "（他给你看了这个）"

        # 会话键来自 `X-Mind-Session` 头（由 `_apply_session_header` 读好）。
        #
        # 为什么需要：**测试不该污染那几十轮真实对话**。
        # 前端带上 `X-Mind-Session: test` 就落到 saves/sessions/test.json，
        # 和 desktop 完全隔离（记忆、观察、承诺各存各的）。
        #
        # 但**关系仍然取自游戏存档** —— 那是全局的，见 sessions.py 的说明。
        # 所以测试档里的她仍然是「交往 9 心」，只是不记得你们聊过什么。
        session_key = current_session()
        use_relation = (current_session() == DESKTOP_KEY
                        or load_settings().get("use_game_relation", True))

        result = self._run_chat(user_text, session_key, use_relation,
                                  images=images, channel="web")
        result["session"] = session_key
        return self._json(result)

    @staticmethod
    def _maybe_remember(ch: Character, text: str) -> str | None:
        """从玩家输入里挑出值得长期记住的事。

        ⚠️ 这段的宽窄很关键，实测踩过坑：
        第一版只匹配「我有个…」这类句式，结果「我家的狗叫大黄」一个字都没记住，
        两轮之后上下文滑出，她就完全忘了那条狗，还会**谎称自己记得**。
        所以改成先剥掉时间状语再匹配，覆盖面大大加宽。
        """
        if len(text) < 4:
            return None

        # 剥掉句首的时间/语气状语，否则「昨天我钓到…」匹配不上「我钓到…」
        cleaned = re.sub(r"^(昨天|今天|前天|刚才|刚刚|早上|中午|晚上|下午|"
                         r"对了|话说|其实|我想说|我跟你说|那个|嗯|哦|啊)+[，,、\s]*",
                         "", text.strip())

        patterns = [
            # 自称与身份
            r"我(?:的名字)?叫[^，。！？；]{1,12}",
            r"我是[^，。！？；]{1,16}",
            # 家人、宠物、朋友 —— 带「的」的所属句式
            r"我(?:家|的)[^，。！？；]{1,8}(?:叫|是)[^，。！？；]{1,12}",
            # 命名类：「……取名叫 X」「……叫作 X」。
            # 实测漏过「我给农场新养的那头小牛取名叫青团」——
            # 「取名叫」是常见说法，必须单列出来。
            r"我[^，。！？；]{0,16}(?:取名叫|取名为|起名叫|起名为|叫作|叫做)[^，。！？；]{1,14}",
            # 亲属称谓直接跟「叫/是」的句式。
            # 实测踩过：「我妹妹叫小满」没被记下 —— 因为原来的词表里没有「妹妹」。
            r"我(?:家|的)?(?:狗|猫|鸡|牛|马|羊|兔|猪|鸟|鱼|"
            r"孩子|儿子|女儿|老婆|妻子|丈夫|先生|太太|"
            r"爸|妈|爹|娘|哥|弟|姐|妹|"
            r"爷爷|奶奶|外公|外婆|姥姥|姥爷|"
            r"哥哥|弟弟|姐姐|妹妹|表哥|表弟|表姐|表妹|"
            r"朋友|邻居|师父|老师)"
            r"[^，。！？；]{0,10}(?:叫|是|在|有)[^，。！？；]{1,14}",
            # 喜好与厌恶
            r"我(?:很|最|特别|挺)?(?:喜欢|爱|讨厌|不喜欢|怕|恨)[^，。！？；]{1,20}",
            # 居所与地点
            r"我住在[^，。！？；]{1,20}",
            r"我在[^，。！？；]{1,16}(?:工作|上班|上学|读书)",
            r"我在[^，。！？；]{1,12}(?:开了|开了个|有一家|经营)[^，。！？；]{0,12}",
            # 打算与经历
            r"我(?:正在|打算|准备|想|要)[^，。！？；]{1,20}",
            # 经历类：「我钓到/种了/挖到…」这类具体的事值得记
            r"我(?:钓到|钓了|抓到|种了|种下|挖到|捡到|养了|做了|做了个)[^，。！？；]{1,20}",
            r"我(?:昨天|前天|以前|曾经|上周)[^，。！？；]{1,20}",
            # 拥有的东西
            r"我(?:有个|有一个|有只|有一只|有块|有一块|种了|养了)[^，。！？；]{1,20}",
        ]
        for p in patterns:
            m = re.search(p, cleaned)
            if m:
                fact = m.group(0).strip("，,、 ")
                if len(fact) >= 4:
                    ch.remember(fact)
                    return fact
        return None


def _top_memories(ch, n: int = 3) -> list:
    """按强度取前 n 条记忆（给左栏「最近记得」用）。

    和提示词里挑记忆用的是同一套 mind_weight.strength ——
    界面上看到的，就是她此刻真正常想起的那几条。
    """
    try:
        import mind_weight
        mems = getattr(ch, "memories", None) or []

        def key(m):
            return dict(kind=getattr(m, "kind", "fact"),
                        weight=getattr(m, "weight", 1),
                        last_touch=(getattr(m, "last_touch", "")
                                    or getattr(m, "created", "")),
                        hits=getattr(m, "hits", 0),
                        key=getattr(m, "text", ""))

        return [getattr(m, "text", "") for m in mind_weight.rank(mems, key)[:n]]
    except Exception:
        return []



PERSONA_DIR = HERE / "personas"
PERSONA_CURRENT = PERSONA_DIR / "current.json"


def _persona_now() -> dict:
    """当前人设（没有 current.json 就用 blank.json）。"""
    for f in (PERSONA_CURRENT, PERSONA_DIR / "blank.json"):
        try:
            if f.is_file():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def _needs_setup() -> list[str]:
    """返回还缺什么；空列表 = 可以开始。"""
    missing = []
    p = _persona_now()
    if not str(p.get("name") or "").strip():
        missing.append("名字")
    if not str(p.get("characterCard") or "").strip():
        missing.append("人格正文")
    # 密钥：.apikey 或环境变量
    try:
        import os as _os
        has_key = bool(_os.environ.get("MIND_AI_API_KEY"))
    except Exception:
        has_key = False
    if not has_key:
        try:
            kf = HERE / ".apikey"
            has_key = kf.is_file() and bool(kf.read_text(encoding="utf-8").strip())
        except OSError:
            has_key = False
    if not has_key:
        missing.append("API 密钥")
    return missing


def _apply_setup(data: dict) -> tuple[bool, str]:
    """把向导填的内容落盘。返回 (成功, 说明)。"""
    try:
        PERSONA_DIR.mkdir(parents=True, exist_ok=True)
        base = {}
        if (PERSONA_DIR / "blank.json").is_file():
            base = json.loads((PERSONA_DIR / "blank.json").read_text(encoding="utf-8"))
        cur = {}
        if PERSONA_CURRENT.is_file():
            cur = json.loads(PERSONA_CURRENT.read_text(encoding="utf-8"))
        base.update(cur)

        if data.get("name"):
            base["name"] = str(data["name"]).strip()
        if data.get("call_name") is not None:
            base["call_name"] = str(data["call_name"]).strip()
        if data.get("pronoun"):
            base["pronoun"] = str(data["pronoun"]).strip()
        if data.get("characterCard"):
            base["characterCard"] = str(data["characterCard"])
        base["_说明"] = "由启动向导生成；以后可直接编辑本文件。"
        PERSONA_CURRENT.write_text(
            json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        city = data.get("city") or {}
        sf = HERE / "settings.json"
        st = {}
        if sf.is_file():
            st = json.loads(sf.read_text(encoding="utf-8"))
        st["city"] = {"name": str(city.get("name") or ""),
                      "lat": float(city.get("lat") or 0) or 31.30,
                      "lon": float(city.get("lon") or 0) or 120.62}
        sf.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")

        key = str(data.get("api_key") or "").strip()
        if key:
            (HERE / ".apikey").write_text(key + "\n", encoding="utf-8")

        # QQ（2026-11-10）：向导里一起填，顺手生成 bridge 配置
        qq = data.get("qq") or {}
        if qq:
            st.setdefault("qq", {})
            for k in ("bot_uin", "owner_uin", "autostart"):
                if k in qq:
                    st["qq"][k] = qq[k]
            sl_in = qq.get("snowluma") or {}
            sl = st["qq"].setdefault("snowluma", {})
            for k in ("dir", "base", "username", "password"):
                if k in sl_in:
                    sl[k] = sl_in[k]
            sf.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
            try:
                bc = build_bridge_config(st)
                (HERE / "bridge_config.json").write_text(
                    json.dumps(bc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            except Exception:
                pass
        return True, "已保存"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _setup_page_state() -> dict:
    p = _persona_now()
    st = {}
    try:
        sf = HERE / "settings.json"
        if sf.is_file():
            st = json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    return {"needs": _needs_setup(),
            "qq": st.get("qq") or {},
            "name": p.get("name", ""),
            "call_name": p.get("call_name", ""),
            "pronoun": p.get("pronoun", "她"),
            "characterCard": p.get("characterCard", ""),
            "city": st.get("city") or {}}

def _bridge_running() -> bool:
    """bridge.py 是否已经在跑（按命令行判断）。"""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*bridge.py*' } | "
             "Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=8).stdout.strip()
        return out.isdigit() and int(out) > 0
    except Exception:
        return False


def _start_bridge() -> str:
    """把 bridge 拉起来（后台）。返回一句人话。"""
    import subprocess
    import sys as _sys
    if _bridge_running():
        return "bridge 已在运行"
    cfg = HERE / "bridge_config.json"
    if not cfg.is_file():
        return "缺 bridge_config.json（向导里填 QQ 后会自动生成）"
    try:
        subprocess.Popen([_sys.executable, "-u", "bridge.py"], cwd=str(HERE),
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)
        return "bridge 已启动"
    except Exception as e:
        return f"bridge 启动失败：{e}"


def build_bridge_config(cfg: dict | None = None) -> dict:
    """按 settings.qq 生成 bridge 要的配置文件。"""
    cfg = cfg or {}
    q = (cfg.get("qq") if cfg else None) or {}
    sl = q.get("snowluma") or {}
    return {
        "owner_qq": int(q.get("owner_uin") or 0),
        "self_qq": int(q.get("bot_uin") or 0),
        "enabled": bool(q.get("autostart")),
        "chat_server": "http://127.0.0.1:8731",
        "token": "",
        "reverse_http": {"enabled": False, "host": "127.0.0.1", "port": 8732},
        "forward_ws": {"enabled": True,
                       "url": "ws://127.0.0.1:3001",
                       "api_url": (sl.get("base") or "http://127.0.0.1:5099")
                                  .replace("5099", "3000")},
        "behavior": {"reply_in_group_only_when_at": True,
                     "max_reply_chars": 500,
                     "typing_delay_ms": [800, 2200]},
    }


def _qq_state(base_dir) -> dict:
    """QQ 桥接的最近状态（给左栏那块显示用）。"""
    import json as _json
    from datetime import datetime as _dt
    out = {"configured": False, "count": 0, "last_at": "",
           "minutes": -1, "owner": False, "bridge_on": False}
    try:
        f = Path(base_dir) / "saves" / "qq_state.json"
        if f.is_file():
            d = _json.loads(f.read_text(encoding="utf-8"))
            out["count"] = int(d.get("count") or 0)
            out["last_at"] = str(d.get("last_at") or "")
            out["owner"] = bool(d.get("last_owner"))
            out["configured"] = True
            if out["last_at"]:
                t0 = _dt.fromisoformat(out["last_at"][:19])
                out["minutes"] = int((_dt.now() - t0).total_seconds() // 60)
    except Exception:
        pass

    # bridge_config.json 里 enabled 是不是开着 —— 左栏"桥接"那行用
    try:
        bc = Path(base_dir) / "bridge_config.json"
        if bc.is_file():
            out["bridge_on"] = bool(
                _json.loads(bc.read_text(encoding="utf-8")).get("enabled"))
    except Exception:
        pass
    return out


def _resolve_place(ch) -> str:
    """她此刻在哪。

    2026-11-05：加了一层"从她的动作推断"的兜底。
    模型那条路经常不给 place（实测明确写了"往水缸那边走"也不给），
    于是地图一直停在旧值。这里按这个顺序定：

        ① 睡着        -> 卧房
        ② 她最后一句的动作里有位置线索 -> 用它
        ③ 存档里的 today_place
        ④ 兜底        -> 堂屋
    """
    try:
        # mind_activity 在 server.py 里是**函数内** import 的，
        # 模块级看不到它 —— 这里要自己导入一次，
        # 否则 NameError 被下面的 except 吞掉，永远返回「堂屋」。
        import mind_activity
        p = mind_activity.place_display(ch)
        if p:
            return p
        turns = getattr(ch, "turns", None) or []
        for t in reversed(turns[-6:]):
            if getattr(t, "role", "") != "her":
                continue
            guess = mind_activity.place_infer(getattr(t, "text", ""))
            if guess:
                return guess
        return getattr(ch, "today_place", "") or "堂屋"
    except Exception:
        return "堂屋"
