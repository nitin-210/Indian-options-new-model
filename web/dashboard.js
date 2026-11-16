/* India Options dashboard: live chart, index calls with entry / stop / targets, stock picks and movers.
   Read-only. It shows signals; it never places anything. */
(() => {
  'use strict';
  const LW = window.LightweightCharts;
  const $ = (s, el = document) => el.querySelector(s);
  const C = { up: '#5CE1E6', dn: '#FF8A3D', gold: '#F5C451', stop: '#FF5C7A', frost: '#E6ECFF', slate: '#8F9BC6' };
  const INDEX_ORDER = ['NIFTY', 'SENSEX', 'BANKNIFTY'];
  const INDEX_LABEL = { NIFTY: 'NIFTY 50', SENSEX: 'SENSEX', BANKNIFTY: 'NIFTY BANK' };

  const S = {
    pred: null, state: null, movers: null,
    sel: { kind: 'index', key: 'NIFTY' }, lastIndex: 'NIFTY',
    tf: 5, hist: { earliest: null, loading: false, more: true, detached: false }, ctxDays: 1, ctxN: 0, ctxLines: [], rp: { on: false, picking: false, all: [], idx: 0, speed: 2, playing: false, timer: null }, mvTab: 'bull', seen: {}, openNames: new Set(), lots: 1, ticket: null, ticketKey: '', paperOpen: false, paperData: null, busy: false,
    bars: [], five: new Map(), last: null, day: null, hover: false,
    loadToken: 0, connected: false, opened: false,
    phases: {}, lineKey: '', markKey: '', lines: [], levels: null,
    stock: null, tabsKey: '', view: null,
  };
  // which symbol and candle size the chart's data belongs to; live updates are applied only when it matches
  const viewKey = () => (S.sel.kind === 'index' ? 'i:' + S.sel.key : 's:' + S.sel.name) + ':' + S.tf;

  // ------------------------------------------------------------------ formatting
  const nf2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
  const ok = v => v !== null && v !== undefined && Number.isFinite(+v);
  const px = v => ok(v) ? nf2.format(v) : '—';
  const n0 = v => ok(v) ? nf0.format(v) : '—';
  const rs = (v, dp = 0) => ok(v) ? '₹' + (dp ? nf2 : nf0).format(v) : '—';
  const signed = (v, f) => !ok(v) ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + f(Math.abs(v));
  const pct = v => signed(v, x => x.toFixed(2)) + (ok(v) ? '%' : '');
  const tone = v => !ok(v) || v === 0 ? '' : v > 0 ? 'up' : 'dn';
  const dir = side => side === 'CE' ? 'up' : side === 'PE' ? 'dn' : '';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const dShort = iso => { if (!iso) return ''; const [, m, d] = iso.split('-').map(Number); return `${d} ${MONTHS[m - 1]}`; };
  const strikeText = o => o ? `${n0(o.strike)} ${o.opt} ${dShort(o.expiry)}` : '';
  const minus5 = hm => { if (!hm) return ''; const [h, m] = hm.split(':').map(Number); const t = h * 60 + m - 5;
                         return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`; };
  const STAR = '<path d="M12 2.8l2.75 5.95 6.5.72-4.86 4.4 1.36 6.4L12 17.05l-5.75 3.22 1.36-6.4-4.86-4.4 6.5-.72z"/>';
  function stars(n, title, cls = '') {
    let h = '';
    for (let i = 1; i <= 3; i++) h += `<svg viewBox="0 0 24 24" class="${i <= n ? 'on' : ''}" aria-hidden="true">${STAR}</svg>`;
    return `<span class="stars ${cls}" role="img" aria-label="${n} of 3 stars${title ? ': ' + esc(title) : ''}" title="${esc(title || '')}">${h}</span>`;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };

  async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(String(r.status)); return r.json(); }
  const postJSON = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  // ------------------------------------------------------------------ chart
  const frame = $('#frame'), chartEl = $('#chart'), ov = $('#overlay'), ctx = ov.getContext('2d');
  const emptyEl = $('#chart-empty'), legendEl = $('#legend');
  let chart = null, candles = null, volume = null, markers = null;
  if (LW) {
    chart = LW.createChart(chartEl, {
      autoSize: true,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: C.slate, fontSize: 11,
                fontFamily: 'Manrope, "Segoe UI", system-ui, sans-serif' },
      grid: { vertLines: { color: 'rgba(126,148,230,0.06)' }, horzLines: { color: 'rgba(126,148,230,0.06)' } },
      rightPriceScale: { borderColor: 'rgba(126,148,230,0.20)', scaleMargins: { top: 0.08, bottom: 0.16 } },
      timeScale: { borderColor: 'rgba(126,148,230,0.20)', timeVisible: true, secondsVisible: false, rightOffset: 10, barSpacing: 9 },
      crosshair: {
        mode: LW.CrosshairMode.Normal,
        vertLine: { color: 'rgba(230,236,255,0.28)', labelBackgroundColor: '#1C2C5C' },
        horzLine: { color: 'rgba(230,236,255,0.28)', labelBackgroundColor: '#1C2C5C' },
      },
      localization: { priceFormatter: p => nf2.format(p) },
    });
    candles = chart.addSeries(LW.CandlestickSeries, {
      upColor: C.up, downColor: C.dn, wickUpColor: C.up, wickDownColor: C.dn, borderVisible: false,
      priceLineColor: 'rgba(230,236,255,0.55)', priceLineStyle: LW.LineStyle.Dotted,
    });
    candles.applyOptions({
      autoscaleInfoProvider: original => {
        const r = original();
        const L = S.levels;
        if (!r || !r.priceRange || !L) return r;
        const vals = [];
        if (L.plan) vals.push(L.plan.stop, L.plan.entry, L.plan.T1, L.plan.T2, L.plan.target);
        else for (const w of L.watch) vals.push(w.trigger);
        if (L.or) vals.push(L.or.high, L.or.low);
        for (const v of vals) if (ok(v)) { r.priceRange.minValue = Math.min(r.priceRange.minValue, v); r.priceRange.maxValue = Math.max(r.priceRange.maxValue, v); }
        return r;
      },
    });
    volume = chart.addSeries(LW.HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.86, bottom: 0 }, visible: false });
    markers = LW.createSeriesMarkers(candles, []);
    chart.subscribeCrosshairMove(p => {
      const d = p && p.time ? p.seriesData.get(candles) : null;
      S.hover = !!d;
      legend(d);
    });
  }

  // for web/tools.js (drawings and indicators)
  window.IO_CHART = LW ? { LW, chart, candles, frame, get bars() { return S.bars; }, get tf() { return S.tf; },
    get key() { return S.sel.kind === 'index' ? S.sel.key : 'STK:' + S.sel.name; } } : null;

  const toC = b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });
  const toV = b => ({ time: b.time, value: S.sel.kind === 'stock' ? (b.volume || 0) : 0,
                      color: b.close >= b.open ? 'rgba(92,225,230,0.26)' : 'rgba(255,138,61,0.26)' });
  function agg15(list) {
    const out = [];
    let cur = null;
    for (const b of list) {
      const t = Math.floor(b.time / 900) * 900;
      if (!cur || cur.time !== t) {
        cur = { time: t, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume || 0 };
        out.push(cur);
      } else {
        cur.high = Math.max(cur.high, b.high); cur.low = Math.min(cur.low, b.low); cur.close = b.close; cur.volume += b.volume || 0;
      }
    }
    return out;
  }
  const bucket15 = t => agg15([t, t + 300, t + 600].map(k => S.five.get(k)).filter(Boolean))[0];
  const dayOf = t => Math.floor(t / 86400) * 86400;          // bar times are IST clock times on a UTC axis
  const hhmm = (day0, s) => { if (!s || day0 == null) return null; const [h, m] = s.split(':').map(Number); return day0 + h * 3600 + m * 60; };

  function symLabel() {
    if (S.sel.kind === 'stock') return S.sel.name;
    return (S.pred && S.pred.indices[S.sel.key] && S.pred.indices[S.sel.key].label) || INDEX_LABEL[S.sel.key] || S.sel.key;
  }

  function legend(d) {
    const b = d || S.last;
    if (!b) { legendEl.innerHTML = `<span class="sym">${esc(symLabel())}</span>`; return; }
    legendEl.innerHTML = `<span class="sym">${esc(symLabel())} ${S.tf}m</span>`
      + `<span>O<b>${px(b.open)}</b></span><span>H<b>${px(b.high)}</b></span><span>L<b>${px(b.low)}</b></span>`
      + `<span>C<b class="${b.close >= b.open ? 'up' : 'dn'}">${px(b.close)}</b></span>`;
  }

  function showEmpty(on, loading = false) {
    emptyEl.hidden = !on;
    if (!on) return;
    if (!LW) { emptyEl.textContent = 'The chart library did not load. Reload the page.'; return; }
    emptyEl.textContent = loading ? `Loading ${symLabel()}…`
      : S.sel.kind === 'stock' ? `Loading ${S.sel.name}'s candles from Angel One…`
      : 'No candles yet today. The chart fills from 09:15 IST.';
  }

  function clearChart() {
    S.view = null; S.bars = []; S.last = null; S.day = null; S.five = new Map(); S.levels = null;
    candles.setData([]); volume.setData([]);
    markers.setMarkers([]); S.markKey = '[]';
    for (const l of S.lines) candles.removePriceLine(l);
    S.lines = []; S.lineKey = '';
    for (const l of S.ctxLines) candles.removePriceLine(l);
    S.ctxLines = []; S.ctxN = 0;
  }
  // earlier days' highs / lows / closes as labelled dotted lines (liquidity levels)
  function ctxLevels(levels) {
    for (const l of S.ctxLines) candles.removePriceLine(l);
    S.ctxLines = [];
    const col = { high: C.gold, low: C.gold, close: C.slate, 'high-old': 'rgba(245,196,81,.55)', 'low-old': 'rgba(245,196,81,.55)' };
    for (const v of levels || []) {
      if (!ok(v.price)) continue;
      S.ctxLines.push(candles.createPriceLine({ price: v.price, color: col[v.kind] || C.slate, lineWidth: 1,
        lineStyle: LW.LineStyle.Dotted, axisLabelVisible: true, title: v.label }));
    }
  }

  // Fit prices to the candles again (dragging the price axis turns this off).
  function autoFit() {
    chart.priceScale('right').applyOptions({ autoScale: true });
    const n = S.bars.length;
    const ts = chart.timeScale();
    if (S.tf === 1) {
      // 1-minute: the latest two hours, following new candles
      ts.applyOptions({ shiftVisibleRangeOnNewBar: true });
      if (n) ts.setVisibleLogicalRange({ from: Math.max(-2, n - 120), to: Math.max(n, 120) + 4 });
    } else {
      // 5- and 15-minute: the whole session (09:15 to 15:30), filling from left to right
      const perDay = S.tf === 5 ? 75 : 25;
      ts.applyOptions({ shiftVisibleRangeOnNewBar: false });
      if (n) ts.setVisibleLogicalRange({ from: -1, to: Math.max(n, (S.ctxN || 0) + perDay) + 1 });
    }
  }

  async function loadChart() {
    if (!LW) { showEmpty(true); return; }
    if (S.rp.on) stopReplay(false);       // a new symbol / candle size ends a candle replay
    S.hist.detached = false; $('#jump-today').hidden = true;
    const tok = ++S.loadToken;
    const key = viewKey();
    const switching = S.view !== key;
    if (switching) { clearChart(); showEmpty(true, true); legend(null); }
    const q = S.tf === 1 ? 1 : 5;
    let list = [];
    try {
      if (S.sel.kind === 'index') {
        list = await getJSON(`/api/candles?index=${encodeURIComponent(S.sel.key)}&tf=${q}`);
      } else {
        const d = await getJSON(`/api/stock?name=${encodeURIComponent(S.sel.name)}&tf=${q}`);
        if (tok !== S.loadToken) return;
        S.stock = d;
        list = d.bars || [];
      }
    } catch {
      if (tok !== S.loadToken) return;
      // the app did not answer: keep what is on the chart and try again shortly
      if (switching) { emptyEl.hidden = false; emptyEl.textContent = `Couldn't load ${symLabel()}: the app isn't answering. Retrying…`; }
      setTimeout(() => { if (tok === S.loadToken) loadChart(); }, 2500);
      return;
    }
    if (tok !== S.loadToken) return;
    list = list.slice().sort((a, b) => a.time - b.time);
    if (list.length) {                    // one session only: never yesterday's candles next to today's
      const d = dayOf(list[list.length - 1].time);
      list = list.filter(b => dayOf(b.time) === d);
    }
    const today = list.length ? dayOf(list[list.length - 1].time) : null;
    let ctx = [], levels = [];
    if (S.sel.kind === 'index' && S.ctxDays > 0) {
      const c = await getJSON(`/api/context?index=${encodeURIComponent(S.sel.key)}&tf=${q}&days=${S.ctxDays}`).catch(() => null);
      if (tok !== S.loadToken) return;
      if (c) {
        ctx = (c.bars || []).filter(b => today == null || dayOf(b.time) !== today).sort((a, b) => a.time - b.time);
        if (list.length) ctx = ctx.filter(b => b.time < list[0].time);
        levels = c.levels || [];
      }
    }
    list = ctx.concat(list);
    S.five = new Map();
    if (S.tf === 15) { for (const b of list) S.five.set(b.time, b); list = agg15(list); }
    S.bars = list;
    S.last = list.length ? list[list.length - 1] : null;
    S.day = today;
    S.ctxN = today == null ? list.length : list.filter(b => dayOf(b.time) !== today).length;
    try {
      candles.setData(list.map(toC));
      volume.setData(list.map(toV));
    } catch (e) {
      console.warn('chart data rejected', e);
      clearChart(); showEmpty(true); return;
    }
    S.view = key;
    S.hist.earliest = list.length ? isoOf(list[0].time) : null; S.hist.more = true; S.hist.loading = false;
    S.lineKey = ''; S.markKey = 'x';
    ctxLevels(levels);
    decorate();
    if (switching) autoFit();
    showEmpty(today == null && !list.length);
    legend(null);
  }

  function applyBar(b) {
    if (!b || (S.last && b.time < S.last.time)) return;
    try {
      candles.update(toC(b));
      volume.update(toV(b));
    } catch (e) {                       // never leave the chart half-updated: reload it
      console.warn('chart update rejected', e);
      S.view = null; loadChart(); return;
    }
    if (!S.last || b.time > S.last.time) {
      if (S.day == null) { S.day = dayOf(b.time); showEmpty(false); }
      S.bars.push(b);
    } else {
      S.bars[S.bars.length - 1] = b;
    }
    S.last = b;
  }

  function onBars(d) {
    if (S.rp.on || S.hist.detached) return;   // the candle replay / a jumped-to day owns the chart
    if (!LW || S.view !== viewKey()) return;      // a different symbol or candle size is still loading
    let src = null;
    if (S.sel.kind === 'index') src = d[S.sel.key];
    else if (d.STOCK && d.STOCK.name === S.sel.name) src = d.STOCK;
    if (!src) return;
    const list = (src[S.tf === 1 ? '1' : '5'] || []).slice().sort((a, b) => a.time - b.time);
    if (!list.length) return;
    const newest = list[list.length - 1];
    const len = S.tf * 60;
    if (S.last && (newest.time < S.last.time - len || dayOf(newest.time) !== S.day)) { loadChart(); return; }  // replay restarted or new day
    for (const b of list) {
      if (S.tf === 15) { S.five.set(b.time, b); applyBar(bucket15(Math.floor(b.time / 900) * 900)); }
      else applyBar(b);
    }
    if (!S.hover) legend(null);
  }

  // ------------------------------------------------------------------ levels on the chart
  function pickByName(name) {
    if (!S.pred) return null;
    return (S.pred.picks || []).find(p => p.name === name) || (S.pred.selected && S.pred.selected.name === name ? S.pred.selected : null);
  }

  function levelsFor() {
    if (!S.pred || S.day == null) return null;
    const d0 = S.day;
    const base = { start: d0 + 9 * 3600 + 15 * 60, orEnd: d0 + 9 * 3600 + 30 * 60, plan: null, watch: [] };
    if (S.sel.kind === 'index') {
      const c = S.pred.indices[S.sel.key];
      if (!c) return null;
      const o = c.or && ok(c.or.high) ? c.or : null;
      const L = { ...base, or: o && { high: o.high, low: o.low, mid: o.mid, complete: o.complete, buf: o.buf || 0 } };
      if (c.plan) {
        const p = c.plan;
        L.plan = { side: p.side, entry: p.entry, stop: ok(p.stop_now) ? p.stop_now : p.stop, T1: p.targets.T1, T2: p.targets.T2, T3: p.targets.T3, t: hhmm(d0, p.signal_time),
                   trigger: p.trigger, tlabel: p.signal_time };
      } else if (c.watch && c.watch.length) {
        L.watch = c.watch.map(w => ({ side: w.side, trigger: w.trigger }));
      }
      const stt = (S.pred.trades || []).find(r => r.key === S.sel.key && r.strategy === 'Supertrend flip' && r.status !== 'missed');
      if (stt) L.st = { side: stt.side, entry: stt.entry_index, stop: stt.stop_index, t: hhmm(d0, stt.time), tlabel: stt.time };
      return L;
    }
    if (!S.stock || !S.stock.today) {
      const o = S.stock && S.stock.or;
      return o ? { ...base, or: { high: o.high, low: o.low, mid: (o.high + o.low) / 2, complete: true, buf: 0.2 * (o.high - o.low) } } : null;
    }
    const pk = pickByName(S.sel.name);
    const o = (pk && pk.or) || (S.stock && S.stock.or);
    const L = { ...base, or: o ? { high: o.high, low: o.low, mid: (o.high + o.low) / 2, complete: true, buf: 0.2 * (o.high - o.low) } : null };
    const lr = listedRow(S.sel.name);
    if (lr) {
      L.plan = { side: lr.side, entry: lr.entry, stop: lr.stop, target: lr.target, t: hhmm(d0, lr.time), tlabel: lr.time, listed: true };
    } else if (pk && pk.levels) {
      const v = pk.levels;
      L.plan = { side: v.side, entry: v.entry, stop: v.stop, T1: v.targets.T1, T2: v.targets.T2, T3: v.targets.T3,
                 t: pk.breakout ? hhmm(d0, pk.breakout.time) : null, ref: true, tlabel: pk.breakout ? pk.breakout.time : '',
                 trigger: pk.breakout ? (v.side === 'CE' ? v.up : v.dn) : null,
                 stoppedAt: pk.outcome && pk.outcome.status === 'stopped' ? hhmm(d0, pk.outcome.time) : null,
                 stoppedLabel: pk.outcome && pk.outcome.status === 'stopped' ? pk.outcome.time : '' };
    }
    return L;
  }

  function setLines(L) {
    const key = (L && L.plan ? ['p', L.plan.side, L.plan.entry, L.plan.stop, L.plan.T1, L.plan.T2, L.plan.T3, L.plan.ref].join('|')
      : L && L.watch.length ? 'w|' + L.watch.map(w => w.side + w.trigger).join('|') : '')
      + (L && L.st ? `|st${L.st.side}${L.st.entry}${L.st.stop}` : '')
      + (L && L.plan && L.plan.listed ? `|ls${L.plan.target}` : '')
      + (L && L.paper ? `|pp${L.paper.side}${L.paper.entry}${L.paper.stop}${L.paper.target}` : '');
    if (key === S.lineKey) return;
    S.lineKey = key;
    for (const l of S.lines) candles.removePriceLine(l);
    S.lines = [];
    if (!L) return;
    const add = (price, color, title, style) => {
      if (!ok(price)) return;
      S.lines.push(candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
    };
    if (L.plan && L.plan.listed) {
      add(L.plan.target, L.plan.side === 'CE' ? C.up : C.dn, 'Target', LW.LineStyle.Dashed);
      add(L.plan.entry, C.frost, 'Entry', LW.LineStyle.Solid);
      add(L.plan.stop, C.stop, 'Stop', LW.LineStyle.Solid);
    } else if (L.plan) {
      const dc = L.plan.side === 'CE' ? C.up : C.dn;
      add(L.plan.T3, dc, 'T3', LW.LineStyle.Dashed);
      add(L.plan.T2, dc, 'T2', LW.LineStyle.Dashed);
      add(L.plan.T1, dc, 'T1', LW.LineStyle.Dashed);
      add(L.plan.entry, C.frost, L.plan.ref ? 'Ref entry' : 'Entry', LW.LineStyle.Solid);
      add(L.plan.stop, C.stop, 'Stop', LW.LineStyle.Solid);
    } else {
      for (const w of L.watch) add(w.trigger, w.side === 'CE' ? C.up : C.dn, `${w.side} trigger`, LW.LineStyle.LargeDashed);
    }
    if (L.st) {
      add(L.st.entry, C.frost, 'ST entry', LW.LineStyle.Dotted);
      add(L.st.stop, C.stop, 'ST stop', LW.LineStyle.Dotted);
    }
    if (L.paper) {
      add(L.paper.entry, C.gold, 'Paper buy', LW.LineStyle.Solid);
      add(L.paper.stop, C.stop, 'Paper stop', LW.LineStyle.Dotted);
      add(L.paper.target, C.gold, 'Paper target', LW.LineStyle.Dotted);
    }
  }

  function agg5(list) {
    const out = [];
    let cur = null;
    for (const b of list) {
      const t = Math.floor(b.time / 300) * 300;
      if (!cur || cur.time !== t) { cur = { time: t, open: b.open, high: b.high, low: b.low, close: b.close }; out.push(cur); }
      else { cur.high = Math.max(cur.high, b.high); cur.low = Math.min(cur.low, b.low); cur.close = b.close; }
    }
    return out.filter(b => b.time + 300 <= (S.last ? S.last.time + S.tf * 60 : Infinity));   // only finished 5-min candles
  }
  // ------------------------------------------------------------------ v14.34: what each day on the chart did
  // Your confirmed breakout of the 09:15-09:30 range, then the first opposite-colour candle after it, read as
  // "pullback" (small, shallow, still well outside: in testing the move made a new high before closing back inside
  // the range 3 times in 4) or "reversal risk" (big, deep, or at the range line: back inside first 7 times in 10).
  const SCALE = 283;
  function five(dayBars) {
    if (S.tf === 5) return dayBars.filter(b => b.closed !== false);
    if (S.tf === 1) return agg5(dayBars.filter(b => b.closed !== false));
    const d = dayOf(dayBars[0].time);
    return [...S.five.values()].filter(b => dayOf(b.time) === d && b.closed !== false).sort((a, b) => a.time - b.time);
  }
  function dayReads() {
    if (!S.bars.length) return [];
    const key = S.bars.length + ':' + S.bars[0].time + ':' + S.bars[S.bars.length - 1].time + ':' + S.tf + ':' + (S.last && S.last.close);
    if (S.readKey === key) return S.readsCache;
    const byDay = new Map();
    for (const b of S.bars) { const d = dayOf(b.time); if (!byDay.has(d)) byDay.set(d, []); byDay.get(d).push(b); }
    const days = [...byDay.keys()];
    const ranges = days.map(d => { const L = byDay.get(d); return Math.max(...L.map(b => b.high)) - Math.min(...L.map(b => b.low)); });
    const out = [];
    days.forEach((d, di) => {
      const B = five(byDay.get(d));
      if (B.length < 5 || B[0].time !== d + 9 * 3600 + 15 * 60) return;
      const prev = ranges.slice(Math.max(0, di - 14), di);
      const atr = prev.length >= 3 ? prev.reduce((a, v) => a + v, 0) / prev.length : B[0].open * (S.sel.kind === 'index' ? 0.011 : 0.022);
      const u = atr / SCALE;                                    // one NIFTY-equivalent point in this symbol's points
      const H = Math.max(B[0].high, B[1].high, B[2].high), Lo = Math.min(B[0].low, B[1].low, B[2].low);
      const r = { d, iso: new Date(d * 1000).toISOString().slice(0, 10) };
      for (let k = 4; k < Math.min(B.length, 57); k++) {
        const p = B[k - 1], c = B[k];
        const up = p.close > H && c.low > H && c.close > H, dn = p.close < Lo && c.high < Lo && c.close < Lo;
        if (!up && !dn) continue;
        const hm = t => new Date(t * 1000).toISOString().slice(11, 16);
        r.brk = { up, t: c.time + 300, label: hm(c.time + 300) };
        const s = up ? 1 : -1, edge = up ? H : Lo;
        for (let j = k + 1; j < Math.min(B.length, 60); j++) {
          const o = B[j];
          if (!(up ? o.close < o.open : o.close > o.open)) continue;
          const ext = up ? Math.max(...B.slice(k, j + 1).map(b => b.high)) : Math.min(...B.slice(k, j + 1).map(b => b.low));
          const body = Math.abs(o.close - o.open) / u;
          const depth = (ext - o.close) * s / Math.max((ext - edge) * s, 1e-9);
          const outside = (o.close - edge) * s / u;
          const past = up ? o.close < B[j - 1].low : o.close > B[j - 1].high;
          const v = (body < 25 && depth < 0.5 && !past) ? 'pull' : (body > 40 || depth > 0.7 || outside < 5) ? 'rev' : 'mid';
          r.opp = { t: o.time + 300, label: hm(o.time + 300), v, body: Math.round(body * u), depth: Math.round(depth * 100) };
          break;
        }
        break;
      }
      out.push(r);
    });
    S.readKey = key; S.readsCache = out;
    return out;
  }
  const OPP = { pull: ['Pullback', 'Pullback (went on 3 in 4)'],
                rev: ['Reversal risk', 'Reversal risk (back inside 7 in 10)'],
                mid: ['50/50', '50/50'] };

  function setMarkers(L) {
    const ms = [];
    if (LW && S.bars.length) {           // your confirmed breakout and the first pullback after it, on every day (v14.34)
      const len = S.tf * 60, have = new Set(S.bars.map(b => b.time));
      const at = t => { const bt = Math.floor((t - 60) / len) * len; return have.has(bt) ? bt : null; };
      for (const r of dayReads()) {
        if (r.brk) { const t = at(r.brk.t); if (t != null) ms.push({ time: t, position: r.brk.up ? 'belowBar' : 'aboveBar', shape: r.brk.up ? 'arrowUp' : 'arrowDown', color: r.brk.up ? C.up : C.dn, text: `Breakout ✓ ${r.brk.label}` }); }
        if (r.opp) {
          const t = at(r.opp.t);
          if (t != null) ms.push({ time: t, position: r.brk.up ? 'aboveBar' : 'belowBar', shape: 'circle',
            color: r.opp.v === 'pull' ? C.gold : r.opp.v === 'rev' ? C.stop : C.slate, text: OPP[r.opp.v][0] });
        }
      }
    }
    if (L && L.plan && L.plan.t) {
      const len = S.tf * 60;
      const bt = Math.floor((L.plan.t - 60) / len) * len;
      if (S.bars.some(b => b.time === bt)) {
        const up = L.plan.side === 'CE';
        ms.push({ time: bt, position: up ? 'belowBar' : 'aboveBar', shape: up ? 'arrowUp' : 'arrowDown',
                  color: up ? C.up : C.dn,
                  text: (L.plan.ref ? 'Breakout ' : 'Buy ') + L.plan.side + (L.plan.tlabel ? ' ' + L.plan.tlabel : '') });
      }
    }
    if (L && L.plan && L.plan.stoppedAt) {
      const len = S.tf * 60;
      const bt = Math.floor((L.plan.stoppedAt - 60) / len) * len;
      if (S.bars.some(b => b.time === bt)) {
        ms.push({ time: bt, position: L.plan.side === 'CE' ? 'belowBar' : 'aboveBar', shape: 'circle', color: C.stop,
                  text: 'Stopped ' + L.plan.stoppedLabel });
      }
      ms.sort((a, b) => a.time - b.time);
    }
    if (L && L.st && L.st.t) {
      const len = S.tf * 60;
      const bt = Math.floor((L.st.t - 60) / len) * len;
      if (S.bars.some(b => b.time === bt)) {
        const up = L.st.side === 'CE';
        ms.push({ time: bt, position: up ? 'belowBar' : 'aboveBar', shape: up ? 'arrowUp' : 'arrowDown', color: C.gold,
                  text: `Supertrend ${L.st.side} ${L.st.tlabel}` });
      }
    }
    ms.sort((a, b) => a.time - b.time);
    const key = JSON.stringify(ms);
    if (key === S.markKey) return;
    S.markKey = key;
    markers.setMarkers(ms);
  }

  function paperLevels() {
    const t = S.ticket && S.ticket.open;
    if (!t || S.ticketKey !== ticketKey()) return null;
    return { side: t.side, entry: t.entry_spot, stop: ok(t.stop_now) ? t.stop_now : t.stop_spot, target: t.target_spot };
  }

  function decorate() {
    if (!LW) return;
    if (S.hist.detached) {                 // an old day: no live plan lines, only the day marks
      S.levels = { watch: [] }; setLines(S.levels); setMarkers(S.levels); return;
    }
    S.levels = levelsFor();
    const pp = paperLevels();
    if (pp) S.levels = { ...(S.levels || { watch: [] }), paper: pp };
    setLines(S.levels);
    setMarkers(S.levels);
  }

  // Opening-range corridor and risk / reward zones, drawn over the chart.
  function draw() {
    const w = ov.clientWidth || frame.clientWidth, h = ov.clientHeight || frame.clientHeight, dpr = window.devicePixelRatio || 1;
    const W = Math.round(w * dpr), H = Math.round(h * dpr);
    if (ov.width !== W || ov.height !== H) { ov.width = W; ov.height = H; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const L = S.levels;
    if (!LW || !L || !S.last || !S.bars.length) return;
    const ts = chart.timeScale();
    let ph = h - ts.height();
    try { ph = chart.panes()[0].getHeight(); } catch { /* one pane */ }   // keep the shading on the price pane
    const pw = ts.width();
    if (pw <= 0 || ph <= 0) return;
    const y = p => (ok(p) ? candles.priceToCoordinate(p) : null);
    const half = (ts.options().barSpacing || 8) / 2;
    const xAt = t => { const x = ts.timeToCoordinate(t); return x === null ? null : x; };
    const firstX = xAt(S.bars[0].time);
    let x0 = xAt(Math.floor(L.start / (S.tf * 60)) * S.tf * 60);
    if (x0 === null) x0 = firstX === null ? 0 : firstX;
    x0 -= half;
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, pw, ph); ctx.clip();

    // day boundaries when earlier days are shown
    if (S.ctxN) {
      ctx.strokeStyle = 'rgba(126,148,230,.30)'; ctx.setLineDash([3, 4]); ctx.lineWidth = 1;
      ctx.fillStyle = 'rgba(143,155,198,.85)'; ctx.font = '600 10px Manrope, system-ui, sans-serif';
      let prev = null;
      for (const b of S.bars) {
        const d = dayOf(b.time);
        if (d !== prev) {
          const x = xAt(b.time);
          if (x !== null) {
            const xx = x - half;
            if (prev !== null) { ctx.beginPath(); ctx.moveTo(xx, 0); ctx.lineTo(xx, ph); ctx.stroke(); }
            ctx.fillText(new Date(b.time * 1000).toLocaleDateString('en-GB', { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short' }), xx + 4, 12);
          }
          prev = d;
        }
      }
      ctx.setLineDash([]);
    }

    // risk / reward zones from the signal bar
    if (L.plan) {
      const p = L.plan;
      const len = S.tf * 60;
      let xs = p.t ? xAt(Math.floor((p.t - 60) / len) * len) : null;
      xs = xs === null ? Math.max(x0, 0) : xs - half;
      const ye = y(p.entry), yst = y(p.stop), yt = y(p.T3);
      if (ye !== null && yt !== null) {
        const g = ctx.createLinearGradient(xs, 0, pw, 0);
        const rgb = p.side === 'CE' ? '92,225,230' : '255,138,61';
        g.addColorStop(0, `rgba(${rgb},${p.ref ? 0.05 : 0.09})`); g.addColorStop(1, `rgba(${rgb},0.02)`);
        ctx.fillStyle = g; ctx.fillRect(xs, Math.min(ye, yt), pw - xs, Math.abs(yt - ye));
      }
      if (ye !== null && yst !== null) {
        const g = ctx.createLinearGradient(xs, 0, pw, 0);
        g.addColorStop(0, `rgba(255,92,122,${p.ref ? 0.07 : 0.12})`); g.addColorStop(1, 'rgba(255,92,122,0.03)');
        ctx.fillStyle = g; ctx.fillRect(xs, Math.min(ye, yst), pw - xs, Math.abs(yst - ye));
      }
    }

    // the opening-range corridor
    if (L.or) {
      const yh = y(L.or.high), yl = y(L.or.low), ym = y(L.or.mid);
      if (yh !== null && yl !== null) {
        const g = ctx.createLinearGradient(x0, 0, pw, 0);
        g.addColorStop(0, 'rgba(143,155,198,0.17)'); g.addColorStop(1, 'rgba(143,155,198,0.04)');
        ctx.fillStyle = g; ctx.fillRect(x0, yh, pw - x0, yl - yh);
        ctx.lineWidth = 1;
        ctx.shadowColor = 'rgba(92,225,230,0.55)'; ctx.shadowBlur = 8;
        ctx.strokeStyle = 'rgba(230,236,255,0.55)';
        ctx.beginPath(); ctx.moveTo(x0, Math.round(yh) + 0.5); ctx.lineTo(pw, Math.round(yh) + 0.5);
        ctx.moveTo(x0, Math.round(yl) + 0.5); ctx.lineTo(pw, Math.round(yl) + 0.5); ctx.stroke();
        ctx.shadowBlur = 0;
        if (ym !== null) {
          ctx.setLineDash([2, 4]); ctx.strokeStyle = 'rgba(230,236,255,0.22)';
          ctx.beginPath(); ctx.moveTo(x0, Math.round(ym) + 0.5); ctx.lineTo(pw, Math.round(ym) + 0.5); ctx.stroke();
          ctx.setLineDash([]);
        }
        // breakout margins (a 5-minute close beyond these is the signal)
        if (L.or.complete && L.or.buf > 0 && !L.plan && !L.watch.length) {   // index triggers are drawn as price lines
          let xe = xAt(Math.floor(L.orEnd / (S.tf * 60)) * S.tf * 60);
          xe = xe === null ? x0 : xe - half;
          ctx.setLineDash([6, 5]);
          const yu = y(L.or.high + L.or.buf), yd = y(L.or.low - L.or.buf);
          if (yu !== null) { ctx.strokeStyle = 'rgba(92,225,230,0.5)'; ctx.beginPath(); ctx.moveTo(xe, Math.round(yu) + 0.5); ctx.lineTo(pw, Math.round(yu) + 0.5); ctx.stroke(); }
          if (yd !== null) { ctx.strokeStyle = 'rgba(255,138,61,0.5)'; ctx.beginPath(); ctx.moveTo(xe, Math.round(yd) + 0.5); ctx.lineTo(pw, Math.round(yd) + 0.5); ctx.stroke(); }
          ctx.setLineDash([]);
        }
        ctx.font = '600 10.5px Manrope, "Segoe UI", sans-serif';
        const label = L.or.complete ? 'Opening range 09:15 to 09:30' : 'Opening range forming';
        const tw = ctx.measureText(label).width;
        const lx = Math.min(Math.max(x0 + 8, 8), pw - tw - 120);
        // the range label sits on the side away from a call's trigger (above the range) or a put's (below it)
        const below = (L.plan && L.plan.side === 'CE') || yh <= 22;
        const ly = below ? yl + 16 : yh - 8;
        ctx.fillStyle = 'rgba(10,19,48,0.82)';
        ctx.fillRect(lx - 5, ly - 11, tw + 10, 16);
        ctx.fillStyle = 'rgba(214,222,250,0.9)';
        ctx.fillText(label, lx, ly);
      }
    }
    // after a signal: the trigger it had to clear, from the end of the range to the signal candle
    if (L.plan && ok(L.plan.trigger)) {
      const p = L.plan, len = S.tf * 60;
      const yt = y(p.trigger);
      let xe = xAt(Math.floor(L.orEnd / len) * len);
      xe = xe === null ? x0 : xe - half;
      let xs = p.t ? xAt(Math.floor((p.t - 60) / len) * len) : null;
      xs = xs === null ? pw : xs + half;
      if (yt !== null && xs > xe) {
        const col = p.side === 'CE' ? 'rgba(92,225,230,0.85)' : 'rgba(255,138,61,0.85)';
        ctx.setLineDash([5, 4]); ctx.strokeStyle = col; ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(xe, Math.round(yt) + 0.5); ctx.lineTo(xs, Math.round(yt) + 0.5); ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = '600 10.5px Manrope, "Segoe UI", sans-serif';
        const label = `${p.side} trigger ${nf2.format(p.trigger)}`;
        const tw = ctx.measureText(label).width;
        let lx, ly;
        if (tw + 14 <= xs - xe) {            // room before the signal: on the side away from the candles that had not cleared it
          lx = Math.max(xe + 6, 6); ly = p.side === 'CE' ? yt - 7 : yt + 15;
        } else {                              // no room: just after the signal candle, on the side price has left
          lx = Math.min(xs + 8, pw - tw - 8); ly = p.side === 'CE' ? yt + 15 : yt - 7;
        }
        ctx.fillStyle = 'rgba(10,19,48,0.85)'; ctx.fillRect(lx - 4, ly - 11, tw + 8, 15);
        ctx.fillStyle = col; ctx.fillText(label, lx, ly);
      }
    }
    ctx.restore();
  }
  const fitBtn = $('#fit');
  fitBtn.addEventListener('click', autoFit);
  frame.addEventListener('dblclick', autoFit);
  let lastDraw = 0;
  function loop(t) {
    requestAnimationFrame(loop);
    if (document.hidden || t - lastDraw < 80) return;
    lastDraw = t;
    draw();
    if (LW) fitBtn.hidden = chart.priceScale('right').options().autoScale !== false;
    if (S.downSince && t % 1000 < 100) renderFeed();
  }

  // ------------------------------------------------------------------ top rail and banner
  // If the app was updated and restarted while this page was open, load the new dashboard.
  const PAGE_BUILD = document.documentElement.dataset.build;
  function checkBuild(st) {
    const rs_ = $('#restart');
    const stale = st.build_on_disk && st.build && st.build_on_disk !== st.build;
    if (stale) rs_.innerHTML = `<b>Version ${esc(st.build_on_disk)} is installed, but the app is still running ${esc(st.build)}.</b> `
      + 'The dashboard\'s Restart button only restarts the replay. Close the app window (or press Ctrl+C in it) and start it again to use the new version.';
    rs_.hidden = !stale;
    if (!st.build || !PAGE_BUILD || PAGE_BUILD.startsWith('__') || st.build === PAGE_BUILD) return;
    try { if (sessionStorage.getItem('io.reloaded') === st.build) return; sessionStorage.setItem('io.reloaded', st.build); } catch { /* reload anyway */ }
    location.reload();
  }

  function onState(st) {
    checkBuild(st);
    S.state = st;
    $('#clk-ist').textContent = st.now_ist || '--:--:--';
    $('#clk-uk').textContent = (st.now_uk || '--:--').slice(0, 5);
    const v = st.vix || {};
    $('#r-vix').textContent = ok(v.ltp) && v.ltp > 0 ? px(v.ltp) : '—';
    const vc = $('#r-vix-chg');
    vc.textContent = ok(v.chg) ? signed(v.chg, x => x.toFixed(2)) : '';
    vc.className = tone(v.chg) === 'up' ? 'dn' : tone(v.chg) === 'dn' ? 'up' : '';   // rising VIX = fear
    // Paper P&L: booked (closed trades) plus open trades marked at the current bid.
    const t = st.today || {};
    const money = v => signed(v, x => '₹' + nf0.format(x));
    const booked = ok(t.realized) ? t.realized : 0;
    let open = 0, nOpen = 0;
    for (const k of Object.keys(st.indices || {})) {
      const tr = st.indices[k].trade;
      if (tr && tr.status === 'open' && tr.live_pnl && ok(tr.live_pnl.pnl)) { open += tr.live_pnl.pnl; nOpen++; }
    }
    const pnl = $('#r-pnl');
    pnl.textContent = money(booked + open);
    pnl.className = tone(booked + open);
    const parts = [`${money(booked)} booked`];
    if (nOpen) parts.push(`${money(open)} open`);
    $('#r-trades').textContent = parts.join(', ');
    $('#r-pnl').parentElement.title = `The tested NIFTY/SENSEX signals the app tracks by itself (not your own paper trades: those are under Paper trading). Booked = closed (${money(booked)}). `
      + `Open = trades still running, valued at the current bid (${money(open)}); they are booked when they exit `
      + `(stop or 15:00 IST). ${ok(t.trades) ? `${t.trades} of ${t.max_trades} trades used.` : ''}`;
    renderFeed();
    const src = st.source || {};
    const rp = $('#replay');
    rp.hidden = !(st.mode === 'demo' || st.mode === 'replay');
    if (!rp.hidden) {
      $('#rp-play').textContent = src.playing ? 'Pause' : 'Play';
      $('#rp-play').setAttribute('aria-label', src.playing ? 'Pause replay' : 'Play replay');
      const sp = $('#rp-speed');
      if (document.activeElement !== sp && src.speed) {
        const want = String(Math.round(src.speed));
        if (![...sp.options].some(o => o.value === want)) sp.add(new Option(want + 'x', want));
        sp.value = want;
      }
    }
    if (S.pred) { renderCalls(); renderSheet(); }
  }

  function renderFeed() {
    const el = $('#r-feed');
    const st = S.state;
    let s = 'connecting', text = 'Connecting';
    if (S.opened && !S.connected) {
      s = 'down';
      text = S.downSince && Date.now() - S.downSince > 8000
        ? 'Not connected: is the app running? Close extra India Options tabs' : 'Reconnecting to the app';
    }
    else if (st && st.feed) {
      const f = st.feed;
      s = f.state === 'live' ? 'live' : (f.state === 'stale' || f.state === 'waiting') ? 'stale' : f.state === 'down' || f.state === 'error' ? 'down' : 'closed';
      text = st.mode === 'demo' ? 'Demo, synthetic prices'
        : st.mode === 'replay' ? 'Replaying a recorded day'
        : f.state === 'live' ? 'Live feed' : f.state === 'stale' ? 'Feed delayed' : (f.reason || 'Market closed');
    }
    el.dataset.s = s;
    el.querySelector('span').textContent = text;
    el.title = st && st.note ? st.note : '';
  }

  function renderBanner() {
    const b = $('#banner');
    const show = S.pred && S.pred.settings && S.pred.settings.stop_rule === 'mid' && store.get('io.banner.stop') !== '1';
    if (show && b.hidden) {
      const ml = S.state && S.state.today ? S.state.today.max_loss : null;
      $('#banner-text').innerHTML = '<b>Stop rule.</b> Stops are on the middle of the opening range; the backtested model uses the far side. '
        + 'The far side doubles the stop distance, so 1 lot may lose more than your max loss per trade' + (ok(ml) ? ` (${rs(ml)})` : '') + '.';
    }
    if (b.hidden === !show) return;
    b.hidden = !show;
    document.documentElement.style.setProperty('--banner-h', show ? b.offsetHeight + 'px' : '0px');
  }
  // keep the space under the banner right when the window is resized (the banner text wraps differently)
  new ResizeObserver(() => {
    const b = $('#banner');
    if (!b.hidden) document.documentElement.style.setProperty('--banner-h', b.offsetHeight + 'px');
  }).observe($('#banner'));
  $('#banner-go').addEventListener('click', async () => {
    try {
      const r = await postJSON('/api/settings', { stop_rule: 'far' });
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error((d.errors || ['Could not save']).join('; '));
      $('#banner').hidden = true;
      document.documentElement.style.setProperty('--banner-h', '0px');
    } catch (e) {
      $('#banner-text').textContent = `The stop rule was not changed: ${e.message}. Change it under Settings in the classic view.`;
    }
  });
  $('#banner-no').addEventListener('click', () => {
    store.set('io.banner.stop', '1');
    $('#banner').hidden = true;
    document.documentElement.style.setProperty('--banner-h', '0px');
  });

  // replay controls (demo and recorded days only)
  $('#rp-play').addEventListener('click', () => {
    const playing = S.state && S.state.source && S.state.source.playing;
    postJSON('/api/replay', { action: playing ? 'pause' : 'play' });
  });
  $('#rp-speed').addEventListener('change', e => postJSON('/api/replay', { action: 'speed', speed: Number(e.target.value) }));
  $('#rp-restart').addEventListener('click', () => postJSON('/api/replay', { action: 'restart' }).then(() => setTimeout(loadChart, 600)));

  // ------------------------------------------------------------------ symbol tabs and quote
  function renderTabs() {
    const items = INDEX_ORDER.filter(k => !S.pred || S.pred.indices[k]).map(k => ({
      id: 'i:' + k, label: (S.pred && S.pred.indices[k] && S.pred.indices[k].label) || INDEX_LABEL[k],
      sel: S.sel.kind === 'index' && S.sel.key === k,
    }));
    if (S.sel.kind === 'stock') items.push({ id: 's:' + S.sel.name, label: S.sel.name, sel: true, close: true });
    const key = items.map(i => i.id + i.sel).join(',');
    if (key === S.tabsKey) return;
    S.tabsKey = key;
    $('#symbols').innerHTML = items.map(i => `<button type="button" role="tab" data-id="${esc(i.id)}" aria-selected="${i.sel}">${esc(i.label)}`
      + (i.close ? `<span class="x" data-close aria-label="Close ${esc(i.label)}" title="Back to ${esc(INDEX_LABEL[S.lastIndex])}">×</span>` : '') + '</button>').join('');
  }
  $('#symbols').addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    if (e.target.closest('[data-close]')) { select({ kind: 'index', key: S.lastIndex }); return; }
    const [k, v] = btn.dataset.id.split(':');
    select(k === 'i' ? { kind: 'index', key: v } : { kind: 'stock', name: v });
  });
  // full-screen chart: the browser's full screen when allowed, else the chart fills the window
  const stageEl = document.querySelector('.stage');
  const fsOn = () => document.fullscreenElement === stageEl || document.webkitFullscreenElement === stageEl
    || stageEl.classList.contains('max');
  function fsIcon() {
    const on = fsOn();
    $('#fs-ico').setAttribute('d', on ? 'M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5' : 'M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5');
    $('#fs-btn').title = on ? 'Back to normal (Esc or F)' : 'Full screen chart (F). Esc or click again to go back';
    $('#fs-btn').setAttribute('aria-label', on ? 'Exit full screen' : 'Full screen chart');
  }
  async function fsToggle() {
    if (fsOn()) {
      if (stageEl.classList.contains('max')) stageEl.classList.remove('max');
      else if (document.exitFullscreen) await document.exitFullscreen().catch(() => {});
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    } else {
      try {
        if (stageEl.requestFullscreen) await stageEl.requestFullscreen();
        else if (stageEl.webkitRequestFullscreen) stageEl.webkitRequestFullscreen();
        else throw new Error('no fullscreen');
      } catch { stageEl.classList.add('max'); }
    }
    fsIcon();
  }
  $('#fs-btn').addEventListener('click', fsToggle);
  document.addEventListener('fullscreenchange', fsIcon);
  document.addEventListener('webkitfullscreenchange', fsIcon);
  document.addEventListener('keydown', e => {
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
    if ((e.key === 'f' || e.key === 'F') && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); fsToggle(); }
    else if (e.key === 'Escape' && stageEl.classList.contains('max')) { stageEl.classList.remove('max'); fsIcon(); }
  });

  $('#tfs').addEventListener('click', e => {
    const btn = e.target.closest('button[data-tf]');
    if (!btn) return;
    S.tf = Number(btn.dataset.tf);
    store.set('io.tf', String(S.tf));
    for (const b of $('#tfs').querySelectorAll('button')) b.setAttribute('aria-selected', String(b === btn));
    loadChart();
  });

  function renderQuote() {
    let ltp, chg, chgPct;
    if (S.sel.kind === 'index') {
      const c = S.pred && S.pred.indices[S.sel.key];
      if (c) { ltp = c.ltp; chg = c.chg; chgPct = c.chg_pct; }
    } else {
      const pk = pickByName(S.sel.name);
      ltp = pk ? pk.ltp : S.stock && S.stock.ltp;
      chgPct = pk ? pk.pct : S.stock && S.stock.pct;
    }
    const html = ok(ltp) ? `<span class="num">${px(ltp)}</span><small class="${tone(chgPct)}">${ok(chg) ? signed(chg, px) + ' ' : ''}(${pct(chgPct)})</small>` : '';
    const q = $('#quote');
    if (q._h !== html) { q.innerHTML = html; q._h = html; }
  }

  // ------------------------------------------------------------------ index call cards
  function tradeOf(k) { return S.state && S.state.indices && S.state.indices[k] ? S.state.indices[k].trade : null; }

  function tradeLine(tr) {
    if (!tr) return '';
    const v = ok(tr.pnl) ? tr.pnl : tr.live_pnl && ok(tr.live_pnl.pnl) ? tr.live_pnl.pnl : null;
    const word = tr.status === 'open' ? 'Paper trade open' : tr.status === 'closed' ? 'Paper trade closed' : 'Paper trade';
    return ok(v) ? `${word}: <b class="${tone(v)}">${signed(v, x => '₹' + nf0.format(x))}</b>` : word;
  }

  function cardHTML(c, k) {
    const rt = c.rating;
    const n = rt && !rt.pending ? rt.stars : 0;
    const top = `<div class="top"><span><span class="name">${esc(c.label)}</span>${c.tested ? '' : '<span class="tag" title="Same rule, but NIFTY BANK was not part of the backtest">Untested</span>'}</span>`
      + (n ? stars(n, rt.label) : '') + '</div>'
      + `<div class="px"><span class="num">${px(c.ltp)}</span><small class="${tone(c.chg_pct)}">${pct(c.chg_pct)}</small></div>`;
    let body = '';
    if (c.plan) {
      const p = c.plan, d = dir(p.side), o = p.atm;
      const prem = o && ok(o.ltp) ? o.ltp : p.premium_now;
      body = `<div class="call"><span class="act ${d}">BUY ${p.side}</span><span class="ct">${esc(strikeText(o))}</span>`
        + `<span class="when" title="The ${esc(minus5(p.signal_time))}–${esc(p.signal_time)} candle closed beyond the trigger">${esc(p.signal_time)} IST</span></div>`
        + `<dl class="lv"><div><dt>Entry</dt><dd>${px(p.entry)}</dd></div><div><dt>Stop</dt><dd class="stop">${px(p.stop)}</dd></div>`
        + `<div><dt>Premium</dt><dd>${rs(prem, 2)}</dd></div>`
        + `<div><dt>T1</dt><dd class="${d}">${px(p.targets.T1)}</dd></div><div><dt>T2</dt><dd class="${d}">${px(p.targets.T2)}</dd></div>`
        + `<div><dt>T3</dt><dd class="${d}">${px(p.targets.T3)}</dd></div></dl>`;
    } else if (c.watch && c.watch.length) {
      const one = c.watch.length === 1 ? c.watch[0] : null;
      body = (one
        ? `<p class="nowait">No trade yet. Buy <span class="${dir(one.side)}">${one.side}</span> only if a 5-min candle closes ${one.side === 'CE' ? 'above' : 'below'}:</p>`
        : '<p class="nowait">No trade yet. Waiting for a 5-min close beyond a trigger:</p>')
        + c.watch.map(w => `<div class="watch">${one ? '' : `<span class="act ghost ${dir(w.side)}">${w.side}</span>`}`
          + `<b>${px(w.trigger)}</b><small>${n0(Math.abs(w.distance))} pts away</small></div>`).join('');
    } else {
      body = `<p class="idle">${esc(c.status)}</p>`;
    }
    const tl = tradeLine(tradeOf(k));
    const foot = tl || (rt ? (rt.pending ? 'Move-size rating from 09:30 IST' : esc(rt.label)) : '');
    return top + body + (foot ? `<div class="foot"><span>${foot}</span></div>` : '');
  }

  function renderCalls() {
    const host = $('#calls');
    for (const k of INDEX_ORDER) {
      const c = S.pred.indices[k];
      if (!c) continue;
      let el = host.querySelector(`[data-k="${k}"]`);
      if (!el) {
        el = document.createElement('button');
        el.type = 'button'; el.className = 'card'; el.dataset.k = k;
        el.addEventListener('click', () => select({ kind: 'index', key: k }));
        host.appendChild(el);
      }
      const side = c.plan ? c.plan.side : (c.watch && c.watch.length === 1 ? c.watch[0].side : null);
      el.classList.toggle('is-up', side === 'CE');
      el.classList.toggle('is-dn', side === 'PE');
      el.setAttribute('aria-pressed', String(S.sel.kind === 'index' && S.sel.key === k));
      const html = cardHTML(c, k);
      if (el._h !== html) { el.innerHTML = html; el._h = html; }
      const prev = S.phases[k];
      if (prev && prev !== 'signal' && c.phase === 'signal') {
        el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
      }
      S.phases[k] = c.phase;
    }
    const sr = S.pred.settings || {};
    $('#calls-note').textContent = sr.stop_rule === 'far' ? 'Stop: far side of the range' : 'Stop: middle of the range';
  }

  // ------------------------------------------------------------------ trade sheet
  function ladderHTML(side, rows, cols, idxName) {
    // rows: [{k, lbl, cls, lvl}], cols: [{head, sub, flag, at, base}]
    const d = dir(side);
    const head = `<tr><th>Level</th><th><span class="k">${esc(idxName)}</span>points</th>`
      + cols.map(c => `<th><span class="k">${esc(c.head)}</span>${c.flag ? `<span class="flag">${esc(c.flag)}</span> ` : ''}${esc(c.sub || '')}</th>`).join('') + '</tr>';
    const entry = rows.find(r => r.k === 'entry');
    const body = rows.map(r => {
      const dist = entry && ok(r.lvl) && r.k !== 'entry' ? signed(r.lvl - entry.lvl, n0) + ' pts' : '';
      const cells = cols.map(c => {
        const v = c.at ? c.at[r.k] : null;
        const chg = ok(v) && ok(c.base) && c.base > 0 && r.k !== 'entry' ? signed((v / c.base - 1) * 100, x => x.toFixed(0)) + '%' : '';
        return `<td>${rs(v, 2)}${chg ? `<span class="sub">${chg}</span>` : ''}</td>`;
      }).join('');
      const rr = { T1: '1R', T2: '2R', T3: '3R', stop: '−1R' }[r.k];
      return `<tr class="${r.cls}"><td>${esc(r.lbl)}${rr ? `<span class="sub">${rr}</span>` : ''}</td><td>${px(r.lvl)}${dist ? `<span class="sub">${dist}</span>` : ''}</td>${cells}</tr>`;
    }).join('');
    return `<table class="ladder ${d}"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  const optCol = (o, flag) => o ? {
    head: `${n0(o.strike)} ${o.opt}`, flag: flag || o.money || '',
    sub: ok(o.ltp) ? `now ${rs(o.ltp, 2)}` : `exp ${dShort(o.expiry)}`, at: o.at, base: o.at ? o.at.entry : null,
  } : null;

  function meterHTML(rt, side) {
    if (!rt) return '<p class="meter-note">The move-size model did not load.</p>';
    if (rt.pending) return `<p class="meter-note">${esc(rt.text)}</p>`;
    const pc = v => ok(v) ? Math.round(v * 100) : 0;
    const rows = [
      ['Big move up', rt.p_big_up, 'bu', side === 'CE'], ['Medium', rt.p_medium, 'md', false],
      ['Small / range', rt.p_small, 'sm', false], ['Big move down', rt.p_big_dn, 'bd', side === 'PE'],
    ];
    return '<div class="meter">' + rows.map(([lbl, p, cls, mine]) =>
      `<div class="row ${cls}${mine ? ' mine' : ''}"><span>${lbl}</span><span class="bar"><i style="width:${pc(p)}%"></i></span><span class="num">${pc(p)}%</span></div>`).join('')
      + `</div><p class="meter-note">${esc(rt.text)}</p>`
      + '<ul class="legend-stars">'
      + `<li>${stars(3)} Big move our way likely (50%+)</li>`
      + `<li>${stars(2)} Could extend, or a normal day</li>`
      + `<li>${stars(1)} Small, range-bound day likely (70%+)</li></ul>`;
  }

  function sheetIndex(k) {
    const c = S.pred.indices[k];
    const rt = c.rating;
    const n = rt && !rt.pending ? rt.stars : 0;
    const side = c.plan ? c.plan.side : (c.watch && c.watch.length === 1 ? c.watch[0].side : null);
    const tr = tradeOf(k);
    let head = `<div class="sheet-head"><span class="title">${esc(c.label)}</span>`
      + (n ? stars(n, rt.label, 'lg') + `<span class="rlabel">${esc(rt.label)}</span>` : '')
      + (c.tested ? '' : '<span class="pill warn">Not in the backtest</span>')
      + `<span class="pill${c.phase === 'signal' ? ' live' : ''}">${esc(c.status)}</span>`
      + (tr ? `<span class="note">${tradeLine(tr)}</span>` : '') + '</div>';
    let plan = '';
    if (c.plan) {
      const p = c.plan;
      const rows = [
        { k: 'T3', lbl: 'T3', cls: 't', lvl: p.targets.T3 }, { k: 'T2', lbl: 'T2', cls: 't', lvl: p.targets.T2 },
        { k: 'T1', lbl: 'T1', cls: 't', lvl: p.targets.T1 }, { k: 'entry', lbl: 'Entry', cls: 'e', lvl: p.entry },
        { k: 'stop', lbl: 'Stop', cls: 's', lvl: p.stop },
      ];
      const cols = [optCol(p.atm, `Suggested ${p.atm && p.atm.money ? p.atm.money : ''}`.trim()), optCol(p.otm)].filter(Boolean);
      const o = p.atm || {};
      const facts = [
        `Buy <b>${esc(strikeText(o))}</b> at or below <b>${rs(p.entry_limit, 2)}</b>; skip it above <b>${rs(p.skip_above, 2)}</b>.`,
        `Premium stop about <b>${rs(p.prem_stop, 2)}</b>. 1 lot is ${n0(p.lot)} qty, costs about <b>${rs(o.lot_cost)}</b> and loses about <b>${rs(p.one_lot_loss)}</b> at the stop.`,
        esc(p.exit),
      ];
      if (p.otm) facts.push(`${esc(strikeText(p.otm))} (${esc(p.otm.money || 'OTM')}) is cheaper but needs a bigger move to pay.`);
      if (p.risk && p.risk !== 'PASS') facts.push(`Risk check: <b>${esc(p.risk)}</b>${p.risk_reasons.length ? ', ' + esc(p.risk_reasons.join('; ')) : ''}.`);
      plan = `<div class="block"><h3>Trade plan</h3>`
        + `<div class="sheet-head" style="margin-bottom:8px"><span class="act ${dir(p.side)}">BUY ${p.side === 'CE' ? 'CALL' : 'PUT'}</span>`
        + `<span class="contract">${esc(o.label || '')}</span><span class="note">Signal at ${esc(p.signal_time)} IST, when the `
        + `${esc(minus5(p.signal_time))}–${esc(p.signal_time)} candle closed. 1R = ${n0(p.risk_pts)} pts</span></div>`
        + ladderHTML(p.side, rows, cols, c.label)
        + `<ul class="facts">${facts.map(f => `<li>${f}</li>`).join('')}</ul></div>`;
    } else if (c.watch && c.watch.length) {
      plan = `<div class="block if"><h3>No trade yet. If it breaks out, the plan would be:</h3>` + c.watch.map(w => {
        const rows = [
          { k: 'T3', lbl: 'T3', cls: 't', lvl: w.targets.T3 }, { k: 'T2', lbl: 'T2', cls: 't', lvl: w.targets.T2 },
          { k: 'T1', lbl: 'T1', cls: 't', lvl: w.targets.T1 }, { k: 'entry', lbl: 'Trigger', cls: 'e', lvl: w.trigger },
          { k: 'stop', lbl: 'Stop', cls: 's', lvl: w.stop },
        ];
        return `<div class="sheet-head" style="margin:4px 0 8px"><span class="act ghost ${dir(w.side)}">${w.side}</span>`
          + `<span class="note">Nothing to buy until a 5-minute candle closes ${w.side === 'CE' ? 'above' : 'below'} <b class="num" style="color:var(--frost)">${px(w.trigger)}</b> `
          + `(${n0(Math.abs(w.distance))} pts away). A wick through it does not count.</span></div>`
          + ladderHTML(w.side, rows, [optCol(w.option)].filter(Boolean), c.label);
      }).join('') + '<ul class="facts"><li>Premiums are estimates at each level from the option\'s current implied volatility.</li></ul></div>';
    } else {
      const o = c.or;
      plan = `<div class="block"><h3>Today</h3><ul class="facts"><li><b>${esc(c.status)}</b></li>`
        + (o && ok(o.high) ? `<li>Opening range <b>${px(o.low)}</b> to <b>${px(o.high)}</b> (${n0(o.high - o.low)} pts).</li>` : '')
        + (c.yday && c.yday.side ? `<li>Yesterday: ${c.yday.side === 'CE' ? 'up' : 'down'} ${pct(c.yday.chg_pct)}, range ${ok(c.yday.range_atr) ? c.yday.range_atr.toFixed(2) + 'x normal' : 'n/a'}.</li>` : '')
        + '</ul></div>';
    }
    const why = `<h3>Why this call</h3><ul class="why">${c.reasons.map((r, i) => `<li class="${i === 0 && c.plan ? 'key' : ''}">${esc(r)}</li>`).join('')}</ul>`;
    const meter = `<h3>Move size today</h3>${meterHTML(rt, side)}`;
    return head + `<div class="sheet-grid">${plan}<div class="block stack"><div>${meter}</div><div>${why}</div></div></div>`;
  }

  function sheetStock(name) {
    const pk = pickByName(name);
    const mv = S.movers && S.movers.detail && S.movers.detail.name === name ? S.movers.detail : null;
    const ltp = pk ? pk.ltp : mv ? mv.ltp : S.stock && S.stock.ltp;
    let head = `<div class="sheet-head"><span class="title">${esc(name)}</span>`
      + (pk ? stars(pk.stars, pk.star_text, 'lg') : '')
      + (pk && pk.in_picks === false ? '<span class="pill">Not in today\'s top 10</span>' : '')
      + '<span class="pill warn">Not a tested signal on stocks</span>'
      + (ok(ltp) ? `<span class="note">${px(ltp)} (${pct(pk ? pk.pct : mv && mv.pct)})</span>` : '') + '</div>';
    if (!pk) {
      const why = mv && mv.compare && mv.compare.text ? mv.compare.text : mv && mv.compare && mv.compare.error ? mv.compare.error : '';
      return head + `<div class="sheet-grid"><div class="block"><h3>Not in today's picks</h3><ul class="facts">`
        + `<li>${esc(name)} is not among today's 10 highest-scoring stocks, so it has no reference plan here.</li>`
        + (mv && mv.buildup ? `<li>Futures build-up: <b>${esc(mv.buildup)}</b>.</li>` : '')
        + `</ul></div><div class="block"><h3>Opening-range view</h3><ul class="why">${why ? `<li>${esc(why)}</li>` : '<li>Waiting for its opening range and option prices.</li>'}</ul></div></div>`;
    }
    let plan = '';
    if (pk.levels) {
      const v = pk.levels;
      const rows = [
        { k: 'T3', lbl: 'T3', cls: 't', lvl: v.targets.T3 }, { k: 'T2', lbl: 'T2', cls: 't', lvl: v.targets.T2 },
        { k: 'T1', lbl: 'T1', cls: 't', lvl: v.targets.T1 }, { k: 'entry', lbl: pk.breakout ? 'Entry' : 'Trigger', cls: 'e', lvl: v.entry },
        { k: 'stop', lbl: 'Stop', cls: 's', lvl: v.stop },
      ];
      const o = pk.option;
      const lead = pk.breakout
        ? `Broke ${pk.breakout.side === 'CE' ? 'above' : 'below'} its range at ${esc(pk.breakout.time)} IST`
        : `Needs a 5-minute candle closing ${v.side === 'CE' ? 'above' : 'below'} ${px(v.entry)}`
          + (v.next_check ? ` (next candle closes at ${v.next_check} IST)` : '') + '.';
      const oc = pk.outcome || {};
      const done = oc.status === 'stopped' || oc.status === 'closed';
      const h3 = !pk.breakout ? 'No breakout yet. If it breaks out, the levels would be:'
        : oc.status === 'stopped' ? `Breakout signal: stopped out at ${esc(oc.time)} IST (−1R)`
        : oc.status === 'closed' ? `Breakout signal: held to 15:00 (${signed(oc.r, x => x.toFixed(2))}R)`
        : `Breakout signal${ok(oc.r) ? `: open, ${signed(oc.r, x => x.toFixed(2))}R now` : ''}`;
      plan = `<div class="block${pk.breakout && !done ? '' : ' if'}"><h3 class="${oc.status === 'stopped' ? 'stop' : ''}">${h3}</h3>`
        + `<div class="sheet-head" style="margin-bottom:8px">`
        + (pk.breakout && !done ? `<span class="act ${dir(v.side)}">BUY ${v.side === 'CE' ? 'CALL' : 'PUT'}</span>`
          : `<span class="act ghost ${dir(v.side)}">${v.side}</span>`)
        + `<span class="contract">${o ? esc(o.label) : ''}</span><span class="note">${lead}</span></div>`
        + ladderHTML(v.side, rows, [optCol(o)].filter(Boolean), name)
        + '<ul class="facts">'
        + (o ? `<li>1 lot is ${n0(o.lot)} qty${ok(o.lot_cost) ? `, costs about <b>${rs(o.lot_cost)}</b>` : ''}.</li>` : '')
        + '<li>The plain breakout rule lost money on stocks in testing (about −0.01R a trade, 2017 to 2026). Treat these as levels to watch.</li>'
        + '</ul></div>';
    } else {
      plan = `<div class="block"><h3>Reference plan</h3><ul class="facts"><li>Levels appear after its first 15 minutes (09:30 IST).</li></ul></div>`;
    }
    const why = `<h3>Why it is on the list</h3><ul class="why">${pk.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>`;
    const sg = [
      ['Score', ok(pk.score) ? pk.score.toFixed(2) : '—'], ['Rank today', ok(pk.rank_pct) ? `top ${Math.max(1, Math.ceil(100 - pk.rank_pct))}%` : '—'],
      ['Gap at open', pct(pk.gap_pct)], ['Normal daily range', ok(pk.atr) ? px(pk.atr) : '—'],
      ['Turnover', ok(pk.turnover_cr) ? `₹${pk.turnover_cr} cr` : '—'], ['Lot size', n0(pk.lot)],
    ];
    const stats = `<h3>Stock stats</h3><dl class="statgrid">${sg.map(([a, b]) => `<div><dt>${a}</dt><dd>${b}</dd></div>`).join('')}</dl>`;
    return head + `<div class="sheet-grid">${plan}<div class="block stack"><div>${stats}</div><div>${why}</div></div></div>`;
  }

  function renderSheet() {
    if (!S.pred) return;
    let html;
    if (S.sel.kind === 'index') html = S.pred.indices[S.sel.key] ? sheetIndex(S.sel.key) : '';
    else html = sheetStock(S.sel.name);
    const el = $('#sheet');
    if (el._h !== html) { el.innerHTML = html; el._h = html; }
  }

  // ------------------------------------------------------------------ stock trades: Breakouts now (left), Today's picks (right)
  // Only running trades, each with its option, price, stop and target. A row leaves the moment its trade ends.
  function listedRow(name) {
    const L = S.pred && S.pred.stock_trades;
    if (!L) return null;
    for (const k of ['picks', 'movers']) {
      const r = ((L[k] && L[k].live) || []).find(x => x.name === name);
      if (r) return r;
    }
    return null;
  }

  function stockTradeHTML(r) {
    const ce = r.side === 'CE';
    const R = ok(r.r) ? (r.r >= 0 ? '+' : '−') + Math.abs(r.r).toFixed(1) + 'R' : '';
    const mine = S.openNames && S.openNames.has(r.name);
    return `<span class="r1"><span class="act ${ce ? 'up' : 'dn'}">BUY ${r.side}</span><span class="ct">${esc(r.contract)}</span>`
      + (mine ? '<span class="intrade" title="You have an open paper trade on this stock">IN TRADE</span>' : '')
      + `<span class="rr ${ok(r.r) ? (r.r >= 0 ? (ce ? 'up' : 'dn') : 'stop') : ''}" title="Move since the break, in units of the risk (break close to stop)">${R}</span></span>`
      + `<span class="r2"><b>${esc(r.name)}</b><span class="num">${px(r.ltp)}</span><span class="chg ${tone(r.pct)}">${pct(r.pct)}</span>`
      + `<span class="when">${ce ? '▲' : '▼'} ${esc(r.time)}</span></span>`
      + `<span class="r3"><span>Buy ${r.buy_est ? '≈' : ''}<b>${rs(r.buy, 2)}</b></span><span>1 lot <b>${rs(r.lot_cost)}</b></span>`
      + `<span>Stop <b class="stop">${px(r.stop)}</b></span><span>Target <b class="${ce ? 'up' : 'dn'}">${px(r.target)}</b></span></span>`
      + `<span class="why">${r.stars >= 2 ? stars(r.stars, 'Volatility rank today') : ''}${esc(r.why)}`
      + `${ok(r.lot_risk) ? ` · <em class="${r.lot_risk > 5000 ? 'big' : ''}" title="About what 1 lot loses if the stop is hit">risk ≈ ${rs(r.lot_risk)}</em>` : ''}</span>`;
  }

  function renderStockList(which, hostSel, subSel, endedSel) {
    const L = (S.pred && S.pred.stock_trades && S.pred.stock_trades[which]) || { live: [], done: [], max: 0 };
    const live = L.live || [], done = L.done || [];
    const host = $(hostSel);
    const t = S.pred && S.pred.time_ist ? S.pred.time_ist.slice(0, 5) : '';
    $(subSel).textContent = t >= '09:30' ? `${live.length} running${L.max ? ' · max ' + L.max : ''}` : '';
    // a new row lights up once (not on the first load, and not after a replay restart)
    const keys = new Set(live.map(r => r.name + r.side + r.time));
    const prev = S.seen[which];
    S.seen[which] = keys;
    const html = live.length ? live.map(r => {
      const fresh = prev && prev.size <= keys.size && !prev.has(r.name + r.side + r.time);
      return `<li><button type="button" class="tk st ${r.side === 'CE' ? 'up' : 'dn'}${fresh ? ' fresh' : ''}" data-name="${esc(r.name)}"`
        + ` aria-pressed="${S.sel.kind === 'stock' && S.sel.name === r.name}">${stockTradeHTML(r)}</button></li>`;
    }).join('')
      : `<li class="empty">${!t || t < '09:30' ? 'Starts after 09:30, once the first 15 minutes are in.'
        : t >= '14:00' ? 'Nothing running. No new breakouts after 14:00 (the tested window ends then).'
        : which === 'picks' ? 'None of today\'s picks is holding a breakout right now.'
        : 'No top gainer or loser is holding a breakout right now.'}</li>`;
    if (L.thin) $(subSel).title = `${L.thin} more left out today: their option is too thin (spread and slippage over 15% of the expected gain).`;
    if (host._h !== html.replace(/ fresh/g, '')) { host.innerHTML = html; host._h = html.replace(/ fresh/g, ''); }
    const el = $(endedSel);
    if (done.length) {
      const net = L.done_r;
      el.innerHTML = `Ended today: <b>${done.length}</b> · net <b class="${tone(net)}">${signed(net, x => x.toFixed(1))}R</b>`;
      el.title = done.map(d => `${d.name} ${d.side} ${d.time} → ${d.exit.time} ${d.exit.why} ${d.exit.r >= 0 ? '+' : ''}${d.exit.r}R`).join('\n');
    } else { el.textContent = ''; el.title = ''; }
  }

  function renderPicks() {
    renderStockList('picks', '#picks', '#picks-sub', '#picks-ended');
    const w = (S.pred && S.pred.stock_trades && S.pred.stock_trades.waiting) || [];
    const t = S.pred && S.pred.time_ist ? S.pred.time_ist.slice(0, 5) : '';
    const el = $('#picks-wait');
    const html = w.length && t >= '09:30'
      ? 'Waiting for a break: ' + w.slice(0, 7).map(n => `<button type="button" class="nmbtn" data-name="${esc(n)}">${esc(n)}</button>`).join(' ')
      : (S.pred && S.pred.picks && S.pred.picks.length && t < '09:30' ? 'Picks are chosen at 09:30 from the first 15 minutes.' : '');
    if (el._h !== html) { el.innerHTML = html; el._h = html; }
  }
  function renderBreaks() { renderStockList('movers', '#breaks', '#bk-sub', '#bk-ended'); }
  for (const id of ['#picks', '#breaks', '#picks-wait']) {
    $(id).addEventListener('click', e => {
      const b = e.target.closest('button[data-name]');
      if (b) select({ kind: 'stock', name: b.dataset.name });
    });
  }

  function renderMovers() {
    const host = $('#movers');
    const rows = (S.movers && S.movers[S.mvTab]) || [];
    const html = rows.length ? rows.map(r => `<li><button type="button" class="mover" data-name="${esc(r.name)}" aria-pressed="${S.sel.kind === 'stock' && S.sel.name === r.name}">`
      + `<span class="nm">${esc(r.name)}<small>${esc(r.buildup && r.buildup !== '—' ? r.buildup : '')}</small></span>`
      + `<span class="num">${px(r.ltp)}</span><span class="chg ${tone(r.pct)}">${pct(r.pct)}</span></button></li>`).join('')
      : '<li class="empty">Movers appear once stock prices arrive.</li>';
    if (host._h !== html) { host.innerHTML = html; host._h = html; }
  }
  $('#movers').addEventListener('click', e => {
    const b = e.target.closest('button.mover');
    if (b) select({ kind: 'stock', name: b.dataset.name });
  });
  $('#mv-tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-k]');
    if (!b) return;
    S.mvTab = b.dataset.k;
    for (const x of $('#mv-tabs').querySelectorAll('button')) x.setAttribute('aria-selected', String(x === b));
    renderMovers();
  });

  // ------------------------------------------------------------------ trades to take (left column, top)
  function renderTrades() {
    const rows = (S.pred && S.pred.trades) || [];
    const live = rows.filter(r => r.status !== 'missed');
    $('#tk-sub').textContent = live.length ? `${live.length} today` : '';
    const host = $('#trades');
    const t = S.pred && S.pred.time_ist ? S.pred.time_ist.slice(0, 5) : '';
    const html = rows.length ? rows.map(r => {
      const ce = r.side === 'CE';
      if (r.status === 'missed') {
        return `<li><button type="button" class="tk done ${ce ? 'up' : 'dn'}" data-key="${esc(r.key)}"><span class="r1"><span class="act ghost ${ce ? 'up' : 'dn'}">${ce ? 'CALL' : 'PUT'}</span>`
          + `<span class="ct">${esc(r.index)}</span><span class="stt muted">missed</span></span>`
          + `<span class="r2">${esc(r.strategy)} · ${esc(r.time)} IST · ${esc(r.text)}</span></button></li>`;
      }
      const closed = r.status === 'closed';
      const pnl = closed ? r.pnl : r.live_pnl;
      const stt = closed ? `closed ${ok(pnl) ? signed(pnl, x => rs(x)) : ''}` : r.status === 'open' ? `open ${ok(pnl) ? signed(pnl, x => rs(x)) : ''}` : 'getting prices…';
      const stopTxt = ok(r.stop_now) ? `${px(r.stop_now)} (trailing)` : px(r.stop_index);
      const exitTxt = r.exit_mode === 'trail' ? `${r.exit_time} · trails after ${px(r.trail_from)}` : `${r.exit_time} or the stop`;
      const blocked = r.verdict === 'BLOCK' && r.reasons && r.reasons.length;
      return `<li><button type="button" class="tk ${ce ? 'up' : 'dn'} ${closed ? 'done' : ''}" data-key="${esc(r.key)}">`
        + `<span class="r1"><span class="act ${ce ? 'up' : 'dn'}">BUY ${ce ? 'CE' : 'PE'}</span><span class="ct">${esc(r.contract)}</span></span>`
        + `<span class="r2"><span class="when" title="${esc(r.strategy)}">${r.strategy === 'Supertrend flip' ? 'Supertrend' : 'Breakout'} ${esc(r.time)}${closed && r.exit_reason ? ` → ${esc(r.exit_at || '')} ${esc(r.exit_reason)}` : ''}</span>`
        + `<span class="stt ${ok(pnl) ? (pnl >= 0 ? 'up' : 'stop') : 'muted'}">${esc(stt)}</span></span>`
        + `<span class="r3"><span>Buy up to <b>₹${px(r.limit)}</b></span><span>Skip above <b>₹${px(r.skip_above)}</b></span>`
        + `<span>Stop ${esc(r.index)} <b>${stopTxt}</b></span><span>Exit <b>${esc(exitTxt)}</b></span></span>`
        + (blocked ? `<span class="warn">${esc(/max loss/i.test(r.reasons[0]) && ok(r.risk_rupees)
            ? `1 lot risks ${rs(r.risk_rupees)} at the stop: above your ${rs(r.max_loss)} max loss (Classic view → Settings).`
            : r.reasons[0])}</span>` : '')
        + '</button></li>';
    }).join('')
      : `<li class="empty">${t && t < '09:45' ? 'Signals start from 09:35 (breakout) and 09:45 (Supertrend).' : t > '14:00' ? 'No trade today: neither tested rule fired.' : 'No trade yet today. Expect 0 to 4 a day, about 1 on average.'}</li>`;
    if (host._h !== html) { host.innerHTML = html; host._h = html; }
  }
  $('#trades').addEventListener('click', e => {
    const b = e.target.closest('button.tk');
    if (b) select({ kind: 'index', key: b.dataset.key });
  });

  // ------------------------------------------------------------------ selection
  function select(sel) {
    const same = sel.kind === S.sel.kind && (sel.kind === 'index' ? sel.key === S.sel.key : sel.name === S.sel.name);
    S.sel = sel;
    if (sel.kind === 'index') S.lastIndex = sel.key;
    store.set('io.sel', JSON.stringify(sel));
    if (sel.kind === 'stock') { S.stock = null; postJSON('/api/mover', { name: sel.name }).catch(() => {}); }
    if (!same) { S.ticket = null; S.ticketKey = ''; renderTicket(); loadTicket(); }
    renderAll();
    if (!same) loadChart();
  }

  function renderAll() {
    if (!S.pred) return;
    renderTabs(); renderQuote(); renderTrades(); renderCalls(); renderSheet(); renderPicks(); renderMovers(); renderBreaks(); renderBanner();
    renderMine();
    const br = S.pred.breadth;
    const be = $('#r-breadth');
    be.textContent = ok(br) ? `${Math.round(br * 100)}% up` : '—';
    be.className = ok(br) ? (br >= 0.55 ? 'up' : br <= 0.45 ? 'dn' : '') : '';
    decorate();
  }

  // ------------------------------------------------------------------ paper trading: one click under the chart
  const ticketKey = () => (S.sel.kind === 'index' ? `kind=index&name=${encodeURIComponent(S.sel.key)}`
    : `kind=stock&name=${encodeURIComponent(S.sel.name)}`) + `&lots=${S.lots}`;
  let toastTimer = null;
  // ------------------------------------------------------------------ candle replay ("Replay from…", from the v14.32 feature copy)
  // Click "Replay from…", click a candle: everything after it is hidden; step or play forward. PDH / PDL follow the replayed day.
  function rpBar() {
    const bar = $('#rpbar');
    bar.hidden = !S.rp.on;
    $('#rp-start').classList.toggle('on', S.rp.on);
    $('#rp-start').innerHTML = S.rp.on ? 'Exit replay' : '<span class="rp-full">Replay from…</span><span class="rp-short">Replay</span>';
    if (!S.rp.on) return;
    if (S.rp.picking) {
      bar.innerHTML = '<span class="rp-msg">Click the candle to start the replay from</span><button type="button" data-rp="exit">Cancel</button>';
      return;
    }
    const b = S.bars[S.bars.length - 1];
    const when = b ? new Date(b.time * 1000).toLocaleString('en-GB', { timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
    const sp = [1, 2, 5, 10, 20].map(v => `<option value="${v}"${v === S.rp.speed ? ' selected' : ''}>${v} candle${v > 1 ? 's' : ''}/s</option>`).join('');
    bar.innerHTML = `<button type="button" data-rp="back" title="One candle back (←)">&#9198;</button>`
      + `<button type="button" data-rp="play" class="pl" title="Play / pause (Space)">${S.rp.playing ? '&#10074;&#10074; Pause' : '&#9654; Play'}</button>`
      + `<button type="button" data-rp="next" title="One candle forward (→)">&#9197;</button>`
      + `<select data-rp="speed" aria-label="Replay speed">${sp}</select>`
      + `<span class="rp-msg">${esc(when)} · ${S.rp.idx + 1} / ${S.rp.all.length}</span>`
      + `<button type="button" data-rp="pick" title="Pick another starting candle">Pick again</button>`
      + `<button type="button" data-rp="exit">Exit replay</button>`;
  }
  function rpLevels(list) {           // previous day's high / low for the day of the last shown candle
    const last = list[list.length - 1];
    if (!last) return [];
    const d = dayOf(last.time);
    const prev = list.filter(b => dayOf(b.time) < d);
    if (!prev.length) return [];
    const pd = dayOf(prev[prev.length - 1].time), day = prev.filter(b => dayOf(b.time) === pd);
    return [{ label: 'PDH', price: Math.max(...day.map(b => b.high)), kind: 'high' },
            { label: 'PDL', price: Math.min(...day.map(b => b.low)), kind: 'low' }];
  }
  function rpShow() {
    const list = S.rp.all.slice(0, S.rp.idx + 1);
    S.bars = list; S.last = list[list.length - 1] || null; S.day = S.last ? dayOf(S.last.time) : null;
    S.ctxN = S.day == null ? 0 : list.filter(b => dayOf(b.time) !== S.day).length;
    candles.setData(list.map(toC)); volume.setData(list.map(toV));
    ctxLevels(rpLevels(list));
    S.lineKey = ''; S.markKey = 'x';
    decorate(); legend(null); rpBar();
    const ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
    if (r && S.rp.idx > r.to - 3) ts.scrollToPosition(6, false);
  }
  function startReplay() {
    if (!LW || !S.bars.length) { toast('No candles to replay on this chart.', true); return; }
    stopTimer();
    S.rp.on = true; S.rp.picking = true; S.rp.all = S.bars.slice(); S.rp.playing = false;
    rpBar();
  }
  function stopTimer() { if (S.rp.timer) { clearInterval(S.rp.timer); S.rp.timer = null; } S.rp.playing = false; }
  function stopReplay(reload = true) {
    stopTimer();
    S.rp.on = false; S.rp.picking = false;
    rpBar();
    if (reload) { S.view = null; loadChart(); }
  }
  function rpPlay() {
    stopTimer();
    S.rp.playing = true;
    S.rp.timer = setInterval(() => {
      if (S.rp.idx >= S.rp.all.length - 1) { stopTimer(); rpBar(); return; }
      S.rp.idx++; rpShow();
    }, 1000 / S.rp.speed);
    rpBar();
  }
  if (LW) chart.subscribeClick(p => {
    if (!S.rp.on || !S.rp.picking || !p || p.time == null) return;
    const i = S.rp.all.findIndex(b => b.time === p.time);
    if (i < 0) return;
    S.rp.picking = false; S.rp.idx = i;
    rpShow();
  });
  $('#rp-start').addEventListener('click', () => (S.rp.on ? stopReplay() : startReplay()));
  $('#rpbar').addEventListener('click', e => {
    const b = e.target.closest('button[data-rp]');
    if (!b) return;
    const a = b.dataset.rp;
    if (a === 'exit') stopReplay();
    else if (a === 'pick') { stopTimer(); S.rp.picking = true; const all = S.rp.all; S.bars = all; candles.setData(all.map(toC)); volume.setData(all.map(toV)); S.lineKey = ''; S.markKey = 'x'; decorate(); rpBar(); }
    else if (a === 'play') { if (S.rp.playing) { stopTimer(); rpBar(); } else rpPlay(); }
    else if (a === 'next' && S.rp.idx < S.rp.all.length - 1) { stopTimer(); S.rp.idx++; rpShow(); }
    else if (a === 'back' && S.rp.idx > 0) { stopTimer(); S.rp.idx--; rpShow(); }
  });
  $('#rpbar').addEventListener('change', e => {
    if (e.target.dataset.rp !== 'speed') return;
    S.rp.speed = Number(e.target.value);
    if (S.rp.playing) rpPlay();
  });
  document.addEventListener('keydown', e => {
    if (!S.rp.on || S.rp.picking || /INPUT|SELECT|TEXTAREA/.test(document.activeElement && document.activeElement.tagName)) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); if (S.rp.idx < S.rp.all.length - 1) { stopTimer(); S.rp.idx++; rpShow(); } }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); if (S.rp.idx > 0) { stopTimer(); S.rp.idx--; rpShow(); } }
    else if (e.key === ' ') { e.preventDefault(); if (S.rp.playing) { stopTimer(); rpBar(); } else rpPlay(); }
  });

  // ------------------------------------------------------------------ older days: scroll left, jump to a day (feature 2)
  function isoOf(t) { return new Date(t * 1000).toISOString().slice(0, 10); }
  async function loadOlder() {
    const H = S.hist;
    if (!LW || H.loading || !H.more || !H.earliest || S.sel.kind !== 'index' || S.rp.on || !S.bars.length) return;
    H.loading = true;
    const tok = S.loadToken, key = viewKey(), q = S.tf === 1 ? 1 : 5, n = S.tf === 1 ? 2 : 5;
    let d = null;
    try { d = await getJSON(`/api/older?index=${encodeURIComponent(S.sel.key)}&tf=${q}&before=${H.earliest}&n=${n}`); } catch { /* try on the next scroll */ }
    H.loading = false;
    if (!d || tok !== S.loadToken || key !== viewKey() || S.rp.on || !S.bars.length) return;
    H.more = !!d.more;
    if (d.days && d.days.length) H.earliest = d.days[0];
    let add = (d.bars || []).filter(b => b.time < S.bars[0].time).sort((a, b) => a.time - b.time);
    if (!add.length) return;
    if (S.tf === 15) { for (const b of add) S.five.set(b.time, b); add = agg15(add); }
    const ts = chart.timeScale(), r = ts.getVisibleLogicalRange();
    S.bars = add.concat(S.bars); S.ctxN = (S.ctxN || 0) + add.length;
    candles.setData(S.bars.map(toC)); volume.setData(S.bars.map(toV));
    if (r) ts.setVisibleLogicalRange({ from: r.from + add.length, to: r.to + add.length });
    S.lineKey = ''; S.markKey = 'x'; decorate();
  }
  if (LW) chart.timeScale().subscribeVisibleLogicalRangeChange(r => { if (r && r.from < 20) loadOlder(); });
  function scrollToIdx(i) {
    const per = S.tf === 1 ? 375 : S.tf === 5 ? 75 : 25;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(-2, i - 5), to: i + per + 5 });
  }
  async function jumpTo(iso) {
    if (!LW || !iso) return;
    if (S.sel.kind !== 'index') { toast('Jump to a day works on NIFTY 50, SENSEX and NIFTY BANK.', true); return; }
    const at = S.bars.findIndex(b => isoOf(b.time) >= iso);
    if (at >= 0 && isoOf(S.bars[at].time) === iso) { scrollToIdx(at); return; }      // already on the chart
    const q = S.tf === 1 ? 1 : 5;
    const d = await getJSON(`/api/older?index=${encodeURIComponent(S.sel.key)}&tf=${q}&around=${iso}`).catch(() => null);
    if (!d || !(d.bars || []).length) { toast('No candles saved for that day.', true); return; }
    if (S.rp.on) stopReplay(false);
    S.loadToken++;
    S.hist = { earliest: d.days[0], loading: false, more: !!d.more, detached: true };
    let list = d.bars.slice().sort((a, b) => a.time - b.time);
    S.five = new Map();
    if (S.tf === 15) { for (const b of list) S.five.set(b.time, b); list = agg15(list); }
    S.bars = list; S.last = list[list.length - 1]; S.day = dayOf(S.last.time); S.ctxN = list.length;
    candles.setData(list.map(toC)); volume.setData(list.map(toV));
    const shown = list.findIndex(b => isoOf(b.time) >= iso);
    const dayIso = shown >= 0 ? isoOf(list[shown].time) : iso;
    ctxLevels(rpLevels(list.filter(b => isoOf(b.time) <= dayIso)));     // PDH / PDL of the day jumped to
    S.lineKey = ''; S.markKey = 'x'; decorate(); showEmpty(false); legend(null);
    scrollToIdx(Math.max(0, shown));
    $('#jump-today').hidden = false;
    if (dayIso !== iso) toast(`No session on ${iso} (holiday or weekend): showing ${dayIso}.`);
  }
  $('#jump-day').addEventListener('change', e => { const v = e.target.value; e.target.blur(); $('.jump-wrap').classList.remove('open'); jumpTo(v); });
  $('#jump-btn').addEventListener('click', () => {
    const inp = $('#jump-day');
    try { inp.showPicker(); } catch { $('.jump-wrap').classList.toggle('open'); if ($('.jump-wrap').classList.contains('open')) inp.focus(); }
  });
  $('#jump-today').addEventListener('click', () => { S.view = null; loadChart(); });
  (async () => {                           // the date box offers the days we have candles for
    const d = await getJSON('/api/chart-days?index=NIFTY').catch(() => null);
    if (d && d.first) { $('#jump-day').min = d.first; $('#jump-day').max = d.last; }
  })();

  function toast(msg, bad = false) {
    const el = $('#toast');
    el.textContent = msg; el.className = 'toast' + (bad ? ' bad' : ''); el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 4000);
  }
  async function errText(r) {
    try { const j = await r.json(); return j.detail || r.statusText; } catch { return r.statusText || 'Failed'; }
  }

  async function loadTicket() {
    const key = ticketKey();
    try {
      const t = await getJSON('/api/paper/ticket?' + key);
      if (key !== ticketKey()) return;
      S.ticket = t; S.ticketKey = key;
    } catch {
      if (key !== ticketKey()) return;
      S.ticket = { error: true }; S.ticketKey = key;
    }
    renderTicket(); renderPaperBtn(); decorate();
  }

  function sideBtn(t, side) {
    const d = (t.sides || {})[side] || {};
    const sys = t.system_side === side;
    const ce = side === 'CE';
    const cap = (t.account && t.account.capital) || 100000;
    const big = ok(d.max_loss) && d.max_loss > 0.05 * cap;
    const lvls = ok(d.target) ? `Stop ${px(d.stop)} · Target ${px(d.target)}` : ok(d.stop) ? `Stop ${px(d.stop)}${/trails/.test(d.rule || '') ? ' (trails)' : ''} · exit ${(/exit (\d\d:\d\d)/.exec(d.rule || '') || [])[1] || '15:00'}` : '';
    return `<button type="button" class="buy ${ce ? 'up' : 'dn'}${sys ? ' sys' : ''}" data-side="${side}" ${d.can_buy ? '' : 'disabled'}`
      + ` title="${esc(d.can_buy ? d.rule : d.why || '')}">`
      + `<span class="b1">BUY ${side}${sys ? ' <i>signal</i>' : ''}</span>`
      + `<span class="b2">${esc(d.contract || '—')}</span>`
      + `<span class="b3">${ok(d.price) ? `${rs(d.price, 2)}${d.price_source === 'model' ? '*' : ''} × ${d.qty} = <b>${rs(d.cost)}</b>` : ''}</span>`
      + `<span class="b4">${esc(lvls)}${ok(d.max_loss) ? ` · max loss ≈ <em class="${big ? 'big' : ''}">${rs(d.max_loss)}${big ? ` (${Math.round(d.max_loss / cap * 100)}% of the account)` : ''}</em>` : ''}</span></button>`;
  }

  function strBtn(t) {
    const q = t.straddle || {};
    return `<button type="button" class="buy both" data-side="BOTH" ${q.can_buy ? '' : 'disabled'} title="${esc(q.can_buy ? q.rule : q.why || '')}">`
      + `<span class="b1">BUY CE + PE</span>`
      + `<span class="b2">${esc(q.contract || '—')}</span>`
      + `<span class="b3">${ok(q.cost) ? `(${rs(q.price_ce, 2)} + ${rs(q.price_pe, 2)})${q.source === 'model' ? '*' : ''} × ${q.qty} = <b>${rs(q.cost)}</b>` : ''}</span>`
      + `<span class="b4">${ok(q.breakeven_up) ? `Breakeven ${px(q.breakeven_dn)} / ${px(q.breakeven_up)} · no stop, 15:15` : ''}</span></button>`;
  }

  function renderTicket() {
    const host = $('#ticket');
    const t = S.ticket;
    let html;
    if (!t || S.ticketKey !== ticketKey()) html = '<div class="tkt-msg">Paper trade: getting prices…</div>';
    else if (t.error) html = '<div class="tkt-msg">Paper trade: not available for this symbol right now.</div>';
    else {
      const o = t.open;
      const sysCls = t.system_side ? (t.system_side === 'CE' ? 'up' : 'dn') : '';
      const left = `<div class="tkt-l"><span class="tag">PAPER <span class="lots" role="group" aria-label="Lots per order">`
        + `<button type="button" data-lots="-1" aria-label="One lot less" ${S.lots <= 1 ? 'disabled' : ''}>−</button>`
        + `<b>${S.lots}</b><button type="button" data-lots="1" aria-label="One lot more" ${S.lots >= (t.max_lots || 50) ? 'disabled' : ''}>+</button>`
        + ` LOT${S.lots > 1 ? 'S' : ''}</span></span><span class="sys ${sysCls}">${esc(t.system_text)}</span>`
        + (t.closed_why && !o ? `<span class="why-off">${esc(t.closed_why)}</span>` : '') + '</div>';
      let mid;
      // a single CE/PE trade and a straddle can both be open on one symbol
      if (o) {
        const pnl = o.live_pnl;
        const stop = ok(o.stop_now) ? o.stop_now : o.stop_spot;
        mid = `<div class="tkt-open ${o.side === 'CE' ? 'up' : 'dn'}">`
          + `<span class="o1"><span class="act ${o.side === 'CE' ? 'up' : 'dn'}">${o.side}</span><b>${esc(o.contract)}</b>`
          + `<span class="pnl ${tone(pnl)}">${ok(pnl) ? signed(pnl, x => rs(x)) : '—'}</span></span>`
          + `<span class="o2">Bought ${esc(o.opened)} at <b>${rs(o.entry_premium, 2)}</b> × ${o.qty} · now <b>${ok(o.mark) ? rs(o.mark, 2) : '—'}</b>${o.mark_source === 'model' ? '*' : ''}`
          + ` · stop <b>${px(stop)}</b>${ok(o.stop_now) && o.stop_now !== o.stop_spot ? ' (trailing)' : ''}${ok(o.target_spot) ? ` · target <b>${px(o.target_spot)}</b>` : ''}</span>`
          + `<span class="o3">${esc(o.rule_text)}</span></div>`
          + `<button type="button" class="exit" data-id="${o.id}">EXIT TRADE</button>`;
      } else {
        mid = sideBtn(t, 'CE') + sideBtn(t, 'PE');
      }
      const legs = t.straddle_open || [];
      if (legs.length) {
        const ce = legs.find(x => x.side === 'CE') || legs[0];
        const pnl = legs.reduce((a, x) => a + (ok(x.live_pnl) ? x.live_pnl : 0), 0);
        const leg = x => `${x.side} ${rs(x.entry_premium, 0)}→<b>${ok(x.mark) ? rs(x.mark, 0) : '—'}</b>`;
        mid += `<div class="tkt-open strad">`
          + `<span class="o1"><span class="act both">CE+PE</span><b>${Number(ce.strike).toLocaleString('en-IN')}</b>`
          + `<span class="pnl ${tone(pnl)}">${signed(pnl, x => rs(x))}</span></span>`
          + `<span class="o2">${ce.auto ? 'Auto ' : ''}${esc(ce.opened)} × ${ce.qty} · ${legs.map(leg).join(' · ')}</span>`
          + `<span class="o3">No stop · closes 15:15${legs.length > 1 ? ` · <button type="button" class="lnk exit" data-group="${esc(ce.group)}">exit both</button>` : ''}</span></div>`
          + `<div class="legx${legs.length === 1 ? ' one' : ''}">` + legs.map(x =>
            `<button type="button" class="exit leg" data-id="${x.id}" title="Sell only the ${x.side}">EXIT ${x.side}`
            + `<small>${ok(x.live_pnl) ? signed(x.live_pnl, v => rs(v)) : '—'}</small></button>`).join('') + '</div>';
      } else {
        mid += strBtn(t);
      }
      const a = t.account || {};
      const right = `<div class="tkt-r"><span>Free cash</span><b>${rs(a.cash)}</b><span>Today <b class="${tone(a.today)}">${signed(a.today, x => rs(x))}</b></span></div>`;
      html = left + mid + right;
    }
    if (host._h !== html) { host.innerHTML = html; host._h = html; }
    for (const b of host.querySelectorAll('button')) if (S.busy) b.disabled = true;
  }

  async function paperPost(url, body) {
    if (S.busy) return null;
    S.busy = true; renderTicket();
    try {
      const r = await postJSON(url, body);
      if (!r.ok) { toast(await errText(r), true); return null; }
      return await r.json();
    } catch { toast('The app did not answer. Is it still running?', true); return null; }
    finally { S.busy = false; $('#ticket')._h = ''; $('#mine-list')._h = ''; await loadTicket(); await loadPaper(); }
  }

  $('#ticket').addEventListener('click', async e => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.dataset.lots) {
      S.lots = Math.max(1, Math.min(50, S.lots + Number(b.dataset.lots)));
      store.set('io.lots', String(S.lots));
      $('#ticket')._h = ''; loadTicket();
      return;
    }
    if (b.classList.contains('buy')) {
      const body = S.sel.kind === 'index' ? { kind: 'index', name: S.sel.key, side: b.dataset.side, lots: S.lots }
        : { kind: 'stock', name: S.sel.name, side: b.dataset.side, lots: S.lots };
      if (b.dataset.side === 'BOTH') {
        const legs = await paperPost('/api/paper/straddle', body);
        if (legs) toast(`Paper BUY straddle ${legs[0].contract.replace(' CE ', ' CE + PE ')}: ${rs(legs[0].entry_premium, 2)} + ${rs(legs[1].entry_premium, 2)} × ${legs[0].qty}. Closes at 15:15 or when you exit.`);
        return;
      }
      const t = await paperPost('/api/paper/open', body);
      if (t) toast(`Paper BUY ${t.contract} × ${t.qty} at ${rs(t.entry_premium, 2)}. It exits by itself at the stop${ok(t.target_spot) ? ', the target' : ''} or the time exit.`);
    } else if (b.classList.contains('exit')) {
      await exitClick(b);
    }
  });

  async function exitClick(b) {
    if (b.dataset.group) {
      const legs = await paperPost('/api/paper/exit', { group: b.dataset.group });
      if (legs) {
        const pnl = legs.reduce((a, x) => a + (x.pnl || 0), 0);
        toast(`Paper SELL straddle ${legs[0].name}: ${signed(pnl, x => rs(x))} after charges (both legs).`, pnl < 0);
      }
      return;
    }
    const t = await paperPost('/api/paper/exit', { id: Number(b.dataset.id) });
    if (t) toast(`Paper SELL ${t.contract} at ${rs(t.exit_premium, 2)}: ${signed(t.pnl, x => rs(x))} after charges.`, (t.pnl || 0) < 0);
  }

  // top rail button and the paper trading panel
  function renderPaperBtn() {
    const a = (S.paperData && S.paperData.account) || (S.ticket && S.ticket.account);
    if (!a) return;
    $('#pp-eq').textContent = rs(a.equity);
    const td = $('#pp-today');
    td.textContent = ok(a.today) && (a.today !== 0 || a.open) ? `today ${signed(a.today, x => rs(x))}` : '';
    td.className = tone(a.today);
  }

  function splitCell(v) { return ok(v) ? `<td class="${tone(v)}">${signed(v, x => rs(x))}</td>` : '<td class="muted">—</td>'; }

  function renderPaper() {
    const d = S.paperData;
    const host = $('#pp-body');
    if (!d) { host.innerHTML = '<p class="empty">Loading…</p>'; return; }
    const a = d.account;
    $('#pp-since').textContent = a.since ? `Account since ${dShort(String(a.since).slice(0, 10))} · ${a.mode} prices` : '';
    const tile = (label, v, cls = '', sub = '') => `<div class="tile"><dt>${label}</dt><dd class="${cls}">${v}</dd>${sub ? `<small>${sub}</small>` : ''}</div>`;
    const net = a.equity - a.capital;
    let h = '<dl class="tiles">'
      + tile('Equity', rs(a.equity), '', `${signed(net, x => rs(x))} on ${rs(a.capital)}`)
      + tile('Booked', signed(a.realized, x => rs(x)), tone(a.realized), `${a.trades} closed trade${a.trades === 1 ? '' : 's'}`)
      + tile('Open P&L', signed(a.open_pnl, x => rs(x)), tone(a.open_pnl), `${a.open} open`)
      + tile('Free cash', rs(a.cash), '', a.blocked ? `${rs(a.blocked)} in open trades` : '')
      + tile('Win rate', ok(a.win_rate) ? Math.round(a.win_rate * 100) + '%' : '—', '', `${a.wins} won · ${a.losses} lost`)
      + tile('Avg win / loss', `${ok(a.avg_win) ? rs(a.avg_win) : '—'} / ${ok(a.avg_loss) ? rs(Math.abs(a.avg_loss)) : '—'}`)
      + '</dl>'
      + '<h3>Where the money came from (closed trades)</h3><dl class="tiles five">'
      + tile('Price move (delta)', signed(a.delta, x => rs(x)), tone(a.delta))
      + tile('Time decay (theta)', signed(a.theta, x => rs(x)), tone(a.theta))
      + tile('Volatility (IV)', signed(a.vega, x => rs(x)), tone(a.vega))
      + tile('Spread & slippage', signed(sumSplit(d.closed, 'spread'), x => rs(x)), tone(sumSplit(d.closed, 'spread')))
      + tile('Charges', signed(-a.charges, x => rs(x)), a.charges ? 'dn' : '')
      + '</dl>';
    h += `<div class="autosw"><button type="button" class="sw${d.auto_straddle ? ' on' : ''}" id="pp-auto" aria-pressed="${!!d.auto_straddle}">`
      + `<span class="knob"></span></button><span><b>Auto straddle at 09:15</b> on NIFTY 50, NIFTY BANK and SENSEX: buys the at-the-money CE + PE (1 lot each) `
      + `at the first price after 09:15, closes at 15:15. Paper only, untested.</span></div>`;
    h += `<h3>Open trades <small>${d.open.length}</small></h3>`;
    h += d.open.length ? '<table class="pp"><thead><tr><th>Contract</th><th>Bought</th><th>Now</th><th>P&amp;L</th><th>Stop · target</th><th></th></tr></thead><tbody>'
      + d.open.map(t => `<tr><td>${t.group ? '<span class="autotag">straddle</span> ' : ''}<span class="sd ${dir(t.side)}">${t.side}</span> <button type="button" class="lnk" data-kind="${t.kind}" data-name="${esc(t.name)}">${esc(t.contract)}</button><small>${esc(t.rule_text)}</small></td>`
        + `<td>${esc(t.opened)} · ${rs(t.entry_premium, 2)} × ${t.qty}</td><td>${ok(t.mark) ? rs(t.mark, 2) : '—'}${t.mark_source === 'model' ? '*' : ''}</td>`
        + `<td class="${tone(t.live_pnl)}"><b>${signed(t.live_pnl, x => rs(x))}</b></td>`
        + `<td>${px(ok(t.stop_now) ? t.stop_now : t.stop_spot)}${ok(t.target_spot) ? ' · ' + px(t.target_spot) : ''}</td>`
        + `<td><button type="button" class="exit sm" data-id="${t.id}">Exit</button></td></tr>`).join('') + '</tbody></table>'
      : '<p class="empty">No open paper trade. Open a chart and click Buy CE or Buy PE under it.</p>';
    h += `<h3>Past trades <small>${d.closed.length}</small></h3>`;
    h += d.closed.length ? '<div class="pp-scroll"><table class="pp"><thead><tr><th>Day</th><th>Contract</th><th>In → out</th><th>Premium</th><th>Why it closed</th>'
      + '<th title="The underlying\'s move">Move</th><th title="Time decay">Decay</th><th title="Change in implied volatility">IV</th><th title="Bid-ask spread and slippage">Spread</th><th>Charges</th><th>P&amp;L</th></tr></thead><tbody>'
      + d.closed.map(t => {
        const sp = t.split || {};
        return `<tr><td>${dShort(t.day)}</td><td><span class="sd ${dir(t.side)}">${t.side}</span> ${esc(t.contract)}</td><td>${esc(t.opened)} → ${esc(t.closed || '')}</td>`
          + `<td>${rs(t.entry_premium, 2)} → ${rs(t.exit_premium, 2)}</td><td>${esc(t.exit_reason || '')}</td>`
          + splitCell(sp.delta) + splitCell(sp.theta) + splitCell(sp.vega) + splitCell(sp.spread) + splitCell(sp.charges)
          + `<td class="${tone(t.pnl)}"><b>${signed(t.pnl, x => rs(x))}</b></td></tr>`;
      }).join('') + '</tbody></table></div>'
      : '<p class="empty">No closed paper trades yet.</p>';
    h += '<p class="pp-note">1 lot per click (a straddle: 1 lot of each) from your paper account. Prices are the live ask to buy and bid to sell (* = no live quote: the app\'s Black-Scholes estimate). '
      + 'Charges are Indian F&amp;O charges on both legs. Nothing is ever sent to a broker.</p>'
      + `<div class="pp-reset"><label>Start again with <select id="pp-cap">${[100000, 200000, 300000, 500000].map(v => `<option value="${v}"${v === (S.resetCap || 100000) ? ' selected' : ''}>${rs(v)}</option>`).join('')}</select></label>`
      + `<button type="button" class="reset" id="pp-reset">${S.resetArmed ? `Click again to delete all paper trades and start at ${rs(S.resetCap || 100000)}` : 'Reset paper account'}</button>`
      + `<small>The three 09:15 straddles together cost about ₹1-1.2 lakh at recent prices, so on ₹1 lakh one of them is skipped.</small></div>`;
    if (host._h !== h) { host.innerHTML = h; host._h = h; }
  }
  const sumSplit = (rows, k) => rows.reduce((t, r) => t + ((r.split && ok(r.split[k])) ? r.split[k] : 0), 0);

  async function loadPaper() {
    let d;
    try { d = await getJSON('/api/paper/trades'); } catch { return; }
    // a trade that closed by itself (stop, target, time) since the last look: say so
    const before = S.paperData ? new Set(S.paperData.open.map(t => t.id)) : null;
    S.paperData = d;
    if (before) {
      for (const t of d.closed) {
        if (before.has(t.id)) toast(`Paper trade closed (${t.exit_reason}): ${t.contract} ${signed(t.pnl, x => rs(x))} after charges.`, (t.pnl || 0) < 0);
      }
    }
    S.openNames = new Set(d.open.map(t => t.name));
    if (S.paperOpen) renderPaper();
    renderPaperBtn(); renderMine(); renderPicks(); renderBreaks();
  }

  // ------------------------------------------------------------------ my paper trades (top of the left column)
  function renderMine() {
    const d = S.paperData;
    const sec = $('#mine');
    if (!d) return;
    const today = S.pred && S.pred.now ? S.pred.now.slice(0, 10) : '';
    const closedToday = d.closed.filter(t => t.day === today);
    sec.hidden = !d.open.length && !closedToday.length;
    if (sec.hidden) return;
    const openPnl = d.open.reduce((a, t) => a + (ok(t.live_pnl) ? t.live_pnl : 0), 0);
    $('#mine-sub').innerHTML = d.open.length ? `${d.open.length} open leg${d.open.length === 1 ? '' : 's'} · <b class="${tone(openPnl)}">${signed(openPnl, x => rs(x))}</b>` : 'none open';
    const groups = {};
    for (const t of d.open) if (t.group) (groups[t.group] = groups[t.group] || []).push(t);
    const shown = new Set();
    const html = d.open.length ? d.open.map(t => {
      if (t.group && groups[t.group].length > 1) {
        if (shown.has(t.group)) return '';
        shown.add(t.group);
        const L = groups[t.group];
        const pnl = L.reduce((a, x) => a + (ok(x.live_pnl) ? x.live_pnl : 0), 0);
        return `<li><div class="mt both${S.sel.kind === t.kind && (t.kind === 'index' ? S.sel.key === t.name : S.sel.name === t.name) ? ' on' : ''}">`
          + `<button type="button" class="mt-main" data-kind="${t.kind}" data-name="${esc(t.name)}" title="Open its chart">`
          + `<span class="r1"><span class="act both">CE+PE</span><b>${esc(t.name)} ${Number(t.strike).toLocaleString('en-IN')}</b></span>`
          + `<span class="r2">${t.auto ? 'Auto ' : ''}${esc(t.opened)} · no stop · closes 15:15</span>`
          + `<span class="r2">${L.map(x => `${x.side} <span class="${tone(x.live_pnl)}">${ok(x.live_pnl) ? signed(x.live_pnl, v => rs(v)) : '—'}</span>`).join(' · ')}</span></button>`
          + `<span class="mt-pnl ${tone(pnl)}">${signed(pnl, x => rs(x))}</span>`
          + `<span class="mt-legs">${L.map(x => `<button type="button" class="exit xs" data-id="${x.id}" ${S.busy ? 'disabled' : ''} `
            + `title="Sell only the ${x.side}: ${ok(x.live_pnl) ? signed(x.live_pnl, v => rs(v)) : ''}">Exit ${x.side}</button>`).join('')}</span></div></li>`;
      }
      const ce = t.side === 'CE';
      const stop = ok(t.stop_now) ? t.stop_now : t.stop_spot;
      return `<li><div class="mt ${ce ? 'up' : 'dn'}${S.sel.kind === t.kind && (t.kind === 'index' ? S.sel.key === t.name : S.sel.name === t.name) ? ' on' : ''}">`
        + `<button type="button" class="mt-main" data-kind="${t.kind}" data-name="${esc(t.name)}" title="Open its chart">`
        + `<span class="r1"><span class="act ${ce ? 'up' : 'dn'}">${t.side}</span><b>${esc(t.contract)}</b></span>`
        + `<span class="r2">${esc(t.opened)} · ${rs(t.entry_premium, 2)} → ${ok(t.mark) ? rs(t.mark, 2) : '—'}${t.mark_source === 'model' ? '*' : ''}</span>`
        + `<span class="r2">${t.rule === 'straddle' ? 'Straddle leg · no stop · closes 15:15' : `Stop ${px(stop)}${ok(t.target_spot) ? ` · target ${px(t.target_spot)}` : ''}`}</span></button>`
        + `<span class="mt-pnl ${tone(t.live_pnl)}">${ok(t.live_pnl) ? signed(t.live_pnl, x => rs(x)) : '—'}</span>`
        + `<button type="button" class="exit sm" data-id="${t.id}" ${S.busy ? 'disabled' : ''}>Exit</button></div></li>`;
    }).join('') : '<li class="empty sm">No open paper trade.</li>';
    const host = $('#mine-list');
    if (host._h !== html) { host.innerHTML = html; host._h = html; }
    const booked = closedToday.reduce((a, t) => a + (t.pnl || 0), 0);
    $('#mine-all').innerHTML = closedToday.length
      ? `Closed today: ${closedToday.length} · <b class="${tone(booked)}">${signed(booked, x => rs(x))}</b> — see all trades`
      : 'See all paper trades';
  }
  $('#mine').addEventListener('click', async e => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.classList.contains('exit')) {
      await exitClick(b);
    } else if (b.classList.contains('mt-main')) {
      select(b.dataset.kind === 'index' ? { kind: 'index', key: b.dataset.name } : { kind: 'stock', name: b.dataset.name });
    } else if (b.id === 'mine-all') openPaper(true);
  });
  function openPaper(on) {
    S.paperOpen = on;
    $('#paper').hidden = !on;
    $('#paper-btn').setAttribute('aria-expanded', String(on));
    S.resetArmed = false;
    if (on) { $('#pp-body')._h = ''; loadPaper(); $('#pp-close').focus(); }
  }
  $('#paper-btn').addEventListener('click', () => openPaper(!S.paperOpen));
  $('#pp-close').addEventListener('click', () => openPaper(false));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && S.paperOpen) openPaper(false); });
  $('#pp-body').addEventListener('change', e => {
    if (e.target.id === 'pp-cap') { S.resetCap = Number(e.target.value); S.resetArmed = false; $('#pp-body')._h = ''; renderPaper(); }
  });
  $('#pp-body').addEventListener('click', async e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.classList.contains('exit')) {
      const t = await paperPost('/api/paper/exit', { id: Number(b.dataset.id) });
      if (t) toast(`Paper SELL ${t.contract} at ${rs(t.exit_premium, 2)}: ${signed(t.pnl, x => rs(x))} after charges.`);
    } else if (b.classList.contains('lnk')) {
      openPaper(false);
      select(b.dataset.kind === 'index' ? { kind: 'index', key: b.dataset.name } : { kind: 'stock', name: b.dataset.name });
    } else if (b.id === 'pp-auto') {
      const on = b.getAttribute('aria-pressed') !== 'true';
      const r = await postJSON('/api/paper/prefs', { auto_straddle: on });
      if (r.ok) { S.paperData = await r.json(); $('#pp-body')._h = ''; renderPaper(); toast(`Auto 09:15 straddle ${on ? 'on' : 'off'}.`); }
    } else if (b.id === 'pp-reset') {
      if (!S.resetArmed) { S.resetArmed = true; renderPaper(); setTimeout(() => { S.resetArmed = false; if (S.paperOpen) renderPaper(); }, 5000); return; }
      S.resetArmed = false;
      const r = await postJSON('/api/paper/reset', { capital: S.resetCap || 100000 });
      if (r.ok) { S.paperData = await r.json(); $('#pp-body')._h = ''; renderPaper(); toast(`Paper account reset to ${rs(S.resetCap || 100000)}.`); loadTicket(); }
      else toast(await errText(r), true);
    }
  });
  setInterval(() => {
    if (document.hidden) return;
    if (!S.busy) loadTicket();
    loadPaper();
  }, 2000);

  // ------------------------------------------------------------------ live stream
  // Browsers allow only 6 connections to one address, and every open tab keeps one live stream. So a tab
  // in the background lets go of its stream after a few seconds and takes it back when you return to it.
  let es = null, hideTimer = null;
  function release() {
    if (!es) return;
    es.close(); es = null; S.connected = false;
  }
  document.addEventListener('visibilitychange', () => {
    clearTimeout(hideTimer);
    if (document.hidden) hideTimer = setTimeout(release, 4000);
    else if (!es) { connect(); refreshNow(); }
  });
  async function refreshNow() {
    const [pred, st, mv] = await Promise.all([
      getJSON('/api/predictions').catch(() => null), getJSON('/api/state').catch(() => null), getJSON('/api/movers').catch(() => null),
    ]);
    if (pred) S.pred = pred;
    if (mv) S.movers = mv;
    if (st) onState(st);
    renderAll();
  }

  function connect() {
    if (es) return;
    es = new EventSource('/api/stream');
    es.onopen = () => {
      const again = S.opened;
      S.connected = true; S.opened = true; S.downSince = null;
      renderFeed();
      if (again) loadChart();
    };
    es.onerror = () => { S.connected = false; if (!S.downSince) S.downSince = Date.now(); renderFeed(); };
    es.addEventListener('state', e => onState(JSON.parse(e.data)));
    es.addEventListener('predictions', e => { S.pred = JSON.parse(e.data); renderAll(); });
    es.addEventListener('bars', e => onBars(JSON.parse(e.data)));
    es.addEventListener('movers', e => { S.movers = JSON.parse(e.data); renderMovers(); if (S.sel.kind === 'stock') renderSheet(); });
    es.addEventListener('stock_ready', e => {
      const d = JSON.parse(e.data);
      if (S.sel.kind === 'stock' && S.sel.name === d.name) loadChart();
    });
  }

  async function init() {
    const tf = Number(store.get('io.tf'));
    S.lots = Math.max(1, Math.min(50, Number(store.get('io.lots')) || 1));
    if ([1, 5, 15].includes(tf)) S.tf = tf;
    for (const b of $('#tfs').querySelectorAll('button')) b.setAttribute('aria-selected', String(Number(b.dataset.tf) === S.tf));
    try {
      const sel = JSON.parse(store.get('io.sel') || 'null');
      if (sel && sel.kind === 'index' && INDEX_ORDER.includes(sel.key)) { S.sel = sel; S.lastIndex = sel.key; }
      else if (sel && sel.kind === 'stock' && sel.name) S.sel = sel;
    } catch { /* default selection */ }
    const [pred, st, mv] = await Promise.all([
      getJSON('/api/predictions').catch(() => null), getJSON('/api/state').catch(() => null), getJSON('/api/movers').catch(() => null),
    ]);
    S.pred = pred; S.movers = mv;
    if (st) onState(st);
    if (S.sel.kind === 'stock') postJSON('/api/mover', { name: S.sel.name }).catch(() => {});
    renderAll();
    loadTicket(); loadPaper();
    await loadChart();
    connect();
    requestAnimationFrame(loop);
  }
  // ---------------------------------------------------------------- past days (practice replays)
  const DY = { open: false, timer: null, data: null };
  const fmtDay = d => new Date(d + 'T12:00:00').toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  function dyRender(d) {
    DY.data = d;
    const busy = d.state === 'rebuilding' || d.state === 'starting';
    $('#dy-go').disabled = busy;
    const box = $('#dy-status-box');
    box.hidden = d.state === 'idle' && !d.message;
    const st = $('#dy-status');
    st.textContent = d.message || (d.state === 'idle' ? 'Nothing running.' : d.state);
    st.dataset.s = d.state;
    const open = $('#dy-open');
    open.hidden = d.state !== 'ready';
    open.href = d.url;
    $('#dy-stop').hidden = d.state === 'idle';
    const lg = $('#dy-log');
    lg.hidden = !(d.log && (busy || d.state === 'failed'));
    lg.textContent = d.log || '';
    const list = $('#dy-list');
    list.innerHTML = (d.days || []).map(x => `<li><b>${esc(fmtDay(x.day))}</b><small>${x.kind === 'rebuilt' ? 'rebuilt from candles'
      : (x.today ? 'recorded live · still recording' : 'recorded live')}</small>
      <button type="button" data-day="${esc(x.day)}" data-folder="${esc(x.folder)}"${busy ? ' disabled' : ''}>Practise</button></li>`).join('');
    $('#dy-empty').hidden = (d.days || []).length > 0;
  }
  async function dyLoad() {
    const d = await getJSON('/api/practice').catch(() => null);
    if (d && !d.available && d.home) {        // a practice replay: the button goes back to the day picker
      const b = $('#days-btn');
      b.hidden = false; b.textContent = 'Past days ↗'; b.title = 'Pick another day (opens the main screen)';
      DY.home = d.home;
      return;
    }
    if (!d || !d.available) return;
    $('#days-btn').hidden = false;
    if (DY.open) dyRender(d); else DY.data = d;
  }
  function dyToggle(show) {
    DY.open = show;
    $('#days').hidden = !show;
    $('#days-btn').setAttribute('aria-expanded', String(show));
    clearInterval(DY.timer);
    if (show) { dyLoad(); DY.timer = setInterval(dyLoad, 1500); }
  }
  async function dyStart(body) {
    const r = await postJSON('/api/practice', body).then(r => r.json()).catch(() => ({ ok: false, error: 'The app did not answer.' }));
    if (!r.ok) { $('#dy-status-box').hidden = false; $('#dy-status').textContent = r.error; $('#dy-status').dataset.s = 'failed'; }
    dyLoad();
  }
  (function dyInit() {
    const t = new Date();
    do { t.setDate(t.getDate() - 1); } while (t.getDay() === 0 || t.getDay() === 6);
    const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    $('#dy-date').value = iso(t);
    $('#dy-date').max = iso(new Date());
    $('#days-btn').addEventListener('click', () => { if (DY.home) window.open(DY.home, 'io-main'); else dyToggle(!DY.open); });
    $('#dy-close').addEventListener('click', () => dyToggle(false));
    $('#dy-go').addEventListener('click', () => {
      const day = $('#dy-date').value;
      if (!day) return;
      dyStart({ action: 'start', day, stocks: $('#dy-stocks').checked });
    });
    $('#dy-stop').addEventListener('click', () => postJSON('/api/practice', { action: 'stop' }).then(dyLoad));
    $('#dy-list').addEventListener('click', e => {
      const b = e.target.closest('button[data-day]');
      if (b) dyStart({ action: 'start', day: b.dataset.day, folder: b.dataset.folder });
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && DY.open) dyToggle(false); });
    dyLoad().then(() => { if (location.hash === '#days' && !$('#days-btn').hidden && !DY.open) dyToggle(true); });
  })();

  if (new URLSearchParams(location.search).has('debug')) window.__io = { S, candles, chart };   // for local testing only
  init();
})();
