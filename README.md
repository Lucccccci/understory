# understory

> **v0.1（试用版）** —— 机制齐全，接口可能还会变。

**一个"内在生活"的模拟框架。**
她不只是一个会回复的模型 —— 她有心情、有手边的事、有会忘的记忆、有每天写下的日记。
你可以把任意一个角色填进去。

> 这个仓库是**框架**，不含任何具体角色。
> 名字、长相、说话方式、你们的关系，全部由 `personas/` 里的一个 JSON 决定。

---

## 三分钟开始

```bash
git clone <this-repo> mind
cd mind

# 1) 依赖（只有窗口模式需要；纯跑服务不需要任何第三方包）
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

# 2) 启动
python chat_app.py          # 只跑服务（浏览器打开 http://127.0.0.1:8731）
python desktop.py           # 想要独立窗口（Windows 会用 Edge 应用模式）
python desktop.py --browser # 用默认浏览器
```

**第一次启动会进入填写页** —— 名字、称呼、城市、人格正文、API 密钥，
如果要用 QQ，也能一起填。填完自动进入正式界面。

```
┌──────────────────────────────┐
│  先把她填出来                  │
│  名字       [            ]    │
│  称呼/代词   [    ]  [她▾]     │
│  城市/经纬度  [    ][    ][  ] │
│  人格正文（预填骨架，直接改）    │
│  QQ（可选）                   │
│  API 密钥   [            ]    │
│  [        开 始        ]      │
└──────────────────────────────┘
```

---

## 她由什么构成

| 模块 | 文件 | 作用 |
|---|---|---|
| **人格** | `personas/current.json` | 她是谁、怎么说话、你们什么关系 |
| **心情** | `mind_mood.py` | valence / energy 两个轴，每小时按当天底色重掷 |
| **此刻** | `mind_activity.py` | 她在哪、在做什么（会困、会睡、会去院子） |
| **日子** | `mind_life.py` `mind_daily.py` | 每天的手边事 / 心里事 / 小麻烦 / 口味 |
| **记忆** | `mind_memory.py` `mind_weight.py` | 事实带权重，按时间衰减，弱的先出局 |
| **观察** | `mind_observe.py` | 跨天重复才成立的行为；带证据、会淡出 |
| **事件** | `mind_event.py` | 发生过的事，带日期 |
| **承诺** | `mind_promise.py` | 你说过要做的；兑现自动识别，也能手动标记 |
| **摘要** | `mind_digest.py` `mind_week.py` `mind_rollup.py` | 日 / 周 / 长对话压缩 |
| **日记** | `mind_diary.py` | 每天睡前一篇，只给你看 |
| **时间** | `mind_season.py` `mind_festival.py` | 真实日历、节气、天气（按你填的城市） |
| **对话** | `mind_brain.py` `mind_feel.py` | 提示词组装 + 每轮的后台判定 |
| **主动** | `mind_note.py` `greeting.py` | 留话、主动开口 |
| **界面** | `web/` | 三栏：她此刻 / 对话 / 时间与日记 |

---

## 她会怎么"记得"

```
每轮对话之后，后台再判一次（不影响回复速度）：
    这段是暖/中性/冷       → 心情微调
    这事重不重要           → 记忆权重
    有没有值得记的事实     → memories
    有没有"发生过一件事"   → events（改写成第三人称，带日期）
    她该挪到哪儿           → 房子地图
```

记忆的取舍：

```
事实    强度 = 权重 × 时间衰减，低于阈值就不进提示词（不删，能搜回来）
观察    必须跨天重复；带证据；14 天没再看见就淡出
事件    不衰减，按日期排
承诺    到期优先，但绝不催
旧对话  攒够 200 轮压成一段 700 字摘要，前面的原文不再进提示词
```

---

## 配置

`settings.json`：

```json
{
  "theme": "ink",              // light / moon / ink / candle
  "portrait_source": "local",  // 立绘来源
  "city": { "name": "", "lat": 31.30, "lon": 120.62 },
  "qq": {
    "bot_uin": 0,              // 这个角色的 QQ 号
    "owner_uin": 0,            // 你自己的 QQ（只有你能跟她说话）
    "autostart": false,
    "snowluma": { "dir": "", "base": "http://127.0.0.1:5099",
                  "username": "admin", "password": "" }
  }
}
```

`personas/current.json` 的字段：

```json
{
  "name": "名字",
  "call_name": "她怎么称呼你",
  "pronoun": "她",
  "home": { "rooms": ["卧房", "堂屋", "厨房", "院子"] },
  "identity": ["在镇上帮工", "会修东西"],
  "habits": ["说话简短", "紧张时摸袖口"],
  "tone": { "sentence": "短句，随口说", "avoid": ["客服腔"], "nickname": "" },
  "relationshipStages": { "stranger": "…", "familiar": "…",
                          "close": "…", "lover": "…" },
  "characterCard": "## 你是谁\n\n…"
}
```

**只有 `name` 和 `characterCard` 是必须的**，其余缺了会用中性默认值。

### 立绘

把表情图放进 `assets/face/`，文件名见 `server.py` 的 `EMOTION_TO_FILE`。
仓库自带一张灰色占位图 `face_default.png`，不给也不会破图。

### 语音

**v1.0 不含语音。** TTS 相关代码与前端已移除。
想接 TTS，自己加一个 `/api/tts` 路由并在 `web/app.js` 里调用即可。

### QQ（可选）

需要额外装 [SnowLuma](https://github.com/SnowLuma/SnowLuma)（不是 pip 依赖），
在向导里填它的目录与面板密码，然后：

```
点左栏「接上 QQ」
  → 没开就自动启动 SnowLuma
  → 登录 → 找到这个角色的 QQ 进程 → 注入
  → 自动起 bridge，连上 OneBot
```

**不填 QQ 也完全能用** —— 网页版不需要它。

---

## 数据存在哪

```
saves/                 ← 全部在这，和其它项目互不影响
  sessions/*.json      ← 对话与状态（每人一份）
  snapshots/           ← 快照
  backups/             ← 整包备份（每天最多一份）
  diagnostics.log      ← 静默失败记录；健康时是空的
```

- 换机器：整份 `saves/` 拷走
- 重新开始：删掉 `saves/`
- `.apikey` 只在本机，**不要提交**

---

## 一些说明

**关于 role 字段**
存档里"她说的那句话"记作 `"role": "her"`。
更早的版本用 `"shu"`，读档时会自动迁移，旧存档可以直接用。

**关于游戏存档**
`world_save.py` / `world_weather.py` / `mind_lines.py` 是**可选**的：用来读某个特定游戏的
存档、天气与台词。不装那个游戏、不用这些文件，框架照常运行。

**关于失败**
所有"本该成功却失败"的地方都会写进 `saves/diagnostics.log`，而不是静默吞掉。
那个文件为空 = 健康。

---

## 目录

```
understory/
  personas/        人设（blank.json 是空模板，example.json 是示例）
  web/             界面（index.html / app.js / style.css / setup.html）
  saves/           运行时数据（不进仓库）
  assets/          字体、背景纹理、立绘
  mind_*.py        核心模块
  world_*.py       可选：对接游戏存档
  bridge.py        可选：QQ 桥接
  desktop.py       窗口启动器
  chat_app.py      纯服务启动器
```

---

## 许可

MIT。见 [LICENSE](LICENSE)。

**v0.1 说明**：这是试用版 —— 内在生活那套机制已经齐了，
但 persona 字段、接口、界面都还可能调整；升级前请备份 `saves/`。
