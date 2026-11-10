#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把三层记忆合成为她的「已知世界」。

## 三层记忆

| 层 | 来源 | 作用 |
|---|---|---|
| 对话上下文 | 最近若干轮对话 | 短期连贯，接得住话 |
| 个人背景   | player_profile.json | 她一开始就知道你是谁 |
| 游戏交互   | 这座镇子存档 | 她知道你们共同经历的那个世界 |
| 长期记忆   | 对话中提取的事实 | 跨会话记住你提过的事 |

前三层是「静态的已知」，第四层是「动态积累的」。

## 为什么要单独一个文件

这三层的读取各有各的失败模式（存档可能被游戏占用、profile 可能没填），
集中在一处做**统一降级**：任何一层读不到就跳过，绝不让它拖垮整个对话。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from world_save import GameContext, read_game_context

HERE = Path(__file__).resolve().parent
PROFILE_FILE = HERE / "player_profile.json"


@dataclass
class PlayerProfile:
    """你在 player_profile.json 里填的个人背景。全部可选。"""
    call_name: str = ""
    identity: str = ""
    origin: str = ""
    traits: list[str] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    cared_about: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)

    @staticmethod
    def load(path: Path = PROFILE_FILE) -> "PlayerProfile":
        if not path.is_file():
            return PlayerProfile()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return PlayerProfile()

        def lst(key: str) -> list[str]:
            v = raw.get(key)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []

        return PlayerProfile(
            call_name=str(raw.get("称呼") or "").strip(),
            identity=str(raw.get("身份") or "").strip(),
            origin=str(raw.get("来处") or "").strip(),
            traits=lst("性格"),
            likes=lst("喜欢"),
            dislikes=lst("讨厌"),
            cared_about=lst("在意的人"),
            extras=lst("其他"),
        )

    def as_facts(self) -> list[str]:
        """整理成「她该知道的事实」。空字段自动省略。"""
        f: list[str] = []
        if self.call_name:
            # 这是**昵称**，不是名字本身 —— 名字来自游戏存档。
            f.append(f"他让你叫他「{self.call_name}」")
        if self.identity:
            f.append(f"他的身份：{self.identity}")
        if self.origin:
            f.append(f"他为什么会在这里：{self.origin}")
        if self.traits:
            f.append("他的性子：" + "；".join(self.traits))
        if self.likes:
            f.append("他喜欢：" + "、".join(self.likes))
        if self.dislikes:
            f.append("他讨厌：" + "、".join(self.dislikes))
        if self.cared_about:
            f.append("他在意的人：" + "、".join(self.cared_about))
        for e in self.extras:
            f.append(e)
        return f


@dataclass
class KnownWorld:
    """三层记忆合并后的「她已知的一切」。"""
    profile: PlayerProfile = field(default_factory=PlayerProfile)
    game: GameContext = field(default_factory=GameContext)
    profile_loaded: bool = False
    game_loaded: bool = False

    # 这份「已知世界」是否属于主人的会话。
    # 非主人会话里，个人档案与游戏存档都不出现 ——
    # 「阿明」「瓦伦丁农场」都是关于你的信息，
    # 不该让陌生人从她嘴里听到。（实测踩过：陌生人的对话里她喊出了「阿明」）
    is_owner: bool = True

    @staticmethod
    def load(use_game_save: bool = True) -> "KnownWorld":
        """加载已知世界。

        Args:
            use_game_save: 是否读游戏存档与个人档案。
                **只有主人的会话才该为 True。**
                否则她会对陌生人说出你的农场名、角色名、和谁交好 ——
                那些都是你的信息，不该泄露给别人的对话。
        """
        kw = KnownWorld(is_owner=use_game_save)

        if use_game_save:
            kw.profile = PlayerProfile.load()
            kw.profile_loaded = bool(kw.profile.as_facts())

            kw.game = read_game_context()
            kw.game_loaded = kw.game.found

        return kw

    def as_prompt_block(self) -> str:
        """组装成 prompt 里的一段。没有任何事实时返回空串。"""
        lines: list[str] = []

        # 游戏世界
        try:
            game_facts = self.game.as_facts() if self.game_loaded else []
        except Exception:
            game_facts = []
        if game_facts:
            lines.append("### 你们身处的这个地方（你本来就该知道）")
            lines.extend(f"- {x}" for x in game_facts)

        # 个人背景
        try:
            prof_facts = self.profile.as_facts() if self.profile_loaded else []
        except Exception:
            prof_facts = []
        if prof_facts:
            if lines:
                lines.append("")
            lines.append("### 关于对方，你已经知道的")
            lines.extend(f"- {x}" for x in prof_facts)

        if not lines:
            return ""

        header = [
            "## 你已知的世界（背景常识，不是新消息）",
            "下面这些是你本来就知道的事，不需要对方告诉你。",
            "**请自然地运用，不要逐条复述，也不要说「根据我的资料」这种话。**",
            "只在话题相关时顺带提起，像一个真的了解情况的人那样。",
            "",
        ]
        return "\n".join(header + lines)

    def call_name(self) -> str:
        """她该怎么称呼对方。

        ## 优先级（这里**曾经写反过**，是个真 bug）

            1. 游戏存档里的角色名     ← 主来源
            2. 个人档案里的「称呼」    ← 只在玩家主动填了时作为**昵称**覆盖
            3. 「你」

        原来顺序是反的（档案优先）。后果：player_profile.json 模板里
        我留的示例值「阿明」压过了真实游戏 ID，她一直管玩家叫阿明。
        一个没被改掉的示例值不该有这种权力。

        ⚠️ 非主人会话一律用「你」——
        「阿明」「NPC」都是关于你的称呼，不该用来称呼陌生人。
        （实测踩过：陌生人对话里她喊出了「阿明」。）
        """
        if not self.is_owner:
            return "你"
        # 昵称：只在玩家主动填了的时候才算数
        nick = (self.profile.call_name or "").strip()
        if nick:
            return nick
        if self.game.player_name:
            return self.game.player_name
        return "你"


if __name__ == "__main__":
    kw = KnownWorld.load()
    print("=== 档案 ===")
    print("  已加载:" , kw.profile_loaded)
    for f in kw.profile.as_facts():
        print("   ·", f)
    print()
    print("=== 游戏 ===")
    print("  已加载:", kw.game_loaded)
    for f in kw.game.as_facts():
        print("   ·", f)
    print()
    print("=== 称呼 ===", kw.call_name())
    print()
    print("=== 注入 prompt 的内容 ===")
    print(kw.as_prompt_block() or "（空）")
