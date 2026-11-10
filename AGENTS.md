# 维护规则（给未来的自己 / 给 AI 助手）

这份文件只讲**这个仓库的规矩**，不讲来历。

---

## 一、存档是敏感数据

```
saves/               对话与状态。绝不进仓库（见 .gitignore）
saves/sessions/*.json 每人一份，文件名就是会话键
```

- 改动存档必须先确认（属于用户数据，不是代码）
- 测试一律用测试会话键（`probe`），不要碰 `desktop`
- 写测试脚本前先读一遍：里面不能出现 `desktop` 字面量

---

## 二、三个会话键

```
desktop   真实对话
test      用户自己的调试档
probe     测试用（第一次会自动从基线复制）
```

服务端对**无头请求**（没有 `X-Mind-Session` 头）有兜底：
写入会被改写到 `test`，再被身份守卫拒绝，并记进 `diagnostics.log`。
**兜底不是许可** —— 该带的头要带。

---

## 三、重启服务 = 可能写存档

`/api/status` 到点会重掷心情并落盘。所以：

```
重启前告诉用户；重启后核对一遍轮次（turns 数量）
能用测试档验证的，就别重启生产服务
```

---

## 四、改完前端必须用无头浏览器验

```bash
msedge --headless=new --disable-gpu --no-sandbox   --user-data-dir=$TEMP/edgeprof --virtual-time-budget=15000   --dump-dom http://127.0.0.1:8731/
```

然后数元素、看 id 在不在，不要只说"刷新看看"。

---

## 五、判据先验证，再下结论

```
· 读时间用系统时钟，别从文件里的时间戳推
· 0 是合法值时，别写 `x or 默认值`
· 测量工具（ASR / CER / 截图裁剪）本身也要先验证
```

---

## 六、低频分支要专门触发

```
· 心情重掷      距上次 > 1 小时
· 跨天          把 today_key 改成昨天
· 观察          30 分钟冷却 + 4 轮
· 睡眠/离开     深夜 + 静默
· 空档守卫      盘上 N 轮 → 写 0 轮
· 身份守卫      跨会话写
```

改完先问："有哪些分支平时不走？"然后逐个造出来。

---

## 七、静默失败要留痕

```python
try:
    do()
except Exception:
    pass          # ✗ 出事没人知道
```

改成 `mind_diag.note(说明, e)`。
` saves/diagnostics.log` 为空 = 健康。

---

## 八、改 HTML 结构前先备份

```
cp web/index.html web/index.html.bak-$(date +%Y%m%d-%H%M%S)
```

改完核对三件事：div 配平、details 数量、关键 id 还在。
锚点用**唯一 id**，不要用中文文案（注释里也会出现）。
