'use strict';
(() => {
  const LC = window.LightweightCharts;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const IST_SHIFT = 19800;

  const S = {
    sel: 'NIFTY', stock: null, stockInfo: null, tf: 5, state: null, chain: null, odds: {}, journal: null, events: [],
    selToken: null, side: 'CE', alertsOn: false, audio: null, wake: null, explainKey: null,
    checksOpen: false, lines: [], olines: [], overlaySig: '', optSig: '', lastBarTime: 0, oLast: 0,
    lastStateAt: 0, appConnected: false, settings: null, chainKey: '', cells: [], tab: 'odds',
    cardHtml: '', lastFeed: null, titleFlash: null,
  };

  // ---------------------------------------------------------------- format
  const nf2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf1 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
  const ok = (x) => x !== null && x !== undefined && !Number.isNaN(x);
  const f2 = (x) => (ok(x) ? nf2.format(x) : '—');
  const f0 = (x) => (ok(x) ? nf0.format(x) : '—');
  const rupee = (x, d = 0) => (ok(x) ? (x < 0 ? '−' : '') + '₹' + (d ? nf2 : nf0).format(Math.abs(x)) : '—');
  const signed = (x, nf = nf2) => (ok(x) ? (x > 0 ? '+' : x < 0 ? '−' : '') + nf.format(Math.abs(x)) : '—');
  const cls = (x) => (ok(x) && x > 0 ? 'up' : ok(x) && x < 0 ? 'down' : '');
  const compact = (n) => {
    if (!ok(n)) return '—';
    const a = Math.abs(n), s = n < 0 ? '−' : '';
    if (a >= 1e7) return s + (a / 1e7).toFixed(2) + 'Cr';
    if (a >= 1e5) return s + (a / 1e5).toFixed(1) + 'L';
    if (a >= 1e3) return s + (a / 1e3).toFixed(1) + 'K';
    return s + Math.round(a);
  };
  const tfmt = {};
  const fmtTime = (d, tz, secs = false) => {
    const k = tz + secs;
    tfmt[k] = tfmt[k] || new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: secs ? '2-digit' : undefined, hour12: false, timeZone: tz });
    return tfmt[k].format(d instanceof Date ? d : new Date(d));
  };
  const both = (iso) => `${fmtTime(iso, 'Asia/Kolkata')} IST (${fmtTime(iso, 'Europe/London')} UK)`;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const chartTime = (iso) => Math.floor(Date.parse(iso) / 1000) + IST_SHIFT;
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  // ----------------------------------------------------------------- theme
  const getTheme = () => { try { return localStorage.getItem('theme') || 'dark'; } catch { return 'dark'; } };
  function setChartSize(z) {
    if (!['s', 'm', 'l'].includes(z)) z = 'm';
    document.documentElement.dataset.chart = z;
    $$('#chart-size button').forEach((b) => toggle(b, 'on', b.dataset.size === z));
    try { localStorage.setItem('chartSize', z); } catch { /* storage unavailable */ }
  }
  const getChartSize = () => { try { return localStorage.getItem('chartSize') || 'm'; } catch { return 'm'; } };

  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    $('#theme-btn').textContent = t === 'dark' ? 'Light' : 'Dark';
    try { localStorage.setItem('theme', t); } catch { /* storage unavailable */ }
    applyChartTheme();
  }

  // ---------------------------------------------------------------- charts
  const timeFormatter = (t) => {
    const d = new Date((t - IST_SHIFT) * 1000);
    return `${fmtTime(d, 'Asia/Kolkata')} IST · ${fmtTime(d, 'Europe/London')} UK`;
  };
  const chartOpts = () => ({
    autoSize: true,
    layout: { background: { type: 'solid', color: cssVar('--panel') }, textColor: cssVar('--muted'), fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 11 },
    grid: { vertLines: { color: cssVar('--line') }, horzLines: { color: cssVar('--line') } },
    rightPriceScale: { borderColor: cssVar('--line') },
    timeScale: { borderColor: cssVar('--line'), timeVisible: true, secondsVisible: false, rightOffset: 6 },
    crosshair: { mode: LC ? LC.CrosshairMode.Normal : 0 },
    localization: { timeFormatter, priceFormatter: (p) => nf2.format(p) },
  });
  const candleColors = () => ({
    upColor: cssVar('--up'), downColor: cssVar('--down'), borderUpColor: cssVar('--up'), borderDownColor: cssVar('--down'),
    wickUpColor: cssVar('--up'), wickDownColor: cssVar('--down'),
  });

  // Opening-range band drawn under the candles (a series primitive).
  class Band {
    constructor() { this.on = false; this._views = [{ zOrder: () => 'bottom', renderer: () => ({ draw: (t) => this.draw(t) }) }]; }
    attached(p) { this.chart = p.chart; this.series = p.series; this.req = p.requestUpdate; }
    detached() { this.req = null; }
    paneViews() { return this._views; }
    updateAllViews() {}
    set(top, bot, t0, t1, complete) { Object.assign(this, { top, bot, t0, t1, complete, on: true }); this.req && this.req(); }
    clear() { this.on = false; this.req && this.req(); }
    draw(target) {
      if (!this.on || !this.series) return;
      target.useBitmapCoordinateSpace((scope) => {
        const y1 = this.series.priceToCoordinate(this.top), y2 = this.series.priceToCoordinate(this.bot);
        if (y1 === null || y2 === null) return;
        const ts = this.chart.timeScale();
        let x0 = ts.timeToCoordinate(this.t0); if (x0 === null) x0 = 0;
        let x1 = ts.timeToCoordinate(this.t1); if (x1 === null) x1 = scope.mediaSize.width;
        const W = scope.mediaSize.width, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
        const top = Math.min(y1, y2) * vr, h = Math.max(1, Math.abs(y2 - y1) * vr);
        const ctx = scope.context;
        ctx.fillStyle = cssVar('--accent-bg');
        ctx.fillRect(Math.max(0, x0) * hr, top, (Math.max(x1, 0) - Math.max(0, x0)) * hr, h);
        ctx.globalAlpha = 0.45;
        ctx.fillRect(Math.max(0, x1) * hr, top, (W - Math.max(0, x1)) * hr, h);
        ctx.globalAlpha = 1;
      });
    }
  }

  let chart, candle, band, markers, ochart, oseries;
  function initCharts() {
    if (!LC) { $('#chart').innerHTML = '<p class="err" style="padding:20px">The chart library did not load (web/vendor).</p>'; return; }
    chart = LC.createChart($('#chart'), chartOpts());
    candle = chart.addSeries(LC.CandlestickSeries, candleColors());
    candle.applyOptions({
      autoscaleInfoProvider: (orig) => {
        const r = orig();
        if (!r || !S.levels || !S.levels.length) return r;
        const pad = (Math.max(...S.levels) - Math.min(...S.levels)) * 0.04;
        r.priceRange.minValue = Math.min(r.priceRange.minValue, Math.min(...S.levels) - pad);
        r.priceRange.maxValue = Math.max(r.priceRange.maxValue, Math.max(...S.levels) + pad);
        return r;
      },
    });
    band = new Band();
    candle.attachPrimitive(band);
    markers = LC.createSeriesMarkers(candle, []);
    ochart = LC.createChart($('#opt-chart'), chartOpts());
    oseries = ochart.addSeries(LC.CandlestickSeries, candleColors());
  }
  function applyChartTheme() {
    if (!chart) return;
    for (const c of [chart, ochart]) c.applyOptions(chartOpts());
    for (const s of [candle, oseries]) s.applyOptions(candleColors());
    S.overlaySig = ''; S.optSig = '';
    drawOverlay(); drawOptLines();
  }
  const toBar = (b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });

  async function loadBars() {
    if (!candle) return;
    try {
      let bars;
      if (S.stock) {
        const want = S.stock;
        const r = await fetch(`/api/stock?name=${encodeURIComponent(want)}&tf=${S.tf}`);
        if (!r.ok || want !== S.stock) return;
        const d = await r.json();
        S.stockInfo = d;
        bars = d.bars;
      } else {
        const r = await fetch(`/api/candles?index=${S.sel}&tf=${S.tf}`);
        bars = await r.json();
      }
      candle.setData(bars.map(toBar));
      S.lastBarTime = bars.length ? bars[bars.length - 1].time : 0;
      S.overlaySig = '';
      S.userZoomed = false;
      drawOverlay();
      chart.timeScale().fitContent();
    } catch { /* retried on next state */ }
  }

  function onBars(d) {
    for (const k of ['NIFTY', 'SENSEX']) {
      const el = $('#px-' + k);
      if (el && d[k]) el.textContent = d[k].ltp ? f2(d[k].ltp) : '';
    }
    if (d.STOCK && d.STOCK.name === S.stock) { const el = $('#px-STOCK'); if (el && d.STOCK.ltp) el.textContent = f2(d.STOCK.ltp); }
    const src = S.stock ? (d.STOCK && d.STOCK.name === S.stock ? d.STOCK : null) : d[S.sel];
    const mine = src && src[String(S.tf)];
    if (!mine || !candle) return;
    if (mine.length && mine[mine.length - 1].time < S.lastBarTime - 6 * 3600) { loadBars(); return; }
    let added = false;
    for (const b of mine) {
      if (b.time >= S.lastBarTime) { added = added || b.time > S.lastBarTime; candle.update(toBar(b)); S.lastBarTime = b.time; }
    }
    if (added && !S.userZoomed) chart.timeScale().fitContent();
  }

  function clearLines(series, arr) { for (const l of arr) series.removePriceLine(l); arr.length = 0; }
  function line(series, arr, price, color, title, style = 0, width = 1) {
    arr.push(series.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title }));
  }

  function drawStockOverlay() {
    const d = S.stockInfo;
    if (!candle) return;
    const sig = JSON.stringify(['stock', S.stock, S.tf, d && [d.or, d.prev_close, d.day, d.bars.length > 0]]);
    if (sig === S.overlaySig) return;
    S.overlaySig = sig;
    clearLines(candle, S.lines);
    band.clear();
    markers.setMarkers([]);
    S.levels = [];
    const LS = LC.LineStyle, legend = [];
    if (d && d.or) {
      band.set(d.or.high, d.or.low, chartTime(`${d.day}T09:15:00+05:30`), chartTime(`${d.day}T09:30:00+05:30`), d.or.complete);
      line(candle, S.lines, d.or.high, cssVar('--muted'), 'Range high', LS.Dashed);
      line(candle, S.lines, d.or.low, cssVar('--muted'), 'Range low', LS.Dashed);
      S.levels.push(d.or.high, d.or.low);
      legend.push(`<span><i class="sw" style="background:${cssVar('--accent')};height:8px;opacity:.4"></i>Opening range ${f2(d.or.low)} – ${f2(d.or.high)}</span>`);
    }
    if (d && d.prev_close) {
      line(candle, S.lines, d.prev_close, cssVar('--muted'), 'Prev close', LS.Dotted);
      legend.push(`<span>Prev close ${f2(d.prev_close)}${ok(d.pct) ? ` · <span class="${cls(d.pct)}">${signed(d.pct)}%</span>` : ''}</span>`);
    }
    if (d) legend.push(`<span>Lot ${f0(d.lot)}</span>`);
    if (d && !d.today && d.bars.length) legend.push(`<span class="muted">Showing ${esc(d.day)} (last session)</span>`);
    if (!d || !d.bars.length) legend.push('<span class="muted">Loading candles…</span>');
    legend.push(`<span class="muted">${esc(S.stock)} chart for reference only: the app's breakout signals cover NIFTY and SENSEX · times in IST</span>`);
    $('#legend').innerHTML = legend.join('');
  }

  function renderStockTab() {
    let b = $('#idx-tabs button[data-stock]');
    if (!S.stock) {
      if (b) b.remove();
      $$('#idx-tabs button[data-idx]').forEach((x) => toggle(x, 'on', x.dataset.idx === S.sel));
      return;
    }
    if (!b) { b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'tab'); $('#idx-tabs').appendChild(b); }
    if (b.dataset.stock !== S.stock) {
      b.dataset.stock = S.stock;
      b.innerHTML = `${esc(S.stock)} <span class="px" id="px-STOCK"></span><span class="x" title="Close the stock chart" aria-label="Close">×</span>`;
    }
    $$('#idx-tabs button').forEach((x) => toggle(x, 'on', x === b));
  }
  function showStock(name) {
    if (S.stock === name) return;
    S.stock = name;
    S.stockInfo = null;
    S.overlaySig = '';
    renderStockTab();
    drawStockOverlay();
    loadBars();
    const r = $('#chart').getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) $('.chart-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function closeStock() {
    if (!S.stock) return;
    S.stock = null;
    S.stockInfo = null;
    S.overlaySig = '';
    renderStockTab();
    loadBars();
  }

  function drawOverlay() {
    if (S.stock) { drawStockOverlay(); return; }
    const ix = S.state && S.state.indices[S.sel];
    if (!ix || !candle) return;
    const p = ix.plan && !ix.plan.error ? ix.plan : (ix.missed && ix.missed.targets ? ix.missed : null);
    const sig = JSON.stringify([S.sel, S.tf, ix.or, p && [p.entry_index, p.stop_index, p.targets, p.target_key], ix.signal && ix.signal.bar_start, S.state.now.slice(0, 10)]);
    if (sig === S.overlaySig) return;
    S.overlaySig = sig;
    clearLines(candle, S.lines);
    band.clear();
    S.levels = [];
    if (ix.or) S.levels.push(ix.or.high, ix.or.low);
    if (p) S.levels.push(p.stop_index, p.entry_index, ...Object.values(p.targets));
    const legend = [];
    const LS = LC.LineStyle;
    if (ix.or) {
      const day = S.state.now.slice(0, 10);
      band.set(ix.or.high, ix.or.low, chartTime(`${day}T09:15:00+05:30`), chartTime(`${day}T09:30:00+05:30`), ix.or.complete);
      line(candle, S.lines, ix.or.high, cssVar('--muted'), 'Range high', LS.Dashed);
      line(candle, S.lines, ix.or.low, cssVar('--muted'), 'Range low', LS.Dashed);
      legend.push(`<span><i class="sw" style="background:${cssVar('--accent')};height:8px;opacity:.4"></i>Opening range ${f2(ix.or.low)} – ${f2(ix.or.high)}${ix.or.complete ? '' : ' (forming)'}</span>`);
    }
    if (p) {
      line(candle, S.lines, p.entry_index, cssVar('--accent'), 'Entry', LS.Solid, 1);
      line(candle, S.lines, p.stop_index, cssVar('--down'), 'Stop', LS.Solid, 2);
      const hold = ['hold', 'trail'].includes(p.exit_mode);
      for (const k of ['1R', '1.5R', '2R']) {
        const prim = !hold && k === p.target_key;
        line(candle, S.lines, p.targets[k], cssVar('--up'), prim ? `Target ${k}` : (hold ? `${k} ref` : k), prim ? LS.Solid : LS.Dotted, prim ? 2 : 1);
      }
      legend.push(`<span><i class="sw" style="background:${cssVar('--down')}"></i>Stop ${f2(p.stop_index)}</span>`);
      legend.push(hold ? `<span><i class="sw" style="background:${cssVar('--up')};opacity:.5"></i>No fixed target: hold to ${both(p.exit_time)} (1R–2R shown for reference)</span>`
        : `<span><i class="sw" style="background:${cssVar('--up')}"></i>Target ${p.target_key} ${f2(p.target_index)}</span>`);
    }
    if (ix.signal) {
      const s = ix.signal, ce = s.side === 'CE';
      const t = chartTime(s.bar_start) + (S.tf === 1 ? 240 : 0);
      markers.setMarkers([{ time: t, position: ce ? 'belowBar' : 'aboveBar', color: ce ? cssVar('--call') : cssVar('--put'), shape: ce ? 'arrowUp' : 'arrowDown', text: `BUY ${s.side}` }]);
      legend.push(`<span>Signal ${both(s.time)}</span>`);
    } else {
      markers.setMarkers([]);
    }
    legend.push('<span class="muted">Times in IST · hover for UK time</span>');
    $('#legend').innerHTML = legend.join('');
  }

  // ------------------------------------------------------- premium chart
  async function selectToken(tok) {
    S.selToken = tok;
    S.optSig = '';
    try {
      const r = await fetch('/api/option?token=' + encodeURIComponent(tok));
      if (!r.ok) return;
      const d = await r.json();
      oseries.setData(d.bars.map(toBar));
      S.oLast = d.bars.length ? d.bars[d.bars.length - 1].time : 0;
      S.optLabel = d.contract.label;
      ochart.timeScale().fitContent();
      drawOptLines();
      renderOptHead();
      renderChain(true);
    } catch { /* ignore */ }
  }
  function onOptBars(d) {
    const b = S.selToken && d[S.selToken];
    if (b && oseries && b.time >= S.oLast) {
      const added = b.time > S.oLast;
      oseries.update(toBar(b)); S.oLast = b.time;
      if (added) ochart.timeScale().fitContent();
    }
  }
  function drawOptLines() {
    if (!oseries) return;
    const ix = S.state && S.state.indices[S.sel];
    const p = ix && ix.plan && !ix.plan.error ? ix.plan : null;
    const mine = p && p.contract.token === S.selToken;
    const sig = JSON.stringify([S.selToken, mine && [p.entry_fill, p.exit_fill_stop, p.exit_fill_target]]);
    if (sig === S.optSig) return;
    S.optSig = sig;
    clearLines(oseries, S.olines);
    if (mine) {
      line(oseries, S.olines, p.entry_fill, cssVar('--accent'), 'Entry ≈');
      line(oseries, S.olines, p.exit_fill_stop, cssVar('--down'), 'Stop ≈', LC.LineStyle.Dashed);
      line(oseries, S.olines, p.exit_fill_target, cssVar('--up'), 'Target ≈', LC.LineStyle.Dashed);
    }
  }
  function findQuote(tok) {
    const c = S.chain && S.chain[S.sel];
    if (!c) return null;
    for (const r of c.rows) for (const side of ['CE', 'PE']) if (r[side] && r[side].token === tok) return { row: r, side, q: r[side] };
    return null;
  }
  function renderOptHead() {
    const el = $('#opt-head');
    if (!S.selToken) { el.textContent = 'Click a strike in the option chain to see its live premium chart.'; return; }
    const f = findQuote(S.selToken);
    const q = f && f.q;
    const bits = [`<b>${esc(S.optLabel || '')}</b>`];
    if (q && ok(q.ltp)) {
      bits.push(`LTP <b class="num">${f2(q.ltp)}</b> <span class="${cls(q.chg)}">${signed(q.chg_pct, nf1)}%</span>`);
      bits.push(`bid/ask <span class="num">${f2(q.bid)} / ${f2(q.ask)}</span>`);
      if (ok(q.iv)) bits.push(`IV <span class="num">${q.iv}%</span> · Δ <span class="num">${q.delta}</span> · θ <span class="num">${signed(q.theta)}</span>/day`);
    }
    el.innerHTML = bits.join(' · ');
  }

  // ---------------------------------------------------------------- chain
  function buildChain(c) {
    const tb = $('#chain tbody');
    tb.innerHTML = c.rows.map((r) => `<tr><td class="ce oi"><div class="bar"></div><span></span><small></small></td><td class="ce iv"></td><td class="ce p"></td>
      <td class="k">${nf0.format(r.strike)}</td><td class="pe p"></td><td class="pe iv"></td><td class="pe oi"><div class="bar"></div><span></span><small></small></td></tr>`).join('');
    S.cells = $$('tr', tb).map((tr) => {
      const td = tr.children;
      return { tr, CE: { oi: td[0], iv: td[1], p: td[2] }, PE: { p: td[4], iv: td[5], oi: td[6] } };
    });
  }
  const setText = (el, t) => { if (el._t !== t) { el._t = t; el.textContent = t; } };
  const setHtml = (el, h) => { if (el._h !== h) { el._h = h; el.innerHTML = h; } };
  const toggle = (el, c, on) => { if (el.classList.contains(c) !== !!on) el.classList.toggle(c, !!on); };

  function renderChain(force) {
    const c = S.chain && S.chain[S.sel];
    if (!c) return;
    $('#chain-exp').textContent = c.expiry_label ? ` · ${c.expiry_label} expiry · lot ${c.lot ?? '—'}` : ' · loading contracts…';
    const key = c.expiry + '|' + c.rows.map((r) => r.strike).join(',');
    if (key !== S.chainKey || force === 'rebuild') { S.chainKey = key; buildChain(c); }
    let maxOI = 1;
    for (const r of c.rows) for (const s of ['CE', 'PE']) {
      const q = r[s];
      if (q && ok(q.oi)) maxOI = Math.max(maxOI, q.oi);
    }
    c.rows.forEach((r, i) => {
      const cell = S.cells[i];
      if (!cell) return;
      toggle(cell.tr, 'atm', r.atm);
      for (const s of ['CE', 'PE']) {
        const q = r[s], el = cell[s];
        const itm = s === 'CE' ? r.strike < c.spot : r.strike > c.spot;
        for (const k of ['oi', 'iv', 'p']) toggle(el[k], 'itm', itm && c.spot > 0);
        const oiNum = el.oi.children[1], oiChg = el.oi.children[2];
        if (!q || !ok(q.ltp)) {
          setText(el.p, q ? '…' : '—'); setText(el.iv, ''); setText(oiNum, ''); setText(oiChg, '');
          el.oi.firstChild.style.width = '0'; el.p.dataset.token = q ? q.token : '';
          continue;
        }
        el.p.dataset.token = q.token;
        setHtml(el.p, `${f2(q.ltp)}<small class="${cls(q.chg)}">${signed(q.chg_pct, nf1)}%</small>`);
        el.p.title = `${s === 'CE' ? 'Call' : 'Put'} ${nf0.format(r.strike)} · Δ ${q.delta ?? '—'} · θ ${q.theta ?? '—'}/day · bid ${f2(q.bid)} / ask ${f2(q.ask)} · volume ${compact(q.vol)}`;
        setText(el.iv, ok(q.iv) ? nf1.format(q.iv) : '—');
        setText(oiNum, compact(q.oi));
        setText(oiChg, ok(q.oi_chg) ? (q.oi_chg > 0 ? '+' : '') + compact(q.oi_chg) : 'Δ —');
        toggle(oiChg, 'up', q.oi_chg > 0); toggle(oiChg, 'down', q.oi_chg < 0);
        el.oi.title = `Open interest ${f0(q.oi)} · change since yesterday ${ok(q.oi_chg) ? signed(q.oi_chg, nf0) : 'not available yet'}`;
        el.oi.firstChild.style.width = `${Math.round((q.oi / maxOI) * 90)}%`;
        toggle(el.p, 'plan', q.token === c.plan_token);
        toggle(el.p, 'sel', q.token === S.selToken);
      }
    });
    renderOptHead();
  }

  // ---------------------------------------------------------- signal card
  function explainText(k, p) {
    const idx = p.index, c = p.contract, ce = p.side === 'CE';
    const exp = new Date(c.expiry + 'T00:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    const ruleTxt = p.stop_rule === 'mid' ? 'the middle of the 09:15–09:30 opening range' : 'the far side of the opening range';
    const lim = S.state.today.max_loss;
    const cs = p.costs_at_stop;
    switch (k) {
      case 'buy': return `<b>BUY ${esc(idx)} ${nf0.format(c.strike)} ${c.opt}</b> means buying a <b>${ce ? 'call' : 'put'} option</b>. A ${ce ? 'call gains value when ' + idx + ' rises' : 'put gains value when ' + idx + ' falls'}. <b>${nf0.format(c.strike)}</b> is the strike price and <b>${exp}</b> is when the contract expires. <b>₹${f2(p.entry_fill)}</b> is the premium for one unit, the price you pay. One lot is ${c.lot} units, so 1 lot costs about <b>${rupee(p.entry_fill * c.lot)}</b>. As a buyer, the most you can lose is what you paid. You place the order yourself in the Angel One app; this app never places anything.`;
      case 'stop': return `The stop is your exit if the idea turns out wrong. It is watched on <b>${esc(idx)} itself</b>, not on the option: if ${esc(idx)} ${ce ? 'falls' : 'rises'} to <b>${f2(p.stop_index)}</b> (${ruleTxt}), sell the option. ≈ ₹${f2(p.exit_fill_stop)} is the estimated option price at that moment (Black-Scholes, so the real price can differ). A stop is a plan, not a guarantee: in a fast market the fill can be worse. Never move it further away once you're in.`;
      case 'target': if (p.exit_mode === 'half') return `Version B: when the index has moved <b>+${p.half_at_r}R</b> your way, sell <b>half</b> the position and move the stop on the rest to your entry level (<b>${f2(p.entry_index)}</b>), so the rest can no longer lose. The rest is held until the stop or <b>${both(p.exit_time)}</b>. In four years of history this won more often (about 55% of trades) but made less than holding everything (version A), because it gives away part of the big trend days. You need at least 2 lots to split.`;
        if (['hold', 'trail'].includes(p.exit_mode)) return `This rule has <b>no fixed target</b>. You stay in the trade until either the stop is hit or the time exit at <b>${both(p.exit_time)}</b>, whichever comes first. Why: in four years of NIFTY and SENSEX history almost all of the profit came from the few days that kept trending all day; taking profit at 2R cut those days short and turned the rule into a small loser. Expect to be stopped out or to close small most days, and to have the odd very large win. The ${p.target_key} level (<b>${f2(p.target_index)}</b>, ≈ ₹${f2(p.exit_fill_target)}) is shown only as a reference, and it is also used for the "don't chase" check.`;
        return `The target is where you take profit. <b>${p.target_key}</b> means ${p.target_key.replace('R', '')} times the risk. The risk is ${f2(p.risk_pts)} index points (entry ${f2(p.entry_index)} to stop ${f2(p.stop_index)}), so the target is <b>${f2(p.target_index)}</b>. ≈ ₹${f2(p.exit_fill_target)} is the estimated option price there. The chart also shows the 1R, 1.5R and 2R levels.`;
      case 'size': return `How many lots to buy: ${p.lots} × ${c.lot} units = <b>${p.qty || c.lot}</b>. The app takes your max loss per trade (${rupee(lim)}) and picks the most lots whose loss at the stop, after all charges, still fits inside it. Buying costs about ${rupee(p.entry_fill * (p.qty || c.lot))}. Decided by ${esc(p.sized_by)}.`;
      case 'loss': return `If the stop is hit you'd lose about <b>${rupee(p.lots ? p.loss_at_stop : p.one_lot_loss)}</b>: the premium falling from ≈ ₹${f2(p.entry_fill)} to ≈ ₹${f2(p.exit_fill_stop)} on ${p.qty || c.lot} units, plus charges of ${rupee(cs.total, 2)} (brokerage ${rupee(cs.brokerage)}, STT ${rupee(cs.stt, 2)}, exchange ${rupee(cs.exchange, 2)}, GST ${rupee(cs.gst, 2)}, stamp ${rupee(cs.stamp, 2)}, SEBI ${rupee(cs.sebi, 2)}), with slippage allowed for. At the target you'd make about <b>${rupee(p.gain_at_target)}</b> after charges.`;
      case 'entry': return `This is the premium to take the trade at. Place a <b>limit</b> buy order at about <b>₹${f2(p.entry_limit)}</b> (the ask when the signal fired, plus a tick); never a market order, which can fill much worse on options. If the premium has already run above <b>₹${f2(p.max_entry_premium)}</b> (${esc(idx)} ${ce ? 'above' : 'below'} ${f2(p.max_entry_index)}), skip the trade: from there the rest of the move to the target pays less than ${(S.settings && S.settings.min_reward_risk) || 1} to 1 against the stop. The green or red line under the fields tells you live which side of that line the price is.`;
      case 'exit': return `If neither the stop nor the target is hit, sell by <b>${both(p.exit_time)}</b> anyway. Options lose value every hour (time decay: θ ≈ ${signed(p.theta_day)} per unit per day right now), and this rule never holds a position overnight. If ${esc(idx)} doesn't move at all, the option would be worth about ₹${f2(p.prem_time_exit_flat)} by then.`;
      default: return '';
    }
  }

  function strikeTable(p) {
    const rows = p.strikes || [];
    if (!rows.length) return '';
    const b = p.best;
    const planTok = p.contract.token;
    let h = `<details class="cmp" ${S.cmpOpen ? 'open' : ''}><summary>Compare strikes · best fit: <b>${b ? `${nf0.format(b.strike)} ${b.opt} at ₹${f2(b.premium)} (${b.label})` : 'none passes the checks'}</b></summary>`;
    if (b) h += `<p class="best-line">${esc(b.reason)}${b.token !== planTok ? ` Your plan uses ${S.settings && S.settings.strike_mode === 'itm1' ? '1 ITM' : 'ATM'} (change it in Settings → Strike → Best fit).` : ''}</p>`;
    h += `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Strike</th><th>Premium</th><th>Δ</th><th>Spread</th><th>Lots</th><th>Loss at stop</th><th>Gain at target</th><th>Gain : loss</th><th>Note</th></tr></thead><tbody>`;
    for (const r of rows) {
      h += `<tr class="${r.best ? 'best' : ''} ${r.ok ? '' : 'no'}"><td>${nf0.format(r.strike)} ${r.opt} · ${r.label}${r.best ? '<span class="tag best">best</span>' : ''}${r.token === planTok ? '<span class="tag plan">plan</span>' : ''}</td>
        <td>₹${f2(r.premium)}</td><td>${f2(Math.abs(r.delta))}</td><td>${ok(r.spread_pct) ? r.spread_pct + '%' : '—'}</td><td>${r.lots}</td>
        <td class="down">−${rupee(r.loss)}</td><td class="up">+${rupee(r.gain)}</td><td>${f2(r.rr)}</td><td>${esc((r.why_not || []).join(', ')) || 'ok'}</td></tr>`;
    }
    h += `</tbody></table></div><p class="note">Gain and loss are after all charges and include the time decay of an average holding time. Best fit: delta 0.40–0.70, spread within your limit, premium in your range, at least 1 lot inside your max loss; then the best gain-to-loss ratio, with near-ties going to the delta closest to 0.55. Estimates, not promises.</p></details>`;
    return h;
  }

  function tradeLine(tr) {
    if (!tr) return '';
    const shadow = tr.kind === 'shadow';
    const ver = tr.versions;
    const vtxt = (k, val, live) => {
      const x = ver && ver[k];
      const note = k === 'B' && x && x.half ? ` (half sold ≈ ₹${f2(x.half.fill)})` : '';
      return ok(val) ? `${k}: <b class="${cls(val)}">${rupee(val)}</b>${live && x && !x.open ? ' closed' : ''}${note}` : '';
    };
    if (tr.status === 'open') {
      const lp = tr.live_pnl, lv = tr.live_versions;
      const ab = lv ? ` · ${vtxt('A', lv.A, true)} · ${vtxt('B', lv.B, true)}` : (lp ? ` · P&amp;L after costs <b class="${cls(lp.pnl)}">${rupee(lp.pnl)}</b>` : '');
      return `<div class="trade-line">${shadow ? 'Blocked signal, tracked in the journal so you can see what happened' : 'Paper trade open'}: bought ≈ ₹${f2(tr.entry_fill)} × ${tr.qty}${lp ? ` · now ≈ ₹${f2(lp.exit_now)}` : ''}${ab}</div>`;
    }
    const why = { target: 'Target hit', stop: 'Stop hit', time: 'Time exit at 15:00' }[tr.exit_reason] || esc(tr.exit_reason || 'Closed');
    const ab = ver && ver.A && ok(ver.A.pnl) ? ` · version ${vtxt('A', ver.A.pnl)} · version ${vtxt('B', ver.B.pnl)}` : '';
    return `<div class="trade-line">${shadow ? 'Blocked signal (not taken)' : 'Paper trade'}: ${why}. Sold ≈ ₹${f2(tr.exit_fill)} · P&amp;L after costs <b class="${cls(tr.pnl)}">${rupee(tr.pnl)}</b> (${signed(tr.r_mult)}R)${ab}</div>`;
  }

  function oddsLine() {
    const o = S.odds[S.sel];
    if (!o) return '';
    const bits = [];
    if (o.mc && !o.mc.error) bits.push(['hold', 'trail'].includes(o.mc.exit_mode)
      ? `Monte Carlo, ${f0(o.mc.replays)} replays (seed ${o.mc.seed}): ${esc(o.mc.verdict)}`
      : `Monte Carlo, ${f0(o.mc.replays)} replays (seed ${o.mc.seed}): target first <b>${o.mc.target_first}%</b> · stop first <b>${o.mc.stop_first}%</b> · neither ${o.mc.neither}%. ${esc(o.mc.verdict)}`);
    else if (o.mc_running) bits.push('Running the Monte Carlo replay…');
    else if (o.mc && o.mc.error) bits.push(esc(o.mc.error));
    const b = o.backtest;
    if (b && b.fired) bits.push(['hold', 'trail'].includes(b.exit_mode)
      ? `History: ${b.fired} signals in ${b.sessions} sessions → stopped out ${b.stop}, held to 15:00 ${b.neither}; average ${signed(b.avg_r_after_costs)}R after costs (estimate).`
      : `History: ${b.fired} signals in ${b.sessions} sessions → target ${b.target}, stop ${b.stop}, neither ${b.neither}; average ${signed(b.avg_r_after_costs)}R after costs (estimate).`);
    return bits.length ? `<div class="odds-line">${bits.join('<br>')} <a href="#" data-goto="odds">Details</a></div>` : '';
  }

  function chipText(ix) {
    const p = ix.plan, tr = ix.trade;
    if (ix.phase === 'signal' && p && !p.error) {
      const s = tr && tr.status === 'closed' ? ({ target: 'target hit', stop: 'stopped out', time: 'time exit' }[tr.exit_reason] || 'closed') : (ix.risk ? ix.risk.verdict : '');
      return `BUY ${p.side} · ${s}`;
    }
    if (ix.phase === 'missed' && ix.missed) return `BUY ${ix.missed.side} · missed (app was off) · ${({ target: 'target hit', stop: 'stop hit', time: 'time exit', open: 'open' })[ix.missed.outcome] || ''}`;
    return ({ before: 'before the open', forming: 'range forming', waiting: 'waiting', expired: 'no trade today', holiday: 'closed', closed: 'closed' })[ix.phase] || ix.phase;
  }

  function renderSignal() {
    const st = S.state;
    const ix = st.indices[S.sel];
    const card = ix.card, p = ix.plan, risk = ix.risk;
    let h = '';
    if (card.state === 'signal' && p && !p.error) {
      const v = risk ? risk.verdict : '…';
      h += `<div class="sig-row"><span class="sig-badge ${p.side}">BUY ${p.side === 'CE' ? 'CALL · CE' : 'PUT · PE'}</span>
        <h1 class="sig-title">${esc(p.headline)}</h1><span class="verdict ${v}">Risk check: ${v}</span></div>`;
      const fields = [['entry', 'Entry', p.entry_text || ''], ['stop', 'Stop', p.stop_text], ['target', 'Target', p.target_text], ['size', 'Size', p.size_text],
        ['loss', 'Max loss', p.loss_text.replace(/^Max loss /, '')], ['exit', 'Exit by', p.exit_text.replace(/^Exit by /, '')]];
      h += `<div class="fields">${fields.map(([k, l, val]) => `<button type="button" class="field ${k} ${S.explainKey === k ? 'open' : ''}" data-explain="${k}"><div class="lab">${l}<i>what this means</i></div><div class="val">${esc(val)}</div></button>`).join('')}</div>`;
      if (p.half_text) h += `<div class="half-line">${esc(p.half_text)}</div>`;
      h += `<div class="meta"><a href="#" data-explain="buy">What does "${esc(p.headline.split(' at ')[0])}" mean?</a></div>`;
      if (S.explainKey) h += `<div class="explain">${explainText(S.explainKey, p)}</div>`;
      const fz = findQuote(p.contract.token);
      const taken = ix.trade && ix.trade.status === 'closed';
      if (fz && ok(fz.q.ask) && fz.q.ask > 0 && ok(p.max_entry_premium) && !taken) {
        const inside = fz.q.ask <= p.max_entry_premium;
        h += `<div class="zone ${inside ? 'in' : 'out'}">${inside
          ? `Premium now ₹${f2(fz.q.ask)} (ask): inside the entry zone. Limit buy at up to ₹${f2(p.max_entry_premium)}.`
          : `Premium now ₹${f2(fz.q.ask)} (ask) is above ₹${f2(p.max_entry_premium)}: too late, skip this one.`}</div>`;
      }
      h += strikeTable(p);
      const ageMin = Math.max(0, Math.round((Date.parse(st.now) - Date.parse(p.signal_time)) / 60000));
      const f = findQuote(p.contract.token);
      const meta = [`Signal <b>${both(p.signal_time)}</b>`, `age <b>${ageMin} min</b>`];
      if (f && ok(f.q.ltp)) meta.push(`premium now <b>₹${f2(f.q.ltp)}</b> (bid ${f2(f.q.bid)} / ask ${f2(f.q.ask)})`);
      meta.push(`IV <b>${p.iv_pct}%</b>`, `Δ <b>${p.delta}</b>`, `θ <b>${signed(p.theta_day)}</b>/day`, `lot <b>${p.lot}</b>`);
      h += `<div class="meta">${meta.map((m) => `<span>${m}</span>`).join('')}</div>`;
      const flags = [...(p.expiry_flags || []), ...(p.notes || [])];
      if (flags.length) h += `<div class="meta flag">${flags.map(esc).join(' · ')}</div>`;
      if (risk && risk.blocks && risk.blocks.length) h += `<ul class="blocks">${risk.blocks.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>`;
      if (risk) {
        const taken = ix.trade && ix.trade.kind === 'paper';
        h += `<details class="checks" ${S.checksOpen ? 'open' : ''}><summary>All ${risk.checks.length} risk checks${taken ? ' (as at the signal)' : ''}</summary><ul>${risk.checks.map((c) => `<li><span class="st ${c.status}">${c.status}</span><span><b>${esc(c.name)}</b>: ${esc(c.message)}</span></li>`).join('')}</ul></details>`;
      }
      h += tradeLine(ix.trade);
      h += oddsLine();
    } else if (p && p.error) {
      h += `<div class="sig-row"><span class="sig-badge idle">Signal</span><h1 class="sig-title">${esc(card.title)}</h1></div><p class="sig-text err">${esc(p.error)}</p>`;
    } else {
      const lab = ({ before: 'Pre-market', forming: 'Range forming', waiting: 'Waiting', expired: 'No trade', closed: 'Closed', holiday: 'Closed', missed: 'Missed' })[card.state] || 'Status';
      h += `<div class="sig-row"><span class="sig-badge idle">${lab}</span><h1 class="sig-title">${esc(card.title)}</h1></div><p class="sig-text">${esc(card.text)}</p>`;
      if (ix.history) h += `<div class="odds-line">Odds history: ${esc(ix.history)}</div>`;
    }
    h += `<div class="others">${Object.entries(st.indices).map(([k, v]) => `<button type="button" class="chip" data-idx="${k}"><b>${esc(v.label)}</b> ${esc(chipText(v))}</button>`).join('')}</div>`;
    if (h !== S.cardHtml) { S.cardHtml = h; $('#signal').innerHTML = h; }
  }

  // ---------------------------------------------------------- status strip
  function renderStatus() {
    const st = S.state, feed = st.feed;
    const mode = $('#mode');
    mode.textContent = st.mode.toUpperCase();
    toggle(mode, 'live', st.mode === 'live');
    const mk = $('#mkt');
    const ph = feed.phase;
    const sim = st.mode !== 'live';
    mk.textContent = (sim ? 'Demo: ' : '') + (ph === 'open' ? 'market open' : ph === 'pre-open' ? 'pre-open' : 'market closed');
    if (!sim) mk.textContent = mk.textContent.charAt(0).toUpperCase() + mk.textContent.slice(1);
    mk.className = 'pill ' + (ph === 'open' ? 'good' : ph === 'pre-open' ? 'amber' : '');
    $('#clk-ist').textContent = st.now_ist;
    $('#clk-uk').textContent = st.now_uk;
    const fd = $('#feed');
    let lab = { live: feed.latency_ms !== null ? `Live · ${feed.latency_ms} ms` : 'Live', delayed: 'Data delayed', disconnected: 'Disconnected', closed: 'Feed idle', waiting: 'Waiting for prices' }[feed.state] || feed.state;
    if (sim) lab = { live: 'Simulated prices', delayed: 'Simulated delay', disconnected: 'Simulated drop', closed: 'Demo idle', waiting: 'Demo starting' }[feed.state] || lab;
    fd.textContent = lab;
    fd.className = 'pill ' + ({ live: 'good', delayed: 'amber', disconnected: 'bad', waiting: 'amber' }[feed.state] || '');
    fd.title = feed.reason || 'Market data feed';
    const vx = st.vix;
    $('#vix').innerHTML = vx.ltp ? `${f2(vx.ltp)} <small class="${cls(vx.chg)}">${signed(vx.chg)}</small>` : '—';
    const ix = st.indices[S.sel];
    $('#tte').textContent = ix.expiry_label ? `${ix.expiry_label} · ${ix.tte}` : '—';
    const t = st.today;
    const pnl = $('#pnl');
    pnl.textContent = rupee(t.realized);
    pnl.className = cls(t.realized);
    $('#pnl-limit').textContent = `/ −${rupee(t.daily_limit)}`;
    $('#trades').textContent = `${t.trades}/${t.max_trades}`;
    toggle(document.body, 'stale', ph === 'open' && !feed.live);
    const live = st.indices[S.sel];
    const flash = live.phase === 'signal' && live.trade && live.trade.status === 'open' ? '● ' : '';
    document.title = `${flash}${live.label} ${live.ltp ? f2(live.ltp) : ''} · India Options`;
    renderBanner();
    if (S.lastFeed && S.lastFeed !== feed.state && ph === 'open' && (feed.state === 'disconnected' || feed.state === 'delayed')) {
      if (S.alertsOn) { chime('feed'); notify(`Data ${feed.state}`, feed.reason || 'Prices are not live', 'feed'); }
    }
    S.lastFeed = feed.state;
  }

  function renderBanner() {
    const b = $('#banner');
    const st = S.state;
    let text = '', kind = '';
    if (!S.appConnected) {
      text = 'Lost connection to the app on this computer. Reconnecting… Prices on screen are NOT live.'; kind = 'bad';
    } else if (st) {
      const f = st.feed;
      const last = f.last ? `${fmtTime(f.last, 'Asia/Kolkata', true)} IST (${fmtTime(f.last, 'Europe/London', true)} UK)` : 'never';
      if (f.state === 'disconnected' && (f.phase === 'open' || st.mode === 'live')) {
        text = `Data disconnected: ${f.reason}. Reconnecting automatically. Last price at ${last}. Prices on screen are NOT live.`; kind = 'bad';
      } else if (f.state === 'delayed') {
        text = `Data delayed: no new prices for ${Math.round(f.age_s)} s (last at ${last}). Prices on screen may be out of date.`; kind = 'amber';
      } else if (st.note && st.mode === 'live') {
        text = st.note; kind = 'info';
      } else if (st.mode !== 'live' && st.source) {
        text = `${st.source.label} · ${st.date} · simulated clock`; kind = 'info';
      }
    }
    b.hidden = !text;
    if (text) { b.textContent = text; b.className = 'banner ' + kind; }
  }

  // ------------------------------------------------------------------ tabs
  function svgFan(mc, plan) {
    const W = 600, H = 190, P = 6;
    const ser = mc.fan;
    const all = [...ser.p5, ...ser.p95, plan.stop_index, plan.target_index, plan.entry_index];
    const lo = Math.min(...all), hi = Math.max(...all);
    const n = ser.p50.length;
    const x = (i) => P + (i / Math.max(1, n - 1)) * (W - 2 * P);
    const y = (v) => P + (1 - (v - lo) / (hi - lo || 1)) * (H - 2 * P);
    const area = (a, b) => `M${a.map((v, i) => `${x(i)},${y(v)}`).join('L')}L${b.map((v, i) => `${x(n - 1 - i)},${y(v)}`).reverse().join('L')}Z`;
    const pth = (a) => `M${a.map((v, i) => `${x(i)},${y(v)}`).join('L')}`;
    const hl = (v, c, t) => `<line x1="${P}" x2="${W - P}" y1="${y(v)}" y2="${y(v)}" stroke="${c}" stroke-dasharray="4 3"/><text x="${W - P - 4}" y="${y(v) - 4}" text-anchor="end" fill="${c}" font-size="11">${t} ${f2(v)}</text>`;
    return `<svg viewBox="0 0 ${W} ${H}" class="mini-chart" role="img" aria-label="Monte Carlo fan chart">
      <path d="${area(ser.p95, ser.p5)}" fill="${cssVar('--accent')}" opacity=".10"/><path d="${area(ser.p75, ser.p25)}" fill="${cssVar('--accent')}" opacity=".18"/>
      <path d="${pth(ser.p50)}" stroke="${cssVar('--accent')}" fill="none" stroke-width="1.6"/>
      ${hl(plan.target_index, cssVar('--up'), plan.hold ? '2R ref' : 'Target')}${hl(plan.stop_index, cssVar('--down'), 'Stop')}${hl(plan.entry_index, cssVar('--muted'), 'Entry')}</svg>`;
  }
  function svgHist(o) {
    const W = 600, H = 90, n = o.hist.length, mx = Math.max(...o.hist, 1);
    const bw = W / n;
    return `<svg viewBox="0 0 ${W} ${H + 14}" class="hist" role="img" aria-label="Histogram of R after costs">
      ${o.hist.map((v, i) => { const mid = (o.hist_edges[i] + o.hist_edges[i + 1]) / 2; const hh = (v / mx) * H; return `<rect x="${i * bw + 1}" y="${H - hh}" width="${bw - 2}" height="${hh}" fill="${mid < 0 ? cssVar('--down') : cssVar('--up')}" opacity=".7"/>`; }).join('')}
      <text x="0" y="${H + 12}" font-size="11" fill="${cssVar('--muted')}">−2R</text><text x="${W / 3}" y="${H + 12}" font-size="11" fill="${cssVar('--muted')}">0</text><text x="${W - 22}" y="${H + 12}" font-size="11" fill="${cssVar('--muted')}">+4R</text></svg>`;
  }
  const stat = (l, v, c = '') => `<div class="stat"><span>${l}</span><b class="${c}">${v}</b></div>`;

  function renderOdds() {
    const o = S.odds[S.sel];
    const el = $('#tab-odds');
    if (!o) { el.innerHTML = '<p class="muted">Loading…</p>'; return; }
    const b = o.backtest, mc = o.mc;
    const plan = mc && !mc.error ? { entry_index: mc.entry, stop_index: mc.stop, target_index: mc.target, hold: ['hold', 'trail'].includes(mc.exit_mode) } : null;
    let h = '<div class="grid2"><div class="box"><h3>History: the same rule on past sessions</h3>';
    h += `<p class="note">${esc(o.status || '')}</p>`;
    if (b && b.sessions) {
      h += `<div class="stats">${stat('Sessions', f0(b.sessions))}${stat('Signals', f0(b.fired))}${['hold', 'trail'].includes(b.exit_mode) ? `${stat('Stopped out', f0(b.stop), 'down')}${stat('Held to 15:00', f0(b.neither))}` : `${stat('Target first', f0(b.target), 'up')}${stat('Stop first', f0(b.stop), 'down')}${stat('Neither', f0(b.neither))}`}${stat('Avg R (index)', signed(b.avg_r_index), cls(b.avg_r_index))}${stat('Avg R after costs*', signed(b.avg_r_after_costs), cls(b.avg_r_after_costs))}${stat('Win rate after costs*', ok(b.win_rate_after_costs) ? Math.round(b.win_rate_after_costs * 100) + '%' : '—')}</div>`;
      h += `<p class="verdict-line">${esc(b.verdict)}</p><p class="note">Rule: the 5-minute close must clear the range by ${b.breakout_buffer ?? 0}× its width; stop at the range ${b.stop_rule === 'mid' ? 'midpoint' : 'far side'}; ${b.trend_filter === 'yesterday' ? "only in the direction of yesterday's move; " : ''}${['hold', 'trail'].includes(b.exit_mode) ? 'no fixed target, exit at the stop or 15:00' : b.exit_mode === 'half' ? `half sold at +${b.half_at_r}R, the rest's stop moved to entry, rest held to 15:00` : `target ${b.target_r}R, time exit 15:00`}. ${esc(b.note)} * = estimate.</p>`;
      if (o.trades && o.trades.length) {
        h += `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Date</th><th>Side</th><th>Time</th><th>Entry</th><th>Result</th><th>R index</th><th>Est. P&amp;L</th></tr></thead><tbody>${o.trades.slice(-12).reverse().map((t) => `<tr><td>${t.date}</td><td>${t.side}</td><td>${t.time}</td><td>${f2(t.entry)}</td><td>${{ neither: b.exit_mode === 'target' ? 'neither' : 'held to 15:00', 'half+stop': 'half, rest at entry', 'half+time': 'half, rest to 15:00' }[t.outcome] || t.outcome}</td><td class="${cls(t.r_index)}">${signed(t.r_index)}</td><td class="${cls(t.pnl)}">${rupee(t.pnl)}</td></tr>`).join('')}</tbody></table></div>`;
      }
    } else h += '<p class="muted">No history yet.</p>';
    h += '</div><div class="box"><h3>Monte Carlo: the rest of today, replayed</h3>';
    if (mc && !mc.error && plan) {
      h += `<div class="stats">${['hold', 'trail'].includes(mc.exit_mode) ? `${stat('Stopped out', mc.stop_first + '%', 'down')}${stat('Held to 15:00', mc.neither + '%')}` : `${stat('Target first', mc.target_first + '%', 'up')}${stat('Stop first', mc.stop_first + '%', 'down')}${stat('Neither by 15:00', mc.neither + '%')}`}${stat('Green', mc.green + '%')}${stat('Index at exit, median', f2(mc.final_p50))}${stat('90% range', `${f0(mc.final_p5)}–${f0(mc.final_p95)}`)}</div>`;
      h += svgFan(mc, plan);
      h += `<p class="verdict-line">${esc(mc.verdict)}</p>${mc.verdict_note ? `<p class="note">${esc(mc.verdict_note)}</p>` : ''}`;
      if (mc.option) {
        const op = mc.option;
        h += `<div class="stats">${stat('Avg P&L after costs*', rupee(op.avg_pnl), cls(op.avg_pnl))}${stat('Median*', rupee(op.median_pnl), cls(op.median_pnl))}${stat('Chance of profit*', op.prob_profit + '%')}${stat('Avg R after costs*', signed(op.avg_r_after_costs), cls(op.avg_r_after_costs))}</div>`;
        h += svgHist(op);
        h += `<p class="note">${f0(mc.replays)} replays, seed ${mc.seed}, drawn from ${mc.pool_sessions} past sessions at the same time of day. Stop is checked before target inside a bar. ${esc(op.note)} * = estimate. A simulation is not a prediction.</p>`;
      }
      h += '<div class="form-actions"><button class="btn" type="button" id="mc-rerun">Run again</button></div>';
    } else if (o.mc_running) h += '<p class="muted">Running…</p>';
    else if (mc && mc.error) h += `<p class="muted">${esc(mc.error)}</p>`;
    else h += '<p class="muted">Runs automatically when a signal fires, using the plan\'s entry, stop and target.</p>';
    h += '</div></div>';
    el.innerHTML = h;
  }

  function equitySvg(eq) {
    if (!eq.length) return '<p class="muted">No closed paper trades yet.</p>';
    const W = 600, H = 150, P = 8;
    const vals = [0, ...eq.map((e) => e.cum)];
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const x = (i) => P + (i / Math.max(1, vals.length - 1)) * (W - 2 * P);
    const y = (v) => P + (1 - (v - lo) / (hi - lo || 1)) * (H - 2 * P);
    return `<svg viewBox="0 0 ${W} ${H}" class="mini-chart" role="img" aria-label="Paper P&L over time"><line x1="${P}" x2="${W - P}" y1="${y(0)}" y2="${y(0)}" stroke="${cssVar('--line-2')}"/><path d="M${vals.map((v, i) => `${x(i)},${y(v)}`).join('L')}" fill="none" stroke="${cssVar(vals[vals.length - 1] >= 0 ? '--up' : '--down')}" stroke-width="2"/></svg>`;
  }

  function renderJournal() {
    const j = S.journal, el = $('#tab-journal');
    if (!j) { el.innerHTML = '<p class="muted">Loading…</p>'; return; }
    const p = j.stats.paper, sh = j.stats.shadow;
    let h = `<div class="stats">${stat('Paper trades', f0(p.signals))}${stat('Closed', f0(p.closed))}${stat('Win rate', ok(p.win_rate) ? Math.round(p.win_rate * 100) + '%' : '—')}${stat('Net P&L after costs', rupee(p.net_pnl), cls(p.net_pnl))}${stat('Avg R', signed(p.avg_r), cls(p.avg_r))}${stat('Targets / stops / time', `${p.targets} / ${p.stops} / ${p.time_exits}`)}${stat('Blocked signals', f0(sh.signals))}${stat('Blocked would have made', rupee(sh.net_pnl), cls(sh.net_pnl))}</div>`;
    const vs = j.stats.versions;
    if (vs && (vs.A.closed || vs.B.closed)) {
      const vrow = (k, x, name) => `${stat(`Version ${k}: ${name}`, `${ok(x.win_rate) ? Math.round(x.win_rate * 100) + '% wins' : '—'} · ${rupee(x.net_pnl)}`, cls(x.net_pnl))}`;
      h += `<div class="stats">${vrow('A', vs.A, 'hold to 15:00')}${vrow('B', vs.B, 'half at +1R')}${stat('Trades compared', f0(Math.max(vs.A.closed, vs.B.closed)))}</div>`;
    }
    h += `<div class="box"><h3>Paper P&amp;L over time (${esc(j.mode)} mode)</h3>${equitySvg(j.stats.equity)}</div>`;
    h += `<div class="tbl-wrap" style="margin-top:12px"><table class="tbl"><thead><tr><th>Day</th><th>Index</th><th>Contract</th><th>Kind</th><th>Signal</th><th>Lots</th><th>Entry ₹</th><th>Exit ₹</th><th>Result</th><th>P&amp;L</th><th>R</th><th>A ₹</th><th>B ₹</th></tr></thead><tbody>`;
    h += j.trades.map((t) => `<tr><td>${t.day}</td><td>${t.idx}</td><td>${esc(t.symbol)}</td><td>${t.kind === 'paper' ? 'paper' : 'blocked'}</td><td>${fmtTime(t.signal_time, 'Asia/Kolkata')}</td><td>${t.lots}</td><td>${f2(t.entry_fill)}</td><td>${t.exit_fill ? f2(t.exit_fill) : '—'}</td><td>${t.status === 'open' ? 'open' : esc(t.exit_reason)}</td><td class="${cls(t.pnl)}">${t.pnl !== null ? rupee(t.pnl) : '—'}</td><td>${t.r_mult !== null ? signed(t.r_mult) : '—'}</td>${['A', 'B'].map((k) => { const x = t.versions && t.versions[k]; return `<td class="${x ? cls(x.pnl) : ''}">${x && ok(x.pnl) ? rupee(x.pnl) : '—'}</td>`; }).join('')}</tr>`).join('') || '<tr><td colspan="13" class="muted">No signals yet. Every signal is logged here automatically.</td></tr>';
    h += '</tbody></table></div><p class="note">Paper trades buy at the live ask when the signal fires and sell at the live bid at the stop or 15:00, after all charges. Every trade is tracked two ways: A holds everything to the stop or 15:00; B sells half at +1R and moves the rest\'s stop to entry (B needs at least 2 lots to do for real). Blocked signals never count toward your limits.</p>';
    el.innerHTML = h;
  }

  function renderReplay() {
    const st = S.state, el = $('#tab-replay');
    if (!st || !st.source) return;
    const src = st.source;
    let h;
    if (st.mode === 'live') {
      h = `<p>Live mode. Today's ticks are being recorded so you can replay the session later.</p><div class="stats">${stat('Recording to', esc(src.recording || '—'))}${stat('Ticks recorded', f0(src.ticks_recorded))}${stat('Feed reconnects', f0(src.reconnects))}</div>
        <p class="note">To replay a recorded day later: <code>.\\.venv\\Scripts\\python.exe -m app --replay data\\sessions\\YYYY-MM-DD</code>. To rebuild a past day from Angel's 1-minute candles: <code>python -m app rebuild YYYY-MM-DD</code>.</p>`;
    } else {
      const sp = [1, 2, 5, 10, 60, 300];
      h = `<p><b>${esc(src.label)}</b> · ${esc(st.date)} · simulated time ${esc(st.now_ist)} IST</p>
        <div class="form-actions"><button class="btn ${src.playing ? '' : 'primary'}" type="button" data-replay="${src.playing ? 'pause' : 'play'}">${src.playing ? 'Pause' : 'Play'}</button>
        <div class="seg">${sp.map((s) => `<button type="button" data-speed="${s}" class="${src.speed === s ? 'on' : ''}">${s}×</button>`).join('')}</div>
        <button class="btn" type="button" data-replay="restart">Restart the day</button></div>
        <p class="note">The demo is synthetic: a NIFTY breakout that is held to the 15:00 exit in profit, a SENSEX breakdown that is stopped out, and a short feed drop at 11:50 IST so you can see the warning banner. It is not real market data.</p>`;
    }
    if (el._h !== h) { el._h = h; el.innerHTML = h; }
  }

  const FORM = [
    ['Risk limits', [['max_loss_per_trade', 'Max loss per trade (₹)', 'number', 1], ['daily_loss_limit', 'Daily loss limit (₹)', 'number', 1], ['max_trades_per_day', 'Max trades a day', 'number', 1], ['max_lots', 'Max lots', 'number', 1]]],
    ['Strategy', [['stop_rule', 'Stop on the index', 'select', [['mid', 'Range midpoint'], ['far', 'Far side of range']]], ['breakout_buffer', 'Close must clear range by (× width)', 'number', 0.05], ['trend_filter', 'Trend filter', 'select', [['yesterday', "Yesterday's direction"], ['none', 'Off (both sides)']]], ['exit_mode', 'Exit rule', 'select', [['trail', 'Trail after +1R (tested)'], ['hold', 'A: stop or 15:00'], ['half', 'B: half at +1R'], ['target', 'At the target']]], ['half_at_r', 'B: sell half at (R)', 'number', 0.25], ['target_r', 'Target / reference level', 'select', [['1', '1R'], ['1.5', '1.5R'], ['2', '2R']]], ['strike_mode', 'Strike', 'select', [['atm', 'At the money'], ['itm1', '1 strike in the money'], ['best', 'Best fit (comparison)']]], ['max_signal_age_min', 'Max signal age (min)', 'number', 1], ['min_reward_risk', "Don't chase below gain:loss", 'number', 0.1]]],
    ['Option checks', [['nifty_min_premium', 'NIFTY min premium ₹', 'number', 1], ['nifty_max_premium', 'NIFTY max premium ₹', 'number', 1], ['sensex_min_premium', 'SENSEX min premium ₹', 'number', 1], ['sensex_max_premium', 'SENSEX max premium ₹', 'number', 1], ['max_spread_pct', 'Max bid/ask spread %', 'number', 0.1]]],
    ['Costs', [['brokerage_per_order', 'Brokerage per order ₹', 'number', 0.5], ['stt_sell_pct', 'STT on sell %', 'number', 0.001], ['nse_txn_pct', 'NSE exchange %', 'number', 0.00001], ['bse_txn_pct', 'BSE exchange %', 'number', 0.00001], ['stamp_buy_pct', 'Stamp duty on buy %', 'number', 0.001], ['gst_pct', 'GST %', 'number', 1], ['sebi_per_crore', 'SEBI fee ₹/crore', 'number', 1], ['slippage_ticks', 'Slippage (ticks a side)', 'number', 1]]],
    ['Model', [['risk_free_rate_pct', 'Interest rate %', 'number', 0.1], ['default_iv_pct', 'Fallback IV %', 'number', 0.5], ['mc_replays', 'Monte Carlo replays', 'number', 1000], ['mc_seed', 'Monte Carlo seed', 'number', 1]]],
  ];
  function renderSettings(msg = '') {
    const s = S.settings, el = $('#tab-settings');
    if (!s) { el.innerHTML = '<p class="muted">Loading…</p>'; return; }
    let h = s.max_loss_confirmed ? '' : '<p class="warn"><b>Set your own max loss per trade and press Save.</b> Until then the live risk check blocks every signal.</p>';
    h += '<form class="form" id="settings-form">';
    for (const [legend, rows] of FORM) {
      h += `<fieldset><legend>${legend}</legend>`;
      for (const [k, lab, type, opt] of rows) {
        if (type === 'select') h += `<label>${lab}<select name="${k}">${opt.map(([v, t]) => `<option value="${v}" ${String(s[k]) === v || Number(s[k]) === Number(v) ? 'selected' : ''}>${t}</option>`).join('')}</select></label>`;
        else h += `<label>${lab}<input name="${k}" type="number" step="${opt}" value="${s[k]}"></label>`;
      }
      h += '</fieldset>';
    }
    h += `</form><div class="form-actions"><button class="btn primary" type="button" id="settings-save">Save</button><button class="btn" type="button" id="settings-3x">Daily limit = 3 × max loss</button><span id="settings-msg">${msg}</span></div>
      <p class="note">Saved to config/settings.toml. New signals use the new values; a plan that is already active keeps its stop. Check the cost rates against one of your Angel One contract notes (NRI accounts can have different brokerage).</p>`;
    el.innerHTML = h;
  }
  async function saveSettings() {
    const fd = new FormData($('#settings-form'));
    const body = {};
    for (const [k, v] of fd.entries()) body[k] = v;
    const r = await post('/api/settings', body);
    const d = await r.json();
    if (d.ok) { S.settings = d.settings; renderSettings('<span class="ok">Saved.</span>'); }
    else $('#settings-msg').innerHTML = `<span class="err">${(d.errors || ['Could not save']).map(esc).join('; ')}</span>`;
  }

  function renderLog() {
    const el = $('#tab-log');
    el.innerHTML = `<ul class="log">${S.events.slice().reverse().map((e) => `<li class="${e.level === 'warn' ? 'warn' : ''}"><time>${fmtTime(e.t, 'Asia/Kolkata', true)} IST · ${fmtTime(e.t, 'Europe/London')} UK</time><b>${esc(e.kind)}</b><span>${esc(e.text)}</span></li>`).join('') || '<li class="muted">Nothing yet.</li>'}</ul>`;
  }

  async function refreshOdds() {
    for (const k of ['NIFTY', 'SENSEX']) {
      try { S.odds[k] = await (await fetch('/api/odds?index=' + k)).json(); } catch { /* retry later */ }
    }
    if (S.tab === 'odds') renderOdds();
    if (S.state) renderSignal();
  }
  async function refreshJournal() {
    try { S.journal = await (await fetch('/api/journal')).json(); } catch { return; }
    if (S.tab === 'journal') renderJournal();
  }
  function showTab(t) {
    S.tab = t;
    $$('#tabbar button').forEach((b) => toggle(b, 'on', b.dataset.tab === t));
    $$('.tab').forEach((d) => toggle(d, 'on', d.id === 'tab-' + t));
    if (t === 'odds') renderOdds();
    if (t === 'journal') { renderJournal(); refreshJournal(); }
    if (t === 'replay') renderReplay();
    if (t === 'settings') renderSettings();
    if (t === 'log') renderLog();
  }

  // ---------------------------------------------------------------- alerts
  async function enableAlerts() {
    try { S.audio = S.audio || new (window.AudioContext || window.webkitAudioContext)(); await S.audio.resume(); } catch { /* no audio */ }
    if ('Notification' in window && Notification.permission === 'default') { try { await Notification.requestPermission(); } catch { /* ignore */ } }
    await keepAwake();
    S.alertsOn = true;
    chime('test');
    const b = $('#alerts-btn');
    b.textContent = 'Alerts on';
    b.classList.add('on');
    b.title = 'Sound + browser notifications on. The screen is kept awake while this tab is open.';
  }
  function chime(kind) {
    if (!S.audio) return;
    const seq = { signal: [660, 880, 1100, 0, 660, 880, 1100, 0, 660, 880, 1320], exit: [880, 660, 0, 880, 660], feed: [330, 0, 330, 0, 330], test: [880, 1100] }[kind] || [880];
    let t = S.audio.currentTime + 0.03;
    for (const f of seq) {
      if (f) {
        const o = S.audio.createOscillator(), g = S.audio.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.4, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
        o.connect(g).connect(S.audio.destination); o.start(t); o.stop(t + 0.25);
      }
      t += 0.26;
    }
  }
  function notify(title, body, tag) {
    if (!S.alertsOn) return;
    try {
      if ('Notification' in window && Notification.permission === 'granted') {
        const n = new Notification(title, { body, tag, requireInteraction: true });
        n.onclick = () => { window.focus(); n.close(); };
      }
    } catch { /* ignore */ }
  }
  async function keepAwake() { try { if ('wakeLock' in navigator) S.wake = await navigator.wakeLock.request('screen'); } catch { /* not allowed */ } }

  // ---------------------------------------------------------------- movers
  function moverRows(list, el) {
    const h = list.map((r) => `<tr data-name="${esc(r.name)}" class="${S.movers && S.movers.selected === r.name ? 'sel' : ''}" title="Turnover ${r.turnover_cr !== null ? '₹' + nf1.format(r.turnover_cr) + ' Cr' : '—'} · lot ${r.lot} · futures OI ${ok(r.oi_chg_pct) ? signed(r.oi_chg_pct, nf1) + '%' : '—'}">
      <td class="nm">${esc(r.name)}<small class="${r.tone}">${esc(r.buildup)}</small></td><td class="n">${f2(r.ltp)}</td><td class="n ${cls(r.pct)}">${signed(r.pct)}%</td></tr>`).join('')
      || '<tr><td class="muted">Waiting for prices…</td></tr>';
    if (el._h !== h) { el._h = h; el.innerHTML = h; }
  }
  function stockCompare(d) {
    const c = d.compare;
    if (!c) return '';
    let h = '<div class="stock-cmp"><h4>Which strike to buy</h4>';
    if (c.error) return h + `<p class="muted">${esc(c.error)}</p></div>`;
    h += `<p>${esc(c.text)}</p>`;
    if (!c.live) h += '<p class="warn-line">Market closed: these use the last traded prices. Check again after 09:30 IST (04:00 UK) before buying.</p>';
    const b = c.best, fb = c.fallback;
    if (b) h += `<p class="best-line"><b>Best: ${nf0.format(b.strike)} ${b.opt} (${esc(b.label)}) at about ₹${f2(b.premium)}.</b> ${esc(b.reason)}</p>`;
    else if (fb) h += `<p class="best-line warn"><b>No strike fits your max loss.</b> ${esc(fb.reason)}</p>`;
    else h += '<p class="best-line warn"><b>No strike passes the checks right now</b> (see the notes in the table).</p>';
    h += `<table class="tbl cmp-mini"><thead><tr><th>Strike</th><th>Premium</th><th>Loss at stop</th><th>Gain at 2R</th><th>Gain:loss</th></tr></thead><tbody>`;
    for (const r of c.rows || []) {
      const pick = r.best || (fb && fb.token === r.token);
      const tag = r.best ? '<span class="tag best">best</span>' : (pick ? '<span class="tag plan">closest</span>' : '');
      const why = (r.why_not || []).map((w) => w.replace('1 lot is over your max loss', 'over max loss').replace(/delta \S+ outside 0\.40-0\.70/, 'Δ outside 0.40–0.70').replace('no live quote', 'no quote')).join(', ');
      h += `<tr class="${pick ? 'best' : ''} ${r.ok ? '' : 'no'}"><td>${nf0.format(r.strike)} ${r.opt} ${tag}<small>${esc(r.label)}${why ? ' · ' + esc(why) : ' · ok'}</small></td>
        <td>₹${f2(r.premium)}<small>Δ ${f2(Math.abs(r.delta))} · ${ok(r.spread_pct) ? r.spread_pct + '%' : '—'}</small></td>
        <td class="down">−${rupee(r.loss)}<small>${r.lots ? r.lots + ' lot' + (r.lots > 1 ? 's' : '') : '1 lot*'}</small></td><td class="up">+${rupee(r.gain)}</td><td>${f2(r.rr)}</td></tr>`;
    }
    h += `</tbody></table></div><p class="note">Same rule as the index card: delta 0.40–0.70, spread within your limit, at least 1 lot inside your max loss, then the best gain-to-loss, near-ties to delta 0.55. Loss and gain are after charges and time decay, for your lots (1 lot* = one lot, over your max loss). This stock setup uses the index rule but has not been backtested on stocks. Estimates, not promises.</p></div>`;
    return h;
  }

  function renderMovers() {
    const m = S.movers;
    if (!m) return;
    const st = S.state;
    const note = m.label ? `${m.label} · ` : '';
    $('#movers-note').textContent = `${note}${m.priced} of ${m.tracked} F&O stocks priced${st && st.feed.state !== 'live' && st.mode === 'live' ? ' · not live' : ''}`;
    moverRows(m.bull, $('#mv-bull'));
    moverRows(m.bear, $('#mv-bear'));
    const d = m.detail, box = $('#mv-detail');
    if (!d) { box.hidden = true; return; }
    box.hidden = false;
    const exp = new Date(d.fut_expiry + 'T00:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    const opt = (o) => `<div class="opt"><div>${esc(o.label)}</div>${ok(o.ltp) ? `<div><b>₹${f2(o.ltp)}</b> · bid/ask ${f2(o.bid)} / ${f2(o.ask)}${ok(o.spread_pct) ? ` (${o.spread_pct}%)` : ''}</div>
      <div class="muted">IV ${o.iv ?? '—'}% · Δ ${o.delta ?? '—'} · θ ${o.theta !== undefined ? signed(o.theta) : '—'}/day</div><div>1 lot (${f0(o.lot)}) ≈ <b>${rupee(o.lot_cost)}</b></div>` : '<div class="muted">waiting for a quote…</div>'}</div>`;
    const h = `<div><b>${esc(d.name)}</b> ${f2(d.ltp)} <span class="${cls(d.pct)}">${signed(d.pct)}%</span> · <span class="${d.tone}">${esc(d.buildup)}</span> · lot ${f0(d.lot)} · expiry ${exp}</div>
      <div class="opts">${d.options.map(opt).join('') || '<div class="muted">No option contracts found for this stock.</div>'}</div>
      ${stockCompare(d)}
      <p class="note">Stock options expire monthly and are usually less liquid than NIFTY: check the spread, and that one lot fits your max loss, before you trade one. The app's tested breakout signals cover NIFTY and SENSEX only.</p>`;
    if (box._h !== h) { box._h = h; box.innerHTML = h; }
  }

  // ------------------------------------------------------------------- SSE
  function onState(st) {
    const first = !S.state;
    const prevDay = S.state && S.state.now.slice(0, 10);
    S.state = st;
    S.lastStateAt = Date.now();
    S.appConnected = true;
    if (first || prevDay !== st.now.slice(0, 10)) loadBars();
    renderStatus();
    renderSignal();
    drawOverlay();
    const ix = st.indices[S.sel];
    const ptok = ix.plan && ix.plan.contract && ix.plan.contract.token;
    if (ptok && !S.selToken && !S.autoSelected) { S.autoSelected = true; selectToken(ptok); }
    drawOptLines();
    if (S.tab === 'replay') renderReplay();
  }
  // Browsers allow only 6 connections to one address and every open tab keeps one live stream, so a tab in the
  // background lets go of it after a few seconds (unless alerts are on: they need the stream) and takes it back.
  let es = null, hideTimer = null;
  document.addEventListener('visibilitychange', () => {
    clearTimeout(hideTimer);
    if (document.hidden) hideTimer = setTimeout(() => { if (es && !S.alertsOn) { es.close(); es = null; S.appConnected = false; } }, 5000);
    else if (!es) connect();
  });
  function connect() {
    if (es) return;
    es = new EventSource('/api/stream');
    es.addEventListener('state', (e) => onState(JSON.parse(e.data)));
    es.addEventListener('bars', (e) => onBars(JSON.parse(e.data)));
    es.addEventListener('chain', (e) => { S.chain = JSON.parse(e.data); renderChain(); });
    es.addEventListener('opt_bars', (e) => onOptBars(JSON.parse(e.data)));
    es.addEventListener('movers', (e) => { S.movers = JSON.parse(e.data); renderMovers(); });
    es.addEventListener('odds', () => refreshOdds());
    es.addEventListener('stock_ready', (e) => { const d = JSON.parse(e.data); if (d.name === S.stock) { S.overlaySig = ''; loadBars(); } });
    es.addEventListener('event', (e) => {
      const ev = JSON.parse(e.data);
      S.events.push(ev); if (S.events.length > 300) S.events.shift();
      if (S.tab === 'log') renderLog();
      if (['plan', 'exit'].includes(ev.kind)) refreshJournal();
    });
    es.addEventListener('alert', (e) => {
      const a = JSON.parse(e.data);
      chime(a.verdict === 'EXIT' ? 'exit' : 'signal');
      notify(a.headline, a.text, 'sig-' + a.index);
      if (a.verdict !== 'EXIT') {
        if (S.stock && a.index === S.sel) closeStock();  // a signal takes the chart back from a stock
        else if (S.stock) { S.stock = null; S.stockInfo = null; renderStockTab(); }
        if (a.index !== S.sel) switchIndex(a.index);
      }
    });
    es.onopen = () => {
      const again = S.streamOpened;
      S.streamOpened = true; S.appConnected = true; renderBanner();
      if (again) { S.overlaySig = ''; loadBars(); }
    };
    es.onerror = () => { S.appConnected = false; renderBanner(); };
  }

  function switchIndex(k) {
    if (k === S.sel) return;
    S.sel = k;
    S.selToken = null; S.optLabel = ''; S.autoSelected = false;
    if (oseries) { oseries.setData([]); clearLines(oseries, S.olines); }
    S.optSig = ''; S.chainKey = ''; S.cardHtml = ''; S.explainKey = null;
    $$('#idx-tabs button').forEach((b) => toggle(b, 'on', b.dataset.idx === k));
    loadBars();
    if (S.state) { renderStatus(); renderSignal(); }
    renderChain();
    renderOptHead();
    if (S.tab === 'odds') renderOdds();
  }

  // ------------------------------------------------------------------ init
  function bind() {
    $('#theme-btn').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
    $('#alerts-btn').addEventListener('click', enableAlerts);
    $('#idx-tabs').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.stock) { if (e.target.closest('.x')) closeStock(); return; }
      const k = b.dataset.idx;
      if (S.stock) {
        S.stock = null; S.stockInfo = null; renderStockTab();
        if (k === S.sel) { S.overlaySig = ''; loadBars(); return; }
      }
      switchIndex(k);
    });
    $('#chart-size').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setChartSize(b.dataset.size); });
    $('#tf').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      S.tf = Number(b.dataset.tf);
      $$('#tf button').forEach((x) => toggle(x, 'on', x === b));
      loadBars();
    });
    $('#chain-side').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      S.side = b.dataset.side;
      document.body.classList.remove('side-CE', 'side-PE');
      document.body.classList.add('side-' + S.side);
      $$('#chain-side button').forEach((x) => toggle(x, 'on', x === b));
    });
    document.body.classList.add('side-CE');
    $('#chain tbody').addEventListener('click', (e) => {
      const td = e.target.closest('td.p');
      if (td && td.dataset.token) selectToken(td.dataset.token);
    });
    $('#signal').addEventListener('click', (e) => {
      const ex = e.target.closest('[data-explain]');
      if (ex) { e.preventDefault(); S.explainKey = S.explainKey === ex.dataset.explain ? null : ex.dataset.explain; renderSignal(); return; }
      const chip = e.target.closest('[data-idx]');
      if (chip) { switchIndex(chip.dataset.idx); return; }
      const go = e.target.closest('[data-goto]');
      if (go) { e.preventDefault(); showTab(go.dataset.goto); $('.tabs-card').scrollIntoView({ behavior: 'smooth' }); }
    });
    $('#signal').addEventListener('toggle', (e) => {
      if (e.target.matches('details.checks')) S.checksOpen = e.target.open;
      if (e.target.matches('details.cmp')) S.cmpOpen = e.target.open;
    }, true);
    for (const id of ['#mv-bull', '#mv-bear']) {
      $(id).addEventListener('click', async (e) => {
        const tr = e.target.closest('tr[data-name]');
        if (!tr) return;
        showStock(tr.dataset.name);
        try { S.movers = await (await post('/api/mover', { name: tr.dataset.name })).json(); renderMovers(); } catch { /* ignore */ }
      });
    }
    $('#tabbar').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) showTab(b.dataset.tab); });
    $('#tab-replay').addEventListener('click', async (e) => {
      const b = e.target.closest('[data-replay],[data-speed]');
      if (!b) return;
      const body = b.dataset.speed ? { action: 'speed', speed: Number(b.dataset.speed) } : { action: b.dataset.replay };
      await post('/api/replay', body);
      if (body.action === 'restart') { setTimeout(() => { loadBars(); refreshJournal(); refreshOdds(); }, 400); }
    });
    $('#tab-odds').addEventListener('click', async (e) => {
      if (e.target.id === 'mc-rerun') { await post('/api/odds/rerun', { index: S.sel }); setTimeout(refreshOdds, 1500); }
    });
    $('#tab-settings').addEventListener('click', (e) => {
      if (e.target.id === 'settings-save') saveSettings();
      if (e.target.id === 'settings-3x') {
        const f = $('#settings-form');
        f.daily_loss_limit.value = Number(f.max_loss_per_trade.value || 0) * 3;
      }
    });
    document.addEventListener('visibilitychange', () => { if (S.alertsOn && document.visibilityState === 'visible') keepAwake(); });
  }

  async function init() {
    document.documentElement.dataset.theme = getTheme();
    $('#theme-btn').textContent = getTheme() === 'dark' ? 'Light' : 'Dark';
    setChartSize(getChartSize());
    initCharts();
    for (const ev of ['wheel', 'mousedown', 'touchstart']) $('#chart').addEventListener(ev, () => { S.userZoomed = true; }, { passive: true });
    $('#chart').addEventListener('dblclick', () => { S.userZoomed = false; chart && chart.timeScale().fitContent(); });
    bind();
    connect();
    try { S.events = await (await fetch('/api/events')).json(); } catch { /* later */ }
    try { S.settings = await (await fetch('/api/settings')).json(); } catch { /* later */ }
    refreshOdds();
    refreshJournal();
    setInterval(() => {
      if (S.state && Date.now() - S.lastStateAt > 5000 && S.appConnected) { S.appConnected = false; renderBanner(); }
    }, 1000);
    setInterval(refreshOdds, 30000);
    showTab('odds');
  }
  init();
})();
