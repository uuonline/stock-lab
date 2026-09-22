/* StockLab 前端 */
'use strict';

const $  = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const S = {
  view: 'dashboard',
  group: null,
  watchTimer: null,
  detailSymbol: null,
  period: 'trend',
  sub: 'macd',
  lastQuote: null,
  mover: 'up',
  screenPresets: null,
  screenFields: null,
  screenTech: null,
  selectedTech: new Set(),
  techParams: {},
  boxData: null,
  boxMode: 'adaptive',   // 箱体默认按个股节奏取窗口（可切回固定窗口对比）
  suppressHash: false,
  conditions: [],
  btStrategies: null,
  klineChart: null,
  btChart: null,
};

/* ---------------- 基础工具 ---------------- */

async function api(path, opts) {
  const o = Object.assign({ headers: {} }, opts || {});
  if (o.body && typeof o.body !== 'string') {
    o.body = JSON.stringify(o.body);
    o.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch('/api' + path, o);
  let data = null;
  const txt = await res.text();
  if (txt) { try { data = JSON.parse(txt); } catch (e) { data = { detail: txt }; } }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || ('HTTP ' + res.status);
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, kind === 'err' ? 4200 : 2400);
}

function loading(on, text) {
  $('#loadingText').textContent = text || '加载中…';
  $('#loading').classList.toggle('hidden', !on);
}

/* 复制到剪贴板。

   注意：本系统通常部署在局域网 http:// 下，而 navigator.clipboard
   只在安全上下文（HTTPS 或 localhost）里可用 —— 直接用它会静默失败。
   所以必须保留 execCommand 回退。 */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 落到回退方案 */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (!ok) throw new Error('浏览器拒绝了复制，请手动选中复制');
  return true;
}

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function num(v, d) {
  if (v === null || v === undefined || v === '' || isNaN(v)) return '—';
  return Number(v).toFixed(d === undefined ? 2 : d);
}

function pct(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v);
  return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
}

function cls(v) {
  if (v === null || v === undefined || isNaN(v)) return 'flat';
  const n = Number(v);
  return n > 0 ? 'up' : (n < 0 ? 'down' : 'flat');
}

function money(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Math.abs(Number(v));
  const sign = Number(v) < 0 ? '-' : '';
  if (n >= 1e12) return sign + (n / 1e12).toFixed(2) + '万亿';
  if (n >= 1e8)  return sign + (n / 1e8).toFixed(2) + '亿';
  if (n >= 1e4)  return sign + (n / 1e4).toFixed(2) + '万';
  return sign + n.toFixed(2);
}

function vol(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v);
  if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿手';
  if (n >= 1e4) return (n / 1e4).toFixed(2) + '万手';
  return n.toFixed(0) + '手';
}

/* 极简 Markdown 渲染 */
function md(text) {
  if (!text) return '';
  const lines = esc(text).split('\n');
  let out = [], inTable = false, inList = false, inQuote = false, quoteBuf = [];

  const flushQuote = () => {
    if (inQuote) {
      out.push('<blockquote>' + quoteBuf.join('<br>') + '</blockquote>');
      quoteBuf = []; inQuote = false;
    }
  };
  const closeBlocks = () => {
    if (inTable) { out.push('</tbody></table>'); inTable = false; }
    if (inList) { out.push(inList === 'ol' ? '</ol>' : '</ul>'); inList = false; }
    flushQuote();
  };

  for (let i = 0; i < lines.length; i++) {
    let l = lines[i];
    if (/^\s*\|.*\|\s*$/.test(l)) {
      const cells = l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      if (/^[\s\-:|]+$/.test(l.replace(/\|/g, ''))) continue;
      if (!inTable) { closeBlocks(); out.push('<table><tbody>'); inTable = true; }
      const tag = (!inTable || out[out.length - 1] === '<table><tbody>') ? 'th' : 'td';
      out.push('<tr>' + cells.map(c => `<${tag}>${c}</${tag}>`).join('') + '</tr>');
      continue;
    }
    if (inTable) { out.push('</tbody></table>'); inTable = false; }

    // 引用块（报告里的数据说明、免责声明、技术位警示都用 > 开头）
    if (/^\s*&gt;\s?/.test(l)) {
      if (inList) { out.push('</ul>'); inList = false; }
      inQuote = true;
      quoteBuf.push(l.replace(/^\s*&gt;\s?/, ''));
      continue;
    }
    if (inQuote) flushQuote();

    if (/^#{1,3}\s+/.test(l)) { closeBlocks(); out.push('<h2>' + l.replace(/^#{1,3}\s+/, '') + '</h2>'); continue; }
    if (/^\s*[-*]\s+/.test(l)) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + l.replace(/^\s*[-*]\s+/, '') + '</li>');
      continue;
    }
    if (/^\s*\d+\.\s+/.test(l)) {
      if (!inList) { out.push('<ol>'); inList = 'ol'; }
      out.push('<li>' + l.replace(/^\s*\d+\.\s+/, '') + '</li>');
      continue;
    }
    if (inList) { out.push(inList === 'ol' ? '</ol>' : '</ul>'); inList = false; }
    if (/^\s*---+\s*$/.test(l)) { out.push('<hr>'); continue; }
    if (!l.trim()) { out.push('<br>'); continue; }
    l = l.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
         .replace(/`(.+?)`/g, '<code>$1</code>')
         .replace(/^\s*(\d+)\.\s+/, '$1. ');
    out.push('<div>' + l + '</div>');
  }
  closeBlocks();
  return out.join('');
}

/* ---------------- 导航 ---------------- */

/* ---------------- 路由 ----------------
   把当前页面写进地址栏（#detail/600519.SH 这种形式）。
   之前 showView 不改 hash，刷新后必然回到总览 ——
   用户在个股页按 F5 就丢失了正在看的股票。现在刷新、书签、
   浏览器前进后退都能保持。 */

function currentRoute() {
  const h = (location.hash || '').replace(/^#/, '');
  if (!h) return { view: 'dashboard', symbol: null };
  const parts = h.split('/');
  let symbol = parts.length > 1 ? parts.slice(1).join('/') : null;
  if (symbol) { try { symbol = decodeURIComponent(symbol); } catch (e) { /* 保留原值 */ } }
  return { view: parts[0], symbol };
}

function setHash(view, symbol) {
  const want = symbol ? `#${view}/${encodeURIComponent(symbol)}` : `#${view}`;
  if (location.hash === want) return;
  S.suppressHash = true;          // 避免自己触发 hashchange 造成重复渲染
  location.hash = want;
  setTimeout(() => { S.suppressHash = false; }, 0);
}

/* 切换页面。symbol 仅对个股页有意义 */
function showView(v, symbol) {
  S.view = v;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + v));
  clearInterval(S.watchTimer); S.watchTimer = null;
  setHash(v, v === 'detail' ? (symbol || S.detailSymbol) : null);
  if (v === 'detail' && symbol) {
    loadDetail(symbol);
    return;
  }
  render();
}

/* 根据地址栏还原界面。刷新、直接访问带 hash 的网址、前进后退都走这里 */
function applyRoute() {
  const { view, symbol } = currentRoute();
  const v = $('#view-' + view) ? view : 'dashboard';
  S.view = v;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + v));
  clearInterval(S.watchTimer); S.watchTimer = null;
  if (v === 'detail') {
    const target = symbol || S.detailSymbol;
    if (target) { loadDetail(target); return; }
  }
  if (v === 'flow' && symbol) {
    // 带标的的地址（#flow/002241.SZ）直接跑，刷新后能还原
    if ($('#flowSymbol')) $('#flowSymbol').value = symbol;
    if (S.detailSymbol) $('#flowSymbol').value = symbol;
    runFlow(symbol);
    return;
  }
  render();
}

function render() {
  switch (S.view) {
    case 'dashboard': return loadDashboard();
    case 'watchlist': return loadWatchlist();
    case 'screener':  return initScreener();
    case 'flow':
      // 不自动重跑：一次全流程要打多个数据源，切标签就重算太浪费。
      // 已有结果就保留，没有就把输入框填上当前个股。
      if ($('#flowSymbol') && !$('#flowSymbol').value && S.detailSymbol) {
        $('#flowSymbol').value = S.detailSymbol;
      }
      return;
    case 'backtest':  return initBacktest();
    case 'alerts':    return loadAlerts();
    case 'settings':  return loadSettings();
    case 'detail':    if (S.detailSymbol) loadDetail(S.detailSymbol);
  }
}

/* ---------------- 总览 ---------------- */

async function loadDashboard() {
  try {
    const d = await api('/market/overview');
    renderMarketStatus(d.status);
    $('#indices').innerHTML = (d.indices || []).map(x => `
      <div class="idx-card">
        <div class="nm">${esc(x.name)}</div>
        <div class="px ${cls(x.pct_change)}">${num(x.price)}</div>
        <div class="ch ${cls(x.pct_change)}">${num(x.change)} ${pct(x.pct_change)}</div>
      </div>`).join('');

    const b = d.breadth || {};
    $('#breadth').innerHTML = `
      <div class="bi"><div class="n up">${b.up || 0}</div><div class="l">上涨</div></div>
      <div class="bi"><div class="n down">${b.down || 0}</div><div class="l">下跌</div></div>
      <div class="bi"><div class="n flat">${b.flat || 0}</div><div class="l">平盘</div></div>
      <div class="bi"><div class="n up">${b.limit_up || 0}</div><div class="l">涨停</div></div>
      <div class="bi"><div class="n down">${b.limit_down || 0}</div><div class="l">跌停</div></div>
      <div class="bi"><div class="n">${num(b.up_ratio, 1)}%</div><div class="l">上涨占比</div></div>`;

    // 首次启动：数据库为空，后台正在抓全市场数据
    const ss = d.snapshot_state || {};
    if (!ss.ready || (ss.refreshing && ss.refreshing.length)) {
      showInitBanner(ss);
    } else {
      hideInitBanner();
      await loadMovers(S.mover);
    }
  } catch (e) { toast('总览加载失败: ' + e.message, 'err'); }
}

let initTimer = null;

function showInitBanner(ss) {
  const box = $('#moverTable');
  const busy = !!(ss.refreshing && ss.refreshing.length);
  const retryIn = ss.retry_in || 0;

  if (!busy && retryIn > 0) {
    // 抓取失败并进入退避：明确告诉用户，而不是无限转圈
    box.innerHTML = `<tr><td class="empty">
        <div style="font-size:15px;margin-bottom:8px">⚠️ 全市场数据初始化失败</div>
        <div class="muted">数据源暂时不可用（多为接口限流），系统将在
          <b id="retryCount">${retryIn}</b> 秒后自动重试。</div>
        <div class="muted" style="margin-top:6px;font-size:12px">
          失败原因：${esc(String(ss.last_error || '未知').slice(0, 120))}</div>
        <div class="muted" style="margin-top:6px">自选、个股行情、K线、回测等功能不受影响，可正常使用。</div>
      </td></tr>`;
    if (!window._retryTick) {
      window._retryTick = setInterval(() => {
        const el = $('#retryCount');
        if (!el) { clearInterval(window._retryTick); window._retryTick = null; return; }
        const n = (parseInt(el.textContent, 10) || 0) - 2;
        el.textContent = n > 0 ? n : 0;
      }, 2000);
    }
  } else {
    if (window._retryTick) { clearInterval(window._retryTick); window._retryTick = null; }
    box.innerHTML = `<tr><td class="empty">
        <div style="font-size:15px;margin-bottom:8px">⏳ 正在初始化全市场数据…</div>
        <div class="muted">首次启动需要抓取约 9900 只标的（A股/港股/ETF），大约 1-3 分钟。</div>
        <div class="muted" style="margin-top:6px">当前已入库 <b id="initCount">${ss.count || 0}</b> 条，期间可以正常使用自选和个股功能。</div>
      </td></tr>`;
  }

  if (initTimer) return;
  initTimer = setInterval(async () => {
    try {
      const st = await api('/snapshot/status');
      const el = $('#initCount');
      if (el) el.textContent = st.count || 0;
      if (st.ready && !(st.refreshing && st.refreshing.length)) {
        clearInterval(initTimer); initTimer = null;
        if (window._retryTick) { clearInterval(window._retryTick); window._retryTick = null; }
        hideInitBanner();
        toast('全市场数据初始化完成', 'ok');
        loadDashboard();
      } else {
        // 在「抓取中 / 失败退避」之间切换时重绘横幅
        const nowBusy = !!(st.refreshing && st.refreshing.length);
        const nowRetry = st.retry_in || 0;
        if (nowBusy !== busy || (nowRetry > 0) !== (retryIn > 0)) {
          clearInterval(initTimer); initTimer = null;
          showInitBanner(st);
        }
      }
    } catch (e) { /* 静默重试 */ }
  }, 4000);
}

function hideInitBanner() {
  if (initTimer) { clearInterval(initTimer); initTimer = null; }
}

async function loadMovers(kind) {
  S.mover = kind;
  $$('#moverSeg .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.kind === kind));
  try {
    const d = await api('/market/movers?kind=' + kind + '&limit=20');
    const rows = d.rows || [];
    if (!rows.length) { $('#moverTable').innerHTML = '<tr><td class="empty">暂无数据</td></tr>'; return; }
    $('#moverTable').innerHTML = `
      <thead><tr><th>名称</th><th>代码</th><th>最新价</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>PE</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td><span class="link" data-sym="${r.symbol}">${esc(r.name)}</span>
              <span class="tag">${esc(r.board || '')}</span></td>
          <td class="num">${esc(r.symbol)}</td>
          <td class="num ${cls(r.pct_change)}">${num(r.price)}</td>
          <td class="num ${cls(r.pct_change)}">${pct(r.pct_change)}</td>
          <td class="num">${money(r.amount)}</td>
          <td class="num">${num(r.turnover_rate)}%</td>
          <td class="num">${num(r.pe)}</td>
        </tr>`).join('')}</tbody>`;
  } catch (e) { toast('排行榜加载失败: ' + e.message, 'err'); }
}

async function marketReport() {
  const box = $('#marketReport');
  box.className = 'report-box';
  box.innerHTML = '<span class="muted">生成中…</span>';
  try {
    const d = await api('/ai/report', { method: 'POST', body: { scope: 'market' } });
    box.innerHTML = md(d.content);
  } catch (e) { box.innerHTML = '<span class="down">生成失败: ' + esc(e.message) + '</span>'; }
}

/* ---------------- 自选 ---------------- */

async function loadWatchlist() {
  try {
    const withTech = $('#withTech').checked ? 1 : 0;
    const g = S.group ? ('?group=' + encodeURIComponent(S.group)) : '';
    const d = await api('/watchlist' + g + (g ? '&' : '?') + 'with_tech=' + withTech);

    const sel = $('#groupSelect');
    const groups = d.groups || [];
    if (sel.dataset.built !== groups.join('|')) {
      sel.innerHTML = '<option value="">全部</option>' +
        groups.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
      sel.dataset.built = groups.join('|');
      sel.value = S.group || '';
    }

    const rows = d.rows || [];
    if (!rows.length) {
      $('#watchTable').innerHTML = '<tr><td class="empty">还没有自选股，在上方输入代码或名称添加</td></tr>';
      return;
    }
    const techHead = withTech ? '<th>技术面</th>' : '';
    $('#watchTable').innerHTML = `
      <thead><tr><th>名称</th><th>代码</th><th>最新价</th><th>涨跌幅</th><th>今开</th><th>最高</th>
      <th>最低</th><th>成交额</th><th>换手率</th><th>PE</th>${techHead}<th>操作</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td><span class="link" data-sym="${r.symbol}">${esc(r.name)}</span></td>
          <td class="num">${esc(r.symbol)}</td>
          <td class="num ${cls(r.pct_change)}">${num(r.price)}</td>
          <td class="num ${cls(r.pct_change)}">${pct(r.pct_change)}</td>
          <td class="num">${num(r.open)}</td>
          <td class="num">${num(r.high)}</td>
          <td class="num">${num(r.low)}</td>
          <td class="num">${money(r.amount)}</td>
          <td class="num">${num(r.turnover_rate)}%</td>
          <td class="num">${num(r.pe_ttm)}</td>
          ${withTech ? `<td>${r.tech_rating ? `<span class="tag">${esc(r.tech_rating)} (${r.tech_score})</span>` : '—'}</td>` : ''}
          <td><button class="mini-btn del-watch" data-sym="${r.symbol}">删除</button></td>
        </tr>`).join('')}</tbody>`;
  } catch (e) { toast('自选加载失败: ' + e.message, 'err'); }

  if ($('#autoRefresh').checked && !S.watchTimer) {
    S.watchTimer = setInterval(() => {
      if (S.view === 'watchlist' && $('#autoRefresh').checked) loadWatchlist();
    }, 10000);
  }
}

async function addWatch() {
  const v = $('#watchInput').value.trim();
  if (!v) return;
  try {
    const g = S.group || '默认分组';
    const r = await api('/watchlist', { method: 'POST', body: { symbol: v, group: g } });
    toast('已添加 ' + r.name, 'ok');
    $('#watchInput').value = '';
    $('#watchSuggest').innerHTML = '';
    loadWatchlist();
  } catch (e) { toast('添加失败: ' + e.message, 'err'); }
}

async function searchSuggest(inputEl, boxEl, onPick) {
  const q = inputEl.value.trim();
  if (q.length < 1) { boxEl.innerHTML = ''; return; }
  try {
    const d = await api('/search?q=' + encodeURIComponent(q) + '&limit=8');
    const rs = d.results || [];
    boxEl.innerHTML = rs.map(r => `
      <div class="suggest-item" data-sym="${r.symbol}">
        <span><span class="s-name">${esc(r.name)}</span><span class="s-code">${esc(r.symbol)}</span></span>
        <span class="${cls(r.pct_change)}">${num(r.price)} ${pct(r.pct_change)}</span>
      </div>`).join('');
    $$('.suggest-item', boxEl).forEach(el => {
      el.onclick = () => { boxEl.innerHTML = ''; onPick(el.dataset.sym); };
    });
  } catch (e) { boxEl.innerHTML = ''; }
}

/* ---------------- 个股详情 ---------------- */

async function loadDetail(symbol) {
  S.detailSymbol = symbol;
  setHash('detail', symbol);
  $('#detailBody').classList.remove('hidden');
  $('#detailInput').value = symbol;
  try {
    const q = await api('/quote/' + encodeURIComponent(symbol));
    S.lastQuote = q;          // 分时图要用昨收画基准线
    $('#detailQuote').innerHTML = `
      <div class="q-main">
        <div class="q-name">${esc(q.name || symbol)}
          <span class="tag">${esc(q.asset_type || '')}</span>
          <span class="tag">${esc(q.board || '')}</span></div>
        <div class="q-price ${cls(q.pct_change)}">${num(q.price)}</div>
        <div class="q-chg ${cls(q.pct_change)}">${num(q.change)}　${pct(q.pct_change)}</div>
        <div class="mt-sm"><button class="mini-btn" id="addToWatch">+ 加入自选</button>
          <button class="mini-btn" id="quickAlert">+ 设提醒</button></div>
      </div>
      <div class="q-grid">
        ${qItem('今开', num(q.open))}${qItem('昨收', num(q.prev_close))}
        ${qItem('最高', num(q.high))}${qItem('最低', num(q.low))}
        ${qItem('成交量', vol(q.volume))}${qItem('成交额', money(q.amount))}
        ${qItem('换手率', num(q.turnover_rate) + '%')}${qItem('量比', num(q.vol_ratio))}
        ${qItem('振幅', num(q.amplitude) + '%')}${qItem('PE(TTM)', num(q.pe_ttm))}
        ${qItem('PB', num(q.pb))}${qItem('总市值', money(q.market_cap))}
        ${qItem('流通市值', money(q.float_cap))}${qItem('主力净流入', money(q.main_net_inflow))}
      </div>`;
    const aw = $('#addToWatch');
    if (aw) aw.onclick = async () => {
      try {
        const r = await api('/watchlist', { method: 'POST', body: { symbol, group: S.group || '默认分组' } });
        toast('已加入自选: ' + r.name, 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
    const qa = $('#quickAlert');
    if (qa) qa.onclick = () => { showView('alerts'); setTimeout(() => { $('#alSymbol').value = symbol; }, 80); };
  } catch (e) { toast('行情加载失败: ' + e.message, 'err'); }

  const _sub = $('#subSeg');
  if (_sub) _sub.style.display = (S.period === 'trend') ? 'none' : 'inline-flex';
  loadKline();
  loadTech();
  loadFundamentals();
  loadBox();          // 箱体独立加载，避免只在 K线视图下才有数据
  $('#aiReport').className = 'report-box muted';
  $('#aiReport').textContent = '点击「生成简报」开始分析';
}

const qItem = (k, v) => `<div class="qi"><span class="k">${k}</span><span class="v">${v}</span></div>`;

async function loadKline() {
  const sym = S.detailSymbol;
  if (!sym) return;
  const chartEl = $('#klineChart');
  if (!S.klineChart) S.klineChart = echarts.init(chartEl, 'dark', { renderer: 'canvas' });
  S.klineChart.showLoading({ text: '加载中', textColor: '#9aa4b2', maskColor: 'rgba(13,17,23,.6)' });

  if (S.period === 'trend') {
    try {
      const d = await api('/trends/' + encodeURIComponent(sym));
      // 分时图需要昨收来画基准线和对称的涨跌幅轴
      if (!S.lastQuote || S.lastQuote.symbol !== sym) {
        try { S.lastQuote = await api('/quote/' + encodeURIComponent(sym)); } catch (e) {}
      }
      drawTrends(d, S.lastQuote);
    } catch (e) {
      S.klineChart.hideLoading();
      toast('分时加载失败: ' + e.message, 'err');
    }
    return;
  }

  try {
    const d = await api(`/kline/${encodeURIComponent(sym)}?period=${S.period}&limit=600&indicators=1`);
    drawKline(d);
  } catch (e) {
    S.klineChart.hideLoading();
    toast('K线加载失败: ' + e.message, 'err');
  }
}

/* 分时图：价格线 + 均价线 + 分钟量柱 + 昨收基准线
   涨跌幅轴以昨收为中心对称，这是分时图的标准画法，
   否则视觉上会误判涨跌幅度。 */
function drawTrends(d, quote) {
  const rows = (d.trends || []).filter(r => r && r.price != null);
  const chart = S.klineChart;
  if (!rows.length) {
    chart.hideLoading();
    chart.clear();
    toast('暂无分时数据（可能非交易日）', 'err');
    return;
  }
  const prev = (quote && quote.prev_close) || rows[0].price;
  const times = rows.map(r => String(r.time).slice(-5));   // 只留 HH:MM
  const prices = rows.map(r => r.price);
  const avgs = rows.map(r => (r.avg != null ? r.avg : null));
  // 分时量能柱的颜色必须和**前一分钟**比，涨红跌绿（同花顺口径）。
  // 不能用 r.price >= r.open：分时是一分钟一个价，每行的 open 恒等于 price，
  // 这个比较永远成立，结果就是整排量柱全红。第一分钟对比昨收。
  const vols = rows.map((r, i) => {
    const base = i > 0 ? rows[i - 1].price : prev;
    return {
      value: r.volume || 0,
      itemStyle: { color: (r.price >= base) ? '#f0454b' : '#26a269' },
    };
  });

  const last = prices[prices.length - 1];
  const up = last >= prev;
  const lineColor = up ? '#f0454b' : '#26a269';

  // 以昨收为中心对称，保证上下幅度视觉一致
  let dev = 0;
  prices.forEach(p => { dev = Math.max(dev, Math.abs(p - prev)); });
  avgs.forEach(a => { if (a != null) dev = Math.max(dev, Math.abs(a - prev)); });
  if (!dev) dev = prev * 0.001 || 1;
  dev *= 1.08;                                  // 留一点余量
  const yMin = prev - dev, yMax = prev + dev;
  const pct = (v) => ((v - prev) / prev * 100);

  chart.hideLoading();
  chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      backgroundColor: '#1c2129', borderColor: '#2a3038',
      textStyle: { color: '#e6edf3', fontSize: 12 },
      formatter: (ps) => {
        if (!ps || !ps.length) return '';
        const i = ps[0].dataIndex;
        const r = rows[i];
        const c = (r.price >= prev) ? '#f0454b' : '#26a269';
        const avgTxt = r.avg != null ? `均价 ${r.avg.toFixed(2)}` : '均价 —';
        const sign = pct(r.price) >= 0 ? '+' : '';
        return `<b>${r.time}</b><br/>价格 <span style="color:${c}">${r.price}</span>`
             + ` <span style="color:${c}">(${sign}${pct(r.price).toFixed(2)}%)</span><br/>`
             + `${avgTxt}<br/>成交量 ${(r.volume || 0).toFixed(0)} 手`
             + (r.amount ? `<br/>成交额 ${(r.amount / 10000).toFixed(0)} 万` : '');
      },
    },
    legend: { top: 2, textStyle: { color: '#9aa4b2', fontSize: 11 }, data: ['价格', '均价'] },
    grid: [
      { left: 62, right: 62, top: 30, height: '60%' },
      { left: 62, right: 62, top: '76%', height: '17%' },
    ],
    xAxis: [
      { type: 'category', data: times, gridIndex: 0, boundaryGap: false,
        axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', data: times, gridIndex: 1, boundaryGap: false,
        axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { color: '#9aa4b2', fontSize: 10, interval: Math.max(0, Math.floor(times.length / 6)) },
        splitLine: { show: false } },
    ],
    yAxis: [
      { gridIndex: 0, min: yMin, max: yMax, scale: true,
        axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { color: '#9aa4b2', fontSize: 10, formatter: (v) => v.toFixed(2) },
        splitLine: { lineStyle: { color: '#1c2129' } } },
      { gridIndex: 0, min: pct(yMin), max: pct(yMax), position: 'right',
        axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { fontSize: 10, formatter: (v) => v.toFixed(2) + '%',
          color: (v) => (v >= 0 ? '#f0454b' : '#26a269') },
        splitLine: { show: false } },
      { gridIndex: 1, scale: true, axisLabel: { show: false },
        axisLine: { lineStyle: { color: '#2a3038' } }, splitLine: { show: false }, splitNumber: 2 },
    ],
    series: [
      { name: '价格', type: 'line', data: prices, xAxisIndex: 0, yAxisIndex: 0,
        showSymbol: false, lineStyle: { width: 1.6, color: lineColor },
        areaStyle: { color: up ? 'rgba(240,69,75,.13)' : 'rgba(38,162,105,.13)' },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { color: '#8b949e', type: 'dashed', width: 1 },
          data: [{ yAxis: prev, label: { formatter: `昨收 ${prev}`, color: '#8b949e',
                   fontSize: 10, position: 'insideEndTop' } }],
        } },
      { name: '均价', type: 'line', data: avgs, xAxisIndex: 0, yAxisIndex: 0,
        showSymbol: false, lineStyle: { width: 1.2, color: '#e3b341' } },
      { name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 2 },
    ],
  }, true);
}

function drawKline(d) {
  S.lastKline = d;              // 切换箱体模式后要用它重画
  const bars = d.bars || [];
  if (!bars.length) { S.klineChart.hideLoading(); toast('无K线数据', 'err'); return; }
  const ind = d.indicators || {};
  const dates = bars.map(b => b.date);
  const ohlc = bars.map(b => [b.open, b.close, b.low, b.high]);
  const vols = bars.map((b, i) => ({
    value: b.volume,
    itemStyle: { color: b.close >= b.open ? '#f0454b' : '#26a269' },
  }));

  const maKeys = ['ma5', 'ma10', 'ma20', 'ma60'].filter(k => ind[k]);
  const maSeries = maKeys.map(k => ({
    name: k.toUpperCase(), type: 'line', data: ind[k], smooth: true,
    showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0,
  }));

  const subSeries = [];
  let subName = 'MACD', subData = [];
  if (S.sub === 'macd') {
    subName = 'MACD';
    subSeries.push({ name: 'MACD柱', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
      data: (ind.macd || []).map(v => ({ value: v, itemStyle: { color: (v || 0) >= 0 ? '#f0454b' : '#26a269' } })) });
    subSeries.push({ name: 'DIF', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: ind.dif, showSymbol: false, lineStyle: { width: 1 } });
    subSeries.push({ name: 'DEA', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: ind.dea, showSymbol: false, lineStyle: { width: 1 } });
  } else if (S.sub === 'kdj') {
    subName = 'KDJ';
    ['k', 'd', 'j'].forEach(k => subSeries.push({
      name: k.toUpperCase(), type: 'line', xAxisIndex: 1, yAxisIndex: 1,
      data: ind[k], showSymbol: false, lineStyle: { width: 1 },
    }));
  } else {
    subName = 'RSI';
    ['rsi6', 'rsi12', 'rsi24'].forEach(k => subSeries.push({
      name: k.toUpperCase(), type: 'line', xAxisIndex: 1, yAxisIndex: 1,
      data: ind[k], showSymbol: false, lineStyle: { width: 1 },
    }));
  }

  // ---- 箱体（箱顶/箱底 + 半透明区间）----
  // 优先用 loadBox 已取到的数据，避免重复请求
  const boxWin = pickBox(S.boxData || d.box);
  const boxMarks = [];
  if (boxWin && dates.length) {
    // 用**日期**定位而不是索引！
    // 箱体接口按自己取到的 K线根数（300）算索引，而图表可能取 600 根，
    // 直接套索引会把箱体画到完全错误的位置上（真实踩过：箱体被画到一年多前）。
    let i0 = dates.indexOf(boxWin.start_date);
    let i1 = dates.indexOf(boxWin.end_date);
    if (i0 < 0) i0 = Math.max(0, Math.min(boxWin.start_index, dates.length - 1));
    if (i1 < 0) i1 = Math.max(0, Math.min(boxWin.end_index, dates.length - 1));
    if (i1 < i0) { const t = i0; i0 = i1; i1 = t; }
    const d0 = dates[i0], d1 = dates[i1];
    boxMarks.push({
      name: '箱体',
      type: 'candlestick',
      data: [],
      xAxisIndex: 0, yAxisIndex: 0,
      silent: true,
      markArea: {
        silent: true,
        itemStyle: { color: 'rgba(59,130,246,.07)', borderColor: 'rgba(59,130,246,.35)', borderWidth: 1 },
        label: { show: true, position: 'insideTop', color: '#6b7280', fontSize: 10,
                 formatter: `箱体 ${boxWin.window}日 · ${boxWin.shape}` },
        data: [[{ xAxis: d0, yAxis: boxWin.bottom }, { xAxis: d1, yAxis: boxWin.top }]],
      },
      markLine: {
        silent: true, symbol: 'none',
        data: [
          { yAxis: boxWin.top, lineStyle: { color: '#f0454b', type: 'dashed', width: 1.2 },
            label: { formatter: `箱顶 ${boxWin.top}`, color: '#f0454b', fontSize: 10, position: 'insideEndTop' } },
          { yAxis: boxWin.bottom, lineStyle: { color: '#26a269', type: 'dashed', width: 1.2 },
            label: { formatter: `箱底 ${boxWin.bottom}`, color: '#26a269', fontSize: 10, position: 'insideEndBottom' } },
          { yAxis: boxWin.mid, lineStyle: { color: '#6b7280', type: 'dotted', width: 1 } },
        ],
      },
    });
  }

  S.klineChart.hideLoading();
  S.klineChart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    legend: { top: 2, textStyle: { color: '#9aa4b2', fontSize: 11 },
      data: ['K线'].concat(maKeys.map(k => k.toUpperCase())).concat(subSeries.map(s => s.name)) },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: '#1c2129',
      borderColor: '#2a3038', textStyle: { color: '#e6edf3', fontSize: 12 } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 52, right: 16, top: 30, height: '50%' },
      { left: 52, right: 16, top: '68%', height: '13%' },
      { left: 52, right: 16, top: '84%', height: '13%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', data: dates, gridIndex: 2, axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { color: '#9aa4b2', fontSize: 10 }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, axisLine: { lineStyle: { color: '#2a3038' } },
        axisLabel: { color: '#9aa4b2', fontSize: 10 }, splitLine: { lineStyle: { color: '#1c2129' } } },
      { scale: true, gridIndex: 1, axisLabel: { color: '#9aa4b2', fontSize: 10 },
        splitLine: { show: false }, splitNumber: 2 },
      { scale: true, gridIndex: 2, name: subName, nameTextStyle: { color: '#6b7280', fontSize: 10 },
        axisLabel: { color: '#9aa4b2', fontSize: 10 }, splitLine: { show: false }, splitNumber: 2 },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 4, height: 16, start: 60, end: 100,
        textStyle: { color: '#9aa4b2', fontSize: 10 } },
    ],
    series: [
      { name: 'K线', type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: '#f0454b', color0: '#26a269', borderColor: '#f0454b', borderColor0: '#26a269' } },
      ...maSeries,
      { name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 1 },
      ...subSeries,
      ...boxMarks,
    ],
  }, true);
  // 面板由 loadBox 负责渲染（它不依赖图表周期），这里不再重复调用
}

/* 从多窗口结果里挑一个用于画图：
   优先「推荐窗口」（置信度最高的震荡箱体），
   没有箱体时退而使用当前周期窗口，让用户至少看到区间边界。 */
function pickBox(boxData) {
  if (!boxData) return null;
  // 自适应结果：窗口是按个股节奏推出来的，直接用它的分析
  if (boxData.adaptive && boxData.analysis) return boxData.analysis;
  const wins = boxData.windows || {};
  const rec = boxData.recommended;
  if (rec && wins[rec]) return wins[rec];
  const keys = Object.keys(wins);
  if (!keys.length) return null;
  return wins[keys[keys.length - 1]];
}

/* 箱体分析面板 */
function renderBoxPanel(boxData) {
  const el = $('#boxPanel');
  if (!el) return;

  // 模式切换按钮
  const mode = boxData && boxData.adaptive ? 'adaptive' : 'fixed';
  const seg = `
    <div class="seg" id="boxModeSeg" style="margin-bottom:10px">
      <button class="seg-btn${mode === 'adaptive' ? ' active' : ''}" data-boxmode="adaptive">按个股节奏</button>
      <button class="seg-btn${mode === 'fixed' ? ' active' : ''}" data-boxmode="fixed">固定窗口</button>
    </div>`;

  if (boxData && boxData.adaptive) {
    renderAdaptivePanel(el, boxData, seg);
    return;
  }

  if (!boxData || !boxData.windows || !Object.keys(boxData.windows).length) {
    el.innerHTML = seg + '<span class="muted">数据不足，无法识别箱体</span>';
    return;
  }
  const wins = boxData.windows;
  const rec = boxData.recommended;
  const cur = wins[rec] || wins[Object.keys(wins)[0]];

  const shapeColor = cur.shape === '震荡箱体' ? 'var(--accent)'
    : (cur.shape === '上升通道' ? 'var(--up)' : (cur.shape === '下降通道' ? 'var(--down)' : 'var(--fg2)'));
  const statusColor = cur.status === '向上突破箱顶' ? 'up'
    : (cur.status === '向下跌破箱底' ? 'down' : 'flat');

  // 位置条：箱底 0% → 箱顶 100%
  const pos = Math.max(0, Math.min(100, cur.position_pct));
  const bar = `
    <div style="margin:12px 0">
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--fg2);margin-bottom:4px">
        <span>箱底 ${cur.bottom}</span><span>中轴 ${cur.mid}</span><span>箱顶 ${cur.top}</span>
      </div>
      <div style="position:relative;height:8px;border-radius:4px;overflow:hidden;
                  background:linear-gradient(90deg,rgba(38,162,105,.5),rgba(139,148,158,.35),rgba(240,69,75,.5))">
        <div style="position:absolute;top:-3px;left:calc(${pos}% - 2px);width:4px;height:14px;
                    background:#fff;border-radius:2px;box-shadow:0 0 4px rgba(0,0,0,.6)"></div>
      </div>
      <div style="text-align:center;font-size:12px;margin-top:6px">
        当前价 <b>${cur.price}</b> 位于箱体 <b>${cur.position_pct}%</b> 位置 · ${esc(cur.zone)}
      </div>
    </div>`;

  const rows = [
    ['形态判定', `<span style="color:${shapeColor};font-weight:600">${esc(cur.shape)}</span>`],
    ['突破状态', `<span class="${statusColor}">${esc(cur.status)}</span>`],
    ['箱顶 / 箱底', `${cur.top} / ${cur.bottom}`],
    ['箱体高度', `${cur.height_pct}%（${cur.height}）`],
    ['触顶 / 触底次数', `${cur.touch_top} / ${cur.touch_bottom} 次`],
    ['穿越中轴', `${cur.crosses} 次（每10日 ${cur.crosses_per_10} 次）`],
    ['日均斜率', `${cur.slope_pct}%`],
    ['箱体置信度', `${cur.confidence}%`],
    ['观察区间', `${cur.start_date} ~ ${cur.end_date}（${cur.bars} 根）`],
  ];

  // 多窗口对比
  const others = Object.keys(wins).filter(k => k !== rec);
  const cmp = others.length ? `
    <h3 class="mt" style="font-size:13px">其他窗口对比</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>窗口</th><th>形态</th><th>箱底</th><th>箱顶</th><th>位置</th><th>置信度</th></tr></thead>
      <tbody>${others.map(k => {
        const w = wins[k];
        const c = w.shape === '震荡箱体' ? 'var(--accent)'
          : (w.shape === '上升通道' ? 'var(--up)' : (w.shape === '下降通道' ? 'var(--down)' : ''));
        return `<tr>
          <td>${w.window} 日</td>
          <td style="color:${c}">${esc(w.shape)}</td>
          <td class="num">${w.bottom}</td>
          <td class="num">${w.top}</td>
          <td class="num">${w.position_pct}%</td>
          <td class="num">${w.confidence}%</td></tr>`;
      }).join('')}</tbody>
    </table></div>` : '';

  el.innerHTML = `
    ${seg}
    ${bar}
    <div class="kv-list">${rows.map(([k, v]) =>
      `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}</div>
    <div class="warn-box" style="margin-top:12px">
      <b>${esc(cur.zone)}</b>：${esc(cur.note)}
      <br><span style="font-size:12px;opacity:.85">
      以上箱顶/箱底由最近 ${cur.bars} 根 K线的价格分位数机械推算（已抗单根插针），
      <b>不构成买卖建议</b>。箱体可能随时被突破，请结合成交量与基本面判断。</span>
    </div>
    ${cmp}`;
}

/* 自适应箱体面板：要说清楚「为什么是这么多天」，
   否则用户只看到一个数字，没法判断该不该信它。 */
function renderAdaptivePanel(el, d, seg) {
  const a = d.analysis;
  if (!a) {
    el.innerHTML = seg + `<span class="muted">${esc(d.verdict || '无法识别箱体')}</span>`;
    return;
  }
  const rh = d.rhythm || {};
  const band = d.band || {};
  const pl = d.plateau || {};

  const shapeColor = a.shape === '震荡箱体' ? 'var(--accent)'
    : (a.shape === '上升通道' ? 'var(--up)' : (a.shape === '下降通道' ? 'var(--down)' : 'var(--fg2)'));
  const statusColor = a.status === '向上突破箱顶' ? 'up'
    : (a.status === '向下跌破箱底' ? 'down' : 'flat');
  const pos = Math.max(0, Math.min(100, a.position_pct));

  const posBar = `
    <div style="margin:12px 0">
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--fg2);margin-bottom:4px">
        <span>箱底 ${a.bottom}</span><span>中轴 ${a.mid}</span><span>箱顶 ${a.top}</span>
      </div>
      <div style="position:relative;height:8px;border-radius:4px;overflow:hidden;
                  background:linear-gradient(90deg,rgba(38,162,105,.5),rgba(139,148,158,.35),rgba(240,69,75,.5))">
        <div style="position:absolute;top:-3px;left:calc(${pos}% - 2px);width:4px;height:14px;
                    background:#fff;border-radius:2px;box-shadow:0 0 4px rgba(0,0,0,.6)"></div>
      </div>
      <div style="text-align:center;font-size:12px;margin-top:6px">
        当前价 <b>${a.price}</b> 位于箱体 <b>${a.position_pct}%</b> 位置 · ${esc(a.zone)}
      </div>
    </div>`;

  // 节奏阶梯：把嵌套的波动级别摊开给用户看
  const levels = (rh.levels || []).map(lv => `
    <tr${lv.threshold_pct === (rh.mid_level || {}).threshold_pct ? ' style="background:rgba(88,166,255,.10)"' : ''}>
      <td>${lv.threshold_pct}%</td>
      <td class="num">${lv.legs}</td>
      <td class="num">${lv.median_leg_days} 天</td>
      <td class="num">${lv.cycle_days} 天</td>
    </tr>`).join('');

  const rhythmTable = levels ? `
    <h3 class="mt" style="font-size:13px">该股自身的波动节奏</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>波动档位</th><th>波段数</th><th>单边中位长度</th><th>完整往复</th></tr></thead>
      <tbody>${levels}</tbody>
    </table></div>
    <div class="muted mt" style="font-size:12px">
      高亮行是用于推算窗口的档位：小档位是噪声，大档位是趋势，中间档才是「在区间里来回磨」的级别。
    </div>` : '';

  const rows = [
    ['推荐窗口', `<b>${d.recommended_window} 日</b>（按该股节奏推出）`],
    ['形态判定', `<span style="color:${shapeColor};font-weight:600">${esc(a.shape)}</span>`],
    ['突破状态', `<span class="${statusColor}">${esc(a.status)}</span>`],
    ['箱顶 / 箱底', `${a.top} / ${a.bottom}`],
    ['箱体高度', `${a.height_pct}%（${a.height}）`],
    ['触顶 / 触底次数', `${a.touch_top} / ${a.touch_bottom} 次`],
    ['箱内占比', `${Math.round((a.inside_ratio || 0) * 100)}% 的收盘价落在箱内`],
    ['穿越中轴', `${a.crosses} 次（每10日 ${a.crosses_per_10} 次）`],
    ['边界漂移', `上沿 ${a.top_drift} / 下沿 ${a.bot_drift}（单位：箱体高度）`],
    ['日均斜率', `${a.slope_pct}%`],
    ['箱体置信度', `${a.confidence}%`],
    ['观察区间', `${a.start_date} ~ ${a.end_date}（${a.bars} 根）`],
  ];

  const warn = d.trustworthy === false
    ? `<div class="warn-box" style="margin-top:12px;border-color:var(--down)">
         <b style="color:var(--down)">当前没有可信的箱体</b><br>${esc(d.verdict || '')}
       </div>`
    : '';

  el.innerHTML = `
    ${seg}
    ${warn}
    <div class="kv-list">${rows.map(([k, v]) =>
      `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}</div>
    ${posBar}
    ${rhythmTable}
    <h3 class="mt" style="font-size:13px">为什么是 ${d.recommended_window} 日</h3>
    <div class="kv-list">
      <div class="kv"><span class="k">① 节奏定范围</span><span class="v">${esc(band.note || '—')}</span></div>
      <div class="kv"><span class="k">② 分数选平台</span><span class="v">${esc(d.reason || '')}</span></div>
      ${d.fixed_best ? `<div class="kv"><span class="k">对比固定窗口</span><span class="v">
        固定最优 ${d.fixed_best.window} 日（分数 ${d.fixed_best.score}）
        → 自适应 ${d.recommended_window} 日（分数 ${pl.top_score}）</span></div>` : ''}
    </div>
    <div class="warn-box" style="margin-top:12px">
      <b>${esc(a.zone)}</b>：${esc(a.note)}
      <br><span style="font-size:12px;opacity:.85">
      窗口按该股历史波段节奏推算，箱顶/箱底由价格分位数机械得出（已抗单根插针），
      <b>不构成买卖建议</b>。节奏会随行情变化，箱体可能随时被突破。</span>
    </div>`;
}

/* 箱体分析：独立请求，不跟随图表周期。
   箱体是日线级别的概念，看分时图时也应该能看到箱底/箱顶 ——
   之前这个面板只在 drawKline 里更新，导致默认的分时视图下
   永远显示「加载中…」（真实踩过的 bug）。 */
async function loadBox() {
  const sym = S.detailSymbol;
  const el = $('#boxPanel');
  if (!sym || !el) return;
  el.innerHTML = '<span class="muted">加载中…</span>';
  const mode = S.boxMode || 'adaptive';
  try {
    const q = mode === 'adaptive' ? '?adaptive=1' : '?multi=1';
    const d = await api('/box/' + encodeURIComponent(sym) + q);
    d.adaptive = (mode === 'adaptive');     // 标记来源，渲染与画图都要用
    S.boxData = d;
    renderBoxPanel(d);
    drawBoxOnChart();
  } catch (e) {
    S.boxData = null;
    el.innerHTML = `<span class="muted">箱体分析不可用：${esc(e.message)}</span>`;
  }
}

/* 切模式后重画图上的箱体（面板自己有按钮，图上的框要跟着换） */
function drawBoxOnChart() {
  if (S.lastKline) drawKline(S.lastKline);
}

async function loadTech() {
  const sym = S.detailSymbol;
  if (!sym) return;
  try {
    const d = await api('/indicators/' + encodeURIComponent(sym) + '?limit=260');
    const s = d.snapshot || {}, v = s.values || {};
    const score = s.score || 0;
    const w = Math.min(Math.abs(score) / 10 * 50, 50);
    const barHtml = `<div class="rating-bar">
        <span class="lbl ${score > 0 ? 'up' : (score < 0 ? 'down' : 'flat')}">${esc(s.rating || '—')}</span>
        <span class="bar"><i style="${score >= 0 ? 'left:50%' : 'right:50%;left:auto'};width:${w}%;
          background:${score > 0 ? 'var(--up)' : 'var(--down)'}"></i></span>
        <span class="muted">评分 ${score}</span></div>`;
    const items = [
      ['MA5', v.ma5], ['MA10', v.ma10], ['MA20', v.ma20], ['MA30', v.ma30],
      ['MA60', v.ma60], ['MA120', v.ma120], ['MA250', v.ma250],
      ['DIF', v.dif], ['DEA', v.dea], ['MACD柱', v.macd],
      ['K', v.k], ['D', v.d], ['J', v.j],
      ['RSI6', v.rsi6], ['RSI12', v.rsi12], ['RSI24', v.rsi24],
      ['布林上轨', v.boll_upper], ['布林中轨', v.boll_mid], ['布林下轨', v.boll_lower],
      ['ATR14', v.atr14], ['CCI14', v.cci14], ['WR14', v.wr14],
      ['5日量比', s.vol_ratio_5],
    ];
    $('#techPanel').innerHTML = barHtml +
      `<div class="kv-list">${items.map(([k, val]) =>
        `<div class="kv"><span class="k">${k}</span><span class="v">${num(val, 2)}</span></div>`).join('')}</div>` +
      (s.signals && s.signals.length
        ? `<div class="signals">${s.signals.map(x =>
            `<span class="sig ${x.type}">${esc(x.text)}</span>`).join('')}</div>`
        : '<p class="muted mt-sm">暂无明确技术信号</p>');
  } catch (e) { $('#techPanel').innerHTML = '<span class="muted">指标加载失败: ' + esc(e.message) + '</span>'; }
}

async function loadFundamentals() {
  const sym = S.detailSymbol;
  if (!sym) return;
  $('#fundPanel').innerHTML = '<span class="muted">加载中…</span>';
  try {
    const d = await api('/fundamentals/' + encodeURIComponent(sym));
    const l = d.latest || {}, val = d.valuation || {};
    const rows = [
      ['报告期', (l.report_name || '') + ' ' + (l.report_date || '')],
      ['每股收益', num(l.eps) + ' 元'],
      ['每股净资产', num(l.bps) + ' 元'],
      ['营业收入', money(l.revenue)],
      ['营收同比', pct(l.revenue_yoy)],
      ['归母净利润', money(l.net_profit)],
      ['净利同比', pct(l.profit_yoy)],
      ['ROE', num(l.roe) + '%'],
      ['毛利率', num(l.gross_margin) + '%'],
      ['净利率', num(l.net_margin) + '%'],
      ['资产负债率', num(l.debt_ratio) + '%'],
      ['流动比率', num(l.current_ratio)],
      ['PE(TTM)', num(val.pe_ttm)],
      ['PB', num(val.pb)],
    ];
    $('#fundPanel').innerHTML =
      `<div class="kv-list">${rows.map(([k, v]) =>
        `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('')}</div>`;

    const hist = d.history || [];
    if (hist.length > 2) {
      $('#fundPanel').innerHTML += `
        <h3 class="mt">营收 / 净利趋势</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>报告期</th><th>营收</th><th>同比</th><th>净利润</th><th>同比</th><th>ROE</th></tr></thead>
          <tbody>${hist.slice(-8).reverse().map(h => `<tr>
            <td>${esc(h.report_date)}</td>
            <td class="num">${money(h.revenue)}</td>
            <td class="num ${cls(h.revenue_yoy)}">${pct(h.revenue_yoy)}</td>
            <td class="num">${money(h.net_profit)}</td>
            <td class="num ${cls(h.profit_yoy)}">${pct(h.profit_yoy)}</td>
            <td class="num">${num(h.roe)}%</td></tr>`).join('')}</tbody>
        </table></div>`;
    }
  } catch (e) {
    $('#fundPanel').innerHTML = '<span class="muted">财务数据不可用（ETF/指数/港股可能无披露）</span>';
  }
}

async function genAiReport() {
  if (!S.detailSymbol) return;
  const box = $('#aiReport');
  box.className = 'report-box';
  box.innerHTML = '<span class="muted">AI 分析中，请稍候…</span>';
  const btn = $('#aiReportBtn');
  btn.disabled = true;
  try {
    const d = await api('/ai/report', { method: 'POST', body: { symbol: S.detailSymbol, scope: 'single' } });
    box.innerHTML = md(d.content) +
      `<div class="muted mt" style="font-size:11.5px">数据来源：公开行情接口　生成方式：${esc(d.model)}　${esc(d.generated_at)}</div>`;
  } catch (e) { box.innerHTML = '<span class="down">生成失败: ' + esc(e.message) + '</span>'; }
  btn.disabled = false;
}

/* ---------------- 选股 ---------------- */

async function initScreener() {
  if (!S.screenPresets) {
    try {
      const d = await api('/screen/presets');
      S.screenPresets = d.presets || [];
      S.screenFields = d.fields || {};
      S.screenTech = d.tech_conditions || {};
      $('#presetList').innerHTML = S.screenPresets.map(p => `
        <div class="preset" data-key="${p.key}">
          <div class="p-name">${esc(p.name)}</div>
          <div class="p-desc">${esc(p.desc)}</div>
        </div>`).join('');
      $$('#presetList .preset').forEach(el => {
        el.onclick = () => {
          $$('#presetList .preset').forEach(x => x.classList.remove('active'));
          el.classList.add('active');
          const p = S.screenPresets.find(x => x.key === el.dataset.key);
          S.conditions = JSON.parse(JSON.stringify(p.conditions || []));
          S.selectedTech = new Set(p.tech || []);
          // 预设可能带技术条件参数（如箱体置信度门槛），必须一起载入
          S.techParams = JSON.parse(JSON.stringify(p.tech_params || {}));
          renderConditions();
          renderTechChips();
        };
      });
      $('#techPick').innerHTML = Object.entries(S.screenTech).map(([k, label]) =>
        `<span class="tech-chip" data-tech="${k}">${esc(label)}</span>`).join('');
      $$('#techPick .tech-chip').forEach(el => {
        el.onclick = () => {
          const k = el.dataset.tech;
          if (S.selectedTech.has(k)) {
            S.selectedTech.delete(k);
            delete S.techParams[k];        // 移除条件时清掉它的参数
          } else {
            S.selectedTech.add(k);
          }
          renderTechChips();
        };
      });
      if (S.screenPresets.length) {
        const p = S.screenPresets[0];
        S.conditions = JSON.parse(JSON.stringify(p.conditions || []));
        S.selectedTech = new Set(p.tech || []);
        S.techParams = JSON.parse(JSON.stringify(p.tech_params || {}));
        $('#presetList .preset')?.classList.add('active');
      }
      renderConditions();
      renderTechChips();
    } catch (e) { toast('选股配置加载失败: ' + e.message, 'err'); }
  }
}

function renderTechChips() {
  $$('#techPick .tech-chip').forEach(el =>
    el.classList.toggle('active', S.selectedTech.has(el.dataset.tech)));
}

function renderConditions() {
  const fields = S.screenFields || {};
  const ops = [['gt', '>'], ['gte', '>='], ['lt', '<'], ['lte', '<='], ['eq', '='], ['ne', '!=']];
  $('#condList').innerHTML = S.conditions.map((c, i) => `
    <div class="cond-row">
      <select data-i="${i}" class="c-field">${Object.entries(fields).map(([k, v]) =>
        `<option value="${k}" ${c.field === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>
      <select data-i="${i}" class="c-op">${ops.map(([k, v]) =>
        `<option value="${k}" ${c.op === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
      <input data-i="${i}" class="c-val" type="number" step="any" value="${c.value}" placeholder="数值">
      <button class="del" data-i="${i}">×</button>
    </div>`).join('') || '<p class="muted">未设置条件，将返回成交额最大的股票</p>';

  $$('#condList .c-field').forEach(el => el.onchange = () => { S.conditions[+el.dataset.i].field = el.value; });
  $$('#condList .c-op').forEach(el => el.onchange = () => { S.conditions[+el.dataset.i].op = el.value; });
  $$('#condList .c-val').forEach(el => el.oninput = () => { S.conditions[+el.dataset.i].value = parseFloat(el.value); });
  $$('#condList .del').forEach(el => el.onclick = () => { S.conditions.splice(+el.dataset.i, 1); renderConditions(); });
}

async function runScreen() {
  const btn = $('#runScreenBtn');
  const hasTech = S.selectedTech.size > 0;
  const scanLimit = hasTech ? 150 : 0;
  btn.disabled = true;
  loading(true, hasTech ? '技术面选股需逐只拉取K线，约需 1-2 分钟…' : '选股中…');
  try {
    const d = await api('/screen', {
      method: 'POST',
      body: {
        conditions: S.conditions,
        tech: Array.from(S.selectedTech),
        tech_params: S.techParams || {},
        exclude_st: $('#excludeST').checked,
        exclude_new: $('#excludeNew').checked,
        order: $('#orderSelect').value,
        limit: 50,
        tech_scan_limit: scanLimit || 150,
      },
    });
    const rows = d.results || [];
    $('#screenMeta').textContent =
      `候选 ${d.total_candidates} 只，技术面扫描 ${d.tech_scanned || 0} 只，命中 ${d.returned} 只`;

    // 条件写错时后端会忽略并回报 —— 必须让用户看见，
    // 否则他会以为"选股条件生效了但结果很宽松"
    const warns = d.warnings || [];
    const wbox = $('#screenWarn');
    if (warns.length) {
      wbox.className = 'warn-box';
      wbox.innerHTML = '⚠️ 以下设置未生效：<br>' +
        warns.map(w => '· ' + esc(w)).join('<br>');
    } else {
      wbox.className = 'hidden';
      wbox.innerHTML = '';
    }
    if (!rows.length) {
      let hint = '没有符合条件的股票。';
      try {
        const st = await api('/snapshot/status');
        if (!st.ready || (st.refreshing && st.refreshing.length)) {
          hint = `全市场数据正在初始化中（已入库 ${st.count || 0} 条），完成后即可选股。` +
                 `当前进度可在「总览」页查看，约需 1-3 分钟。`;
        } else {
          hint = `没有符合条件的股票（已扫描 ${d.total_candidates} 只候选）。可放宽 PE / 成交额等阈值。`;
        }
      } catch (e) { /* 用默认提示 */ }
      $('#screenTable').innerHTML = `<tr><td class="empty">${esc(hint)}</td></tr>`;
    } else {
      const hasTech = rows.some(r => r.tech_rating);
      $('#screenTable').innerHTML = `
        <thead><tr><th>名称</th><th>代码</th><th>最新价</th><th>涨跌幅</th><th>成交额</th>
        <th>换手率</th><th>PE</th><th>PB</th><th>市值</th>${hasTech ? '<th>技术面</th>' : ''}<th>操作</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td><span class="link" data-sym="${r.symbol}">${esc(r.name)}</span></td>
          <td class="num">${esc(r.symbol)}</td>
          <td class="num ${cls(r.pct_change)}">${num(r.price)}</td>
          <td class="num ${cls(r.pct_change)}">${pct(r.pct_change)}</td>
          <td class="num">${money(r.amount)}</td>
          <td class="num">${num(r.turnover_rate)}%</td>
          <td class="num">${num(r.pe)}</td>
          <td class="num">${num(r.pb)}</td>
          <td class="num">${money(r.market_cap)}</td>
          ${hasTech ? `<td>${r.tech_rating ? `<span class="tag">${esc(r.tech_rating)}</span>` : '—'}</td>` : ''}
          <td><button class="mini-btn add-scr" data-sym="${r.symbol}">加自选</button></td>
        </tr>`).join('')}</tbody>`;
    }
    toast('选股完成，命中 ' + rows.length + ' 只', 'ok');
  } catch (e) { toast('选股失败: ' + e.message, 'err'); }
  btn.disabled = false; loading(false);
}

async function saveScreen() {
  const name = prompt('方案名称：', '我的方案 ' + new Date().toLocaleDateString());
  if (!name) return;
  try {
    await api('/screen/saved', { method: 'POST', body: { name, conditions: S.conditions, tech: Array.from(S.selectedTech) } });
    toast('方案已保存', 'ok');
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

/* ---------------- 回测 ---------------- */

async function initBacktest() {
  if (!S.btStrategies) {
    try {
      const d = await api('/backtest/strategies');
      S.btStrategies = d.strategies || [];
      $('#btStrategy').innerHTML = S.btStrategies.map(s =>
        `<option value="${s.key}">${esc(s.name)} — ${esc(s.desc)}</option>`).join('');
      $('#btStrategy').onchange = renderBtParams;
      renderBtParams();
    } catch (e) { toast('策略列表加载失败: ' + e.message, 'err'); }
  }
}

function renderBtParams() {
  const key = $('#btStrategy').value;
  const s = (S.btStrategies || []).find(x => x.key === key);
  if (!s) return;
  const entries = Object.entries(s.params || {});
  $('#btParams').innerHTML = entries.length
    ? entries.map(([k, v]) => `<label>${esc(k)} <input data-p="${k}" type="${typeof v === 'boolean' ? 'text' : 'number'}"
        step="any" value="${v}"></label>`).join('')
    : '<p class="muted">该策略无可调参数</p>';
}

async function runBacktest() {
  const params = {};
  $$('#btParams input').forEach(el => {
    const v = el.value;
    params[el.dataset.p] = (v === 'true' || v === 'false') ? (v === 'true') : parseFloat(v);
  });
  const body = {
    symbol: $('#btSymbol').value.trim(),
    strategy: $('#btStrategy').value,
    params,
    initial_cash: parseFloat($('#btCash').value) || 100000,
    days: parseInt($('#btDays').value) || 500,
    position_size: parseFloat($('#btPos').value) || 1,
    allow_fractional: $('#btFractional').checked,
    compare_all: $('#btCompare').checked,
  };
  if (!body.symbol) { toast('请填写标的代码', 'err'); return; }
  const btn = $('#runBtBtn');
  btn.disabled = true; loading(true, '回测中…');
  try {
    const d = await api('/backtest', { method: 'POST', body });
    $('#btResult').classList.remove('hidden');
    renderBtMetrics(d);
    drawBtChart(d);
    renderBtCompare(d.comparison);
    renderBtTrades(d.trades);
    toast('回测完成', 'ok');
  } catch (e) { toast('回测失败: ' + e.message, 'err'); }
  btn.disabled = false; loading(false);
}

function renderBtMetrics(d) {
  const m = d.metrics || {};
  const cards = [
    ['总收益', m.total_return + '%', m.total_return],
    ['年化收益', m.annual_return + '%', m.annual_return],
    ['基准(持有)', m.benchmark_return + '%', m.benchmark_return],
    ['超额收益', m.excess_return + '%', m.excess_return],
    ['最大回撤', m.max_drawdown + '%', m.max_drawdown],
    ['夏普比率', m.sharpe, m.sharpe],
    ['索提诺', m.sortino, m.sortino],
    ['卡玛比率', m.calmar, m.calmar],
    ['交易次数', m.trade_count, null],
    ['胜率', m.win_rate + '%', m.win_rate - 50],
    ['盈亏比', m.profit_factor == null ? '∞' : m.profit_factor, null],
    ['平均持仓', m.avg_hold_days + '天', null],
    ['总手续费', m.total_fee + '元', null],
    ['持仓占比', m.exposure + '%', null],
  ];
  let html = `<div class="muted" style="margin-bottom:10px">${esc(d.name)}（${esc(d.symbol)}）
    策略「${esc(d.strategy_name)}」 ${esc(d.period.start)} ~ ${esc(d.period.end)}（${d.period.bars} 根K线）</div>`;
  if (m.warnings && m.warnings.length) {
    html += `<div class="down" style="margin-bottom:10px;font-size:12.5px">⚠ ${m.warnings.map(esc).join('<br>')}</div>`;
  }
  html += '<div class="metrics">' + cards.map(([l, v, c]) => `
    <div class="metric"><div class="m-l">${l}</div>
    <div class="m-v ${c === null ? '' : cls(c)}">${v === null || v === undefined || isNaN(v) ? '—' : v}</div></div>`).join('') + '</div>';
  $('#btMetrics').innerHTML = html;
}

function drawBtChart(d) {
  const el = $('#btChart');
  if (!S.btChart) S.btChart = echarts.init(el, 'dark', { renderer: 'canvas' });
  const eq = d.equity || [];
  if (!eq.length) return;
  const dates = eq.map(x => x.date);
  const equity = eq.map(x => x.equity);
  const base = eq.map(x => (x.close / eq[0].close) * (d.metrics.initial_cash || 100000));

  S.btChart.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: '#1c2129', borderColor: '#2a3038',
      textStyle: { color: '#e6edf3', fontSize: 12 } },
    legend: { top: 2, textStyle: { color: '#9aa4b2', fontSize: 11 }, data: ['策略资金', '基准(买入持有)'] },
    grid: { left: 68, right: 18, top: 34, bottom: 44 },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#2a3038' } },
      axisLabel: { color: '#9aa4b2', fontSize: 10 } },
    yAxis: { type: 'value', scale: true, axisLine: { lineStyle: { color: '#2a3038' } },
      axisLabel: { color: '#9aa4b2', fontSize: 10 }, splitLine: { lineStyle: { color: '#1c2129' } } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 6, height: 16,
      textStyle: { color: '#9aa4b2', fontSize: 10 } }],
    series: [
      { name: '策略资金', type: 'line', data: equity, showSymbol: false, smooth: true,
        lineStyle: { width: 2, color: '#3b82f6' }, areaStyle: { color: 'rgba(59,130,246,.12)' } },
      { name: '基准(买入持有)', type: 'line', data: base, showSymbol: false, smooth: true,
        lineStyle: { width: 1.5, color: '#8b949e', type: 'dashed' } },
    ],
  }, true);
}

function renderBtCompare(list) {
  if (!list || !list.length) {
    $('#btCompareTable').innerHTML = '<tr><td class="empty">未启用策略对比</td></tr>';
    return;
  }
  $('#btCompareTable').innerHTML = `
    <thead><tr><th>策略</th><th>总收益</th><th>年化</th><th>最大回撤</th><th>夏普</th>
    <th>胜率</th><th>交易次数</th><th>盈亏比</th></tr></thead>
    <tbody>${list.map(x => x.error
      ? `<tr><td>${esc(x.name)}</td><td colspan="7" class="down">${esc(x.error)}</td></tr>`
      : `<tr>
        <td>${esc(x.name)}</td>
        <td class="num ${cls(x.total_return)}">${x.total_return}%</td>
        <td class="num ${cls(x.annual_return)}">${x.annual_return}%</td>
        <td class="num down">${x.max_drawdown}%</td>
        <td class="num">${x.sharpe}</td>
        <td class="num">${x.win_rate}%</td>
        <td class="num">${x.trade_count}</td>
        <td class="num">${x.profit_factor == null ? '∞' : x.profit_factor}</td></tr>`).join('')}
    </tbody>`;
}

function renderBtTrades(trades) {
  if (!trades || !trades.length) {
    $('#btTrades').innerHTML = '<tr><td class="empty">无交易记录</td></tr>';
    return;
  }
  $('#btTrades').innerHTML = `
    <thead><tr><th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>金额</th>
    <th>费用</th><th>盈亏</th><th>持仓天数</th><th>原因</th></tr></thead>
    <tbody>${trades.slice().reverse().slice(0, 100).map(t => `<tr>
      <td>${esc(t.date)}</td>
      <td class="${t.action === 'buy' ? 'up' : 'down'}">${t.action === 'buy' ? '买入' : '卖出'}</td>
      <td class="num">${num(t.price, 3)}</td>
      <td class="num">${t.shares}</td>
      <td class="num">${money(t.amount)}</td>
      <td class="num">${num(t.fee)}</td>
      <td class="num ${cls(t.pnl)}">${t.pnl == null ? '—' : (t.pnl > 0 ? '+' : '') + num(t.pnl)}</td>
      <td class="num">${t.hold_days == null ? '—' : t.hold_days}</td>
      <td>${esc(t.reason)}</td></tr>`).join('')}</tbody>`;
}

/* ---------------- 提醒 ---------------- */

async function loadAlerts() {
  try {
    const [d, sys] = await Promise.all([api('/alerts'), api('/status')]);
    const rules = d.rule_types || {};

    if (!$('#alRule').dataset.built) {
      $('#alRule').innerHTML = Object.entries(rules).map(([k, v]) =>
        `<option value="${k}">${esc(v)}</option>`).join('');
      $('#alRule').dataset.built = '1';
    }

    const ns = $('#notifyStatus');
    const chans = (sys.notify || []).map(c =>
      `<span class="tag" style="${c.configured ? 'color:var(--down)' : ''}">${esc(c.name)}${c.configured ? ' ✓' : ' 未配置'}</span>`).join(' ');
    ns.innerHTML = '推送渠道：' + chans +
      '<br><span style="font-size:12px">未配置渠道时，提醒仅记录到「触发历史」，不会外发。</span>';

    const alerts = d.alerts || [];
    $('#alertTable').innerHTML = alerts.length ? `
      <thead><tr><th>标的</th><th>规则</th><th>备注</th><th>状态</th><th>冷却</th><th>已触发</th><th>操作</th></tr></thead>
      <tbody>${alerts.map(a => `<tr>
        <td>${esc(a.name)}<br><span class="muted" style="font-size:11.5px">${esc(a.symbol)}</span></td>
        <td>${esc(a.rule_label)}</td>
        <td>${esc(a.message || '—')}</td>
        <td>${a.enabled ? '<span class="up">启用</span>' : '<span class="muted">停用</span>'}</td>
        <td class="num">${a.cooldown}s</td>
        <td class="num">${a.fired_count}</td>
        <td>
          <button class="mini-btn tg-alert" data-id="${a.id}" data-on="${a.enabled ? 0 : 1}">${a.enabled ? '停用' : '启用'}</button>
          <button class="mini-btn del-alert" data-id="${a.id}">删除</button>
        </td></tr>`).join('')}</tbody>`
      : '<tr><td class="empty">还没有提醒规则</td></tr>';

    const events = d.events || [];
    $('#eventTable').innerHTML = events.length ? `
      <thead><tr><th>时间</th><th>标的</th><th>规则</th><th>说明</th><th>触发价</th></tr></thead>
      <tbody>${events.map(e => `<tr>
        <td>${esc(e.created_at)}</td>
        <td>${esc(e.name)} <span class="muted">${esc(e.symbol)}</span></td>
        <td>${esc(e.rule_type)}</td>
        <td>${esc(e.message)}</td>
        <td class="num">${num(e.price)}</td></tr>`).join('')}</tbody>`
      : '<tr><td class="empty">暂无触发记录</td></tr>';
  } catch (e) { toast('提醒加载失败: ' + e.message, 'err'); }
}

async function addAlert() {
  const symbol = $('#alSymbol').value.trim();
  const rule = $('#alRule').value;
  if (!symbol) { toast('请填写标的代码', 'err'); return; }
  const v = parseFloat($('#alValue').value);
  const params = {};
  if (rule === 'price_above' || rule === 'price_below' || rule === 'pct_up' || rule === 'pct_down') {
    if (isNaN(v)) { toast('请填写阈值', 'err'); return; }
    params.value = v;
  } else if (rule === 'vol_surge') {
    params.value = isNaN(v) ? 2.0 : v;
  } else if (rule === 'near_high' || rule === 'near_low') {
    params.n = isNaN(v) ? 60 : v;
    params.within = 2.0;
  } else if (rule === 'golden_cross' || rule === 'death_cross') {
    params.fast = 5; params.slow = 20;
  }
  try {
    await api('/alerts', { method: 'POST', body: {
      symbol, rule_type: rule, params,
      message: $('#alMessage').value.trim(),
      cooldown: parseInt($('#alCooldown').value) || 1800,
    }});
    toast('提醒已添加', 'ok');
    $('#alValue').value = ''; $('#alMessage').value = '';
    loadAlerts();
  } catch (e) { toast('添加失败: ' + e.message, 'err'); }
}

/* ---------------- 设置 ---------------- */

async function runSelftest() {
  const box = $('#selftestResult');
  const btn = $('#selftestBtn');
  btn.disabled = true;
  box.innerHTML = '<span class="muted">自检进行中，需 30-60 秒…</span>';
  try {
    const d = await api('/selftest');
    const icon = { pass: '✅', warn: '⚠️', fail: '❌' }[d.verdict] || '';
    const groups = { storage: '存储与环境', source: '数据源', market: '行情与财务',
                     engine: '计算引擎', ai: 'AI 简报', ops: '运维' };
    let html = `<div class="metric" style="margin-bottom:12px">
        <div class="m-l">自检结果 · 通过 ${d.passed}/${d.total} · ${d.elapsed_ms}ms</div>
        <div class="m-v ${d.verdict === 'fail' ? 'down' : (d.verdict === 'warn' ? '' : 'up')}">
          ${icon} ${esc(d.summary)}</div></div>`;
    let cur = null;
    html += '<div class="table-wrap"><table><tbody>';
    for (const c of d.checks) {
      if (c.group !== cur) {
        cur = c.group;
        html += `<tr><td colspan="3" style="text-align:left;background:var(--bg3)">
          <strong>${esc(groups[cur] || cur)}</strong></td></tr>`;
      }
      const mark = c.ok ? '<span class="down">✓</span>'
        : (c.level === 'core' ? '<span class="up">✗</span>' : '<span class="muted">○</span>');
      html += `<tr><td style="text-align:left;white-space:nowrap">${mark} ${esc(c.name)}</td>
        <td style="text-align:left;white-space:normal;color:var(--fg2)">${esc(c.detail)}</td>
        <td class="num muted">${c.ms}ms</td></tr>`;
    }
    html += '</tbody></table></div>';
    box.innerHTML = html;
    toast(d.verdict === 'pass' ? '自检全部通过' : d.summary,
          d.verdict === 'fail' ? 'err' : 'ok');
  } catch (e) {
    box.innerHTML = '<span class="down">自检失败: ' + esc(e.message) + '</span>';
  }
  btn.disabled = false;
}

async function loadSettings() {
  try {
    const [d, sst] = await Promise.all([api('/status'), api('/snapshot/status')]);
    const m = d.market || {}, s = d.snapshot || {}, db = d.db || {};
    const jobs = (d.scheduler && d.scheduler.jobs) || [];
    let completeness = '未知';
    if (sst.expected) {
      completeness = sst.complete
        ? `A股完整 (${sst.a_share_count || sst.expected}/${sst.expected})`
        : `A股不完整 (${sst.a_share_count || 0}/${sst.expected}，失败 ${sst.failed_pages} 页) — 再点一次刷新可补齐`;
    }
    $('#sysStatus').innerHTML = `
      <div class="kv-list">
        <div class="kv"><span class="k">当前时间</span><span class="v">${esc(m.now)}</span></div>
        <div class="kv"><span class="k">A股状态</span><span class="v">${esc(m.session)}</span></div>
        <div class="kv"><span class="k">交易日</span><span class="v">${m.is_trading_day ? '是' : '否'}</span></div>
        <div class="kv"><span class="k">快照条数</span><span class="v">${s.count || 0}</span></div>
        <div class="kv"><span class="k">快照更新时间</span><span class="v">${esc(s.a_share || '未建立')}</span></div>
        <div class="kv"><span class="k">数据完整性</span><span class="v ${sst.complete === false ? 'up' : ''}">${esc(completeness)}</span></div>
        <div class="kv"><span class="k">本地K线</span><span class="v">${db.kline_rows || 0} 条</span></div>
        <div class="kv"><span class="k">自选股</span><span class="v">${db.watch_count || 0} 只</span></div>
        <div class="kv"><span class="k">启用提醒</span><span class="v">${db.alert_count || 0} 条</span></div>
        <div class="kv"><span class="k">调度器</span><span class="v">${d.scheduler && d.scheduler.enabled ? '已启用' : '已禁用'}</span></div>
        <div class="kv"><span class="k">AI 简报</span><span class="v">${d.ai && d.ai.enabled ? esc(d.ai.model) : '本地规则引擎'}</span></div>
        <div class="kv"><span class="k">数据库</span><span class="v" style="font-size:11px">${esc(db.path)}</span></div>
      </div>
      ${jobs.length ? `<h3 class="mt">定时任务</h3><div class="table-wrap"><table>
        <thead><tr><th>任务</th><th>下次执行</th></tr></thead>
        <tbody>${jobs.map(j => `<tr><td>${esc(j.id)}</td><td class="num">${esc(j.next_run || '—')}</td></tr>`).join('')}</tbody>
      </table></div>` : ''}`;

    const logs = await api('/tasks/logs?limit=40');
    const rows = logs.logs || [];    $('#taskTable').innerHTML = rows.length ? `
      <thead><tr><th>时间</th><th>任务</th><th>状态</th><th>详情</th><th>耗时</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td>${esc(r.created_at)}</td><td>${esc(r.task)}</td>
        <td class="${r.status === 'ok' ? 'down' : (r.status === 'error' ? 'up' : '')}">${esc(r.status)}</td>
        <td style="text-align:left;white-space:normal">${esc(r.detail)}</td>
        <td class="num">${num(r.duration, 2)}s</td></tr>`).join('')}</tbody>`
      : '<tr><td class="empty">暂无任务日志</td></tr>';
  } catch (e) { toast('状态加载失败: ' + e.message, 'err'); }
}

async function runTask(task) {
  if (task === 'snapshot') {
    if (!confirm('刷新全市场快照需要 1-3 分钟，期间界面可能较慢。确定继续？')) return;
  }
  loading(true, '执行中…');
  try {
    const d = await api('/tasks/run', { method: 'POST', body: { task } });
    toast(d.message || '已执行', 'ok');
    loadSettings();
  } catch (e) { toast('执行失败: ' + e.message, 'err'); }
  loading(false);
}

/* ---------------- 顶栏 ---------------- */

function renderMarketStatus(st) {
  const el = $('#marketStatus');
  if (!st) return;
  el.textContent = (st.a_share_open ? 'A股 ' + st.session : st.session) +
    (st.hk_open ? ' · 港股交易中' : '');
  el.className = 'market-status ' + (st.a_share_open ? 'open' : 'closed');
}

function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  $('#clock').textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* ---------------- 事件绑定 ---------------- */


/* ============ AI 全流程（6 步 13 提示词）============ */
const FLOW_BADGE = {
  grounded: { cls: 'up',   text: '有数据', tip: '结论完全由系统真实数据算出' },
  partial:  { cls: 'flat', text: '部分',   tip: '部分有数据，缺口已标注' },
  no_data:  { cls: 'down', text: '无数据', tip: '本系统没有这类数据，不生成结论' },
};

/* 极简 markdown 渲染：只处理 **加粗**，且先转义再替换，避免 XSS。
   后端文案里用了 ** 强调，直接显示会看到星号。 */
function mdInline(t) {
  return esc(t || '').replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
}

/* 数据截止日期的展示。
   把日期原样显示是不够的 —— 用户需要知道「这条数据有多新」。
   实测行业对比那三张榜单的报告期是 2025-12-31（东财按年报口径发布），
   比当前时间滞后 9 个月；财报最新一期是 2026 中报。
   不说明的话，用户会以为看到的同业数据是最新的。 */
function fmtAsOf(v) {
  const t = String(v || '');
  if (!t) return '—';
  if (/^\d{8}$/.test(t)) {
    return `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)}（行情实时）`;
  }
  const m = t.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return t;
  const today = new Date();
  const d = new Date(`${t}T00:00:00`);
  const days = Math.floor((today - d) / 86400000);
  if (days <= 0) return `${t}（今天）`;
  if (days === 1) return `${t}（昨天）`;
  if (days < 45) return `${t}（${days} 天前）`;
  const months = Math.round(days / 30);
  const stale = days > 120;
  return `${t}（约 ${months} 个月前${stale ? '，注意数据较旧' : ''}）`;
}

function fmtNum(v, digits) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits === undefined ? 2 : digits) : String(v);
}

function fmtPct(v, digits) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return (n >= 0 ? '+' : '') + n.toFixed(digits === undefined ? 2 : digits) + '%';
}

/* 把某一项的结构化数据渲染成表格（有数据项才需要） */
function renderFlowData(id, d) {
  if (!d) return '';
  if (id === 'p3' && d.years) {
    const rows = d.years.map(y => `<tr>
      <td>${esc(y.year)}</td>
      <td class="num">${esc(y.revenue_text)}</td>
      <td class="num">${fmtPct(y.rev_yoy)}</td>
      <td class="num">${esc(y.net_profit_text)}</td>
      <td class="num">${fmtPct(y.np_yoy)}</td>
      <td class="num">${fmtNum(y.roe)}%</td>
      <td class="num">${fmtNum(y.net_margin)}%</td></tr>`).join('');
    const lr = d.long_range || {};
    return `<div class="table-wrap"><table>
      <thead><tr><th>年度</th><th>营收</th><th>营收同比</th><th>净利润</th>
        <th>净利同比</th><th>ROE</th><th>净利率</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <div class="muted mt-sm" style="font-size:12px">
        参考更长区间 ${esc(lr.from)}→${esc(lr.to)}（共 ${lr.years} 年）年均复合：
        营收 ${fmtPct(lr.rev_cagr)}、净利润 ${fmtPct(lr.np_cagr)}</div>`;
  }
  if (id === 'p4' && d.history) {
    const h = d.history;
    return `<div class="kv-list">
      <div class="kv"><span class="k">当前 PE(TTM)</span><span class="v">${fmtNum(d.current_pe)}</span></div>
      <div class="kv"><span class="k">历史分位</span><span class="v"><b>${fmtNum(d.percentile,1)}%</b> · ${esc(d.band)}</span></div>
      <div class="kv"><span class="k">PE 区间</span><span class="v">${fmtNum(h.min)} ~ ${fmtNum(h.max)}（中位 ${fmtNum(h.median)}）</span></div>
      <div class="kv"><span class="k">样本</span><span class="v">${h.days} 个交易日 · ${esc(h.start_date)} ~ ${esc(h.end_date)}</span></div>
      <div class="kv"><span class="k">价格分位</span><span class="v">${d.price_percentile === null ? '—' : fmtNum(d.price_percentile,1) + '%'}</span></div>
    </div>`;
  }
  if (id === 'p5') {
    const o = d.own || {}, m = d.market_rank || {};
    return `<div class="kv-list">
      <div class="kv"><span class="k">本股 PB</span><span class="v">${fmtNum(o.pb)}</span></div>
      <div class="kv"><span class="k">本股 ROE</span><span class="v">${fmtNum(o.roe)}%</span></div>
      <div class="kv"><span class="k">本股 PE(TTM)</span><span class="v">${fmtNum(o.pe)}</span></div>
      ${m.total ? `<div class="kv"><span class="k">全市场 PB 分位</span><span class="v">${fmtNum(m.percentile,1)}%（${m.total} 只，中位 ${fmtNum(m.median)}）</span></div>` : ''}
    </div>`;
  }
  if (id === 'p6') {
    return `<div class="kv-list">
      <div class="kv"><span class="k">区间</span><span class="v">${esc(d.range || '—')}</span></div>
      <div class="kv"><span class="k">净利润复合增速</span><span class="v">${fmtPct(d.profit_cagr)}</span></div>
      <div class="kv"><span class="k">营收复合增速</span><span class="v">${fmtPct(d.rev_cagr)}</span></div>
      <div class="kv"><span class="k">PEG</span><span class="v"><b>${fmtNum(d.peg)}</b>（营收口径 ${fmtNum(d.rev_peg)}）</span></div>
      <div class="kv"><span class="k">结论</span><span class="v">${esc(d.verdict)}</span></div>
    </div>`;
  }
  if (id === 'p7' && d.checks) {
    const rows = d.checks.map(c => `<tr>
      <td>${esc(c.item)}</td><td>${esc(c.value)}</td>
      <td class="${c.flag ? 'down' : ''}">${c.flag ? '⚠ 需关注' : '正常'}</td>
      <td class="muted">${esc(c.read)}</td></tr>`).join('');
    return `<div class="table-wrap"><table>
      <thead><tr><th>侧面检查</th><th>数据</th><th>判定</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }
  if (id === 'p1' && d.segments) {
    let html = '';
    for (const [kind, items] of Object.entries(d.segments)) {
      if (!items || !items.length) continue;
      const rows = items.map(it => `<tr>
        <td>${esc(it.name)}</td>
        <td class="num">${it.income === null ? '—' : (it.income / 1e8).toFixed(2) + ' 亿'}</td>
        <td class="num">${it.ratio === null ? '—' : (it.ratio * 100).toFixed(1) + '%'}</td>
        <td class="num">${it.gross_margin === null ? '—' : (it.gross_margin * 100).toFixed(1) + '%'}</td>
      </tr>`).join('');
      html += `<h3 class="mt" style="font-size:12px">${esc(kind)}</h3>
        <div class="table-wrap"><table>
        <thead><tr><th>项目</th><th>收入</th><th>占比</th><th>毛利率</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    }
    return html || '';
  }
  if (id === 'p2') {
    const tbl = (rows, cols) => rows && rows.length ? `<div class="table-wrap"><table>
      <thead><tr>${cols.map(c => `<th>${esc(c[0])}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(r => `<tr>${cols.map(c => {
        const v = r[c[1]];
        return `<td${c[2] ? ' class="num"' : ''}>${v === null || v === undefined ? '—'
          : (c[2] ? Number(v).toFixed(c[3] === undefined ? 1 : c[3]) : esc(v))}</td>`;
      }).join('')}</tr>`).join('')}</tbody></table></div>` : '';
    let html = '';
    if (d.growth && d.growth.length) {
      html += '<h3 class="mt" style="font-size:12px">成长性榜单（营收同比）</h3>'
        + tbl(d.growth.slice(0, 5), [['公司', 'name'], ['营收同比%', 'revenue_yoy', 1], ['净利同比%', 'profit_yoy', 1], ['行业排名', 'rank', 1, 0]]);
    }
    if (d.valuation && d.valuation.length) {
      html += '<h3 class="mt" style="font-size:12px">估值榜单</h3>'
        + tbl(d.valuation.slice(0, 5), [['公司', 'name'], ['PE(TTM)', 'pe_ttm', 1], ['PB', 'pb', 1, 2]]);
    }
    if (d.finance && d.finance.length) {
      html += '<h3 class="mt" style="font-size:12px">财务榜单（ROE）</h3>'
        + tbl(d.finance.slice(0, 5), [['公司', 'name'], ['ROE%', 'roe_avg', 1], ['净利率%', 'net_margin', 1]]);
    }
    return html;
  }
  if (id === 'p9') {
    const list = (arr, nameKey, kind) => (arr || []).slice(0, 5).map(x => `<tr>
      <td>${esc(x.date)}</td><td>${esc(x[nameKey])}</td>
      <td>${esc(x.direction || (x.shares > 0 ? '增持' : '减持'))}</td>
      <td class="num">${Math.abs(x.shares).toLocaleString()} 股</td></tr>`).join('');
    const rows = list(d.executives, 'person') + list(d.holders, 'holder');
    if (!rows) return '<div class="muted">近一年无增减持记录</div>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>日期</th><th>姓名/股东</th><th>方向</th><th>数量</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }
  if (id === 'p10') {
    return `<div class="kv-list">
      <div class="kv"><span class="k">现价</span><span class="v">${fmtNum(d.price)}</span></div>
      <div class="kv"><span class="k">MA5</span><span class="v">${fmtNum(d.ma5)} · ${esc(d.rel && d.rel.ma5)}</span></div>
      <div class="kv"><span class="k">MA20</span><span class="v">${fmtNum(d.ma20)} · ${esc(d.rel && d.rel.ma20)}</span></div>
      <div class="kv"><span class="k">MA60</span><span class="v">${fmtNum(d.ma60)} · ${esc(d.rel && d.rel.ma60)}</span></div>
      <div class="kv"><span class="k">通道判定</span><span class="v">${esc(d.channel)}</span></div>
    </div>`;
  }
  if (id === 'p11') {
    const lv = (arr, kind) => (arr || []).map(x =>
      `<div class="kv"><span class="k">${kind}</span><span class="v">${fmtNum(x.price)}
        ${x.touches ? `（被触碰 ${x.touches} 次，最近 ${esc(x.last_date)}）`
                    : `（${esc(x.basis)}）`}</span></div>`).join('');
    return `<div class="kv-list">${lv(d.supports, '支撑位')}${lv(d.resistances, '压力位')}</div>`;
  }
  if (id === 'p12' && d.cases) {
    const rows = d.cases.map(c => `<tr>
      <td><b>${esc(c.name)}</b></td>
      <td class="num">${esc(String(c.target))}</td>
      <td class="num">${esc(c.change)}</td>
      <td class="num">${fmtNum(c.prob, 1)}%</td>
      <td>${mdInline(c.desc)}</td></tr>`).join('');
    return `<div class="table-wrap"><table>
      <thead><tr><th>情景</th><th>目标价</th><th>涨跌</th><th>概率</th><th>说明</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <div class="kv-list mt">
        ${d.cases.map(c => `<div class="kv"><span class="k">${esc(c.name)}的应对</span>
          <span class="v">${mdInline(c.plan)}</span></div>`).join('')}
      </div>
      <div class="muted mt-sm" style="font-size:12px">
        近 60 日日均波动 ${fmtNum(d.daily_vol_pct)}%，推到 ${d.horizon_days} 个交易日
        一个标准差为 ${fmtNum(d.sigma_3m_pct, 1)}%。${esc(d.caveat)}</div>`;
  }
  if (id === 'p13') {
    return `<div class="kv-list">
      <div class="kv"><span class="k">综合评分</span><span class="v">${fmtNum(d.score)} / 10 · ${esc(d.verdict)}</span></div>
      <div class="kv"><span class="k">倾向</span><span class="v">${esc(d.action)}</span></div>
      <div class="kv"><span class="k">参考仓位</span><span class="v">${esc(d.position)}（${esc(d.position_reason)}）</span></div>
      <div class="kv"><span class="k">字数</span><span class="v">${d.chars} 字</span></div>
    </div>`;
  }
  return '';
}

function renderFlow(d) {
  const sm = $('#flowSummary');
  const el = $('#flowSteps');
  if (!sm || !el) return;
  if (!d || !d.steps) { el.innerHTML = '<div class="card muted">分析失败</div>'; return; }

  const s = d.summary;
  sm.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>${esc(d.name)} <span class="muted">${esc(d.symbol)}</span></h3>
        <span class="${d.pct_change >= 0 ? 'up' : 'down'}">${fmtNum(d.price)}
          ${fmtPct(d.pct_change)}</span>
      </div>
      <div class="kv-list">
        <div class="kv"><span class="k">13 项完成情况</span><span class="v">
          <span class="up">有数据 ${s.grounded}</span> ·
          <span class="flat">部分 ${s.partial}</span> ·
          <span class="down">无数据 ${s.no_data}</span></span></div>
        <div class="kv"><span class="k">生成时间</span><span class="v">${esc(d.generated_at)}</span></div>
      </div>
      <div class="warn-box" style="margin-top:10px">
        <span style="font-size:12px;opacity:.9">${mdInline(d.disclaimer)}</span>
      </div>
    </div>`;

  el.innerHTML = d.steps.map(st => `
    <div class="card">
      <div class="card-head">
        <h3>${esc(st.title)}</h3>
        <span class="muted" style="font-size:12px">
          ${st.count} 项 · 有数据 ${st.grounded} / 部分 ${st.partial} / 无 ${st.no_data}</span>
      </div>
      ${st.answers.map(a => {
        const b = FLOW_BADGE[a.status] || FLOW_BADGE.partial;
        const gap = a.gap || {};
        return `
        <div style="border-top:1px solid var(--border);padding:12px 0">
          <div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:6px">
            <span class="muted" style="font-size:12px;flex:0 0 auto">#${a.seq}</span>
            <b style="flex:1 1 auto">${esc(a.title)}</b>
            <span class="${b.cls}" style="flex:0 0 auto;font-size:12px"
                  title="${esc(b.tip)}">${b.text}</span>
          </div>
          <div class="muted" style="font-size:12px;padding-left:20px;border-left:2px solid var(--border);margin-bottom:8px">
            ${esc(a.prompt)}
          </div>
          <div style="padding-left:20px">
            ${a.text ? `<div style="margin-bottom:8px">${mdInline(a.text)}</div>` : ''}
            ${renderFlowData(a.id, a.data)}
            ${a.status === 'no_data' ? `
              <div class="warn-box">
                <b>本系统无此数据</b><br>
                <span style="font-size:12px">${esc(gap.reason || '')}</span>
                ${(gap.where || []).length ? `<div style="font-size:12px;margin-top:6px">
                  可从这里查：<ul style="margin:4px 0 0 18px">
                  ${gap.where.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}
              </div>` : ''}
            ${(gap.where || []).length && a.status !== 'no_data' ? `
              <div class="muted mt-sm" style="font-size:12px">
                缺口：${esc(gap.reason || '')}
                <ul style="margin:4px 0 0 18px">${gap.where.map(w => `<li>${esc(w)}</li>`).join('')}</ul>
              </div>` : ''}
            ${a.source ? `<div class="muted mt-sm" style="font-size:11px">
              数据来源：${esc(a.source)}</div>` : ''}
            ${a.as_of ? `<div class="mt-sm" style="font-size:11px">
              <span class="muted">数据截止：</span>${esc(fmtAsOf(a.as_of))}</div>` : ''}
            ${a.method_caveat ? `<div class="muted" style="font-size:11px">
              口径：${esc(a.method_caveat)}</div>` : ''}
          </div>
        </div>`;
      }).join('')}
    </div>`).join('') + `
    <div class="card">
      <div class="warn-box">
        <b>关于这份分析的边界</b>
        <div class="mt-sm" style="font-size:12px;opacity:.9">${mdInline(d.disclaimer)}</div>
        <div class="mt-sm" style="font-size:12px;opacity:.9">
          「有数据」项由系统按固定算法从真实数据算出，口径已逐项标注；
          「无数据」项本系统确实取不到，请按给出的途径自行补充。
          所有内容<b>不构成投资建议</b>，据此操作风险自负。
        </div>
      </div>
    </div>`;
}

async function runFlow(sym) {
  const el = $('#flowSteps');
  const hint = $('#flowHint');
  if (!sym) { toast('请先输入标的', 'err'); return; }
  if (hint) hint.textContent = '分析中，首次取数较慢…';
  if (el) el.innerHTML = '<div class="card muted">加载中…</div>';
  try {
    const d = await api('/flow/' + encodeURIComponent(sym));
    S.flowData = d;
    renderFlow(d);
    setHash('flow', sym);
    if (hint) hint.textContent = '';
  } catch (e) {
    if (el) el.innerHTML = `<div class="card muted">分析失败：${esc(e.message)}</div>`;
    if (hint) hint.textContent = '';
  }
}

async function copyFlowPack() {
  const sym = (S.flowData && S.flowData.symbol) || $('#flowSymbol').value.trim();
  if (!sym) { toast('请先输入标的', 'err'); return; }
  try {
    const d = await api('/flow/' + encodeURIComponent(sym) + '/pack');
    await copyText(d.text);
    toast(`已复制数据包（${d.chars} 字），可直接粘给任意 AI`, 'ok');
  } catch (e) {
    toast('复制失败：' + e.message, 'err');
  }
}

function bind() {
  $('#tabs').onclick = (e) => {
    const t = e.target.closest('.tab');
    if (t) showView(t.dataset.view);
  };
  $('#refreshBtn').onclick = () => { render(); loadSysStatus(); };

  $('#moverSeg').onclick = (e) => {
    const b = e.target.closest('.seg-btn');
    if (b) loadMovers(b.dataset.kind);
  };

  const flowBtn = $('#detailFlowBtn');
  if (flowBtn) {
    flowBtn.onclick = () => {
      if (!S.detailSymbol) { toast('请先查询一只股票', 'err'); return; }
      if ($('#flowSymbol')) $('#flowSymbol').value = S.detailSymbol;
      showView('flow');
      runFlow(S.detailSymbol);
    };
  }

  $('#flowRunBtn').onclick = () => {
    const v = ($('#flowSymbol').value || '').trim().toUpperCase();
    runFlow(v);
  };
  $('#flowSymbol').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = ($('#flowSymbol').value || '').trim().toUpperCase();
      runFlow(v);
    }
  });
  $('#flowPackBtn').onclick = () => copyFlowPack();

  // 箱体模式切换：面板每次渲染都会重建，所以用事件委托绑在外层容器上
  $('#boxPanel').addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn[data-boxmode]');
    if (!b) return;
    const m = b.dataset.boxmode;
    if (m === S.boxMode) return;
    S.boxMode = m;
    loadBox();
  });

  // 点击标的跳转详情（事件委托）
  document.addEventListener('click', (e) => {
    const link = e.target.closest('.link[data-sym]');
    if (link) { showView('detail', link.dataset.sym); return; }

    const add = e.target.closest('.add-scr');
    if (add) {
      api('/watchlist', { method: 'POST', body: { symbol: add.dataset.sym, group: S.group || '默认分组' } })
        .then(r => toast('已加入自选: ' + r.name, 'ok'))
        .catch(err => toast(err.message, 'err'));
      return;
    }
    const del = e.target.closest('.del-watch');
    if (del) {
      api('/watchlist/' + encodeURIComponent(del.dataset.sym), { method: 'DELETE' })
        .then(() => { toast('已删除', 'ok'); loadWatchlist(); })
        .catch(err => toast(err.message, 'err'));
      return;
    }
    const tg = e.target.closest('.tg-alert');
    if (tg) {
      api(`/alerts/${tg.dataset.id}/toggle`, { method: 'POST', body: { enabled: tg.dataset.on === '1' } })
        .then(() => loadAlerts()).catch(err => toast(err.message, 'err'));
      return;
    }
    const da = e.target.closest('.del-alert');
    if (da) {
      api('/alerts/' + da.dataset.id, { method: 'DELETE' })
        .then(() => { toast('已删除', 'ok'); loadAlerts(); }).catch(err => toast(err.message, 'err'));
      return;
    }
    const mr = e.target.closest('[data-action="market-report"]');
    if (mr) { marketReport(); return; }
    const tk = e.target.closest('[data-task]');
    if (tk) { runTask(tk.dataset.task); return; }
  });

  // 自选
  $('#watchAddBtn').onclick = addWatch;
  $('#watchInput').onkeydown = (e) => { if (e.key === 'Enter') addWatch(); };
  let wt = null;
  $('#watchInput').oninput = () => {
    clearTimeout(wt);
    wt = setTimeout(() => searchSuggest($('#watchInput'), $('#watchSuggest'), (s) => {
      $('#watchInput').value = s; addWatch();
    }), 320);
  };
  $('#groupSelect').onchange = () => { S.group = $('#groupSelect').value || null; loadWatchlist(); };
  $('#withTech').onchange = () => loadWatchlist();
  $('#autoRefresh').onchange = () => {
    clearInterval(S.watchTimer); S.watchTimer = null;
    if ($('#autoRefresh').checked) loadWatchlist();
  };

  // 个股
  $('#detailGoBtn').onclick = () => {
    const v = $('#detailInput').value.trim();
    if (v) loadDetail(v);
  };
  $('#detailInput').onkeydown = (e) => {
    if (e.key === 'Enter') { const v = $('#detailInput').value.trim(); if (v) { $('#detailSuggest').innerHTML = ''; loadDetail(v); } }
  };
  let dt = null;
  $('#detailInput').oninput = () => {
    clearTimeout(dt);
    dt = setTimeout(() => searchSuggest($('#detailInput'), $('#detailSuggest'), (s) => loadDetail(s)), 320);
  };
  $('#periodSeg').onclick = (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    $$('#periodSeg .seg-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    S.period = b.dataset.period;
    // 分时图没有 MACD/KDJ 副图，隐藏选择器避免误导
    const subSeg = $('#subSeg');
    if (subSeg) subSeg.style.display = (S.period === 'trend') ? 'none' : 'inline-flex';
    loadKline();
  };
  $('#subSeg').onclick = (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    $$('#subSeg .seg-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    S.sub = b.dataset.sub;
    loadKline();
  };
  $('#aiReportBtn').onclick = genAiReport;

  // 选股
  $('#addCondBtn').onclick = () => {
    const f = Object.keys(S.screenFields || {})[0] || 'pe';
    S.conditions.push({ field: f, op: 'lt', value: 30 });
    renderConditions();
  };
  $('#runScreenBtn').onclick = runScreen;
  $('#saveScreenBtn').onclick = saveScreen;

  // 回测
  $('#runBtBtn').onclick = runBacktest;

  // 自检
  $('#selftestBtn').onclick = runSelftest;

  // 提醒
  $('#addAlertBtn').onclick = addAlert;
  $('#testNotifyBtn').onclick = async () => {
    loading(true, '发送测试消息…');
    try {
      const d = await api('/alerts/test-notify', { method: 'POST' });
      if (!d.ok) toast(d.detail || '推送失败，请检查 .env 配置', 'err');
      else toast('测试消息已发送: ' + Object.entries(d.results).map(([k, v]) => k + (v ? '✓' : '✗')).join(' '), 'ok');
    } catch (e) { toast(e.message, 'err'); }
    loading(false);
  };
  $('#checkAlertBtn').onclick = async () => {
    loading(true, '巡检中…');
    try {
      const d = await api('/alerts/check', { method: 'POST' });
      toast(d.count ? `触发 ${d.count} 条提醒` : '本次未触发提醒', d.count ? 'ok' : '');
      loadAlerts();
    } catch (e) { toast(e.message, 'err'); }
    loading(false);
  };

  // 窗口缩放重绘图表
  let rt = null;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      if (S.klineChart) S.klineChart.resize();
      if (S.btChart) S.btChart.resize();
    }, 200);
  });
}

async function loadSysStatus() {
  try {
    const d = await api('/status');
    renderMarketStatus(d.market);
  } catch (e) { /* 静默 */ }
}

/* ---------------- 启动 ---------------- */

document.addEventListener('DOMContentLoaded', () => {
  bind();
  tickClock();
  setInterval(tickClock, 1000);
  loadSysStatus();
  setInterval(loadSysStatus, 30000);
  applyRoute();                    // 按地址栏还原，刷新不再丢状态
  window.addEventListener('hashchange', () => {
    if (S.suppressHash) return;    // 自己改的 hash 不重复处理
    applyRoute();
  });
});
