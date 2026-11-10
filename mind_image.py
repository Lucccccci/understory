"""图片通道 —— 让她看得见东西。

## 背景

这个能力最早是在 AstrBot 那边做出来的（用它的分块上传 + 视觉模型 fallback）。
但 AstrBot 那一层整体体验不如这个窗口，所以决定**回归 mind-life，把视觉搬过来**。

搬的时候顺手简化了：AstrBot 那边要走「分块上传 + attachment_id」是因为它的
API 只认那个；这里是我们自己的服务，**直接把 base64 放进请求就行**。

## 两条路

    ① 前端直传 base64     最省事。窗口里粘/拖一张图，base64 直接塞进 /api/chat
    ② 文件存盘再引用      图大的时候省带宽，而且能留着（她以后可以「想起」那张图）

现在只做 ① —— KISS。等真的有「她想翻旧图」的需求再上 ②。

## 模型

DeepSeek 的对话模型（`deepseek-chat`）**不认图片**，要换成
`deepseek-v4-flash-vision-exp`。但那个模型：

    * 纯文本能力与 deepseek-flash 持平（可以一直用它）
    * **推理很吃 token** —— 实测一次占掉 591 个 reasoning token

所以第 ② 条要紧：**放宽 max_tokens**，否则回复会被 reasoning 吃光变成空。
（这个坑在 AstrBot 那边踩过一次：图发出去了，回复是空的，看着像失败。）
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

# 视觉模型名
VISION_MODEL = "deepseek-v4-flash-vision-exp"

# 认识的图片格式 -> MIME
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# 单张图的上限。base64 会膨胀 4/3，所以 6 MB 的图编码后约 8 MB —— 
# 再大就给模型传不动了，而且 token 也吃不消。
MAX_BYTES = 6 * 1024 * 1024

# 一次最多几张。她看一张就够反应了，多了反而分心。
MAX_IMAGES = 3

_DATA_URL = re.compile(r"^data:image/(\w+);base64,(.+)$", re.S)


def guess_mime(name: str) -> str:
    """按扩展名猜 MIME。猜不到按 png 算（这座镇子截图都是 png）。"""
    return _MIME.get(Path(name).suffix.lower(), "image/png")


def to_data_url(raw: bytes, mime: str = "image/png") -> str:
    """字节 -> data: URL。模型接口认这个格式。"""
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def from_file(path: str | Path) -> str:
    """本地文件 -> data: URL。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件太大。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"图片太大（{size / 1048576:.1f} MB，上限 "
                         f"{MAX_BYTES / 1048576:.0f} MB）")
    return to_data_url(p.read_bytes(), guess_mime(p.name))


def normalize(items: list | None) -> list[str]:
    """把前端传来的图片整理成 data: URL 列表。

    前端可能给三种形态，都接受：

        "data:image/png;base64,...."     标准 data URL，直接用
        "<base64 裸串>"                   补上前缀
        {"data": "...", "name": "x.png"}  带名字的对象，按名字猜 MIME

    非法或过大的**静默丢掉** —— 她说不出话比说错话好，
    而且这里是输入侧，丢了顶多少看一张图，不该让整轮对话失败。

    Args:
        items: 前端传的 images 字段。

    Returns:
        data: URL 列表，最多 MAX_IMAGES 条。
    """
    if not items:
        return []
    if not isinstance(items, list):
        items = [items]

    out: list[str] = []
    for it in items:
        if len(out) >= MAX_IMAGES:
            break
        try:
            if isinstance(it, dict):
                s = str(it.get("data") or "")
                name = str(it.get("name") or "")
            else:
                s = str(it)
                name = ""

            s = s.strip()
            if not s:
                continue

            m = _DATA_URL.match(s)
            if m:
                # 已经是 data URL。检查一下大小
                if len(m.group(2)) * 3 // 4 > MAX_BYTES:
                    continue
                out.append(s)
                continue

            # 裸 base64 或带换行的
            body = re.sub(r"\s+", "", s)
            if len(body) * 3 // 4 > MAX_BYTES:
                continue
            if not re.fullmatch(r"[A-Za-z0-9+/=]+", body):
                continue
            if len(body) < 64:          # 太短，多半不是真图
                continue
            out.append(f"data:{guess_mime(name)};base64,{body}")
        except Exception:
            continue
    return out


def has_images(images: list | None) -> bool:
    return bool(images)


def build_content(text: str, images: list[str]) -> list[dict] | str:
    """拼成模型要的 content 结构。

    没有图就返回**纯字符串** —— 不是所有接口都接受数组形式，
    没必要为了统一而让无图的情况也走数组。

    Args:
        text: 用户说的话。
        images: data: URL 列表。

    Returns:
        有图时返回 [{"type":"text",...}, {"type":"image_url",...}]，
        否则返回原字符串。
    """
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text or "（他给你看了张图）"}]
    for u in images:
        parts.append({"type": "image_url", "image_url": {"url": u}})
    return parts


if __name__ == "__main__":
    print("  图片通道自测")
    print()
    # 造一张极小的 png（1x1 透明）
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    url = to_data_url(png, "image/png")
    print(f"  data URL 长度 {len(url)}")

    print()
    print("  normalize 的三种形态：")
    cases = {
        "标准 data URL": [url],
        "裸 base64": [url.split(",", 1)[1]],
        "带名字的对象": [{"data": url.split(",", 1)[1], "name": "shot.png"}],
        "混合": [url, "not-an-image", "", None, {"data": "x"}],
        "不是列表": url,
    }
    for name, v in cases.items():
        got = normalize(v)
        print(f"    {name:<16} -> {len(got)} 张")

    print()
    print("  build_content：")
    print("    无图 ->", repr(build_content("你好", []))[:50])
    c = build_content("你看", [url])
    print(f"    有图 -> {len(c)} 段，类型 {[x['type'] for x in c]}")
