/* Chart tools: drawings (trend line, ray, horizontal line, rectangle, Fibonacci, text) and indicators
   (EMA, SMA, VWAP, Bollinger, Supertrend on the price; RSI, MACD, Stochastic, ATR in panes below).
   Works on top of the dashboard's chart through window.IO_CHART; everything is drawn here, nothing is sent anywhere.
   Drawings are kept in this browser per symbol (in time + price, so they stay put across 1m / 5m / 15m). */
(() => {
  'use strict';
  const IO = window.IO_CHART;
  if (!IO || !IO.chart) return;
  const { LW, chart, candles, frame } = IO;
  const $ = s => document.querySelector(s);
  const COL = { gold: '#F5C451', up: '#5CE1E6', dn: '#FF8A3D', frost: '#E6ECFF', slate: '#8F9BC6', stop: '#FF5C7A',
                violet: '#B48CFF', green: '#7CE38B' };
  const PALETTE = [COL.gold, COL.up, COL.frost, COL.stop, COL.green, COL.violet, COL.dn];
  const store = {
    get(k, d) { try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };
  const ok = v => typeof v === 'number' && Number.isFinite(v);
  const dayOf = t => Math.floor(t / 86400);
  const nf = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // ================================================================ indicators (maths)
  const sma = (v, n) => { const o = Array(v.length).fill(null); let s = 0;
    for (let i = 0; i < v.length; i++) { s += v[i]; if (i >= n) s -= v[i - n]; if (i >= n - 1) o[i] = s / n; } return o; };
  const ema = (v, n) => { const o = Array(v.length).fill(null); if (v.length < n) return o; const k = 2 / (n + 1);
    let e = 0; for (let i = 0; i < n; i++) e += v[i]; e /= n; o[n - 1] = e;
    for (let i = n; i < v.length; i++) { e = v[i] * k + e * (1 - k); o[i] = e; } return o; };
  const rma = (v, n) => { const o = Array(v.length).fill(null); let s = 0, c = 0, prev = null;
    for (let i = 0; i < v.length; i++) { if (v[i] == null) continue;
      if (prev == null) { s += v[i]; c++; if (c === n) { prev = s / n; o[i] = prev; } }
      else { prev = (prev * (n - 1) + v[i]) / n; o[i] = prev; } } return o; };
  const emaN = (v, n) => { // EMA over an array that starts with nulls
    const o = Array(v.length).fill(null); const k = 2 / (n + 1); let e = null, s = 0, c = 0;
    for (let i = 0; i < v.length; i++) { if (v[i] == null) continue;
      if (e == null) { s += v[i]; c++; if (c === n) { e = s / n; o[i] = e; } } else { e = v[i] * k + e * (1 - k); o[i] = e; } }
    return o; };
  function trueRange(B) { return B.map((b, i) => i === 0 ? b.high - b.low
    : Math.max(b.high - b.low, Math.abs(b.high - B[i - 1].close), Math.abs(b.low - B[i - 1].close))); }
  function rsi(c, n) { const g = [null], l = [null];
    for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1]; g.push(Math.max(d, 0)); l.push(Math.max(-d, 0)); }
    const ag = rma(g, n), al = rma(l, n);
    return c.map((_, i) => ag[i] == null || al[i] == null ? null : al[i] === 0 ? 100 : 100 - 100 / (1 + ag[i] / al[i])); }
  function stdev(v, n, m) { return v.map((_, i) => { if (m[i] == null) return null; let s = 0;
    for (let j = i - n + 1; j <= i; j++) s += (v[j] - m[i]) ** 2; return Math.sqrt(s / n); }); }
  function vwap(B) { // resets each day; index candles carry no volume, so then every minute counts equally
    const o = []; let d = null, pv = 0, vv = 0, tp = 0, cnt = 0;
    for (const b of B) { if (dayOf(b.time) !== d) { d = dayOf(b.time); pv = vv = tp = cnt = 0; }
      const p = (b.high + b.low + b.close) / 3, v = b.volume || 0; pv += p * v; vv += v; tp += p; cnt++;
      o.push(vv > 0 ? pv / vv : tp / cnt); } return o; }
  function supertrend(B, n, m) {
    const atr = rma(trueRange(B), n); const up = [], dn = []; let fu = null, fl = null, dir = 1;
    for (let i = 0; i < B.length; i++) {
      if (atr[i] == null) { up.push(null); dn.push(null); continue; }
      const hl2 = (B[i].high + B[i].low) / 2, bu = hl2 + m * atr[i], bl = hl2 - m * atr[i], pc = i ? B[i - 1].close : B[i].close;
      fu = fu == null || bu < fu || pc > fu ? bu : fu;
      fl = fl == null || bl > fl || pc < fl ? bl : fl;
      if (dir === 1 && B[i].close < fl) dir = -1; else if (dir === -1 && B[i].close > fu) dir = 1;
      up.push(dir === 1 ? fl : null); dn.push(dir === -1 ? fu : null);
    } return { up, dn }; }

  // ================================================================ indicators (settings + series)
  const DEF = { ema: { on: true, len: [9, 21, ''] }, sma: { on: false, len: [20, 50, ''] }, vwap: { on: false },
    bb: { on: false, len: 20, mult: 2 }, st: { on: false, len: 10, mult: 3 }, rsi: { on: false, len: 14 },
    macd: { on: false, f: 12, s: 26, sig: 9 }, stoch: { on: false, k: 14, ks: 3, d: 3 }, atr: { on: false, len: 14 },
    vp: { on: false, rows: 40 } };
  const cfg = Object.assign(JSON.parse(JSON.stringify(DEF)), store.get('io.ind', {}));
  const LINECOL = [COL.gold, COL.violet, COL.green];
  let made = [];          // series created for indicators
  let sig = '';

  function clearSeries() {
    for (const s of made) { try { chart.removeSeries(s); } catch { /* already gone */ } }
    made = [];
    try { for (let i = chart.panes().length - 1; i > 0; i--) chart.removePane(i); } catch { /* older API */ }
  }
  const line = (pane, color, title, extra = {}) => {
    const s = chart.addSeries(LW.LineSeries, { color, lineWidth: 1.5, title, priceLineVisible: false,
      lastValueVisible: !!title, crosshairMarkerVisible: false, ...extra }, pane);
    made.push(s); return s; };
  const ser = (B, vals) => B.map((b, i) => (vals[i] == null ? { time: b.time } : { time: b.time, value: vals[i] }));

  function rebuild() {
    const B = (IO.bars || []).filter(b => ok(b.close));
    clearSeries();
    if (!B.length) return;
    const C = B.map(b => b.close);
    // on the price chart
    if (cfg.ema.on) cfg.ema.len.forEach((n, i) => { n = +n; if (n > 0) line(0, LINECOL[i], `EMA ${n}`).setData(ser(B, ema(C, n))); });
    if (cfg.sma.on) cfg.sma.len.forEach((n, i) => { n = +n; if (n > 0) line(0, [COL.up, COL.dn, COL.frost][i], `SMA ${n}`).setData(ser(B, sma(C, n))); });
    if (cfg.vwap.on) line(0, COL.frost, 'VWAP', { lineStyle: LW.LineStyle.Dashed }).setData(ser(B, vwap(B)));
    if (cfg.bb.on) { const n = +cfg.bb.len || 20, k = +cfg.bb.mult || 2, m = sma(C, n), sd = stdev(C, n, m);
      line(0, 'rgba(143,155,198,.9)', `BB ${n}`).setData(ser(B, m));
      line(0, 'rgba(143,155,198,.6)', '').setData(ser(B, m.map((x, i) => x == null ? null : x + k * sd[i])));
      line(0, 'rgba(143,155,198,.6)', '').setData(ser(B, m.map((x, i) => x == null ? null : x - k * sd[i]))); }
    if (cfg.st.on) { const r = supertrend(B, +cfg.st.len || 10, +cfg.st.mult || 3);
      line(0, COL.green, 'ST', { lineWidth: 2 }).setData(ser(B, r.up));
      line(0, COL.stop, '', { lineWidth: 2 }).setData(ser(B, r.dn)); }
    // panes below
    let pane = 1;
    const fixed = (lo, hi) => ({ autoscaleInfoProvider: () => ({ priceRange: { minValue: lo, maxValue: hi } }) });
    if (cfg.rsi.on) { const n = +cfg.rsi.len || 14, p = pane++;
      line(p, COL.violet, `RSI ${n}`, fixed(0, 100)).setData(ser(B, rsi(C, n)));
      line(p, 'rgba(255,92,122,.5)', '', { lineWidth: 1, lineStyle: LW.LineStyle.Dashed, ...fixed(0, 100) }).setData(B.map(b => ({ time: b.time, value: 70 })));
      line(p, 'rgba(92,225,230,.5)', '', { lineWidth: 1, lineStyle: LW.LineStyle.Dashed, ...fixed(0, 100) }).setData(B.map(b => ({ time: b.time, value: 30 }))); }
    if (cfg.macd.on) { const f = +cfg.macd.f || 12, s = +cfg.macd.s || 26, g = +cfg.macd.sig || 9, p = pane++;
      const ef = ema(C, f), es = ema(C, s), m = C.map((_, i) => ef[i] == null || es[i] == null ? null : ef[i] - es[i]);
      const sg = emaN(m, g), h = m.map((x, i) => x == null || sg[i] == null ? null : x - sg[i]);
      const hs = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, p); made.push(hs);
      hs.setData(B.map((b, i) => h[i] == null ? { time: b.time } : { time: b.time, value: h[i], color: h[i] >= 0 ? 'rgba(92,225,230,.55)' : 'rgba(255,138,61,.55)' }));
      line(p, COL.up, `MACD ${f},${s},${g}`).setData(ser(B, m));
      line(p, COL.dn, '').setData(ser(B, sg)); }
    if (cfg.stoch.on) { const k = +cfg.stoch.k || 14, ks = +cfg.stoch.ks || 3, d = +cfg.stoch.d || 3, p = pane++;
      const raw = B.map((b, i) => { if (i < k - 1) return null; let hi = -Infinity, lo = Infinity;
        for (let j = i - k + 1; j <= i; j++) { hi = Math.max(hi, B[j].high); lo = Math.min(lo, B[j].low); }
        return hi === lo ? 50 : (b.close - lo) / (hi - lo) * 100; });
      const smooth = (v, n) => v.map((_, i) => { let s = 0; for (let j = i - n + 1; j <= i; j++) { if (j < 0 || v[j] == null) return null; s += v[j]; } return s / n; });
      const K = smooth(raw, ks), D = smooth(K, d);
      line(p, COL.gold, `Stoch ${k},${ks},${d}`, fixed(0, 100)).setData(ser(B, K));
      line(p, COL.violet, '', fixed(0, 100)).setData(ser(B, D));
      for (const lv of [80, 20]) line(p, 'rgba(143,155,198,.45)', '', { lineWidth: 1, lineStyle: LW.LineStyle.Dashed, ...fixed(0, 100) }).setData(B.map(b => ({ time: b.time, value: lv }))); }
    if (cfg.atr.on) { const n = +cfg.atr.len || 14, p = pane++;
      line(p, COL.dn, `ATR ${n}`).setData(ser(B, rma(trueRange(B), n))); }
    try {                               // price chart about 60% of the height, the panes below share the rest
      const ps = chart.panes(), n = ps.length - 1;
      if (n > 0) { ps[0].setStretchFactor(3 * n / 2); for (let i = 1; i < ps.length; i++) ps[i].setStretchFactor(1); }
    } catch { /* pane sizing not available */ }
  }
  function tick() {
    const B = IO.bars || [], L = B[B.length - 1];
    const s = `${IO.key}|${IO.tf}|${B.length}|${L ? L.time + ':' + L.close : ''}|${JSON.stringify(cfg)}`;
    if (s !== sig) { sig = s; try { rebuild(); } catch (e) { console.warn('indicators', e); } }
  }
  setInterval(tick, 700);

  // ---------------------------------------------------------------- indicators menu
  const head = $('.chart-head');
  const btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'ind-btn'; btn.id = 'ind-btn'; btn.textContent = 'Indicators';
  btn.setAttribute('aria-expanded', 'false');
  const fs = $('#fs-btn'); head.insertBefore(btn, fs || null);
  const menu = document.createElement('div');
  menu.className = 'ind-menu'; menu.hidden = true;
  const num = (path, val, w = 44) => `<input type="number" min="1" max="500" step="any" data-k="${path}" value="${val}" style="width:${w}px">`;
  const row = (key, label, inputs) => `<label class="ind-row"><input type="checkbox" data-on="${key}"${cfg[key].on ? ' checked' : ''}>
    <b>${label}</b><span>${inputs}</span></label>`;
  menu.innerHTML = `<div class="ind-h">On the price</div>`
    + row('ema', 'EMA', cfg.ema.len.map((v, i) => num(`ema.len.${i}`, v)).join(''))
    + row('sma', 'SMA', cfg.sma.len.map((v, i) => num(`sma.len.${i}`, v)).join(''))
    + row('vwap', 'VWAP', '<small>resets daily</small>')
    + row('bb', 'Bollinger', num('bb.len', cfg.bb.len) + num('bb.mult', cfg.bb.mult))
    + row('st', 'Supertrend', num('st.len', cfg.st.len) + num('st.mult', cfg.st.mult))
    + row('vp', 'Volume profile', num('vp.rows', cfg.vp.rows) + '<small>rows · visible candles</small>')
    + `<div class="ind-h">Below the chart</div>`
    + row('rsi', 'RSI', num('rsi.len', cfg.rsi.len))
    + row('macd', 'MACD', num('macd.f', cfg.macd.f) + num('macd.s', cfg.macd.s) + num('macd.sig', cfg.macd.sig))
    + row('stoch', 'Stochastic', num('stoch.k', cfg.stoch.k) + num('stoch.ks', cfg.stoch.ks) + num('stoch.d', cfg.stoch.d))
    + row('atr', 'ATR', num('atr.len', cfg.atr.len))
    + `<div class="ind-foot"><small>Empty box = that line off. Indices have no volume, so VWAP weights every minute equally.</small>
       <button type="button" id="ind-none">Remove all</button></div>`;
  head.appendChild(menu);
  const save = () => { store.set('io.ind', cfg); sig = ''; tick(); };
  menu.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset.on) cfg[t.dataset.on].on = t.checked;
    else if (t.dataset.k) {
      const [a, b, c] = t.dataset.k.split('.');
      const v = t.value === '' ? '' : Math.max(0.1, Math.min(500, +t.value));
      if (c !== undefined) cfg[a][b][+c] = v; else cfg[a][b] = v;
      const cb = menu.querySelector(`[data-on="${a}"]`); if (cb && !cb.checked) { cb.checked = true; cfg[a].on = true; }
    }
    save();
  });
  menu.querySelector('#ind-none').addEventListener('click', () => {
    for (const k in cfg) cfg[k].on = false;
    for (const cb of menu.querySelectorAll('[data-on]')) cb.checked = false;
    save();
  });
  btn.addEventListener('click', e => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute('aria-expanded', String(!menu.hidden)); });
  document.addEventListener('click', e => { if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); } });

  // ================================================================ drawings
  const layer = document.createElement('canvas');
  layer.className = 'drawlayer';
  frame.appendChild(layer);
  const g = layer.getContext('2d');
  const bar = document.createElement('div');
  bar.className = 'draw-bar'; bar.setAttribute('role', 'toolbar'); bar.setAttribute('aria-label', 'Drawing tools');
  const ICON = {
    cursor: 'M5 3l6 16 2-7 7-2z',
    trend: 'M4 20L20 4M4 20m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M20 4m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0',
    ray: 'M4 18L22 6M4 18m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0',
    hline: 'M2 12h20M12 12m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0',
    vline: 'M12 2v20M12 12m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0',
    rect: 'M4 6h16v12H4z',
    fib: 'M3 5h18M3 9.5h18M3 14h18M3 19h18M5 19L19 5',
    brush: 'M4 18c3 0 3-4 6-4s3 4 6 4 3-6 4-8',
    text: 'M5 5h14M12 5v14M9 19h6',
    measure: 'M4 20V4M4 20h16M8 16l4-5 3 3 5-7',
    eraser: 'M8 20h12M5 15l8-8 6 6-5 5H9z',
    magnet: 'M6 4v8a6 6 0 0 0 12 0V4M6 4h4v8a2 2 0 0 0 4 0V4h4',
    color: 'M12 3a9 9 0 1 0 0 18c1 0 1.5-.8 1.5-1.5 0-1.2-1-1.5-1-2.5S13.5 15 15 15h2a4 4 0 0 0 4-4c0-4.4-4-8-9-8z',
    del: 'M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13',
  };
  const TIPS = { cursor: 'Select / move (Esc)', trend: 'Trend line', ray: 'Ray (extends right)', hline: 'Horizontal line',
    vline: 'Vertical line (a time)', brush: 'Brush (draw freely)', measure: 'Measure: points, % and candles between two points (not saved)',
    eraser: 'Eraser: click a drawing to delete it',
    rect: 'Rectangle (zone)', fib: 'Fibonacci retracement', text: 'Text note', magnet: 'Magnet: snap to candle high / low / open / close',
    color: 'Colour of the selected drawing (and the next ones)', del: 'Delete the selected drawing (Delete key). Hold Shift to clear all drawings' };
  bar.innerHTML = Object.keys(ICON).map(k => `<button type="button" data-tool="${k}" title="${TIPS[k]}" aria-label="${TIPS[k]}">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="${ICON[k]}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg></button>`).join('');
  frame.appendChild(bar);

  const D = { tool: 'cursor', items: [], sel: null, pending: null, key: null, magnet: store.get('io.draw.magnet', false),
              color: store.get('io.draw.color', COL.gold), drag: null };
  const sk = () => `io.draw.${D.key}`;
  const persist = () => store.set(sk(), D.items);
  function loadKey() { if (IO.key !== D.key) { D.key = IO.key; D.items = store.get(sk(), []); D.sel = null; D.pending = null; } }
  function setTool(t) {
    if (t === 'magnet') { D.magnet = !D.magnet; store.set('io.draw.magnet', D.magnet); paintBar(); return; }
    if (t === 'color') { const i = (PALETTE.indexOf(D.color) + 1) % PALETTE.length; D.color = PALETTE[i]; store.set('io.draw.color', D.color);
      const it = D.items.find(x => x.id === D.sel); if (it) { it.color = D.color; persist(); } paintBar(); return; }
    if (t === 'del') return;
    D.tool = t; D.pending = null;
    layer.style.pointerEvents = t === 'cursor' ? 'none' : 'auto';
    layer.style.cursor = t === 'cursor' ? '' : 'crosshair';
    paintBar();
  }
  function paintBar() {
    for (const b of bar.querySelectorAll('button')) {
      const t = b.dataset.tool;
      b.classList.toggle('on', t === D.tool || (t === 'magnet' && D.magnet));
      if (t === 'color') b.style.color = D.color;
    }
  }
  bar.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.tool === 'del') {
      if (e.shiftKey) { if (D.items.length && confirm('Delete all drawings on this chart?')) { D.items = []; D.sel = null; persist(); } }
      else removeSel();
      return;
    }
    setTool(b.dataset.tool === D.tool && b.dataset.tool !== 'cursor' ? 'cursor' : b.dataset.tool);
  });
  function removeSel() { if (!D.sel) return; D.items = D.items.filter(x => x.id !== D.sel); D.sel = null; persist(); }

  // time <-> x through the bar index, so points between / beyond candles work in any candle size
  function times() { return (IO.bars || []).map(b => b.time); }
  function tToX(t) {
    const T = times(), n = T.length, ts = chart.timeScale(), step = (IO.tf || 5) * 60;
    if (!n) return null;
    let lg;
    if (t <= T[0]) lg = (t - T[0]) / step;
    else if (t >= T[n - 1]) lg = n - 1 + (t - T[n - 1]) / step;
    else { let lo = 0, hi = n - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (T[m] <= t) lo = m; else hi = m; }
      lg = lo + (t - T[lo]) / (T[hi] - T[lo]); }
    return ts.logicalToCoordinate(lg);
  }
  function xToT(x) {
    const T = times(), n = T.length, ts = chart.timeScale(), step = (IO.tf || 5) * 60;
    const lg = ts.coordinateToLogical(x);
    if (!n || lg == null) return null;
    if (lg <= 0) return T[0] + lg * step;
    if (lg >= n - 1) return T[n - 1] + (lg - (n - 1)) * step;
    const i = Math.floor(lg); return T[i] + (lg - i) * (T[i + 1] - T[i]);
  }
  const pToY = p => candles.priceToCoordinate(p);
  const yToP = y => candles.coordinateToPrice(y);
  function pointAt(x, y) {
    let t = xToT(x), p = yToP(y);
    if (t == null || p == null) return null;
    if (D.magnet) {
      const B = IO.bars || [], lg = Math.round(chart.timeScale().coordinateToLogical(x));
      const b = B[Math.max(0, Math.min(B.length - 1, lg))];
      if (b) { t = b.time; let best = null;
        for (const v of [b.high, b.low, b.open, b.close]) { const d = Math.abs(pToY(v) - y); if (d < 14 && (!best || d < best.d)) best = { v, d }; }
        if (best) p = best.v; }
    }
    return { t, p };
  }
  const mainH = () => { try { return chart.panes()[0].getHeight(); } catch { return layer.clientHeight - chart.timeScale().height(); } };
  const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

  function geo(it) { // screen geometry of one drawing
    const ax = tToX(it.a.t), ay = pToY(it.a.p);
    const bx = it.b ? tToX(it.b.t) : null, by = it.b ? pToY(it.b.p) : null;
    return { ax, ay, bx, by };
  }
  // volume profile of the candles on screen: volume when the symbol has it, else time spent at each price (indices)
  function profile(pw, ph) {
    const B = IO.bars || []; if (!B.length) return;
    const r = chart.timeScale().getVisibleLogicalRange(); if (!r) return;
    const i0 = Math.max(0, Math.floor(r.from)), i1 = Math.min(B.length - 1, Math.ceil(r.to));
    if (i1 <= i0) return;
    let lo = Infinity, hi = -Infinity;
    for (let i = i0; i <= i1; i++) { lo = Math.min(lo, B[i].low); hi = Math.max(hi, B[i].high); }
    const n = Math.max(10, Math.min(120, Math.round(+cfg.vp.rows) || 40)), step = (hi - lo) / n;
    if (!(step > 0)) return;
    const acc = new Array(n).fill(0);
    let useVol = false; for (let i = i0; i <= i1; i++) if (B[i].volume > 0) { useVol = true; break; }
    for (let i = i0; i <= i1; i++) {
      const b = B[i], w = useVol ? (b.volume || 0) : 1;
      const a = Math.max(0, Math.min(n - 1, Math.floor((b.low - lo) / step))), z = Math.max(0, Math.min(n - 1, Math.floor((b.high - lo) / step)));
      const share = w / (z - a + 1); for (let k = a; k <= z; k++) acc[k] += share;
    }
    let mx = 0, poc = 0; acc.forEach((v, k) => { if (v > mx) { mx = v; poc = k; } });
    if (!(mx > 0)) return;
    const maxW = pw * 0.22;
    for (let k = 0; k < n; k++) {
      const y1 = pToY(lo + k * step), y2 = pToY(lo + (k + 1) * step); if (y1 == null || y2 == null) continue;
      const w = acc[k] / mx * maxW;
      g.fillStyle = k === poc ? 'rgba(245,196,81,.45)' : 'rgba(143,155,198,.22)';
      g.fillRect(pw - w, Math.min(y1, y2) + 0.5, w, Math.max(1, Math.abs(y2 - y1) - 1));
    }
    const yp = pToY(lo + (poc + 0.5) * step);
    if (yp != null) {
      g.strokeStyle = 'rgba(245,196,81,.8)'; g.lineWidth = 1; g.setLineDash([4, 3]);
      g.beginPath(); g.moveTo(0, yp); g.lineTo(pw, yp); g.stroke(); g.setLineDash([]);
      g.fillStyle = COL.gold; g.font = '600 10px Manrope, system-ui, sans-serif';
      g.fillText(useVol ? 'POC' : 'POC (time at price)', 6, yp - 4);
    }
  }

  function render() {
    requestAnimationFrame(render);
    loadKey();
    const dpr = window.devicePixelRatio || 1, w = layer.clientWidth, h = layer.clientHeight;
    if (layer.width !== Math.round(w * dpr) || layer.height !== Math.round(h * dpr)) { layer.width = Math.round(w * dpr); layer.height = Math.round(h * dpr); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    if (!(IO.bars || []).length) return;
    const pw = chart.timeScale().width(), ph = mainH();
    g.save(); g.beginPath(); g.rect(0, 0, pw, ph); g.clip();
    if (cfg.vp.on) { try { profile(pw, ph); } catch { /* not ready */ } }
    const all = [...D.items, ...(D.pending ? [D.pending] : []), ...(D.temp ? [D.temp] : [])];
    for (const it of all) { try { paint(it, pw, it.id === D.sel || it === D.pending); } catch { /* off screen */ } }
    g.restore();
  }
  function paint(it, pw, sel) {
    const { ax, ay, bx, by } = geo(it);
    const c = it.color || COL.gold;
    g.strokeStyle = c; g.fillStyle = c; g.lineWidth = sel ? 2 : 1.4; g.setLineDash([]);
    g.font = '600 11px Manrope, system-ui, sans-serif';
    if (it.type === 'hline') {
      if (ay == null) return;
      g.beginPath(); g.moveTo(0, ay); g.lineTo(pw, ay); g.stroke();
      g.fillText(nf.format(it.a.p), pw - 76, ay - 4);
    } else if (it.type === 'vline') {
      if (ax == null) return;
      g.beginPath(); g.moveTo(ax, 0); g.lineTo(ax, mainH()); g.stroke();
      g.fillText(new Date(it.a.t * 1000).toLocaleString('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }), ax + 4, mainH() - 6);
    } else if (it.type === 'brush') {
      const pts = (it.pts || []).map(q => [tToX(q.t), pToY(q.p)]).filter(q => q[0] != null && q[1] != null);
      if (pts.length < 2) return;
      g.lineJoin = 'round'; g.lineCap = 'round'; g.beginPath(); g.moveTo(pts[0][0], pts[0][1]);
      for (const q of pts.slice(1)) g.lineTo(q[0], q[1]);
      g.stroke();
    } else if (it.type === 'measure') {
      if (ax == null || ay == null || bx == null || by == null) return;
      const up = it.b.p >= it.a.p, col = up ? COL.up : COL.stop;
      g.globalAlpha = 0.14; g.fillStyle = col; g.fillRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
      g.globalAlpha = 1; g.strokeStyle = col; g.strokeRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
      const dp = it.b.p - it.a.p, pct = dp / it.a.p * 100, T = times();
      const nb = T.filter(t => t > Math.min(it.a.t, it.b.t) && t <= Math.max(it.a.t, it.b.t)).length;
      const txt = `${dp >= 0 ? '+' : ''}${nf.format(dp)} (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%) · ${nb} candle${nb === 1 ? '' : 's'}`;
      g.font = '700 12px Manrope, system-ui, sans-serif';
      const tw = g.measureText(txt).width + 12, tx = Math.min(ax, bx), ty = up ? Math.min(ay, by) - 26 : Math.max(ay, by) + 6;
      g.fillStyle = col; g.fillRect(tx, ty, tw, 20); g.fillStyle = '#0A1330'; g.fillText(txt, tx + 6, ty + 14);
      return;
    } else if (it.type === 'text') {
      if (ax == null || ay == null) return;
      g.font = '600 12px Manrope, system-ui, sans-serif'; g.fillText(it.text || '', ax + 4, ay - 4);
      if (sel) { g.setLineDash([3, 3]); g.strokeRect(ax, ay - 18, g.measureText(it.text || '').width + 8, 20); }
    } else {
      if (ax == null || ay == null || bx == null || by == null) return;
      if (it.type === 'trend') { g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke(); }
      else if (it.type === 'ray') { const dx = bx - ax, dy = by - ay, k = dx === 0 ? 0 : (pw + 50 - ax) / dx;
        g.beginPath(); g.moveTo(ax, ay); g.lineTo(dx >= 0 ? ax + dx * k : bx, dx >= 0 ? ay + dy * k : by); g.stroke(); }
      else if (it.type === 'rect') { g.globalAlpha = 0.12; g.fillRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
        g.globalAlpha = 1; g.strokeRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay)); }
      else if (it.type === 'fib') {
        const x0 = Math.min(ax, bx), x1 = Math.max(pw, Math.max(ax, bx));
        for (const f of FIB) { const p = it.b.p + (it.a.p - it.b.p) * f, y = pToY(p); if (y == null) continue;
          g.globalAlpha = f === 0 || f === 1 ? 1 : 0.75; g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
          g.fillText(`${f} (${nf.format(p)})`, x0 + 4, y - 3); }
        g.globalAlpha = 1; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke(); g.setLineDash([]);
      }
    }
    if (sel) { g.fillStyle = '#0A1330'; g.strokeStyle = c; g.lineWidth = 1.5;
      for (const [x, y] of handles(it)) { if (x == null || y == null) continue; g.beginPath(); g.arc(x, y, 4.5, 0, Math.PI * 2); g.fill(); g.stroke(); } }
  }
  function handles(it) { const { ax, ay, bx, by } = geo(it);
    if (it.type === 'hline') return [[Math.min(layer.clientWidth - 60, 60), ay]];
    if (it.type === 'vline') return [[ax, 30]];
    if (it.type === 'brush' || it.type === 'measure') return [];
    return it.b ? [[ax, ay], [bx, by]] : [[ax, ay]]; }
  function distSeg(px, py, x1, y1, x2, y2) { const dx = x2 - x1, dy = y2 - y1, L = dx * dx + dy * dy;
    const t = L ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / L)) : 0;
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)); }
  function hit(x, y) {
    const pw = chart.timeScale().width();
    for (let i = D.items.length - 1; i >= 0; i--) {
      const it = D.items[i], { ax, ay, bx, by } = geo(it);
      if (ay == null) continue;
      if (it.type === 'hline') { if (Math.abs(y - ay) < 6) return it; continue; }
      if (ax == null) continue;
      if (it.type === 'vline') { if (Math.abs(x - ax) < 6) return it; continue; }
      if (it.type === 'brush') { const P = (it.pts || []).map(q => [tToX(q.t), pToY(q.p)]);
        for (let k = 1; k < P.length; k++) if (P[k - 1][0] != null && P[k][0] != null && distSeg(x, y, P[k - 1][0], P[k - 1][1], P[k][0], P[k][1]) < 6) return it;
        continue; }
      if (it.type === 'text') { if (x >= ax - 4 && x <= ax + 120 && y >= ay - 20 && y <= ay + 4) return it; continue; }
      if (bx == null || by == null) continue;
      if (it.type === 'trend' && distSeg(x, y, ax, ay, bx, by) < 6) return it;
      if (it.type === 'ray') { const dx = bx - ax, k = dx === 0 ? 0 : (pw + 50 - ax) / dx;
        if (distSeg(x, y, ax, ay, dx >= 0 ? ax + dx * k : bx, dx >= 0 ? ay + (by - ay) * k : by) < 6) return it; }
      if (it.type === 'rect' && x >= Math.min(ax, bx) - 4 && x <= Math.max(ax, bx) + 4 && y >= Math.min(ay, by) - 4 && y <= Math.max(ay, by) + 4) return it;
      if (it.type === 'fib' && x >= Math.min(ax, bx) - 4) for (const f of FIB) { const py = pToY(it.b.p + (it.a.p - it.b.p) * f); if (py != null && Math.abs(py - y) < 5) return it; }
    }
    return null;
  }
  const local = e => { const r = layer.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  const newId = () => Math.random().toString(36).slice(2, 10);

  // creating: click-click or press-drag-release
  layer.addEventListener('pointerdown', e => {
    if (D.tool === 'cursor') return;
    e.preventDefault();
    const [x, y] = local(e); const pt = pointAt(x, y); if (!pt) return;
    if (D.tool === 'eraser') { const h = hit(x, y); if (h) { D.items = D.items.filter(i => i !== h); D.sel = null; persist(); } return; }
    D.temp = null;
    if (D.tool === 'brush') { D.pending = { id: newId(), type: 'brush', a: pt, pts: [pt], color: D.color, down: [x, y], last: [x, y] };
      layer.setPointerCapture(e.pointerId); return; }
    if (D.tool === 'hline' || D.tool === 'text' || D.tool === 'vline') {
      const it = { id: newId(), type: D.tool, a: pt, color: D.color };
      if (D.tool === 'text') { const s = prompt('Text for the note:'); if (!s) return; it.text = s.slice(0, 80); }
      D.items.push(it); persist(); D.sel = it.id; setTool('cursor'); return;
    }
    if (!D.pending) { D.pending = { id: newId(), type: D.tool, a: pt, b: pt, color: D.color, down: [x, y] }; layer.setPointerCapture(e.pointerId); }
    else finish(pt);
  });
  layer.addEventListener('pointermove', e => {
    if (!D.pending) return; const [x, y] = local(e); const pt = pointAt(x, y); if (!pt) return;
    if (D.pending.type === 'brush') { if (Math.hypot(x - D.pending.last[0], y - D.pending.last[1]) > 3) { D.pending.pts.push(pt); D.pending.last = [x, y]; } }
    else D.pending.b = pt;
  });
  layer.addEventListener('pointerup', e => {
    if (!D.pending) return; const [x, y] = local(e);
    if (D.pending.type === 'brush') { const it = D.pending; D.pending = null; delete it.down; delete it.last;
      if (it.pts.length > 1) { D.items.push(it); persist(); D.sel = it.id; } return; }
    if (Math.hypot(x - D.pending.down[0], y - D.pending.down[1]) > 8) { const pt = pointAt(x, y); if (pt) finish(pt); }
  });
  function finish(pt) { const it = D.pending; D.pending = null; it.b = pt; delete it.down;
    if (it.type === 'measure') { D.temp = it; setTool('cursor'); return; }   // shown, not saved; Esc or a click clears it
    D.items.push(it); persist(); D.sel = it.id; setTool('cursor'); }

  // selecting and dragging handles (in cursor mode the chart keeps panning and zooming as usual)
  frame.addEventListener('pointerdown', e => {
    if (D.tool !== 'cursor' || e.target.closest('.draw-bar') || e.target.closest('button')) return;
    const [x, y] = local(e);
    const it = D.items.find(i => i.id === D.sel);
    if (it) {
      const hs = handles(it);
      for (let k = 0; k < hs.length; k++) {
        const [hx, hy] = hs[k];
        if (hx != null && hy != null && Math.hypot(hx - x, hy - y) < 9) {
          e.stopPropagation(); e.preventDefault();
          D.drag = { it, k }; chart.applyOptions({ handleScroll: false, handleScale: false }); return;
        }
      }
    }
    const h = hit(x, y);
    D.sel = h ? h.id : null;
    D.temp = null;
    if (h && h.type === 'hline') { e.stopPropagation(); e.preventDefault(); D.drag = { it: h, k: 0 }; chart.applyOptions({ handleScroll: false, handleScale: false }); }
  }, true);
  window.addEventListener('pointermove', e => {
    if (!D.drag) return; const [x, y] = local(e); const pt = pointAt(x, y); if (!pt) return;
    const { it, k } = D.drag;
    if (it.type === 'hline') it.a = { t: it.a.t, p: pt.p }; else if (k === 0) it.a = pt; else it.b = pt;
  });
  window.addEventListener('pointerup', () => { if (!D.drag) return; D.drag = null; persist(); chart.applyOptions({ handleScroll: true, handleScale: true }); });

  document.addEventListener('keydown', e => {
    const t = e.target; if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
    if (e.key === 'Escape') { D.temp = null; if (D.pending || D.tool !== 'cursor') setTool('cursor'); else D.sel = null; }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && D.sel) { e.preventDefault(); removeSel(); }
    else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && D.items.length) { e.preventDefault(); D.items.pop(); D.sel = null; persist(); }
  });
  setTool('cursor');
  requestAnimationFrame(render);
})();
