/* 重做最后一轮 —— 重生成 / 改我的话重发。
 *
 * ## 为什么要这个
 *
 * 她会答偏。实测遇到过两次，而且**偏差会顺着上下文传下去**：
 * 一条答非所问记进历史之后，后面两条也跟着歪
 * （那天第 ② 和第 ④ 条都被第 ① 条带偏了）。
 *
 * 原来唯一的办法是清空整个存档 —— 那代价太大。
 *
 * ## 只有最后那一轮能重做
 *
 * 中间某轮重做会把后面的对话撕开（她记得后面发生过的事，
 * 但那一轮的输入变了）。所以按钮**只挂给最后一条她的话**。
 *
 * 每次渲染完历史（boot / 发完消息 / 重做之后）都要重新标记一次 ——
 * 「最后一条」会变。见 `markRewindable()`。
 *
 * ## 两件事，一个端点
 *
 *     regen   她的回复重新生成（你的话不动）
 *     edit    改了你的话再重跑（把旧的那对问答抹掉）
 *
 * 服务端 `POST /api/rewind`。底下的关键操作是
 * `Character.drop_last_exchange()` —— 不抹掉的话她会看到你说两遍。
 */
(function () {
  'use strict';

  // 当前可重做的那一条（气泡 + 取文本的函数）
  let slot = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  /** 把「最后一条她的话」标成可重做。旧的标记会自动清掉。 */
  function markRewindable() {
    if (slot && slot.bubble) {
      slot.bubble.querySelectorAll('.rewind-btn, .rewind-edit')
        .forEach((n) => n.remove());
      slot.bubble.classList.remove('rewindable');
    }
    slot = null;

    const log = document.getElementById('log');
    if (!log) return;
    const msgs = log.querySelectorAll('.msg');
    // 从末尾找第一条「她说的、并且有文字」的
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (!m.classList.contains('her')) continue;
      const bodyEl = m.querySelector('.bubble .body') ||
                     m.querySelector('.bubble');
      const txt = (bodyEl && bodyEl.textContent || '').trim();
      if (!txt) continue;
      attachButtons(m, bodyEl);
      break;
    }
  }

  function attachButtons(msgEl, bodyEl) {
    const bubble = msgEl.querySelector('.bubble') || msgEl;
    bubble.classList.add('rewindable');
    slot = { bubble, bodyEl };

    // 重生成
    const re = el('button', 'rewind-btn', '再想一次');
    re.type = 'button';
    re.title = '她的回复重新生成一遍（你的话不动）';
    re.addEventListener('click', (ev) => {
      ev.stopPropagation();
      send({ op: 'regen' }, re);
    });
    bubble.appendChild(re);

    // 改我的话
    const ed = el('button', 'rewind-btn', '改我的话');
    ed.type = 'button';
    ed.title = '把你上一句改成别的，让她重新回答';
    ed.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openEditor(msgEl, bodyEl);
    });
    bubble.appendChild(ed);
  }

  /** 「我」那条气泡改成可编辑 —— 取上一句玩家的原话。 */
  function openEditor(shuMsgEl, bodyEl) {
    // 找到她这条前面最近的一条玩家消息
    let userEl = shuMsgEl.previousElementSibling;
    while (userEl && !userEl.classList.contains('me')) {
      userEl = userEl.previousElementSibling;
    }
    if (!userEl) { alert('找不到你上一句'); return; }
    const userBubble = userEl.querySelector('.bubble') || userEl;
    const userBody = userEl.querySelector('.bubble .body') || userBubble;
    const oldText = (userBody.textContent || '').trim();

    if (userBubble.querySelector('.rewind-edit')) return;   // 已经开着

    const box = el('div', 'rewind-edit');
    const ta = document.createElement('textarea');
    ta.value = oldText;
    ta.rows = Math.min(6, Math.max(2, Math.ceil(oldText.length / 22)));
    ta.className = 'rewind-ta';

    const row = el('div', 'rewind-row');
    const save = el('button', 'rewind-btn primary', '就这样发');
    const cancel = el('button', 'rewind-btn', '算了');

    save.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const t = ta.value.trim();
      if (!t) { alert('不能改成空的'); return; }
      box.remove();
      send({ op: 'edit', text: t }, save);
    });
    cancel.addEventListener('click', (ev) => {
      ev.stopPropagation();
      box.remove();
    });
    // Enter 发送，Shift+Enter 换行 —— 和主输入框一致
    ta.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        save.click();
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        box.remove();
      }
    });

    row.appendChild(cancel);
    row.appendChild(save);
    box.appendChild(ta);
    box.appendChild(row);
    userBubble.appendChild(box);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }

  /** 发请求 + 重铺整个聊天区。 */
  async function send(body, btn) {
    const old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '想着…'; }
    if (window.Voice) window.Voice.stopAll();

    try {
      const r = await fetch('/api/rewind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!d.ok) {
        alert('重做失败：' + (d.error || ''));
        return;
      }
      // 重铺整个聊天区 —— 不为了省一次请求去手工拼 DOM，
      // 那种做法容易留下幽灵节点（原来试过，删不干净）。
      if (window.boot) {
        await window.boot();
      }
    } catch (e) {
      alert('连不上服务：' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  window.Rewind = { markRewindable, send };
})();
