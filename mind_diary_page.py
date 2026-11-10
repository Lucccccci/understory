"""日记页面 —— 独立一页，不和聊天挤在一起。

设计取向：
    · 一列，像翻笔记本，日期在左，正文在右
    · ★ 收藏过的永远留着；没收藏的 10 天后自然消失（后端清）
    · 不需要登录、不需要交互以外的任何东西，读完就走
"""
from __future__ import annotations

from pathlib import Path

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>她的日记</title>
<style>
  :root {
    --paper: #f3ede1; --ink: #38322a; --soft: #6b6355;
    --gold: #b09355; --line: #d8cdb6; --night: #171a1f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 48px 20px 80px;
    background: var(--night); color: var(--ink);
    font-family: "Songti SC", "Noto Serif CJK SC", serif;
    display: flex; justify-content: center;
  }
  .wrap { width: 100%; max-width: 720px; }
  header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 28px; }
  h1 { font-size: 20px; font-weight: 500; letter-spacing: .14em;
       color: #d9cdb2; margin: 0; }
  .sub { font-size: 12px; color: #8a8070; letter-spacing: .08em; }
  .back { margin-left: auto; font-size: 12px; color: #b09355;
          text-decoration: none; border-bottom: 1px solid transparent; }
  .back:hover { border-color: #b09355; }

  .entry {
    background: var(--paper); border-radius: 3px;
    padding: 26px 30px; margin-bottom: 18px;
    box-shadow: 0 12px 30px rgba(0,0,0,.35);
  }
  .meta { display: flex; align-items: center; gap: 10px;
          font-size: 12px; color: var(--soft); margin-bottom: 14px; }
  .day { letter-spacing: .1em; }
  .mood { opacity: .8; }
  .star {
    margin-left: auto; cursor: pointer; border: 0; background: none;
    font-size: 15px; color: #b9ad95; padding: 0 2px; line-height: 1;
  }
  .star.on { color: #c9a961; }
  .text { font-size: 15px; line-height: 2.05; white-space: pre-wrap;
          letter-spacing: .02em; }
  .empty { color: #8a8070; font-size: 14px; text-align: center; padding: 60px 0; }
  .hint { margin-top: 26px; font-size: 11px; color: #6b6355; text-align: center;
          letter-spacing: .06em; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>她的日记</h1>
    <span class="sub">睡前写的，只给你看</span>
    <a class="back" href="/">← 回到对话</a>
  </header>
  <div id="list"></div>
  <div class="hint">★ 收藏过的会一直留着；没收藏的十天之后自然消失</div>
</div>

<script>
const esc = (s) => String(s || '').replace(/[&<>]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

async function load() {
  const r = await fetch('/api/diary');
  const d = await r.json();
  const list = document.getElementById('list');
  const items = (d.entries || []).slice().reverse();
  if (!items.length) {
    list.innerHTML = '<div class="empty">还没有日记。<br>她睡下的时候会写。</div>';
    return;
  }
  list.innerHTML = items.map((e) => `
    <div class="entry">
      <div class="meta">
        <span class="day">${esc(e.day)}</span>
        ${e.mood ? `<span class="mood">${esc(e.mood)}</span>` : ''}
        <button class="star ${e.starred ? 'on' : ''}" data-day="${esc(e.day)}"
                title="${e.starred ? '取消收藏' : '收藏，永久保留'}">${e.starred ? '★' : '☆'}</button>
      </div>
      <div class="text">${esc(e.text)}</div>
    </div>`).join('');

  list.querySelectorAll('.star').forEach((b) => {
    b.addEventListener('click', async () => {
      await fetch('/api/diary', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ day: b.dataset.day }),
      });
      load();
    });
  });
}
load();
</script>
</body>
</html>
"""


def write_page(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PAGE, encoding="utf-8")
