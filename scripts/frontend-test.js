/**
 * StockLab 前端冒烟测试
 *
 * 目的：把真实的前端 JS 跑起来，验证界面不是"接口通但页面白屏"。
 * 之前每一轮都只验证了 API，前端从未真正执行过 —— 一个 JS 运行时错误
 * 就能让整个界面不可用，而接口测试完全发现不了。
 *
 * 做法：
 *   1. 从运行中的服务拉真实的 index.html 与 app.js / style.css
 *   2. 在 jsdom 里执行，只 stub 掉 ECharts（jsdom 没有 canvas）
 *   3. 用真实的 fetch 打真实的 API
 *   4. 逐个切换标签页、触发主要交互，收集所有 JS 异常
 */
'use strict';

const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.env.BASE || 'http://127.0.0.1:8787';

let pass = 0, fail = 0;
const errors = [];

function ok(name, detail) {
  pass++;
  console.log(`  \x1b[32m✓\x1b[0m ${name}${detail ? '  ' + detail : ''}`);
}
function bad(name, detail) {
  fail++;
  console.log(`  \x1b[31m✗\x1b[0m ${name}${detail ? '  ' + detail : ''}`);
}

(async function main() {
  console.log('══════════════════════════════════════════════════════');
  console.log(`  StockLab 前端冒烟测试  →  ${BASE}`);
  console.log('══════════════════════════════════════════════════════\n');

  // ---- 1. 拉取真实资源 ----
  const [html, appJs, css] = await Promise.all([
    fetch(`${BASE}/`).then(r => r.text()),
    fetch(`${BASE}/static/js/app.js`).then(r => r.text()),
    fetch(`${BASE}/static/css/style.css`).then(r => r.text()),
  ]);
  console.log(`  资源: HTML ${html.length}B / JS ${appJs.length}B / CSS ${css.length}B\n`);

  // 捕获页面内的 JS 错误
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push('jsdomError: ' + (e.message || e)));
  virtualConsole.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
  // 页面里的 console.log 不打印，避免刷屏

  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;

  // ---- 2. 注入必要的浏览器 API ----
  // 记录箱体请求，供「默认走自适应」「能切回固定窗口」两条断言使用
  window.__boxCalls = [];
  window.fetch = (u, o) => {
    const url = typeof u === 'string' && u.startsWith('/') ? BASE + u : u;
    if (typeof url === 'string' && url.includes('/api/box/')) window.__boxCalls.push(url);
    return fetch(url, o);
  };
  window.matchMedia = window.matchMedia || (() => ({
    matches: false, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
  }));

  // ECharts stub：jsdom 无 canvas，只记录调用
  const chartCalls = { init: 0, setOption: 0, resize: 0, last: null, all: [] };
  window.echarts = {
    init() {
      chartCalls.init++;
      return {
        setOption(o) { chartCalls.setOption++; chartCalls.last = o; chartCalls.all.push(o); },
        hideLoading() {}, showLoading() {},
        resize() { chartCalls.resize++; },
        dispose() {}, clear() {},
      };
    },
  };
  window.alert = () => {};
  window.confirm = () => true;
  window.prompt = () => '测试方案';

  // 未捕获异常也算失败
  window.addEventListener('error', (e) => errors.push('window.onerror: ' + (e.message || e)));
  window.addEventListener('unhandledrejection',
    (e) => errors.push('unhandledrejection: ' + (e.reason && e.reason.message || e.reason)));

  // ---- 3. 执行真实 app.js ----
  try {
    window.eval(appJs);
  } catch (e) {
    bad('app.js 执行', e.message);
    process.exit(1);
  }
  ok('app.js 解析并执行');

  const $ = (s) => window.document.querySelector(s);
  const $$ = (s) => Array.from(window.document.querySelectorAll(s));

  // 触发 DOMContentLoaded（jsdom 已自动触发，但 eval 在之后，手动再跑一次初始化）
  window.document.dispatchEvent(new window.Event('DOMContentLoaded'));

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  // 等条件成立，而不是死等固定时长。
  // 固定 sleep 会因机器/网络快慢而误报（实测 K线 600 根 + 27 个指标
  // 约 210KB，偶发超过 6 秒，于是把"图表没画"误判成 bug）。
  const waitFor = async (fn, timeout = 30000, step = 250) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (fn()) return true; } catch (e) { /* 继续等 */ }
      await sleep(step);
    }
    return false;
  };

  // ---- 4. 检查静态结构 ----
  const tabs = $$('.tab').map(t => t.dataset.view);
  tabs.length === 7 ? ok('标签页渲染', tabs.join(',')) : bad('标签页数量', tabs.length);

  const viewsPresent = tabs.every(v => $(`#view-${v}`));
  viewsPresent ? ok('每个标签都有对应视图容器') : bad('缺少视图容器');

  ['indices', 'breadth', 'moverTable', 'watchTable', 'klineChart', 'screenTable',
   'btMetrics', 'alertTable', 'sysStatus', 'selftestBtn', 'screenWarn']
    .every(id => { const e = $('#' + id); if (!e) bad(`缺少元素 #${id}`); return !!e; })
    && ok('关键 DOM 元素齐全');

  await waitFor(() => $$('#indices .idx-card').length > 0, 25000);

  // ---- 5. 总览页 ----
  const idxCards = $$('#indices .idx-card').length;
  idxCards > 0 ? ok('总览·指数卡片', `${idxCards} 张`) : bad('总览·指数卡片为空');

  const breadth = $('#breadth').textContent.trim();
  breadth && breadth !== '加载中…' ? ok('总览·市场情绪已填充', breadth.slice(0, 30))
                                   : bad('总览·市场情绪未填充');

  const moverRows = $$('#moverTable tr').length;
  moverRows > 1 ? ok('总览·排行榜有数据', `${moverRows} 行`) : bad('总览·排行榜为空');

  // ---- 6. 自选页 ----
  window.document.querySelector('.tab[data-view="watchlist"]').click();
  await waitFor(() => $$('#watchTable tr').length > 0, 25000);
  const watchActive = $('#view-watchlist').classList.contains('active');
  watchActive ? ok('切到自选页') : bad('自选页未激活');
  const watchRows = $$('#watchTable tr').length;
  watchRows > 0 ? ok('自选页表格已渲染', `${watchRows} 行`) : bad('自选页表格为空');

  // ---- 7. 个股页（含 K线图）----
  window.document.querySelector('.tab[data-view="detail"]').click();
  await sleep(300);
  $('#detailInput').value = '600519.SH';
  $('#detailGoBtn').click();

  await waitFor(() => !$('#detailBody').classList.contains('hidden'), 20000)
    ? ok('个股页加载') : bad('个股页未显示');
  await waitFor(() => { const e = $('#detailQuote .q-price'); return e && e.textContent.trim(); }, 20000)
    ? ok('个股行情已渲染', $('#detailQuote .q-price').textContent.trim())
    : bad('个股行情未渲染');
  await waitFor(() => chartCalls.setOption > 0, 45000)
    ? ok('图表已绘制', `setOption ×${chartCalls.setOption}`)
    : bad('图表未绘制（45 秒内未调用 setOption）');

  // ---- 分时图（默认周期）----
  {
    const o = chartCalls.last || {};
    const series = (o.series || []).map(x => x.name);
    const isTrend = series.includes('价格') && series.includes('均价')
                    && series.includes('成交量');
    isTrend ? ok('默认显示分时图', series.join('/'))
            : bad('默认不是分时图', series.join('/') || '(无 series)');
    if (isTrend) {
      const xs = (o.xAxis && o.xAxis[0] && o.xAxis[0].data) || [];
      xs.length > 50 ? ok('分时点数充足', `${xs.length} 点  ${xs[0]}→${xs[xs.length - 1]}`)
                     : bad('分时点数过少', String(xs.length));
      const y0 = o.yAxis[0], y1 = o.yAxis[1];
      // 分时图的涨跌幅轴必须相对昨收对称，否则会误判涨跌幅度
      (y1 && Math.abs(y1.min + y1.max) < 0.05)
        ? ok('涨跌幅轴以昨收对称', `${y1.min.toFixed(2)}% ~ ${y1.max.toFixed(2)}%`)
        : bad('涨跌幅轴不对称');
      (o.series[0] && o.series[0].markLine)
        ? ok('有昨收基准线') : bad('缺少昨收基准线');
      const avgN = (o.series[1].data || []).filter(v => v != null).length;
      avgN > 0 ? ok('均价线有数据', `${avgN} 点`) : bad('均价线为空');
      const volN = (o.series[2].data || []).filter(d => d && d.value > 0).length;
      volN > 0 ? ok('分钟量柱有数据', `${volN} 根`) : bad('分钟量柱为空');

      // 量能柱必须红绿相间（同花顺口径：与前一分钟比，涨红跌绿）。
      // 曾经这里用 r.price >= r.open 判断，而分时每行的 open 恒等于 price，
      // 比较永远成立 —— 结果 242 根量柱全红。
      {
        const items = (o.series[2].data || []).filter(d => d && d.itemStyle);
        const red = items.filter(d => d.itemStyle.color === '#f0454b').length;
        const grn = items.filter(d => d.itemStyle.color === '#26a269').length;
        const other = items.length - red - grn;
        (red > 0 && grn > 0)
          ? ok('分时量柱红绿相间', `红 ${red} / 绿 ${grn}`)
          : bad('分时量柱颜色单一', `红 ${red} / 绿 ${grn} / 其他 ${other}`);
        // 红绿比例不应过度失衡（真实行情下涨跌分钟数接近）
        const ratio = red / Math.max(1, red + grn);
        (ratio > 0.05 && ratio < 0.95)
          ? ok('量柱红绿比例合理', `红占 ${(ratio * 100).toFixed(0)}%`)
          : bad('量柱红绿比例失衡，疑似判断条件写错', `红占 ${(ratio * 100).toFixed(0)}%`);
      }

      $('#subSeg') && $('#subSeg').style.display === 'none'
        ? ok('分时下隐藏副图选择器') : bad('分时下副图选择器未隐藏');
    }

    // 箱体面板必须与图表周期无关 —— 默认停在分时视图时就该有数据。
    // 之前它只在 drawKline 里更新，导致分时视图下永远显示「加载中…」。
    await waitFor(() => {
      const el = $('#boxPanel');
      return el && !el.textContent.includes('加载中');
    }, 45000);
    {
      const bt = ($('#boxPanel') ? $('#boxPanel').textContent : '').replace(/\s+/g, ' ');
      (!bt.includes('加载中') && bt.length > 50)
        ? ok('分时视图下箱体面板也能加载', `${bt.length} 字符`)
        : bad('分时视图下箱体面板卡在加载中', bt.slice(0, 60));
    }
  }
  await waitFor(() => $('#techPanel').textContent.trim().length > 20, 20000)
    ? ok('技术指标面板已填充') : bad('技术指标面板为空');
  await waitFor(() => $('#fundPanel').textContent.trim().length > 20, 20000)
    ? ok('财务面板已填充') : bad('财务面板为空');

  // 切换周期：分时 → 日K，应变成蜡烛图且副图选择器恢复
  const before = chartCalls.setOption;
  window.document.querySelector('#periodSeg .seg-btn[data-period="day"]').click();
  await waitFor(() => chartCalls.setOption > before
    && (chartCalls.last.series || []).some(x => x.type === 'candlestick'), 45000)
    ? ok('切日K变为蜡烛图') : bad('切日K未变为蜡烛图');
  $('#subSeg') && $('#subSeg').style.display !== 'none'
    ? ok('切回K线后副图选择器恢复') : bad('副图选择器未恢复');

  // 日K量柱同样要与蜡烛同色（收 >= 开 为红），不能整排一色
  {
    const volSeries = ((chartCalls.last || {}).series || [])
      .find(x => x.name === '成交量' && Array.isArray(x.data));
    const items = ((volSeries || {}).data || []).filter(d => d && d.itemStyle);
    const red = items.filter(d => d.itemStyle.color === '#f0454b').length;
    const grn = items.filter(d => d.itemStyle.color === '#26a269').length;
    (red > 0 && grn > 0)
      ? ok('日K量柱红绿相间', `红 ${red} / 绿 ${grn}`)
      : bad('日K量柱颜色单一', `红 ${red} / 绿 ${grn}`);

    // 量柱颜色必须和蜡烛颜色一致（同涨同跌），否则视觉上自相矛盾
    const candles = (chartCalls.last.series || []).find(x => x.type === 'candlestick');
    if (candles && Array.isArray(candles.data) && items.length) {
      let mismatch = 0;
      const n = Math.min(candles.data.length, items.length);
      for (let i = 0; i < n; i++) {
        const c = candles.data[i];
        if (!Array.isArray(c)) continue;             // ECharts 的 [open, close, low, high]
        const candleRed = c[1] >= c[0];              // close >= open
        const volRed = items[i].itemStyle.color === '#f0454b';
        if (candleRed !== volRed) mismatch++;
      }
      mismatch === 0
        ? ok('日K量柱与蜡烛同色', `比对 ${n} 根`)
        : bad('量柱与蜡烛颜色不一致', `${mismatch}/${n} 根不符`);
    }
  }

  // ---- 箱体分析 ----
  {
    // 箱体只在日/周/月 K线上绘制（分时是盘中数据，不适用箱体概念），
    // 所以这段检查必须放在切到日K之后
    await waitFor(() => ((chartCalls.last || {}).series || []).some(x => x.name === '箱体'), 45000);
    const boxSeries = ((chartCalls.last || {}).series || []).find(x => x.name === '箱体');
    boxSeries ? ok('K线图上已绘制箱体图层') : bad('K线图缺少箱体图层');
    if (boxSeries) {
      const ma = boxSeries.markArea;
      const ml = boxSeries.markLine;
      (ma && ma.data && ma.data.length) ? ok('箱体半透明区域已绘制') : bad('箱体区域缺失');
      if (ma && ma.data && ma.data[0]) {
        const [p0, p1] = ma.data[0];
        (p0.yAxis != null && p1.yAxis != null && p1.yAxis > p0.yAxis)
          ? ok('箱体区间上下界正常', `${p0.yAxis} → ${p1.yAxis}`)
          : bad('箱体区间异常', JSON.stringify([p0, p1]).slice(0, 80));
        (p0.xAxis && p1.xAxis) ? ok('箱体横向区间正常', `${p0.xAxis} → ${p1.xAxis}`)
                               : bad('箱体横向区间缺失');
        // 箱体起点必须落在图表实际显示的日期范围内，否则是索引错位
        {
          const allDates = ((chartCalls.last || {}).xAxis || [])[0];
          const ds = allDates && allDates.data ? allDates.data : [];
          if (ds.length) {
            const inRange = ds.includes(p0.xAxis) && ds.includes(p1.xAxis);
            inRange ? ok('箱体区间在图内（无索引错位）')
                    : bad('箱体区间超出图表范围，疑似索引错位',
                          `${p0.xAxis} / ${p1.xAxis} 不在 ${ds[0]}~${ds[ds.length-1]} 内`);
          }
        }
      }
      (ml && ml.data && ml.data.length >= 3)
        ? ok('箱顶/箱底/中轴三条线齐全', `${ml.data.length} 条`)
        : bad('箱体边界线不全', String(ml && ml.data && ml.data.length));
    }
    await waitFor(() => $('#boxPanel') && !$('#boxPanel').textContent.includes('加载中'), 45000);
    const bt = $('#boxPanel') ? $('#boxPanel').textContent.replace(/\s+/g, ' ') : '';
    bt.length > 50 ? ok('箱体分析面板已填充', `${bt.length} 字符`) : bad('箱体面板为空');
    bt.includes('形态判定') ? ok('面板含形态判定') : bad('面板缺少形态判定');
    bt.includes('箱顶') ? ok('面板含箱顶信息') : bad('面板缺少箱顶');
    bt.includes('位置') ? ok('面板含位置百分比') : bad('面板缺少位置');
    bt.includes('不构成买卖建议') ? ok('箱体面板带免责声明') : bad('箱体面板缺少免责声明');

    // ---- 自适应窗口（按个股节奏定期数）----
    const boxApi = (window.__boxCalls || []).slice(-1)[0] || '';
    boxApi.includes('adaptive=1')
      ? ok('箱体面板默认走自适应模式', boxApi) : bad('默认没有走自适应模式', boxApi);
    bt.includes('推荐窗口') ? ok('面板显示推荐窗口') : bad('面板缺少推荐窗口');
    bt.includes('为什么是') ? ok('面板说明了窗口的由来') : bad('面板未说明窗口由来');
    bt.includes('波动档位') || bt.includes('该股自身的波动节奏')
      ? ok('面板展示该股自身节奏') : bad('面板缺少节奏说明');
    bt.includes('节奏定范围') && bt.includes('分数选平台')
      ? ok('面板展示两步推导过程') : bad('面板缺少推导过程');
    /推荐窗口\s*\d+\s*日/.test(bt) ? ok('推荐窗口给出了具体天数') : bad('推荐窗口天数缺失');

    // 切到固定窗口模式，应重新请求并渲染对比表
    {
      const before = (window.__boxCalls || []).length;
      const btn = window.document.querySelector('#boxPanel .seg-btn[data-boxmode="fixed"]');
      if (btn) {
        btn.click();
        await waitFor(() => (window.__boxCalls || []).length > before, 30000);
        const called = ((window.__boxCalls || []).slice(-1)[0] || '');
        called.includes('multi=1')
          ? ok('可切换回固定窗口模式', called) : bad('切固定窗口未重新请求', called);
        await waitFor(() => $('#boxPanel').textContent.includes('其他窗口对比')
          || $('#boxPanel').textContent.includes('形态判定'), 30000);
        ($('#boxPanel').textContent.includes('其他窗口对比'))
          ? ok('固定窗口模式显示多窗口对比') : bad('固定模式缺少对比表');
      } else {
        bad('找不到固定窗口切换按钮');
      }
    }
  }

  const before2 = chartCalls.setOption;
  window.document.querySelector('#subSeg .seg-btn[data-sub="kdj"]').click();
  await waitFor(() => chartCalls.setOption > before2, 45000)
    ? ok('切换副图会重绘图表') : bad('切换副图未重绘');
  // 再切回分时，确认可以来回切换
  const before3 = chartCalls.setOption;
  window.document.querySelector('#periodSeg .seg-btn[data-period="trend"]').click();
  await waitFor(() => chartCalls.setOption > before3
    && (chartCalls.last.series || []).some(x => x.name === '均价'), 45000)
    ? ok('可从K线切回分时') : bad('切回分时失败');

  // ---- 8. 选股页 ----
  window.document.querySelector('.tab[data-view="screener"]').click();
  await waitFor(() => $$('#presetList .preset').length > 0, 25000);
  const presets = $$('#presetList .preset').length;
  presets > 0 ? ok('选股·预设方案已加载', `${presets} 个`) : bad('选股·预设方案为空');
  const chips = $$('#techPick .tech-chip').length;
  chips > 0 ? ok('选股·技术条件选项已渲染', `${chips} 个`) : bad('技术条件为空');

  // 点第一个预设 → 应填入条件
  if (presets > 0) {
    $('#presetList .preset').click();
    await sleep(300);
    const condRows = $$('#condList .cond-row').length;
    condRows > 0 ? ok('点击预设会填充条件', `${condRows} 行`) : bad('预设未填充条件');
  }

  // 触发一次纯快照选股（不带技术条件，快）
  const noRows = $$('#screenTable tr').length;
  $('#runScreenBtn').click();
  await waitFor(() => $$('#screenTable tr').length !== noRows, 90000)
    ? ok('选股执行并出结果', `${$$('#screenTable tr').length} 行`)
    : bad('选股结果为空（90 秒内未更新）');

  // ---- 9. 回测页 ----
  window.document.querySelector('.tab[data-view="backtest"]').click();
  await waitFor(() => $$('#btStrategy option').length > 0, 25000);
  const stratOpts = $$('#btStrategy option').length;
  stratOpts > 0 ? ok('回测·策略列表已加载', `${stratOpts} 个`) : bad('策略列表为空');
  const paramInputs = $$('#btParams input').length;
  ok('回测·参数区已渲染', `${paramInputs} 个输入`);

  $('#btSymbol').value = '000001.SZ';
  $('#btStrategy').value = 'macd';
  $('#btStrategy').dispatchEvent(new window.Event('change'));
  await sleep(300);
  $('#btCash').value = '100000';
  $('#btDays').value = '300';
  $('#btCompare').checked = false;   // 关掉对比，跑得快
  const btBefore = chartCalls.setOption;
  $('#runBtBtn').click();
  await waitFor(() => !$('#btResult').classList.contains('hidden'), 60000)
    ? ok('回测结果区已显示') : bad('回测结果区未显示');
  const metricCards = $$('#btMetrics .metric').length;
  metricCards > 0 ? ok('回测指标卡已渲染', `${metricCards} 张`) : bad('回测指标卡为空');
  await waitFor(() => chartCalls.setOption > btBefore, 30000)
    ? ok('资金曲线已绘制') : bad('资金曲线未绘制');

  // ---- 10. 提醒页 ----
  window.document.querySelector('.tab[data-view="alerts"]').click();
  await waitFor(() => $$('#alRule option').length > 0, 25000);
  const ruleOpts = $$('#alRule option').length;
  ruleOpts > 0 ? ok('提醒·规则类型已加载', `${ruleOpts} 种`) : bad('规则类型为空');
  const alertRows = $$('#alertTable tr').length;
  alertRows > 0 ? ok('提醒·规则表已渲染', `${alertRows} 行`) : bad('提醒规则表为空');

  // ---- 11. 设置页 ----
  window.document.querySelector('.tab[data-view="settings"]').click();
  await waitFor(() => $('#sysStatus').textContent.trim().length > 30, 25000);
  const sysHtml = $('#sysStatus').textContent.trim();
  sysHtml.length > 30 ? ok('设置·系统状态已填充') : bad('系统状态为空');
  const taskRows = $$('#taskTable tr').length;
  taskRows > 0 ? ok('设置·任务日志已渲染', `${taskRows} 行`) : bad('任务日志为空');

  // 运行自检按钮存在且可点（不真的等它跑完，太慢）
  $('#selftestBtn') ? ok('设置·自检按钮存在') : bad('自检按钮缺失');

  // ---- 12. 事件绑定健全性 ----
  window.document.querySelector('.tab[data-view="dashboard"]').click();
  await sleep(1500);
  window.document.querySelector('.tab[data-view="watchlist"]').click();
  await sleep(800);
  ok('标签来回切换无异常');

  // ---- 结果 ----
  // ---- 路由：刷新后要保持当前页面与股票 ----
  {
    window.document.querySelector('.tab[data-view="detail"]').click();
    await sleep(300);
    $('#detailInput').value = '000001.SZ';
    $('#detailGoBtn').click();
    await waitFor(() => window.location.hash.includes('000001.SZ'), 20000);
    const h1 = window.location.hash;
    h1.includes('detail') && h1.includes('000001.SZ')
      ? ok('地址栏记录了当前个股', h1) : bad('地址栏未记录个股', h1);

    // 模拟刷新：用当前 hash 重建一个页面，应直接回到该个股
    const dom2 = new JSDOM(html, { url: BASE + '/#' + h1.replace(/^#/, ''),
      runScripts: 'outside-only', pretendToBeVisual: true });
    const w2 = dom2.window;
    w2.fetch = (u, o) => fetch(typeof u === 'string' && u.startsWith('/') ? BASE + u : u, o);
    w2.matchMedia = window.matchMedia;
    w2.echarts = window.echarts;
    w2.alert = () => {}; w2.confirm = () => true; w2.prompt = () => 'x';
    w2.eval(appJs);
    w2.document.dispatchEvent(new w2.Event('DOMContentLoaded'));
    const $2 = (sel) => w2.document.querySelector(sel);
    const restored = await (async () => {
      const t0 = Date.now();
      while (Date.now() - t0 < 40000) {
        if ($2('#view-detail') && $2('#view-detail').classList.contains('active')
            && $2('#detailInput') && $2('#detailInput').value) return true;
        await sleep(400);
      }
      return false;
    })();
    restored
      ? ok('刷新后仍在个股页（路由还原）', $2('#detailInput').value)
      : bad('刷新后未还原到个股页');
    w2.close();
  }

  console.log('\n══════════════════════════════════════════════════════');
  if (errors.length) {
    console.log(`  捕获到 ${errors.length} 个 JS 异常：`);
    errors.slice(0, 12).forEach(e => console.log('    ! ' + String(e).slice(0, 150)));
    fail += errors.length;
  } else {
    console.log('  未捕获到任何 JS 异常');
  }
  console.log(`  通过 ${pass}   失败 ${fail}`);
  console.log('══════════════════════════════════════════════════════');
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => {
  console.error('测试脚本异常:', e);
  process.exit(2);
});
