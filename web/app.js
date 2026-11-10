/* mind · 内在生活 —— 前端逻辑 */

const $ = (id) => document.getElementById(id);

/* ── 会话档 ──────────────────────────────────────────────
   后端用 `X-Mind-Session` 头决定读写哪份存档：

       （不带头）/ desktop   ->  saves/sessions/desktop.json   你的真实记录
       test                 ->  saves/sessions/test.json      调试用，互不影响

   所以**所有**请求都要带上它。与其改二十个 fetch 调用，
   不如包一层 —— 以后新加的请求自动就带上了。 */
function sessionKey() {
  const box = $('useTestSession');
  return (box && box.checked) ? 'test' : 'desktop';
}

const _fetch = window.fetch.bind(window);
window.fetch = function (input, init) {
  const opt = Object.assign({}, init);
  opt.headers = Object.assign({}, opt.headers || {});
  if (opt.headers instanceof Headers) {
    opt.headers.set('X-Mind-Session', sessionKey());
  } else {
    opt.headers['X-Mind-Session'] = sessionKey();
  }
  return _fetch(input, opt);
};

let currentPortrait = 0;
let lastPortraitUrl = '';
// 当前立绘文件名（不含 .png），消息头像也跟着它变
let currentFace = 'face_default';
// 她该怎么称呼你（来自游戏存档，见 call_name）
let myName = '';
let busy = false;
let typingTimer = null;

const EMOTION_INDEX = { '$h': 1, '$s': 2, '$u': 3, '$l': 4, '$a': 5 };
const portraitIndex = (emotion) => EMOTION_INDEX[emotion] ?? 0;

/** 表情 -> 素材文件名。
 *
 *  无表情时用**官方立绘裁出的高清脸**（face_default.png，来自 1464×1464 原图），
 *  有表情时才用 mod 版的表情变体 —— 清晰度和表达力都要。 */
const EMOTION_FILE = {
  '':   'face_default.png',
  '$h': 'expr_happy.png',
  '$s': 'expr_sad.png',
  '$u': 'expr_unique.png',
  '$l': 'expr_love.png',
  '$a': 'expr_angry.png',
};

const STAGE_LABEL = {
  hearts_0_1: '初识', hearts_2_3: '渐渐熟悉', hearts_4_5: '朋友',
  hearts_6_7: '心里有你', hearts_8_plus: '情根深种',
  dating: '交往', engaged: '订婚', married: '成婚',
};

/* ── 节令纹样 ─────────────────────────────────────────
   她的设定里有浓厚的节令感 —— 语音里尽是「清明宜晴，谷雨宜雨」这种农谚。
   所以这里按季节显示对应节令，而不是随便放个装饰。 */
const SEASON_MARK = {
  spring: { label: '春 · 谷雨', season: 'spring' },
  summer: { label: '夏 · 小满', season: 'summer' },
  fall:   { label: '秋 · 白露', season: 'fall' },
  winter: { label: '冬 · 冬至', season: 'winter' },
};

function renderSeasonMark(label) {
  // 传入的是「仲秋」这种现实时令；读不到就按浏览器所在月份推
  let key = null;
  const m = (label || '').match(/[春夏秋冬]/);
  if (m) key = { '春': 'spring', '夏': 'summer', '秋': 'fall', '冬': 'winter' }[m[0]];
  if (!key) {
    const mo = new Date().getMonth() + 1;
    key = (mo <= 2 || mo === 12) ? 'winter'
        : mo <= 5 ? 'spring'
        : mo <= 8 ? 'summer' : 'fall';
  }
  const cfg = SEASON_MARK[key];
  $('seasonMark').dataset.season = cfg.season;
  // 有真实细分名（「仲秋」这种短词）就用它；否则用装饰性的默认文案。
  // 原来默认文案写死成「秋 · 白露」，那是九月的节气，十月底早就过时了。
  const raw = (label || '').trim();
  $('seasonText').textContent = (m && raw.length <= 4) ? raw : cfg.label;
}

/* ── 背景音乐 ─────────────────────────────────────────
   浏览器禁止自动播放，所以首次播放必须发生在用户操作之后。
   这里不硬来：勾选框本身就是那个操作。 */
const bgm = new Audio();
bgm.loop = true;
let bgmWanted = false;

function applyBgm(enabled, track, volume) {
  bgmWanted = !!enabled;

  if (track) {
    const src = `/bgm/${track}`;
    if (!bgm.src.endsWith(src)) {
      bgm.src = src;
      bgm.load();
    }
  }
  if (typeof volume === 'number') bgm.volume = volume;

  if (bgmWanted) {
    bgm.play().catch(() => {
      // 被浏览器拦下 —— 等下一次点击时再试
      const retry = () => {
        bgm.play().catch(() => {});
        document.removeEventListener('click', retry);
      };
      document.addEventListener('click', retry);
    });
  } else {
    bgm.pause();
  }
}



/* ── 她给你留的话 ─────────────────────────────────────
   你不在的时候她留的字条。**不是未读消息** ——
   她不留「你怎么不来」这种话，只是让你知道她也在过日子。

   展示成一张纸条的样子，看完标记已读。 */
let notesOpen = false;
// 服务端给的留话全量缓存。标已读后内容仍要能看到 —— 所以不能在渲染时按 seen 过滤。
let noteCache = [];

/* 留话区：
     合着  ->  一条折角的纸条 + 「她留了 N 句话」
     点开  ->  摊开全部内容，并标为已读

   为什么不自动展开并标已读：实测踩过 —— 自动标已读会被后台轮询误触，
   刚打开窗口纸条就变已读、你根本看不到。而且「收起来慢慢看」本身
   就是这个功能的趣味所在。 */
function renderNotes(list) {
  const box = $('noteBox');
  if (!box) return;

  // 服务端每次给全量，这里缓存住 —— 标已读之后内容还得能看。
  if (Array.isArray(list)) noteCache = list;

  // 「徽标数」和「展示列表」是两件事：
  //   徽标数   = 未读几条（决定还提不提醒你）
  //   展示列表 = 全部（决定点开之后能看到什么）
  const unseen = noteCache.filter(x => !x.seen);
  const key = noteCache.map(x => x.at).join('|');
  const openFlag = notesOpen ? '1' : '0';
  if (box.dataset.key === key && box.dataset.open === openFlag) return;

  box.dataset.key = key;
  box.dataset.open = openFlag;

  if (!noteCache.length) {
    box.classList.add('hidden');
    box.classList.remove('open');
    box.innerHTML = '';
    return;
  }

  box.innerHTML = '';
  box.classList.toggle('open', notesOpen);

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'note-head';
  // 只显示「她留了 N 句话」。
  //
  // ⚠️ 原来还有个「她留过的（N）」分支 —— 读完不消失，一叠旧纸条
  // 永远挂在界面上。用户的原话：「改成已读一次后就清理」。
  // 现在服务端读完即删，所以这里也不该再有那个状态。
  head.textContent = notesOpen ? '　收起来' : `她留了 ${unseen.length} 句话`;
  head.addEventListener('click', () => {
    notesOpen = !notesOpen;
    if (notesOpen) {
      // 展开 = 看过了。通知服务端**删掉**（它现在的语义是清空）。
      // 本地先留着，这样这一次展开还能读；收起时才真的消失。
      markNotesSeen();
    } else {
      // 收起 -> 本地副本也清掉，整块隐掉。
      // 不在界面上留「她留过的（N）」那种残留状态。
      noteCache = [];
      notesOpen = false;
      box.classList.add('hidden');
      box.classList.remove('open');
      box.innerHTML = '';
      box.dataset.key = '';
      return;
    }
    box.dataset.key = '';
    renderNotes(noteCache);
  });
  box.appendChild(head);

  if (notesOpen) {
    // 旧的在前（服务端就是这么排的），读起来像一叠纸条
    noteCache.forEach(x => {
      const d = document.createElement('div');
      d.className = 'note-slip';
      const t = document.createElement('div');
      t.className = 'note-text';
      t.textContent = x.text;
      d.appendChild(t);
      if (x.at) {
        const s = document.createElement('div');
        s.className = 'note-at';
        s.textContent = '留于 ' + x.at.slice(5, 16).replace('T', ' ');
        d.appendChild(s);
      }
      box.appendChild(d);
    });
  }

  box.classList.remove('hidden');
}

function markNotesSeen() {
  fetch('/api/notes_seen', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }).catch(() => {});
}

// 轮询：留话是后台生成的，生成完这一轮就能看到
async function pollNotes() {
  try {
    const st = await (await fetch('/api/status')).json();
    renderNotes(st.notes);
    if (st.notes && st.notes.some(x => !x.seen)) setTimeout(pollNotes, 20000);
  } catch (e) { /* 静默 */ }
}


/* ── 他答应过的事 ─────────────────────────────────────
   她自己心里记着的小事，不是待办清单。
   所以语气上：到期的在前，但不标红、不催、不加感叹号。 */
let promises = [];

async function loadPromises() {
  try {
    const d = await (await fetch('/api/promises')).json();
    promises = d.items || [];
    const b = $('promiseBadge');
    if (b) b.textContent = d.live || 0;
    renderPromises();
  } catch (e) { /* 静默 */ }
}

function renderPromises() {
  const box = $('promiseList');
  if (!box) return;
  const live = promises.filter(x => !x.kept);
  box.innerHTML = '';
  if (!live.length) {
    box.innerHTML = '<div class="promise-empty">她心里没有悬着的事。</div>';
    return;
  }
  live.forEach((p, _i) => {
    const row = document.createElement('div');
    row.className = 'promise-row' + (p.overdue ? ' overdue' : '');

    const t = document.createElement('span');
    t.className = 'promise-text';
    t.textContent = p.text;
    t.title = (p.at || '').slice(0, 10) + ' 说的'
            + (p.overdue ? '　已过说好的日子' : '');
    row.appendChild(t);

    const keep = document.createElement('button');
    keep.type = 'button';
    keep.className = 'promise-btn';
    keep.textContent = '做到了';
    keep.title = '她就放心了';
    // at 可能为空（迁移进来的老条目）→ 把 text 一起带上
    keep.addEventListener('click', () => promiseOp({ op: 'keep', at: p.at, text: p.text, index: _i }));
    row.appendChild(keep);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'promise-btn';
    del.textContent = '不提了';
    del.title = '让她忘掉这件事';
    del.addEventListener('click', () => promiseOp({ op: 'forget', at: p.at, text: p.text, index: _i }));
    row.appendChild(del);

    box.appendChild(row);
  });
}

async function promiseOp(body) {
  await fetch('/api/promises', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  await loadPromises();
}

/* ── 收藏她说过的话 ───────────────────────────────────
   存的不只是一句话，而是**那一刻**：她当时什么心情、关系到哪一步、
   你上一句说了什么把她引过来的。只存孤句的话，半年后翻出来
   完全想不起当时在聊什么。

   `favoriteSet` 是已收藏文本的集合，用来渲染时立刻标出星号，
   不必每条都去问服务器。 */
const favoriteSet = new Set();
let favorites = [];
let lastPlayerLine = '';      // 用来给收藏补上「场景」

function syncFavStars() {
  document.querySelectorAll('.fav-btn').forEach(b => {
    const on = favoriteSet.has(b.dataset.text);
    b.classList.toggle('on', on);
    b.textContent = on ? '★' : '☆';
  });
}

async function loadFavorites() {
  try {
    const d = await (await fetch('/api/favorites')).json();
    favorites = d.items || [];
    favoriteSet.clear();
    favorites.forEach(f => favoriteSet.add((f.text || '').trim()));
    const b = $('favBadge');
    if (b) b.textContent = favorites.length;
  } catch (e) { /* 静默：收藏读不到不该影响聊天 */ }
}

async function toggleFavorite(btn) {
  const text = btn.dataset.text || '';
  if (!text) return;
  const on = btn.classList.contains('on');
  try {
    const url = on ? '/api/unfavorite' : '/api/favorite';
    const body = on ? { text } : { text, context: lastPlayerLine };
    const d = await (await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })).json();
    if (!d.ok) { addSys(d.error || '收藏失败', 'err'); return; }

    if (on) {
      favoriteSet.delete(text);
      btn.classList.remove('on');
      btn.textContent = '☆';
    } else {
      favoriteSet.add(text);
      btn.classList.add('on');
      btn.textContent = '★';
      btn.animate([{ transform: 'scale(1)' }, { transform: 'scale(1.45)' },
                   { transform: 'scale(1)' }], { duration: 260 });
    }
    const b = $('favBadge');
    if (b) b.textContent = d.count;
    await loadFavorites();
  } catch (e) {
    addSys('收藏失败：' + e.message, 'err');
  }
}

/* 收藏列表 */
function openFavorites() {
  const l = $('favList');
  l.innerHTML = '';
  if (!favorites.length) {
    l.innerHTML = '<div class="fav-empty">还没有收藏。<br>' +
      '把鼠标移到她任意一句话上，点那颗星星。</div>';
  } else {
    favorites.forEach(f => {
      const card = document.createElement('div');
      card.className = 'fav-card';

      const q = document.createElement('div');
      q.className = 'fav-quote';
      q.textContent = f.text;
      card.appendChild(q);

      // 场景：你当时说了什么 + 她什么状态 + 关系阶段 + 日期
      const meta = document.createElement('div');
      meta.className = 'fav-meta';
      const bits = [];
      if (f.context) bits.push('你说「' + f.context + '」');
      if (f.mood) bits.push(f.mood);
      if (f.stage) bits.push(f.stage);
      if (f.at) bits.push(f.at.slice(0, 10));
      meta.textContent = bits.join('　·　');
      card.appendChild(meta);

      const note = document.createElement('input');
      note.className = 'fav-note';
      note.placeholder = '写一句备注…';
      note.value = f.note || '';
      note.addEventListener('change', async () => {
        await fetch('/api/favorite_note', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ at: f.at, note: note.value }),
        });
      });
      card.appendChild(note);

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'fav-del';
      del.textContent = '移除';
      del.addEventListener('click', async () => {
        const d = await (await fetch('/api/unfavorite', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ at: f.at }),
        })).json();
        if (d.ok) {
          await loadFavorites();
          // 同步正聊着的那条气泡上的星号
          document.querySelectorAll('.fav-btn').forEach(b => {
            const on = favoriteSet.has(b.dataset.text);
            b.classList.toggle('on', on);
            b.textContent = on ? '★' : '☆';
          });
          openFavorites();
        }
      });
      card.appendChild(del);

      l.appendChild(card);
    });
  }
  $('favBox').classList.remove('hidden');
}

/* ── 配色主题 ─────────────────────────────────────────
   四套：light 宣纸 / moon 月华 / ink 墨夜 / candle 烛火。
   只改 <html data-theme>，其余全部由 CSS 变量接管。 */
const THEMES = ['light', 'moon', 'ink', 'candle'];

function applyTheme(name) {
  const t = THEMES.includes(name) ? name : 'light';
  if (t === 'light') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
  document.querySelectorAll('.theme-btn').forEach(b => {
    b.classList.toggle('on', b.dataset.theme === t);
  });
}

/* ── 立绘 ───────────────────────────────────────────── */
function setPortrait(indexOrFile) {
  // 兼容两种调用：传表情文件名，或传旧的索引
  const file = typeof indexOrFile === 'string'
    ? indexOrFile
    : (Object.keys(EMOTION_INDEX).find(k => EMOTION_INDEX[k] === indexOrFile) || '');
  const url = `/portrait/${file.replace(/\.png$/, '')}`;

  if (url === lastPortraitUrl) return;
  lastPortraitUrl = url;
  currentFace = file.replace(/\.png$/, '');

  // 大立绘
  const img = $('portrait');
  img.classList.add('swap');
  const next = new Image();
  next.onload = () => {
    img.src = next.src;
    img.classList.remove('swap');
  };
  next.onerror = () => img.classList.remove('swap');
  next.src = url;
  currentPortrait = indexOrFile;

  // 消息里她的头像也跟着换 —— 否则大图在笑、小头像还是平静的，会别扭
  document.querySelectorAll('#log .msg.her .avatar img').forEach(a => {
    a.src = url;
  });
}

/* ── 心情 ─────────────────────────────────────────────
   两个维度：情绪（好坏）与精力（想不想说话）。
   显示成一个带颜色的标签，鼠标悬停能看到「今天发生了什么」。

   刻意**不做成进度条** —— 那样太像数值系统，会出戏。
   心情该让人觉得是「她今天的状态」，而不是一个可以刷的条。 */
function renderMood(m) {
  const el = $('moodChip');
  if (!el) return;
  if (!m || !m.label) { el.textContent = '—'; el.className = 'mood-chip'; el.title = ''; return; }

  const good = (m.valence ?? 60) >= 58;
  const low  = (m.energy  ?? 55) < 34;
  let cls = 'mood-chip';
  if (good && !low) cls += ' bright';     // 兴致好
  else if (!good && low) cls += ' dim';   // 提不起劲
  else if (low) cls += ' low';            // 懒懒的
  else if (!good) cls += ' dull';         // 有些闷

  el.className = cls;
  el.textContent = m.label;
  // 悬停显示：今天发生了什么 + 具体状态
  const tip = [];
  if (m.event) tip.push(m.event);
  tip.push(`情绪${m.valence}　精力${m.energy}`);
  el.title = tip.join('\n');
}

/* ── 状态栏 ─────────────────────────────────────────── */
function renderStatus(st) {
  if (!st) return;
  $('stageName').textContent = st.stage_name || '';

  // 好感条改成「在一起 · 第 N 天」，条表示最近 30 天的陪伴度。
  // （2026-10-27 与游戏脱钩：原来读存档的 10 心，脱钩后永远是旧值）
  const d = st.days || {};
  $('affText').textContent = d.together
    ? `在一起 · 第 ${d.together} 天` : '在一起';
  $('affNext').textContent = d.active30
    ? `最近 30 天里 ${d.active30} 天说过话` : '';
  $('affBar').style.width = Math.min(100, ((d.active30 ?? 0) / 30) * 100) + '%';

  $('lifeTask').textContent = st.task || '—';
  $('lifeThought').textContent = st.thought || '—';
  // 小麻烦 / 口味（2026-11-05）
  const _tr = $('lifeTrouble');
  // days 这一块**偶发为空**（服务重启、某次请求赶上 days 统计抛异常）。
  // 空的时候不要清空界面 —— 留着上一次的值，否则「小麻烦/最近记得」
  // 会突然变成「—」和「（还没记住什么）」，看起来像坏了（2026-11-05）。
  window.__mindDays = window.__mindDays || {};
  if (st.days && Object.keys(st.days).length) {
    window.__mindDays = st.days;
  }
  const _d = window.__mindDays;

  // QQ 栏（2026-11-05，独立一栏）
  // 数据实际在 days.qq 里（2026-11-08 修：原来读 st.qq，永远是空）
  const _q = st.qq || (_d && _d.qq) || {};
  const _set = function (id, txt) {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };
  if (_q.configured) {
    const m = _q.minutes;
    _set('qqState', (m >= 0 && m < 10) ? '在线' : '安静');
    _set('qqLast', m < 0 ? '—'
      : m < 1 ? '刚刚'
      : m < 60 ? m + ' 分钟前'
      : Math.floor(m / 60) + ' 小时前');
    _set('qqCount', (_q.count || 0) + ' 条');
    _set('qqBadge', (_q.count || 0) + '');
  } else {
    _set('qqState', '还没接上');
    _set('qqLast', '—');
    _set('qqCount', '0 条');
    _set('qqBadge', '—');
  }
  _set('qqBridge', _q.bridge_on ? '已开启' : '未开启');
  _set('qqHint', _q.bridge_on ? '127.0.0.1:8732' : 'bridge_config.json → enabled');


  // 最近记得的三件事（2026-11-05）
  const _ml = document.getElementById('memTopList');
  if (_ml) {
    const arr = _d.mem_top || [];
    _ml.textContent = '';
    if (!arr.length) {
      const li = document.createElement('li');
      li.className = 'memtop-empty';
      li.textContent = '（还没记住什么）';
      _ml.appendChild(li);
    } else {
      arr.forEach(function (x) {
        const li = document.createElement('li');
        li.textContent = x;
        _ml.appendChild(li);
      });
    }
  }
  if (_tr) _tr.textContent = _d.trouble || '—';
  const _ta = $('lifeTaste');
  if (_ta) {
    const arr = _d.tastes || [];
    // 只留冒号后面的部分：「吃：喜欢烫嘴的」-> 「烫嘴的」
    _ta.textContent = arr.length
      ? arr.map(function (x) { return String(x).split('：').pop(); }).join(' · ')
      : '';
    if (_ta.parentElement) {
      _ta.parentElement.style.display = arr.length ? '' : 'none';
    }
  }
  renderMood(st.mood);

  const n = st.memories ?? 0;
  $('memBadge').textContent = n;

  // 她该怎么称呼你（用于你自己的消息标签）
  myName = st.world?.call_name || '';
  window.__usage = st.usage || {};
  if (st.session) {
    const box = document.getElementById('worldCard') || document.querySelector('.card.world');
    if (box) box.title = '会话：' + st.session;
  }
  if (window.mindPaintRail) window.mindPaintRail(st);
  renderWorld(st.days, st.world);
  renderSeasonMark(st.days?.season || '');

  // 「关系固定 已与游戏脱钩」已删（2026-10-27）：那是过渡期提示，不该长期占位
}

/** 「她知道的」—— 她现在脑子里有什么。
 *
 *  2026-10-27 之前这里是游戏加载状态（你的背景 / 游戏进度）。
 *  与游戏脱钩后那些永远是空的，改成她的四类记忆的**条数**。
 *  数据来自 /api/status 的 `days`。 */
function renderWorld(days, w) {
  const d = days || {};
  // 数据没到（或拿到空对象）就**保留上一次的数字**，不要画成 0。
  // 之前的表现是：某一帧 /api/status 没有 days，面板就整块变成 0，
  // 看起来像"记忆丢了"，其实只是这一次响应没带。2026-10-28
  if (d.mem === undefined && d.evt === undefined && d.together === undefined) {
    return;
  }
  const put = (id, text) => {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.title = text || '';        // 省略号截断时，悬停能看全
  };
  // 值都缩短 —— 侧栏只有 264px，长句会被截
  put('worldProgress', `记忆 ${d.mem ?? 0} · 观察 ${d.obs ?? 0}`);
  put('worldFarm', `事件 ${d.evt ?? 0} · 待办 ${d.prom ?? 0}`);
  put('worldFriends', `日 ${d.dig ?? 0} · 周 ${d.wk ?? 0}`);
  const wx = (d.weather || '').replace('，', ' ').replace(/\s*度上下/, '°');
  put('worldShu', [d.season, wx].filter(Boolean).join(' · '));

  // 右侧的天气行（2026-11-02）：和「眼下」同源，都是现实天气
  const wbox = document.querySelector('.bc-weather');
  const wtxt = document.getElementById('bcWx');
  if (wtxt) {
    wtxt.textContent = wx || '—';
    if (wbox) {
      const mine = /雨/.test(wx) ? 'rain'
                 : /雪/.test(wx) ? 'snow'
                 : /晴/.test(wx) ? 'sunny' : 'cloud';
      wbox.dataset.wx = mine;
    }
  }
  // 今天用掉多少 token（2026-10-27 加，对应 P1 记账）
  const u = (window.__usage || {});
  put('usageLine', u.calls
    ? `${u.calls} 次 · 入 ${Math.round((u.in || 0) / 1000)}k / 出 ${Math.round((u.out || 0) / 1000)}k`
    : '—');
}

/* ── 消息 ───────────────────────────────────────────── */
/** 建一条消息（微信式：头像 + 气泡）。
 *
 *  她的头像用当前立绘（会随表情变），你的头像用游戏里还原的小人。 */
/* ── 时间戳显示 ──────────────────────────────────────────
   ## 踩过的坑（2026-10-16 用户报「两次对话差 9 小时」）

   原来这里是 `at.slice(11, 16)` —— 直接切字符串的第 11~16 位。
   但时间戳有**两种来源、两种时区**：

       历史记录     Python 写的   "2026-10-16T23:38:21"       本地时间
       乐观插入     JS 写的       "2026-10-16T14:45:37.123Z"  **UTC**

   `new Date().toISOString()` 按定义输出 UTC。
   于是同一段对话里，第 1 条显示 23:38、第 2 条显示 14:45
   —— 看起来像「时间读错了」，其实是**字符串切法对 UTC 无效**。

   修法：**先认时区再解析**。
       带 Z 或 ±HH:MM  -> 是绝对时刻，转成本地再取时分
       不带            -> 已经是本地挂钟时间，直接切
   （不带时区的 ISO 串，`new Date()` 会按本地解释，所以不能无脑用它。） */
function hhmm(at) {
  const s = String(at || '');
  if (!s) return '';
  const hasZone = /Z$|[+-]\d{2}:?\d{2}$/.test(s);
  if (hasZone) {
    const d = new Date(s);
    if (!isNaN(d)) {
      return String(d.getHours()).padStart(2, '0') + ':' +
             String(d.getMinutes()).padStart(2, '0');
    }
  }
  return s.slice(11, 16);
}

/* 本地时间的 ISO 串 —— **不要用 `new Date().toISOString()`**。
   那个按定义输出 UTC，混进服务端写的本地时间戳里会显示成两个时区
   （实测：同一段对话里一条 23:38、一条 14:45）。 */
function localISO() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
         `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function addBubble(role, text, at, images) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role === 'her' ? 'her' : 'me');

  // 头像
  const av = document.createElement('div');
  av.className = 'avatar';
  const img = document.createElement('img');
  img.alt = role === 'her' ? '她' : '我';
  if (role === 'her') {
    img.id = 'avatarShu';
    img.src = `/portrait/${currentFace || 'face_default'}`;
    img.onerror = () => { img.src = '/portrait/face_default'; };
  } else {
    img.src = '/avatar/farmer';
  }
  av.appendChild(img);
  el.appendChild(av);

  // 气泡本体
  const stack = document.createElement('div');
  stack.className = 'stack';

  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = role === 'her' ? '她' : (myName || '你');
  stack.appendChild(who);

  const bubble = document.createElement('div');
  bubble.className = 'bubble ' + (role === 'her' ? 'her' : 'me');

  const body = document.createElement('span');
  body.className = 'body';
  // 注意：静态气泡必须在这里就填文字。
  // 流式回复走 typewrite() 会覆盖它，但历史记录不会 ——
  // 少这一行的话，刷新后所有历史消息都是空气泡（实测踩过）。
  body.textContent = text || '';
  bubble.appendChild(body);

  // 图放在文字下面。只用来回看，不重新发给模型
  // （历史里的图重发既费 token，又会让她以为你又发了一遍）。
  if (images && images.length) {
    const wrap = document.createElement('div');
    wrap.className = 'bubble-imgs';
    images.forEach((url) => {
      const im = document.createElement('img');
      im.src = url;
      im.alt = '发过的图';
      im.addEventListener('click', () => window.open(url, '_blank'));
      wrap.appendChild(im);
    });
    bubble.appendChild(wrap);
    // 只发图不打字时，气泡里会是一段空白，给它一点高度
    if (!text) body.remove();
  }
  stack.appendChild(bubble);

  // 收藏按钮 —— 只给她的消息。你说的话没什么好收藏的。
  // 平时淡到看不见，鼠标移到这一条上才显形，不干扰阅读。
  if (role === 'her') {
    const fav = document.createElement('button');
    fav.type = 'button';
    fav.className = 'fav-btn';
    fav.title = '收藏这句';
    fav.addEventListener('click', (ev) => {
      ev.stopPropagation();
      toggleFavorite(fav);
    });
    stack.appendChild(fav);
    attachFavText(bubble, text);   // 统一在这里决定星号亮不亮
  }

  // 语音播放键 —— 只给她的话挂，你的话不需要（你不会想听自己念）。
  //
  // ⚠️ 取文本时**现读 body.textContent**，不用闭包里的 `text`：
  // 流式回复是逐字填进 body 的，闭包捕获的是空串。
  // 而且历史消息里的文字可能还带着表情记号（`$u` 之类）——
  // 那个由服务端摘：`/api/tts` 内部会调 `mind_speech.strip_emotion`。
  //
  // （原来这里写的是「交给 /api/speech 处理」。那个接口 2026-10-18 删了 ——
  // 它只在「前端逐段取音频」的方案里有用，而那个方案因为段间缝隙被否掉了。
  // 现在只有 /api/tts，一次拿整条。）
  if (role === 'her' && window.Voice) {
    window.Voice.attach(bubble, () => body.textContent || text || '');
  }

  // ── 重做这一轮（2026-10-17 加）────────────────────────
  //
  // 她答偏了没法重来，而且那次偏差会**顺着上下文传下去**
  // （实测：一条答非所问之后，后面两条也跟着歪）。
  //
  // ⚠️ **这里不挂按钮。** 只有「最后那一轮」能重做 ——
  // 中间的重做会把后面的对话撕开。按钮由 rewind.js 的
  // `markRewindable()` 在每次渲染完之后挂。
  //
  // 踩过的坑（2026-10-17）：这里原来写了
  // `if (window.Rewind) window.Rewind.register(bubble, ...)`，
  // 而 rewind.js 只导出 `{ markRewindable, send }` —— **没有 register**。
  // 守卫检查的是**对象**，对象在、方法不在，于是 TypeError，
  // **boot 的渲染循环在第二条就断了**。
  // 用户看到的症状：打开界面只有一条消息。
  //
  // 教训：守卫要检查**方法本身** —— `if (window.X && window.X.fn)`。

  if (at) {
    const t = document.createElement('span');
    t.className = 'time';
    t.textContent = hhmm(at);
    stack.appendChild(t);
  }

  el.appendChild(stack);
  $('log').appendChild(el);
  scrollToBottom();

  // 双击复制
  bubble.addEventListener('dblclick', () => {
    navigator.clipboard?.writeText(text).then(
      () => flash(bubble, '已复制'),
      () => {}
    );
  });

  return { el, body, bubble, avatarImg: img };
}

/** 打字机：逐字显示她的回复。
 *  为什么要这个：原来一次性弹出来，像系统通知，不像人在说话。
 *
 *  实现上刻意只用一个定时器 —— 第一版我在标点处切换定时器，
 *  写出了死代码和 arguments.callee（已废弃），很难读也容易出错。
 *
 *  加 `?fast=1` 可关闭打字机（截图核对界面时用，否则永远拍不到完整文字）。 */
const FAST = new URLSearchParams(location.search).has('fast');

/* 把最终文本挂到这一条的收藏按钮上。

   两种时机都要用：
     · addBubble 建静态气泡时（历史记录）
     · typewrite 开始时（新回复，因为它是从空文本打起的） */
function attachFavText(node, text) {
  const stack = node.closest ? node.closest('.stack') : null;
  const btn = stack && stack.querySelector('.fav-btn');
  if (!btn) return;
  const t = (text || '').trim();
  btn.dataset.text = t;
  const on = favoriteSet.has(t);
  btn.classList.toggle('on', on);
  btn.textContent = on ? '★' : '☆';
}

function typewrite(node, text, onDone) {
  clearTimeout(typingTimer);
  node.textContent = '';
  node.classList.add('typing-text');

  // 打字机是从空文本开始打的，所以气泡创建时收藏按钮拿不到内容。
  // 这里补一次 —— 否则满心欢喜点星号，收藏下来的是空字符串。
  attachFavText(node, text);

  if (FAST) {
    node.textContent = text;
    node.classList.remove('typing-text');
    scrollToBottom();
    if (onDone) onDone();
    return;
  }

  let i = 0;

  const step = () => {
    if (i >= text.length) {
      node.classList.remove('typing-text');
      scrollToBottom();
      if (onDone) onDone();
      return;
    }

    node.textContent += text[i++];
    scrollToBottom();

    // 标点后停得久一点，读起来像在断句
    const justTyped = text[i - 1];
    const delay = '。！？…'.includes(justTyped) ? 150
                : '，、；：'.includes(justTyped) ? 70
                : 26;
    typingTimer = setTimeout(step, delay);
  };

  step();
}

function flash(el, msg) {
  const tip = document.createElement('span');
  tip.className = 'flash';
  tip.textContent = msg;
  el.appendChild(tip);
  setTimeout(() => tip.remove(), 900);
}

function scrollToBottom() {
  const log = $('log');
  log.scrollTop = log.scrollHeight;
}

function addSys(text, kind) {
  const el = document.createElement('div');
  el.className = 'sys' + (kind ? ' ' + kind : '');
  el.textContent = text;
  $('log').appendChild(el);
  scrollToBottom();
  return el;
}

/** 日期分隔：现实日期 + 游戏进度 */
function addDayDivider(day, gameProgress) {
  const el = document.createElement('div');
  el.className = 'day-divider';
  const left = document.createElement('span');
  left.textContent = day;
  el.appendChild(left);
  if (gameProgress) {
    const right = document.createElement('span');
    right.className = 'game-progress';
    right.textContent = gameProgress;
    el.appendChild(right);
  }
  $('log').appendChild(el);
}

function addTyping() {
  const el = document.createElement('div');
  el.className = 'msg her';

  const av = document.createElement('div');
  av.className = 'avatar';
  const img = document.createElement('img');
  img.alt = '她';
  img.src = `/portrait/${currentFace || 'face_default'}`;
  img.onerror = () => { img.src = '/portrait/face_default'; };
  av.appendChild(img);
  el.appendChild(av);

  const stack = document.createElement('div');
  stack.className = 'stack';
  const bubble = document.createElement('div');
  bubble.className = 'bubble shu';
  bubble.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  stack.appendChild(bubble);
  el.appendChild(stack);

  $('log').appendChild(el);
  scrollToBottom();
  return el;
}

/* ── 发送 ───────────────────────────────────────────── */
//
//  ## 图片
//
//  待发的图存在 `pendingImages` 里（data: URL 数组），发送时随 text 一起走
//  `/api/chat` 的 images 字段。后端 normalize 会把裸 base64 也一起处理，
//  所以这里统一转成 data: URL。
//
//  三条入口：粘贴（Ctrl+V）、拖放、选文件。都走同一个 addImage()。
const pendingImages = [];

const IMG_MAX = 6 * 1024 * 1024;      // 和后端 shu-image.js 的上限保持一致
const IMG_MAX_N = 3;

function addImage(file) {
  if (!file || !file.type || !file.type.startsWith('image/')) return;
  if (file.size > IMG_MAX) {
    addSys(`图太大了（${(file.size / 1048576).toFixed(1)} MB，上限 6 MB）`, 'err');
    return;
  }
  if (pendingImages.length >= IMG_MAX_N) {
    addSys(`一次最多 ${IMG_MAX_N} 张`, 'err');
    return;
  }
  const fr = new FileReader();
  fr.onload = () => {
    pendingImages.push(String(fr.result));
    renderImageStrip();
  };
  fr.onerror = () => addSys('读不了这张图', 'err');
  fr.readAsDataURL(file);
}

function renderImageStrip() {
  const strip = $('imgStrip');
  if (!strip) return;
  strip.innerHTML = '';
  if (!pendingImages.length) {
    strip.classList.add('hidden');
    return;
  }
  strip.classList.remove('hidden');
  pendingImages.forEach((url, i) => {
    const d = document.createElement('div');
    d.className = 'img-thumb';
    const im = document.createElement('img');
    im.src = url;
    im.alt = '待发送的图';
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'img-del';
    x.textContent = '×';
    x.title = '去掉这张';
    x.addEventListener('click', () => {
      pendingImages.splice(i, 1);
      renderImageStrip();
    });
    d.appendChild(im);
    d.appendChild(x);
    strip.appendChild(d);
  });
}

async function send() {
  if (busy) return;
  const box = $('input');
  const text = box.value.trim();
  // 只发图不打字也可以 —— 后端会补一句「（他给你看了这个）」
  if (!text && !pendingImages.length) return;

  busy = true;
  $('send').disabled = true;
  const imgs = pendingImages.slice();          // 先拷一份，下面要清空
  box.value = '';
  box.style.height = 'auto';

  // 记下来 —— 收藏她那句时要带上「你说了什么」当场景。
  // 只存孤句的话，半年后翻出来完全想不起当时在聊什么。
  lastPlayerLine = text || '（给他看了张图）';
  addBubble('me', lastPlayerLine, localISO(), imgs);

  pendingImages.length = 0;
  renderImageStrip();

  // 不把立绘切回中性 —— 原来那样会闪一下，因为回复到达后还要再切一次
  const typing = addTyping();

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, images: imgs }),
    });
    const data = await resp.json();
    typing.remove();

    if (!data.ok) {
      if (data.need_key) $('keyGate').classList.remove('hidden');
      else addSys(data.error || '出错了', 'err');
      return;
    }

    setPortrait(data.portrait || data.emotion || '');
    // 优先用服务端给的 at（本地时间）；服务端没给才退回本地时间
    const { el, body, bubble } = addBubble('her', '', data.at || localISO());
    bubble.classList.add('streaming');

    typewrite(body, data.reply, () => {
      bubble.classList.remove('streaming');
      if (data.remembered) addSys(`（她记住了：${data.remembered}）`);
      if (data.sync_note) addSys(`（${data.sync_note}）`);
      // 新的最后一轮产生了 -> 可重做的目标变了
      if (window.Rewind) window.Rewind.markRewindable();
      // 她可能又许了新的、也可能刚把旧的了结 —— 侧边栏要跟上
      loadPromises();
    });

    if (data.status) renderStatus(data.status);

  } catch (e) {
    typing.remove();
    addSys('连不上本地服务：' + e.message, 'err');
  } finally {
    busy = false;
    $('send').disabled = false;
    box.focus();
  }
}

/* ── 她主动开口 ─────────────────────────────────────── */
async function askHerToSpeak() {
  if (busy) return;
  busy = true;
  $('btnSpeak').disabled = true;
  const typing = addTyping();
  try {
    const r = await fetch('/api/open_topic', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    typing.remove();
    if (!d.ok) {
      if (d.need_key) $('keyGate').classList.remove('hidden');
      else addSys(d.error || '出错了', 'err');
      return;
    }
    if (!d.reply) {
      addSys('她这会儿没想出要说什么', 'err');
      return;
    }
    setPortrait(d.portrait || d.emotion || '');
    // 这里原来写的是 data.at —— data 不存在，抛 ReferenceError，
    // 被下面 catch 吞掉，于是按钮看起来"点了没反应"（2026-11-10 修）
    const { body, bubble } = addBubble('her', '', d.at || localISO());
    bubble.classList.add('streaming');
    typewrite(body, d.reply, () => bubble.classList.remove('streaming'));
    if (d.status) renderStatus(d.status);
  } catch (e) {
    typing.remove();
    addSys('她没能开口：' + e.message, 'err');
  } finally {
    busy = false;
    $('btnSpeak').disabled = false;
  }
}

/* ── 纠正她 ─────────────────────────────────────────── */
async function correct(text) {
  try {
    const r = await fetch('/api/correct', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const d = await r.json();
    if (d.ok) {
      addSys(`（嘱咐过了：${d.text}）`);
      const st = await (await fetch('/api/status')).json();
      renderStatus(st);
    } else {
      addSys(d.error || '没记上', 'err');
    }
  } catch (e) {
    addSys('连不上本地服务：' + e.message, 'err');
  }
}

/* ── 记忆面板 ───────────────────────────────────────── */
async function openMemory() {
  $('memPanel').classList.remove('hidden');
  await refreshMemory();
  renderSnapshots();          // 快照列表不阻塞主内容
  $('memInput').focus();
}

async function refreshMemory() {
  try {
    const r = await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'list' }),
    });
    const d = await r.json();
    renderMemoryList(d.memories || []);
    renderReadonly(d);
  } catch (e) {
    $('memList').innerHTML = `<div class="mem-empty">读取失败：${e.message}</div>`;
  }
}

/* 只读的三块：观察 / 承诺 / 统计。
 *
 * 为什么要摊出来：她的「记得」有四种东西（记忆、观察、承诺、旧对话），
 * 原来面板只给看一种。于是「她是不是真的记得」变成了猜 ——
 * 你看不到她眼里你是什么样的人，也看不到你答应过的事还挂着几件。
 *
 * 观察和承诺**不给编辑按钮**：它们是她自己看出来的、你亲口说的，
 * 手动改会破坏「这是真的发生过」这件事。要改就改 memories。 */
function renderReadonly(d) {
  const box = $('memReadonly');
  if (!box) return;
  box.innerHTML = '';

  const sec = (title, note) => {
    const h = document.createElement('div');
    h.className = 'mem-sec';
    h.textContent = title;
    if (note) {
      const n = document.createElement('span');
      n.className = 'mem-sec-note';
      n.textContent = note;
      h.appendChild(n);
    }
    box.appendChild(h);
    return h;
  };

  // ── 改删工具（2026-10-19 加）──────────────────────────
  //
  // 原来观察/事件/承诺是**只读**的，于是她记错一条（比如把
  // 「你明天还得早起」推成「他答应的事都记在心上」）你没任何办法去掉。
  //
  // 改完**重拉整个面板**，不手工拼 DOM —— 这样 index 一定是最新的，
  // 不会出现「删了第 3 条，第 4 条顶上来又被误删」。
  const reload = async () => { try { await refreshMemory(); } catch (e) {} };

  const roBtn = (label, title, fn) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'mem-ro-btn';
    b.textContent = label;
    b.title = title;
    b.addEventListener('click', (ev) => { ev.stopPropagation(); fn(); });
    return b;
  };

  const delObs = async (i) => {
    if (!confirm('删掉这条观察？她以后不会再这么看你。')) return;
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'delete', kind: 'observation', index: i }),
    });
    reload();
  };

  const editObs = async (i, old) => {
    const t = prompt('改写这条观察（她看错的地方）：', old);
    if (t == null) return;
    const v = t.trim();
    if (!v || v === old) return;
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'edit', kind: 'observation', index: i, text: v }),
    });
    reload();
  };

  // 手动标 / 取消「头一回」。
  // 标了 ★ 的事件**永远不会被 120 条上限裁掉**（服务端 _trim_events）。
  const starEv = async (i, first) => {
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'star', kind: 'event', index: i, first: !first }),
    });
    reload();
  };

  // ── 她自己的过去 / 手上的活（2026-10-20 加）──────────
  const memPost = async (body) => {
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    reload();
  };

  const delHer = async (i) => {
    if (!confirm('删掉她这一天的事？她以后不会提起了。')) return;
    memPost({ op: 'delete', kind: 'her_event', index: i });
  };

  const editHer = async (i, old) => {
    const t = prompt('改这一天的事：', old);
    if (t == null) return;
    const v = t.trim();
    if (!v || v === old) return;
    memPost({ op: 'edit', kind: 'her_event', index: i, text: v });
  };

  const delProj = async (i) => {
    if (!confirm('删掉这件活？她会当没这回事。')) return;
    memPost({ op: 'delete', kind: 'project', index: i });
  };

  const editProj = async (i, old) => {
    const t = prompt('改这件活的名字：', old);
    if (t == null) return;
    const v = t.trim();
    if (!v || v === old) return;
    memPost({ op: 'edit', kind: 'project', index: i, text: v });
  };

  // 进度 +1 —— 你自己知道她做到哪一步了，不该只能等天数
  const stepProj = async (i, stage, total) => {
    if (stage >= total) return;
    memPost({ op: 'edit', kind: 'project', index: i, stage: stage + 1 });
  };

  // 直接设进度（点那个「2/3」就行）
  const setProjStage = async (i, stage, total, label) => {
    const v = prompt(`${label}\n\n进度改成几？（0 – ${total}，填 ${total} 就是做完了）`, String(stage));
    if (v == null) return;
    const n = parseInt(v, 10);
    if (!isFinite(n) || n < 0 || n > total) return;
    memPost({ op: 'edit', kind: 'project', index: i, stage: n });
  };

  // 标成做完：立刻进「她自己的这些天」，不等明天跨天
  const finishProj = async (i, total) => {
    if (!confirm('标成做完了？\n\n她会记下这件事，以后聊天里可能提起。')) return;
    memPost({ op: 'edit', kind: 'project', index: i, stage: total });
  };

  // 手动记一件事件：你写措辞，你定是不是「头一回」。
  //
  // 为什么需要：自动判据是从你那句话里**剪**片段，很多时刻它认不出
  // （没有活动词、也不是第一次），而且剪出来的是半个句子。
  // 手写一件：就地展开输入框，不再用浏览器 prompt。
  //
  // Ctrl/Cmd+Enter 存，Esc 取消。写不下可以换行 —— 事件本来就该是一句话
  // 说得完的事，三行足够。
  const addEventManual = () => {
    if (box.querySelector('.ev-form')) {
      box.querySelector('.ev-ta').focus();
      return;
    }

    const form = document.createElement('div');
    form.className = 'ev-form';

    const ta = document.createElement('textarea');
    ta.className = 'ev-ta';
    ta.rows = 3;
    ta.maxLength = 160;
    ta.placeholder = '写完整一句 —— 两个月后你自己也要看得懂。\n比如：那一夜，水凉了也没起身。';

    const row = document.createElement('div');
    row.className = 'ev-row';

    const lab = document.createElement('label');
    lab.className = 'ev-first';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(' 头一回（★ 不会被淘汰）'));

    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'ev-save';
    save.textContent = '存';

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'ev-cancel';
    cancel.textContent = '取消';

    row.appendChild(lab);
    row.appendChild(save);
    row.appendChild(cancel);
    form.appendChild(ta);
    form.appendChild(row);
    box.appendChild(form);
    ta.focus();

    const close = () => { try { form.remove(); } catch (e) {} };

    save.addEventListener('click', async () => {
      const v = ta.value.trim();
      if (!v) { ta.focus(); return; }
      save.disabled = true;
      save.textContent = '…';
      try {
        await fetch('/api/memory', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'add', kind: 'event', text: v, first: cb.checked }),
        });
      } catch (e) {}
      close();
      reload();
    });
    cancel.addEventListener('click', close);

    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault(); save.click();
      } else if (e.key === 'Escape') {
        e.preventDefault(); close();
      }
    });
  };

  const delEv = async (i) => {
    if (!confirm('删掉这件事？她会当它没发生过。')) return;
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'delete', kind: 'event', index: i }),
    });
    reload();
  };

  const delPr = async (at) => {
    if (!confirm('让她不提这件事？（等于「已经算了」）')) return;
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'delete', kind: 'promise', at }),
    });
    reload();
  };

  // 事件 —— 你们之间发生过的事（关系史，不是记忆条目）
  const evs = d.events || [];
  sec('你们之间发生过的事', evs.length ? `${evs.length} 件` : '还没攒下什么');
  if (evs.length) {
    const ul = document.createElement('div');
    ul.className = 'mem-ro-list';
    // 「头一回」比「一起做过」重，排前面；同类按日期
    const sorted = evs.slice().sort((a, b) => {
      const k = (x) => (x.kind === 'first' ? 0 : 1);
      return k(a) - k(b) || String(a.day).localeCompare(String(b.day));
    });
    sorted.forEach((e) => {
      const it = document.createElement('div');
      it.className = 'mem-ro-item' + (e.kind === 'first' ? ' first' : '');
      const mark = e.kind === 'first' ? '★ ' : '· ';
      it.textContent = `${mark}（${e.day || ''}）${e.text}`;
      // ⚠️ 删的是**原始数组里的 index**，不是 sorted 之后的位置 ——
      // 排序只是显示用，服务端按原始下标删。
      const raw = evs.indexOf(e);
      const isFirst = e.kind === 'first';
      // ★ 亮着 = 已标「头一回」。点一下切换。
      const st = roBtn(isFirst ? '★' : '☆',
                       isFirst ? '取消「头一回」' : '标成「头一回」（不会被淘汰）',
                       () => starEv(raw, isFirst));
      if (isFirst) st.classList.add('on');
      it.appendChild(st);
      it.appendChild(roBtn('✕', '删掉这件事', () => delEv(raw)));
      ul.appendChild(it);
    });
    box.appendChild(ul);
  }

  // 手动记一件（列表为空时也要有入口）
  const addEvBtn = roBtn('＋ 手写一件', '手写一件事，措辞你自己定', addEventManual);
  addEvBtn.style.position = 'static';
  addEvBtn.style.transform = 'none';
  addEvBtn.style.opacity = '0.75';
  addEvBtn.style.marginTop = '6px';
  addEvBtn.style.padding = '3px 10px';
  addEvBtn.style.width = 'auto';
  addEvBtn.style.height = 'auto';
  addEvBtn.style.border = '1px dashed rgba(201,169,97,.35)';
  addEvBtn.style.borderRadius = '999px';
  addEvBtn.style.fontSize = '11px';
  addEvBtn.style.lineHeight = '18px';
  addEvBtn.style.color = 'var(--gold, #c9a961)';
  box.appendChild(addEvBtn);

  // ── 她自己的这些天 ──────────────────────────────
  //
  // 和上面「你们之间发生过的事」的区别：这些事里**没有你**。
  // 是她自己的日子 —— 挖葱、修窗、去镇上换盐。
  const hers = d.her_events || [];
  sec('她自己的这些天', hers.length ? `${hers.length} 天` : '还没攒下什么');
  if (hers.length) {
    const ul = document.createElement('div');
    ul.className = 'mem-ro-list';
    // 新的在下（服务端就是这么存的），倒过来看着更顺
    hers.slice().reverse().forEach((x) => {
      const it = document.createElement('div');
      it.className = 'mem-ro-item' + (x.kind === 'done' ? ' first' : '');
      const mark = x.kind === 'done' ? '★ ' : '· ';
      it.textContent = `${mark}（${x.day || ''}）${x.text}`;
      const raw = hers.indexOf(x);
      it.appendChild(roBtn('✎', '改这一天的事', () => editHer(raw, x.text)));
      it.appendChild(roBtn('✕', '删掉这一天的事', () => delHer(raw)));
      ul.appendChild(it);
    });
    box.appendChild(ul);
  }

  // ── 她手上没做完的活 ────────────────────────────
  const prj = d.projects || [];
  sec('她手上没做完的', prj.length ? `${prj.length} 件` : '都做完了');
  if (prj.length) {
    const ul = document.createElement('div');
    ul.className = 'mem-ro-list';
    prj.forEach((x, i) => {
      const it = document.createElement('div');
      // act = 这行的按钮常显（项目比观察更需要让人看见能点）
      it.className = 'mem-ro-item act';
      const st = x.stage || 0, tt = x.total || 3;
      const head = x.what ? `${x.text}，${x.what}` : x.text;
      it.textContent = `${head}（${st}/${tt}）`;

      // 进度数字本身可点 —— 点「2/3」直接改成你要的数
      const bar = document.createElement('span');
      bar.className = 'proj-bar';
      bar.textContent = ` ${st}/${tt} `;
      bar.title = '点一下直接改进度';
      bar.addEventListener('click', (ev) => {
        ev.stopPropagation();
        setProjStage(i, st, tt, x.text);
      });
      it.appendChild(bar);

      it.appendChild(roBtn('✎', '改名字', () => editProj(i, x.text)));
      if (st < tt) {
        it.appendChild(roBtn('＋', '进度 +1', () => stepProj(i, st, tt)));
        it.appendChild(roBtn('✓', '标成做完了（会记进她的旧事）',
          () => finishProj(i, tt)));
      }
      it.appendChild(roBtn('✕', '删掉这件活', () => delProj(i)));
      ul.appendChild(it);
    });
    box.appendChild(ul);
  }

  // 观察 —— 她相处下来看出来的你
  const obs = d.observations || [];
  // 每条观察是什么时候看出来的（服务端给 observation_meta）
  const obsMeta = d.observation_meta || {};
  const agoText = (iso) => {
    if (!iso) return '';
    const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
    if (!isFinite(days) || days < 0) return '';
    if (days === 0) return '今天看出来的';
    if (days === 1) return '昨天看出来的';
    return `${days} 天前看出来的`;
  };
  sec('她眼里的你', obs.length ? `${obs.length} 条` : '还没看出来什么');
  if (obs.length) {
    const ul = document.createElement('div');
    ul.className = 'mem-ro-list';
    obs.forEach((t) => {
      const it = document.createElement('div');
      it.className = 'mem-ro-item';
      // 文字后面跟一句「什么时候看出来的」——
      // 用户报过「这个部分好几天没更新了」，而面板上原来完全看不出
      // 哪条是新的、哪条是几个月前的。现在一眼能看见。
      const when = agoText(obsMeta[t]);
      if (when) {
        const w = document.createElement('span');
        w.className = 'mem-ro-when';
        w.textContent = '　' + when;
        it.textContent = t;
        it.appendChild(w);
      } else {
        it.textContent = t;
      }
      // 观察是**她看出来的**，会看错 —— 所以给「改」和「删」两个。
      // 改比删更常用：删掉会少一条认识，改一下能纠正她。
      const oi = obs.indexOf(t);
      it.appendChild(roBtn('✎', '改写这条观察（她看错的地方）', () => editObs(oi, t)));
      it.appendChild(roBtn('✕', '删掉这条观察', () => delObs(oi)));
      ul.appendChild(it);
    });
    box.appendChild(ul);
  }

  // 承诺 —— 他答应过的事
  //
  // ⚠️ 这里的数字要和侧边栏对得上。侧边栏的徽标是**未做到的件数**，
  // 所以这里也必须先说「几件挂着」，再补一句「共几件」——
  // 否则同一个标题下两个地方显示不同的数，会让人以为坏了。
  // （2026-10-18 用户报：「这里和『她的记忆』里的不联动」。）
  const prom = d.promises || [];
  const live = prom.filter((p) => !p.kept).length;
  sec('你答应过的事',
      live ? `${live} 件挂着${live !== prom.length ? `　共 ${prom.length} 件` : ''}`
           : (prom.length ? `都做到了　共 ${prom.length} 件` : '没有挂着的事'));
  if (prom.length) {
    const ul = document.createElement('div');
    ul.className = 'mem-ro-list';
    prom.forEach((p) => {
      const it = document.createElement('div');
      it.className = 'mem-ro-item' + (p.kept ? ' done' : '');
      const who = p.said_by === 'her' ? '她说：' : '';
      it.textContent = (p.kept ? '✓ ' : '· ') + who + p.text +
        (p.due ? `（${p.due} 前）` : '');
      // 承诺只给 ✕：删掉等于「不提了」。
      // 改措辞等于改口供 —— 不给。
      it.appendChild(roBtn('✕', '让她不提这件事', () => delPr(p.at || '')));
      ul.appendChild(it);
    });
    box.appendChild(ul);
  }

  // 统计 —— 一眼看出口径
  const s = d.stats || {};
  if (s.turns !== undefined) {
    const it = document.createElement('div');
    it.className = 'mem-stats';
    const days = s.first_met ? Math.max(1, Math.round(
      (Date.now() - new Date(s.first_met).getTime()) / 86400000)) : 0;
    it.textContent =
      `一起说过 ${s.total_turns || s.turns} 轮话（提示词里保留最近 ${s.turns} 轮）` +
      (days ? `　·　认识 ${days} 天` : '') +
      (s.last_talk_at ? `　·　上次说话 ${s.last_talk_at.slice(5, 16).replace('T', ' ')}` : '');
    box.appendChild(it);
  }
}

function renderMemoryList(items) {
  const box = $('memList');
  box.innerHTML = '';
  if (!items.length) {
    box.innerHTML = '<div class="mem-empty">她还没记住什么。<br>聊几句，或者在上面手动加一条。</div>';
    return;
  }

  for (const m of items) {
    const row = document.createElement('div');
    row.className = 'mem-item' + (m.kind === 'guidance' ? ' guidance' : '');

    const tag = document.createElement('span');
    tag.className = 'mem-tag';
    tag.textContent = m.kind === 'guidance' ? '嘱咐' : '事实';
    row.appendChild(tag);

    const text = document.createElement('span');
    text.className = 'mem-text';
    text.textContent = m.text;
    row.appendChild(text);

    const when = document.createElement('span');
    when.className = 'mem-when';
    when.textContent = (m.created || '').slice(0, 10);
    row.appendChild(when);

    const edit = document.createElement('button');
    edit.className = 'mem-btn';
    edit.textContent = '改';
    edit.onclick = async () => {
      const v = prompt('改成：', m.text);
      if (v === null || !v.trim()) return;
      await memOp({ op: 'edit', index: m.index, text: v.trim() });
    };
    row.appendChild(edit);

    const del = document.createElement('button');
    del.className = 'mem-btn danger';
    del.textContent = '删';
    del.onclick = async () => {
      if (!confirm(`让她忘掉这条？\n\n${m.text}`)) return;
      await memOp({ op: 'delete', index: m.index });
    };
    row.appendChild(del);

    box.appendChild(row);
  }
}

async function memOp(payload) {
  try {
    const r = await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    renderMemoryList(d.memories || []);
    const st = await (await fetch('/api/status')).json();
    renderStatus(st);
  } catch (e) {
    addSys('记忆操作失败：' + e.message, 'err');
  }
}

/* ── 存档与设置 ─────────────────────────────────────── */
async function loadSaves() {
  try {
    const d = await (await fetch('/api/saves')).json();
    // 「读哪个存档」「关系取自游戏」两个控件已删（2026-10-27 与游戏脱钩）。
    // 填表逻辑保留，但有控件才执行 —— 将来要接回来，改一行就行。
    const sel = $('saveSelect');
    if (sel) {
      sel.innerHTML = '';
      const optLatest = document.createElement('option');
      optLatest.value = 'latest';
      optLatest.textContent = '最近玩过的存档';
      sel.appendChild(optLatest);
      for (const s of d.saves || []) {
        const o = document.createElement('option');
        o.value = s.name;
        o.textContent = `${s.label}　${s.played}`;
        sel.appendChild(o);
      }
      sel.value = d.selected || 'latest';
    }

    // 背景音乐
    const tsel = $('bgmTrack');
    tsel.innerHTML = '';
    for (const t of d.bgm_tracks || []) {
      const o = document.createElement('option');
      o.value = t.id;
      o.textContent = t.name;
      tsel.appendChild(o);
    }
    if (d.bgm_track) tsel.value = d.bgm_track;
    if (d.portrait_source) $('portraitSource').value = d.portrait_source;
    applyTheme(d.theme || 'light');
    const vol = Math.round((d.bgm_volume ?? 0.35) * 100);
    $('bgmVol').value = String(vol);
    $('bgmOn').checked = !!d.bgm_enabled;
    applyBgm(d.bgm_enabled, d.bgm_track, d.bgm_volume ?? 0.35);
  } catch (e) { /* 保持默认 */ }
}

async function pushSettings() {
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
                use_game_relation: false,   // 与游戏脱钩（2026-10-27）
        theme: document.documentElement.getAttribute('data-theme') || 'light',
        portrait_source: $('portraitSource').value,
        bgm_enabled: $('bgmOn').checked,
        bgm_track: $('bgmTrack').value,
        bgm_volume: Number($('bgmVol').value) / 100,
      }),
    });
    const d = await r.json();
    if (d.ok && d.status) {
      renderStatus(d.status);
      addSys('设置已更新');
    }
  } catch (e) {
    addSys('设置保存失败：' + e.message, 'err');
  }
}

async function saveKey() {
  const key = $('keyInput').value.trim();
  if (!key) { $('keyError').textContent = '请填入密钥'; return; }
  $('keySave').disabled = true;
  $('keyError').textContent = '正在保存…';
  try {
    const r = await fetch('/api/key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    const d = await r.json();
    if (d.ok) {
      $('keyGate').classList.add('hidden');
      $('keyError').textContent = '';
      addSys('密钥已保存，可以开始说话了');
      $('input').focus();
    } else {
      $('keyError').textContent = d.error || '保存失败';
    }
  } catch (e) {
    $('keyError').textContent = '保存失败：' + e.message;
  }
  $('keySave').disabled = false;
}

async function resetAll() {
  if (!confirm('要让她忘掉这一切，重新开始吗？\n（旧存档会备份为 .json.bak）')) return;
  await fetch('/api/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: true }),
  });
  $('log').innerHTML = '';
  lastPortraitUrl = '';
  setPortrait('');
  await boot();
  addSys('你们重新认识了。');
}

/* ── 启动 ───────────────────────────────────────────── */
/* 挂到 window —— rewind.js 重做一轮之后要重铺整个聊天区，
   而它在本文件之前加载，拿不到这里的函数。 */
window.boot = boot;
async function boot() {
  await loadSaves();
  // 收藏要在渲染历史之前读好 —— 否则历史里的星号不会亮
  await loadFavorites();
  // 「他答应过的事」——**启动时必须拉一次**。
  //
  // 踩过的坑（2026-10-18）：原来这里**没有这一行**，而 `loadPromises()`
  // 只在 `promiseOp()`（点「做到了」之后）和收藏时被调 —— 于是启动后
  // 侧边栏永远是「0」和一个空框，怎么看都像她没记住任何事。
  // 而「她的记忆」面板走的是另一个接口（/api/memory op=list），那个有数据，
  // 所以两处对不上。用户的原话：「这里和『她的记忆』里的不联动」。
  await loadPromises();
  const st = await (await fetch('/api/status')).json();
  renderStatus(st);
  // 留话：先渲染现有的。新的是后台生成的，靠轮询补上。
  renderNotes(st.notes);
  pollNotes();

  const hist = await (await fetch('/api/history')).json();
  $('log').innerHTML = '';
  const turns = hist.turns || [];
  // 分隔条右侧原来是「第 1 年秋季第 7 天」（游戏进度）。脱钩后改成现实时令。
  const progress = st.days?.season || '';

  if (turns.length === 0) {
    addDayDivider('第一次', '');
    const opening = st.greeting || '来了。';
    const { body } = addBubble('her', '', null);
    typewrite(body, opening);
    setPortrait('');
  } else {
    let lastDay = '';
    for (const t of turns) {
      const day = t.at.slice(0, 10);
      if (day !== lastDay) {
        addDayDivider(day, day === turns[turns.length - 1].at.slice(0, 10) ? progress : '');
        lastDay = day;
      }
      addBubble(t.role, t.text, t.at);
    }
    const lastTurn = turns[turns.length - 1];
    // 恢复到她最后一句时的神情；若最后一句是玩家说的，则回到默认脸
    setPortrait(lastTurn.role === 'her' ? (lastTurn.portrait || lastTurn.emotion || '') : '');

    // 历史铺完了才挂「重做」按钮 —— 它只该出现在**最后那一轮**上，
    // 所以要等整个列表都在 DOM 里了再找。
    //
    // ⚠️ 踩过的坑：原来只在 typewrite 的完成回调里调了它（发消息之后），
    // **boot 渲染历史时从来没调** —— 于是打开界面看不到按钮，
    // 只有你刚说完一句才会出现。
    //
    // 守卫检查**方法**不是对象：`window.X && window.X.fn`。
    // 今天因为只检查了对象，栽过一次（见 addBubble 里的注释）。
    if (window.Rewind && window.Rewind.markRewindable) {
      window.Rewind.markRewindable();
    }
  }

  if (!st.has_key) $('keyGate').classList.remove('hidden');
  $('input').focus();
}

/* ── 事件绑定 ───────────────────────────────────────── */
$('send').addEventListener('click', send);
$('btnSpeak').addEventListener('click', askHerToSpeak);

/* 图片的三个入口。都汇到 addImage()。 */
$('btnImage').addEventListener('click', () => $('imgFile').click());
$('imgFile').addEventListener('change', (e) => {
  for (const f of e.target.files) addImage(f);
  e.target.value = '';        // 允许连续选同一张
});
// 粘贴：截图之后 Ctrl+V 直接进来，最常用的一条路
$('input').addEventListener('paste', (e) => {
  const items = (e.clipboardData || {}).items || [];
  let got = false;
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      addImage(it.getAsFile());
      got = true;
    }
  }
  if (got) e.preventDefault();   // 别把图的路径/乱码也粘进输入框
});
// 拖放：拖到整个窗口任意位置都收。
// dragleave 在子元素之间也会触发，所以用计数器判断真的离开了窗口。
let dragDepth = 0;
document.addEventListener('dragenter', (e) => {
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
  dragDepth += 1;
  document.body.classList.add('drop-active');
});
document.addEventListener('dragover', (e) => {
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
  e.preventDefault();          // 不 preventDefault 的话 drop 不会触发
});
document.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) document.body.classList.remove('drop-active');
});
document.addEventListener('drop', (e) => {
  if (!e.dataTransfer) return;
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('drop-active');
  for (const f of e.dataTransfer.files) addImage(f);
});

$('keySave').addEventListener('click', saveKey);
  // 「重新开始」按钮与监听都已删除（2026-10-27）：
  // 误触一次就清空全部对话，风险大于收益。resetAll 函数保留着，
  // 将来真要用可以从控制台调。
const _selEl = $('saveSelect');
if (_selEl) _selEl.addEventListener('change', pushSettings);
// 换立绘来源后刷新：所有立绘（大图与消息头像）都要重新取
$('portraitSource').addEventListener('change', async () => {
  await pushSettings();
  lastPortraitUrl = '';
  setPortrait('');
  document.querySelectorAll('#log .msg .avatar img').forEach(a => {
    const isShu = a.closest('.msg').classList.contains('her');
    a.src = isShu ? `/portrait/${currentFace}` : '/avatar/farmer';
  });
});
const _relEl = $('useRelation');
if (_relEl) _relEl.addEventListener('change', pushSettings);

/* 测试档开关。切换时**要重新拉一遍全部状态** ——
   因为换的是另一份存档，对话、心情、手边的活、观察全都不一样了。
   不重拉的话界面上会混着两份档的内容，看着像串了。 */
$('useTestSession').addEventListener('change', async () => {
  const on = $('useTestSession').checked;
  if (on && !confirm(
      '切到测试档？\n\n' +
      '这之后说的话只记在测试档里，真实记录不受影响。\n' +
      '（可以随时切回来。）')) {
    $('useTestSession').checked = false;
    return;
  }
  lastPortraitUrl = '';
  await boot();
});
$('useTestSession').addEventListener('change', () => {
  if ($('useTestSession').checked) document.body.classList.add('test-mode');
  else document.body.classList.remove('test-mode');
});

// BGM：勾选/换曲立即生效，音量拖动即时反馈（不每次都写配置）
$('bgmOn').addEventListener('change', () => {
  applyBgm($('bgmOn').checked, $('bgmTrack').value, Number($('bgmVol').value) / 100);
  pushSettings();
});
$('bgmTrack').addEventListener('change', () => {
  applyBgm($('bgmOn').checked, $('bgmTrack').value, Number($('bgmVol').value) / 100);
  pushSettings();
});
$('bgmVol').addEventListener('input', () => {
  bgm.volume = Number($('bgmVol').value) / 100;
});
$('bgmVol').addEventListener('change', pushSettings);

// 配色切换：即时生效，并写进配置
$('themeRow').addEventListener('click', (e) => {
  const b = e.target.closest('.theme-btn');
  if (!b) return;
  applyTheme(b.dataset.theme);
  pushSettings();
});

$('btnMemory').addEventListener('click', openMemory);

/* 试召回。
 *
 * 存在的理由：**「她忘了」和「召回的判据漏了」是两回事**，
 * 而从对话里分不出来 —— 你问一句她接不上，可能是真没听过这个词，
 * 也可能是听过但二字重合没匹配上。前者没办法，后者该调判据。
 *
 * 所以给她一个直接试的入口。 */
async function tryRecall() {
  const box = $('recallInput');
  const out = $('recallOut');
  const q = box.value.trim();
  if (!q) return;
  out.innerHTML = '<div class="mem-empty">翻着呢…</div>';
  try {
    const r = await fetch('/api/recall', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q, limit: 8 }),
    });
    const d = await r.json();
    if (!d.ok) {
      out.innerHTML = `<div class="mem-empty">${d.error || '出错了'}</div>`;
      return;
    }
    if (!d.count) {
      out.innerHTML = `<div class="mem-empty">「${q}」一条都没翻到。<br>${d.note || ''}</div>`;
      return;
    }
    out.innerHTML = '';
    const head = document.createElement('div');
    head.className = 'recall-head';
    head.textContent = `翻到 ${d.count} 条 —— 所以她是记得的`;
    out.appendChild(head);
    for (const h of d.hits) {
      const it = document.createElement('div');
      it.className = 'recall-item ' + (h.role === 'player' ? 'me' : 'her');
      const when = document.createElement('span');
      when.className = 'recall-when';
      when.textContent = (h.at || '').slice(5, 16).replace('T', ' ');
      it.appendChild(when);
      const tx = document.createElement('span');
      tx.textContent = h.text;
      it.appendChild(tx);
      out.appendChild(it);
    }
  } catch (e) {
    out.innerHTML = `<div class="mem-empty">读取失败：${e.message}</div>`;
  }
}

$('recallGo').addEventListener('click', tryRecall);
$('recallInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); tryRecall(); }
});

/* ── 记忆快照 ────────────────────────────────────────────
   起因：2026-10-16 误触「重新开始」丢了 134 轮对话。
   那个按钮现在会先自动打快照了，但这个列表让你能看见、能主动回滚。 */
async function snapOp(body) {
  const r = await fetch('/api/snapshot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

async function renderSnapshots() {
  const out = $('snapOut');
  if (!out) return;
  out.innerHTML = '<div class="mem-empty">读着呢…</div>';
  try {
    const d = await snapOp({ op: 'list' });
    const items = d.snapshots || [];
    if (!items.length) {
      out.innerHTML = '<div class="mem-empty">还没有快照。<br>每天启动时会自动打一份。</div>';
      return;
    }
    out.innerHTML = '';
    for (const s of items) {
      const st = s.stats || {};
      const row = document.createElement('div');
      row.className = 'snap-item ' + (s.kind === 'manual' ? 'manual' : 'auto');

      const info = document.createElement('div');
      info.className = 'snap-info';
      const when = (s.at || '').replace('T', ' ').slice(5, 16);
      info.innerHTML =
        `<b>${when}</b>　${st.turns || 0} 轮 / ${st.events || 0} 事件 / ` +
        `${st.observations || 0} 观察` +
        (st.other_turns ? `<span class="snap-other"> ＋其它 ${st.other_turns}</span>` : '') +
        (s.note ? `<br><span class="snap-note">${s.note}</span>` : '');
      row.appendChild(info);

      const back = document.createElement('button');
      back.className = 'mem-btn';
      back.textContent = '回滚';
      back.onclick = async () => {
        if (!confirm(
          `回滚到 ${when} 的状态？\n\n` +
          `会覆盖当前的对话与记忆（回滚前会自动先打一份快照，` +
          `所以回滚错了还能再回来）。`)) return;
        const r = await snapOp({ op: 'restore', name: s.name });
        if (!r.ok) { alert('回滚失败：' + (r.error || '')); return; }
        alert('已回滚。窗口会重新加载记忆。');
        await boot();
        await renderSnapshots();
      };
      row.appendChild(back);

      const del = document.createElement('button');
      del.className = 'mem-btn danger';
      del.textContent = '删';
      del.onclick = async () => {
        if (!confirm(`删掉这份快照？\n\n${when}`)) return;
        await snapOp({ op: 'delete', name: s.name });
        await renderSnapshots();
      };
      row.appendChild(del);

      out.appendChild(row);
    }
  } catch (e) {
    out.innerHTML = `<div class="mem-empty">读取失败：${e.message}</div>`;
  }
}

if ($('snapTake')) {
  $('snapTake').addEventListener('click', async () => {
    const note = prompt('给这份快照起个名（可以不填）：', '') ?? null;
    if (note === null) return;
    const r = await snapOp({ op: 'take', note: note.trim() });
    if (!r.ok) { alert('打快照失败：' + (r.error || '')); return; }
    await renderSnapshots();
  });
}
if ($('btnBaseline')) {
  $('btnBaseline').addEventListener('click', async () => {
    if (!confirm(
      '把测试档恢复成基线？\n\n' +
      '会清掉测试档里的对话轮次（真实档不受影响）。\n' +
      '她的阅历——观察、事件、承诺——会保留。\n\n' +
      '用途：让每次测试从同一个起点开始，结论才可复现。')) return;
    try {
      const r = await fetch('/api/test_baseline', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op: 'reset' }),
      });
      const d = await r.json();
      if (!d.ok) { alert('重置失败：' + (d.error || '')); return; }
      alert(`测试档已回到基线（${d.turns} 轮，保留 ${d.observations} 条观察）。`);
      await boot();
    } catch (e) {
      alert('连不上服务：' + e.message);
    }
  });
}

/* Esc 关面板 —— 键盘党的退路，也省得去找那个按钮。
   只关「记忆面板」，别的 gate（密钥输入那种）保持原样。 */
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const mp = $('memPanel');
  if (mp && !mp.classList.contains('hidden')) {
    e.preventDefault();
    mp.classList.add('hidden');
  }
});

if ($('snapRefresh')) {
  $('snapRefresh').addEventListener('click', renderSnapshots);
}
$('btnFavorites').addEventListener('click', openFavorites);
$('favClose').addEventListener('click', () => $('favBox').classList.add('hidden'));
// 点空白处也关掉
$('favBox').addEventListener('click', (e) => {
  if (e.target === $('favBox')) $('favBox').classList.add('hidden');
});
/* 「嘱咐她」那个按钮删了（2026-11-10）。
   它是个 prompt() 弹窗，写的是长期规矩，误点一次就留一条；
   要她改，直接在对话里说更自然。
   correct() 和 /api/correct 先留着 —— 以后想做"她记错了"的小入口还能用。 */
$('memClose').addEventListener('click', () => $('memPanel').classList.add('hidden'));
$('memAdd').addEventListener('click', async () => {
  const v = $('memInput').value.trim();
  if (!v) return;
  await memOp({ op: 'add', text: v, kind: $('memKind').value });
  $('memInput').value = '';
});
$('memInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('memAdd').click();
});

$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
$('input').addEventListener('input', (e) => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(140, e.target.scrollHeight) + 'px';
});
$('keyInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') saveKey();
});

boot();


/* ── 公众模式（2026-10-27 重写）────────────────────────────────────
   ## 上一版为什么关不掉

   把 `.panel` 整个 `filter: blur()` —— **父元素糊了，子元素跟着糊**，
   连「公众模式」那个勾选框自己也糊了，于是没法点、也看不清状态。

   ## 现在的做法

   只糊**内容块**，永远不糊容器：
       .panel 里的 立绘 / 名字 / 好感条 / 各张 card
       对话区的每条消息
       记忆浮层
   设定区（.panel 里那个 fold）**不糊** —— 开关必须一直能用。

   另外：开启时用短定时器补糊新弹出的浮层，关机时**必须清掉它**，
   否则关掉之后它还在偷偷往回糊。
*/
(function () {
  var TARGETS = [
    '.panel .portrait-frame',
    '.panel .name-row',
    '.panel .affection',
    '.panel .card',
    '#log .msg',
    '#memPanel',
    '.sheet',
  ].join(', ');

  var timer = null;

  // 公众模式（2026-11-06 第五版）
  //
  // 遮挡放在 CSS 里（body.privacy + 选择器），JS 只负责开关。
  // 这样不用改文字内容 —— 值被一块色块盖住，布局不变、开关回来还是原文。
  //
  // 糊的只有对话：她的话轻糊，你说的话重糊，右栏跟着收。
  var BLUR_SOFT = 'blur(5px)';
  var BLUR_HARD = 'blur(9px)';

  function paint(on) {
    document.querySelectorAll('.panel, .panel *, .qq-rail').forEach(function (el) {
      el.style.filter = '';
    });
    document.querySelectorAll('.chat').forEach(function (el) {
      el.style.filter = on ? BLUR_SOFT : '';
      el.style.userSelect = on ? 'none' : '';
    });
    document.querySelectorAll('.msg.me, .msg.me *').forEach(function (el) {
      el.style.filter = on ? BLUR_HARD : '';
    });
    document.querySelectorAll('.gate, .sheet, #memPanel, .fav-panel, .note-box')
      .forEach(function (el) {
        el.style.filter = on ? BLUR_HARD : '';
      });
    document.body.classList.toggle('privacy', !!on);
  }

  window.mindApplyPrivacy = function (on) {
    if (timer) { clearInterval(timer); timer = null; }
    // 勾选框必须和实际状态一致 —— 否则会出现「没打勾却糊着」，
    // 点一下反而变成又开一次，看起来就是关不掉。
    var box = document.getElementById('privacyMode');
    if (box) {
      box.checked = !!on;
      // data-on 只是给自助检查用的（.checked 是 property，
      // dump-dom 那种看 attribute 的方式看不到它）
      box.setAttribute('data-on', on ? '1' : '0');
    }
    paint(on);
    if (!on) return;
    // 浮层是后弹的：短时间补几遍，但只在「开」的时候跑
    var n = 0;
    timer = setInterval(function () {
      paint(true);
      if (++n > 20) { clearInterval(timer); timer = null; }
    }, 250);
  };

  function wire() {
    var box = document.getElementById('privacyMode');
    if (!box) return;
    var saved = '0';
    try { saved = localStorage.getItem('mind_privacy') || '0'; } catch (e) {}
    box.checked = saved === '1';
    if (box.checked) window.mindApplyPrivacy(true);
    box.addEventListener('change', function () {
      window.mindApplyPrivacy(box.checked);
      try {
        localStorage.setItem('mind_privacy', box.checked ? '1' : '0');
      } catch (e) {}
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();


/* 公众模式的常驻出口（2026-10-27）
   整页糊掉之后，那个勾选框可能在侧栏很下面 —— 于是「关不掉」。
   这里在右上角放一个始终可见的按钮，任何时候都能退出。 */
(function () {
  var btn = null;

  function ensure() {
    if (btn) return btn;
    btn = document.createElement('button');
    btn.id = 'shu-privacy-exit';
    btn.type = 'button';
    btn.textContent = '退出公众模式';
    btn.style.cssText = [
      'position:fixed', 'top:14px', 'right:16px', 'z-index:99999',
      'padding:7px 14px', 'border-radius:999px',
      'border:1px solid rgba(201,169,97,.55)',
      'background:rgba(23,26,31,.92)', 'color:#c9a961',
      'font-size:12px', 'letter-spacing:.08em', 'cursor:pointer',
      'box-shadow:0 6px 18px rgba(0,0,0,.45)',
      'display:none',
    ].join(';');
    btn.addEventListener('click', function () {
      if (window.mindApplyPrivacy) window.mindApplyPrivacy(false);
      var box = document.getElementById('privacyMode');
      if (box) box.checked = false;
      try { localStorage.setItem('mind_privacy', '0'); } catch (e) {}
      sync();
    });
    document.body.appendChild(btn);
    return btn;
  }

  function sync() {
    var on = document.body.classList.contains('privacy');
    ensure().style.display = on ? 'block' : 'none';
  }

  // body 的 class 由 mindApplyPrivacy 切换，这里只跟着它显示/隐藏
  var mo = new MutationObserver(sync);
  window.addEventListener('DOMContentLoaded', function () {
    ensure();
    mo.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    sync();
  });
  if (document.readyState !== 'loading') {
    ensure();
    mo.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    sync();
  }
})();




/* 大时针盘（2026-11-02）
   一天转一圈：0 点正下、6 点左、12 点上、18 点右。
   白天的弧从 6 点画到 18 点，JS 只改"画到哪"。 */
(function () {
  let started = false;
  function boot() {
  if (started) return;
  const box = document.getElementById('bigClock');
  if (!box) return;               // 元素还没出现 — 等 DOMContentLoaded 再来一次
  started = true;

  // 24 根刻度：每根 15°，长的那几根是 0/6/12/18
  const ticks = box.querySelector('.bc-ticks');
  if (ticks && !ticks.childNodes.length) {
    const NS = 'http://www.w3.org/2000/svg';
    for (let i = 0; i < 24; i++) {
      const long = i % 6 === 0;
      const el = document.createElementNS(NS, 'line');
      const a = (i / 24) * Math.PI * 2;
      const r1 = long ? 66 : 68;
      const r2 = 70;
      el.setAttribute('x1', 80 + Math.sin(a) * r1);
      el.setAttribute('y1', 80 - Math.cos(a) * r1);
      el.setAttribute('x2', 80 + Math.sin(a) * r2);
      el.setAttribute('y2', 80 - Math.cos(a) * r2);
      ticks.appendChild(el);
    }
  }

  // 白天弧长：6 点到 18 点 = 半圈 = 61 * π ≈ 192
  const arc = box.querySelector('.bc-dayarc');
  const DAY_LEN = 192;

  function tick() {
    const now = new Date();
    const h = now.getHours() + now.getMinutes() / 60;

    const g = box.querySelector('.bc-celestial');
    if (g) g.style.transform = 'rotate(' + ((h / 24) * 360 + 180) + 'deg)';

    const hm = document.getElementById('bcHM');
    if (hm) {
      hm.textContent = String(now.getHours()).padStart(2, '0') + ':' +
                       String(now.getMinutes()).padStart(2, '0');
    }

    const day = h >= 6 && h < 18.5;
    box.classList.toggle('night', !day);

    // 白天走完多少：6→18 映射到 0→192
    if (arc) {
      const p = Math.max(0, Math.min(1, (h - 6) / 12));
      arc.style.strokeDasharray = (DAY_LEN * p) + ' 999';
    }

    const seg = h < 5 ? '深夜' : h < 8 ? '清早' : h < 11 ? '上午'
              : h < 13 ? '晌午' : h < 17 ? '午后' : h < 19 ? '黄昏'
              : h < 22 ? '入夜' : '夜深';
    const s1 = document.getElementById('bcSeg');
    if (s1) s1.textContent = seg;

    const s2 = document.getElementById('bcMore');
    if (s2) s2.textContent = document.getElementById('seasonText')?.textContent || '';
  }
  tick();
  setInterval(tick, 20000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* 右侧栏的下半部分（2026-11-04）
   数据全部来自已有的接口：/api/status 的 days + mood，/api/diary 的最新一篇。 */
(function () {
  function boot() {
    // 日记只取一次（它一天才变一篇）
    fetch('/api/diary').then(function (r) { return r.json(); }).then(function (d) {
      const list = (d && d.entries) || [];
      const el = document.getElementById('railDiary');
      if (!el) return;
      if (!list.length) { el.textContent = '（还没有日记）'; return; }
      const txt = String(list[list.length - 1].text || '').replace(/\s+/g, ' ').trim();
      el.textContent = txt.slice(0, 90) + (txt.length > 90 ? '…' : '');
    }).catch(function () {});

    // 状态每次都跟着主面板刷新
    const paint = function (st) {
      if (!st) return;
      const d = st.days || {};
      const t = document.getElementById('railTogether');
      if (t && d.together) t.textContent = d.together;

      const m = st.mood || {};
      const v = document.getElementById('railValence');
      const e = document.getElementById('railEnergy');
      if (v) v.style.width = Math.max(0, Math.min(100, m.valence || 0)) + '%';
      if (e) e.style.width = Math.max(0, Math.min(100, m.energy || 0)) + '%';
    };
    window.mindPaintRail = paint;

    // 首屏取一次（之后主面板轮询时会再喂）
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(paint).catch(function () {});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* 她的家（2026-11-04）
   days.place 是模型判的七选一；没给就按"堂屋"算，不猜更细。
   位置 -> 房间 id + 光点坐标（对着 SVG 里的格子量的）。 */
(function () {
  const POS = {
    '储物角': { room: 'hm-store',     x: 196, y: 45 },
    '卧房':   { room: 'hm-bed',       x: 60,  y: 45 },
    '厨房':   { room: 'hm-1f',        x: 40,  y: 92 },
    '堂屋':   { room: 'hm-1f',        x: 108, y: 92 },
    '卫生间': { room: 'hm-1f',        x: 194, y: 92 },
    '后廊':   { room: 'hm-corridor',  x: 120, y: 126 },
    '院子':   { room: 'hm-yard',      x: 120, y: 155 },
    '外面':   { room: 'hm-outside',   x: 120, y: 187 }
  };

  function paint(st) {
    const box = document.getElementById('houseMap');
    if (!box || !st) return;
    const place = (st.days && st.days.place) || '';
    const p = POS[place] || POS['堂屋'];

    box.querySelectorAll('.hm-room.on').forEach(function (el) {
      el.classList.remove('on');
    });
    const room = document.getElementById(p.room);
    if (room) room.classList.add('on');

    const dot = document.getElementById('hmDot');
    const halo = document.getElementById('hmHalo');
    if (dot) { dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y); }
    if (halo) { halo.setAttribute('cx', p.x); halo.setAttribute('cy', p.y); }

    const label = document.getElementById('hmPlace');
    if (label) label.textContent = place || '堂屋';
  }

  window.mindPaintHouse = paint;

  // 首屏兜底（2026-11-04）：renderStatus 在脚本后半段的 IIFE 之前就跑过一次，
  // 那时 window.mindPaintRail 还不存在 -> 地图一次都不会画。
  // 这里自己取一次状态，保证打开页面就能看见她在哪。
  window.mindPaintHouseBoot = function () {
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(function (j) { paint(j.status || j); })
      .catch(function () {});
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', window.mindPaintHouseBoot);
  } else {
    window.mindPaintHouseBoot();
  }

  const oldPaint = window.mindPaintRail;
  window.mindPaintRail = function (st) {
    if (oldPaint) oldPaint(st);
    paint(st);
  };
})();





/* 设置齿轮（2026-11-04）
   点一下展开设置块，再点收起。状态记在 body.settings-open，
   不写 localStorage —— 它是"临时看一眼"，下次打开不该停在那儿。 */
(function () {
  function boot() {
    const btn = document.getElementById('settingsBtn');
    const fold = document.getElementById('settingsFold');
    if (!btn || !fold) return;

    function sync(on) {
      document.body.classList.toggle('settings-open', on);
      btn.setAttribute('aria-expanded', on ? 'true' : 'false');
      fold.open = on;
    }
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      sync(!document.body.classList.contains('settings-open'));
    });
    sync(false);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* 入场错峰（2026-11-04）
   给每个折叠区编号（--i），播完就摘掉 body.intro。
   不摘的话，之后每次展开都会重播一次入场动画，看起来像卡了。 */
(function () {
  function boot() {
    const folds = document.querySelectorAll('.fold');
    if (!folds.length) return;
    folds.forEach(function (f, i) { f.style.setProperty('--i', i); });
    document.body.classList.add('intro');
    setTimeout(function () { document.body.classList.remove('intro'); }, 1400);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* 重新生成「她此刻」+ 定时刷新（2026-11-07）
   ① 按钮：调 /api/regenerate 的 life 分支，只重写展示字段
   ② 定时：每 60 秒拉一次 /api/status 并重绘 ——
      之前没有轮询，所以右下角的「她的家」不刷新，要手动 F5。 */
(function () {
  async function refreshStatus() {
    try {
      const j = await (await fetch('/api/status')).json();
      const st = j.status || j;
      // app.js 是 module，renderStatus 不在 window 上 ——
      // 这里和它同一个作用域，直接调就行（原来那行永远不执行）
      renderStatus(st);
    } catch (e) { /* 静默 */ }
  }

  function boot() {
    const btn = document.getElementById('btnRegenLife');
    if (btn) {
      btn.addEventListener('click', async function () {
        if (btn.disabled) return;
        btn.disabled = true;
        btn.classList.add('spin');
        try {
          const r = await fetch('/api/regenerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ what: 'life' })
          });
          const j = await r.json();
          if (j.ok) await refreshStatus();
        } catch (e) { /* 静默 */ }
        btn.classList.remove('spin');
        btn.disabled = false;
      });
    }
    // 每 60 秒刷一次（含地图、右栏、左栏数字）
    setInterval(refreshStatus, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* 左栏「接上 QQ」（2026-11-09）
   GET  /api/qq_link  → 状态
   POST /api/qq_link  → {"action":"inject"} 注入
   失败时说清楚卡在哪一步，别让按钮看着像没反应。 */
(function () {
  async function paint() {
    const b = document.getElementById('qqLinkBtn');
    const n = document.getElementById('qqLinkNote');
    if (!b) return;
    try {
      const st = await (await fetch('/api/qq_link')).json();
      if (st.online && st.injected) {
        b.textContent = '已接上';
        b.classList.add('on');
        b.disabled = true;
        if (n) n.textContent = '她在手机上，可以说话了';
      } else if (st.injected) {
        b.textContent = '等待登录';
        b.classList.add('on');
        if (n) n.textContent = '注进去了，还没登 QQ';
      } else if (st.ok) {
        b.textContent = '接上 QQ';
        b.classList.remove('on');
        if (n) n.textContent = '点一下，把她接到手机上';
      } else {
        b.textContent = '接不上';
        if (n) n.textContent = (st.error || 'SnowLuma 没开').slice(0, 40);
      }
    } catch (e) { /* 静默 */ }
  }

  function boot() {
    paint();
    setInterval(paint, 30000);
    const b = document.getElementById('qqLinkBtn');
    if (!b) return;
    b.addEventListener('click', async function () {
      const n = document.getElementById('qqLinkNote');
      if (b.disabled) return;
      b.disabled = true;
      const old = b.textContent;
      b.textContent = '接上中…';
      try {
        const r = await fetch('/api/qq_link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'inject' })
        });
        const st = await r.json();
        if (n) n.textContent = st.error ? ('失败：' + st.error).slice(0, 46)
                                        : (st.note || '好了');
      } catch (e) {
        if (n) n.textContent = '请求失败';
      }
      b.disabled = false;
      if (b.textContent === '接上中…') b.textContent = old;
      await paint();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* 左栏：窗外的天 + 记忆星图（2026-11-10 v2）

   窗：天气 → data-w；时辰 → data-t + 四套配色（CSS 变量）
   星：/api/star_map → 星带；悬停浮字、点击选中、图例筛选
*/
(function () {
  const PALETTE = {
    dawn:  { s1: '#7c7f9a', s2: '#c9a58a', hill: '#2c3040', tree: '#1b1f2a',
             orb: '#ffe6c4', glow: 'rgba(255,214,170,.6)', warm: 'rgba(255,200,150,.22)' },
    day:   { s1: '#7d8ea3', s2: '#53617a', hill: '#2f3947', tree: '#1e2530',
             orb: '#f6f1e2', glow: 'rgba(255,246,210,.45)', warm: 'rgba(255,214,150,0)' },
    dusk:  { s1: '#6d5f79', s2: '#b07a63', hill: '#2a2733', tree: '#191b24',
             orb: '#ffd7a1', glow: 'rgba(255,170,110,.65)', warm: 'rgba(255,190,130,.28)' },
    night: { s1: '#141b2c', s2: '#0a0e18', hill: '#10151f', tree: '#0a0d14',
             orb: '#e7ecff', glow: 'rgba(200,215,255,.45)', warm: 'rgba(255,204,140,.30)' }
  };

  function bucket(h) {
    if (h >= 5 && h < 7) return 'dawn';
    if (h >= 7 && h < 17) return 'day';
    if (h >= 17 && h < 19) return 'dusk';
    return 'night';
  }

  function weatherKind(text) {
    const t = text || '';
    if (t.indexOf('雪') >= 0) return 'snow';
    if (t.indexOf('雨') >= 0) return 'rain';
    if (t.indexOf('雾') >= 0 || t.indexOf('霾') >= 0) return 'fog';
    if (t.indexOf('晴') >= 0) return 'clear';
    return 'cloudy';
  }

  function paintWindow(st) {
    const box = document.getElementById('wrWindow');
    const note = document.getElementById('wrNote');
    if (!box) return;
    const days = st.days || window.__mindDays || {};
    box.setAttribute('data-w', weatherKind(days.weather || ''));
    const b = bucket(new Date().getHours());
    box.setAttribute('data-t', b);
    const p = PALETTE[b];
    box.style.setProperty('--sky-1', p.s1);
    box.style.setProperty('--sky-2', p.s2);
    box.style.setProperty('--hill', p.hill);
    box.style.setProperty('--tree', p.tree);
    box.style.setProperty('--glow-1', p.glow);
    box.style.setProperty('--glow-2', 'rgba(0,0,0,0)');
    box.style.setProperty('--warm-1', p.warm);
    box.style.setProperty('--warm-2', 'rgba(0,0,0,0)');
    if (note) note.textContent = days.weather || '—';
  }

  /* ── 星图 ───────────────────────────────────────────── */
  const KNAME = { evt: '事', mem: '记', prom: '约' };
  let starData = [];
  let selected = null;
  const hidden = {};

  function drawStars() {
    const svg = document.getElementById('wrStars');
    if (!svg) return;
    const NS = 'http://www.w3.org/2000/svg';
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const stars = starData.filter(function (s) { return !hidden[s.k]; });
    if (!stars.length) return;

    const today = new Date();
    const H = 150, W = 120;
    let oldest = today;
    starData.forEach(function (s) {
      const t = new Date(String(s.at) + 'T00:00:00');
      if (!isNaN(t) && t < oldest) oldest = t;
    });
    const DAYS = Math.max(7, (today - oldest) / 86400000);
    const rnd = function (i) {
      const x = Math.sin(i * 127.1 + 3.7) * 43758.5453;
      return x - Math.floor(x);
    };

    // 同一天的点会叠成一坨 —— 记下每个日期已经放了几颗，横向错开
    const dayCount = {};
    stars.forEach(function (s, i) {
      const t = new Date(String(s.at) + 'T00:00:00');
      if (isNaN(t)) return;
      const age = (today - t) / 86400000;
      if (age < -1) return;
      const n = dayCount[s.at] = (dayCount[s.at] || 0) + 1;
      const jitterY = ((n % 3) - 1) * 2.4;
      const y = H - 10 - (Math.min(age, DAYS) / DAYS) * (H - 20) + jitterY;
      const x = 10 + ((rnd(i * 3.1) + n * 0.17) % 1) * (W - 20);
      const fade = Math.max(0.12, 1 - Math.min(age, DAYS) / DAYS);

      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('cx', x.toFixed(1));
      c.setAttribute('cy', y.toFixed(1));
      let r = 1.1 + (s.w || 0.6) * 1.5;
      let cls = 's-' + (s.k || 'mem');
      if ((s.w || 0) >= 0.99 && s.k === 'mem') { cls = 's-gold'; r += 0.3; }
      c.setAttribute('r', r.toFixed(2));
      c.setAttribute('class', cls + (selected === s ? ' sel' : ''));
      c.setAttribute('opacity', (0.28 + 0.72 * fade).toFixed(2));
      c.style.cursor = 'pointer';

      c.addEventListener('click', function () {
        selected = s;
        const info = document.getElementById('wrStarInfo');
        if (info) {
          info.innerHTML = '<b>' + s.at + '</b> · ' + KNAME[s.k || 'mem'] +
                           '<br>' + (s.t || '');
        }
        Array.prototype.forEach.call(svg.querySelectorAll('circle'), function (el) {
          el.classList.remove('sel');
        });
        c.classList.add('sel');

        // 再往下带出那天说的两句话（2026-11-10）
        if (!info || !s.at) return;
        fetch('/api/day_turns?day=' + encodeURIComponent(s.at))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            const ts = (d && d.turns) || [];
            if (!ts.length) return;
            const lines = ts.slice(-3).map(function (t) {
              const who = t.role === 'player' ? '他' : '她';
              return who + '：' + String(t.text || '').replace(/\n/g, ' ').slice(0, 40);
            });
            info.innerHTML += '<div class="wr-quote">' + lines.join('<br>') + '</div>';
          })
          .catch(function () { /* 静默 */ });
      });

      const title = document.createElementNS(NS, 'title');
      title.textContent = s.at + ' · ' + (s.t || '');
      c.appendChild(title);
      svg.appendChild(c);
    });

    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', 4); line.setAttribute('x2', W - 4);
    line.setAttribute('y1', H - 5); line.setAttribute('y2', H - 5);
    line.setAttribute('stroke', 'rgba(233,196,106,.22)');
    line.setAttribute('stroke-width', '0.6');
    svg.appendChild(line);
  }

  async function loadStars(force) {
    try {
      const d = await (await fetch('/api/star_map')).json();
      starData = d.stars || [];
      drawStars();
    } catch (e) { /* 静默 */ }
  }

  let lastStars = 0;
  async function tick() {
    try {
      const st = await (await fetch('/api/status')).json();
      paintWindow(st);
    } catch (e) { /* 静默 */ }
    if (Date.now() - lastStars > 300000) { lastStars = Date.now(); loadStars(); }
  }

  function boot() {
    const lg = document.getElementById('wrLegend');
    if (lg) {
      lg.addEventListener('click', function (ev) {
        const b = ev.target.closest('.wr-lg');
        if (!b) return;
        const k = b.getAttribute('data-k');
        hidden[k] = !hidden[k];
        b.classList.toggle('off', !!hidden[k]);
        drawStars();
      });
    }
    tick();
    setInterval(tick, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
