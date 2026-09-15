/* Built-in indicators — TradingView-style one-click overlays.
 * Depends on globals from index.html: chart, candleSeries, lastCandles, showToast.
 * Overlays share the price scale; RSI/MACD share one oscillator pane;
 * Volume gets its own pane. Persisted in localStorage.
 */
(function () {
  "use strict";

  const STORE_KEY = "tv_builtin_indicators";

  // ---------- math ----------
  function closes(candles) { return candles.map(c => c.close); }

  function smaArr(values, n) {
    const out = new Array(values.length).fill(null);
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= n) sum -= values[i - n];
      if (i >= n - 1) out[i] = sum / n;
    }
    return out;
  }

  function emaArr(values, n) {
    const out = new Array(values.length).fill(null);
    const k = 2 / (n + 1);
    let prev = null;
    for (let i = 0; i < values.length; i++) {
      prev = prev === null ? values[i] : values[i] * k + prev * (1 - k);
      if (i >= n - 1) out[i] = prev;
    }
    return out;
  }

  function rsiArr(values, n) {
    const out = new Array(values.length).fill(null);
    let gain = 0, loss = 0;
    for (let i = 1; i < values.length; i++) {
      const d = values[i] - values[i - 1];
      const g = d > 0 ? d : 0, l = d < 0 ? -d : 0;
      if (i <= n) { gain += g; loss += l; }
      else {
        gain = (gain * (n - 1) + g) / n;
        loss = (loss * (n - 1) + l) / n;
      }
      if (i >= n) out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    }
    return out;
  }

  function vwapArr(candles) {
    const out = new Array(candles.length).fill(null);
    let pv = 0, v = 0, day = null;
    for (let i = 0; i < candles.length; i++) {
      const c = candles[i];
      if (!c.volume) return out; // no volume -> give up (caller toasts)
      const d = new Date(c.time * 1000).getUTCDate();
      if (d !== day) { pv = 0; v = 0; day = d; }
      const tp = (c.high + c.low + c.close) / 3;
      pv += tp * c.volume; v += c.volume;
      out[i] = v ? pv / v : null;
    }
    return out;
  }

  function toLine(candles, arr) {
    const pts = [];
    for (let i = 0; i < candles.length; i++) {
      if (arr[i] !== null && arr[i] !== undefined && isFinite(arr[i]))
        pts.push({ time: candles[i].time, value: arr[i] });
    }
    return pts;
  }

  // ---------- indicator definitions ----------
  const DEFS = {
    sma20:   { label: "SMA 20", kind: "overlay" },
    ema50:   { label: "EMA 50", kind: "overlay" },
    bb:      { label: "Bollinger 20,2", kind: "overlay" },
    vwap:    { label: "VWAP", kind: "overlay", needsVolume: true },
    volume:  { label: "Volume", kind: "volpane", needsVolume: true },
    rsi:     { label: "RSI 14", kind: "oscpane" },
    macd:    { label: "MACD 12,26,9", kind: "oscpane" },
  };

  const COLORS = {
    sma20: "#2962ff", ema50: "#ff6d00", vwap: "#b71c1c",
    bb: "#787b86", rsi: "#7e57c2", macd: "#2962ff", signal: "#ff6d00",
  };

  let active = {};   // id -> array of series
  try { active = JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (e) { active = {}; }
  Object.keys(active).forEach(k => { if (!DEFS[k]) delete active[k]; });
  const wantOn = {};
  Object.keys(active).forEach(k => { wantOn[k] = true; });
  active = {};

  function hasVolume() {
    return lastCandles.length && lastCandles.some(c => c.volume);
  }

  function removeAll() {
    Object.values(active).flat().forEach(s => { try { chart.removeSeries(s); } catch (e) {} });
    active = {};
    layoutPanes();
  }

  function layoutPanes() {
    const vol = !!active.volume, osc = !!(active.rsi || active.macd);
    try {
      chart.priceScale("right").applyOptions({
        scaleMargins: { top: 0.06, bottom: (vol || osc) ? 0.28 : 0.06 },
      });
      if (vol) chart.priceScale("vol-pane").applyOptions({
        scaleMargins: { top: osc ? 0.80 : 0.84, bottom: osc ? 0.12 : 0 },
      });
      if (osc) chart.priceScale("osc-pane").applyOptions({
        scaleMargins: { top: vol ? 0.90 : 0.84, bottom: 0 },
      });
    } catch (e) {}
  }

  function build(id) {
    const def = DEFS[id];
    const cs = lastCandles;
    if (!cs.length) return [];
    if (def.needsVolume && !hasVolume()) {
      showToast("אין נתוני נפח לסימול/אינטרוול הזה");
      return [];
    }
    const created = [];
    const addLine = (data, color, scaleId, width) => {
      const s = chart.addLineSeries({
        color: color, lineWidth: width || 1, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false,
        priceScaleId: scaleId || "right",
      });
      s.setData(data);
      created.push(s);
      return s;
    };

    const cl = closes(cs);
    if (id === "sma20") addLine(toLine(cs, smaArr(cl, 20)), COLORS.sma20);
    else if (id === "ema50") addLine(toLine(cs, emaArr(cl, 50)), COLORS.ema50);
    else if (id === "vwap") addLine(toLine(cs, vwapArr(cs)), COLORS.vwap);
    else if (id === "bb") {
      const mid = smaArr(cl, 20);
      const up = [], lo = [];
      for (let i = 0; i < cl.length; i++) {
        if (mid[i] === null) { up.push(null); lo.push(null); continue; }
        let sq = 0;
        for (let j = i - 19; j <= i; j++) sq += (cl[j] - mid[i]) ** 2;
        const sd = Math.sqrt(sq / 20);
        up.push(mid[i] + 2 * sd); lo.push(mid[i] - 2 * sd);
      }
      addLine(toLine(cs, mid), COLORS.bb);
      addLine(toLine(cs, up), COLORS.bb);
      addLine(toLine(cs, lo), COLORS.bb);
    }
    else if (id === "volume") {
      const s = chart.addHistogramSeries({
        priceScaleId: "vol-pane", priceLineVisible: false, lastValueVisible: false,
      });
      s.setData(cs.map(c => ({
        time: c.time,
        value: c.volume || 0,
        color: c.close >= c.open ? "rgba(8,153,129,0.5)" : "rgba(242,54,69,0.5)",
      })));
      created.push(s);
    }
    else if (id === "rsi") {
      addLine(toLine(cs, rsiArr(cl, 14)), COLORS.rsi, "osc-pane", 1);
    }
    else if (id === "macd") {
      const e12 = emaArr(cl, 12), e26 = emaArr(cl, 26);
      const m = cl.map((_, i) => (e12[i] === null || e26[i] === null) ? null : e12[i] - e26[i]);
      const mv = m.map(v => v === null ? 0 : v);
      const sig = emaArr(mv, 9);
      const s1 = chart.addLineSeries({
        color: COLORS.macd, lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false, priceScaleId: "osc-pane",
      });
      s1.setData(toLine(cs, m)); created.push(s1);
      const s2 = chart.addLineSeries({
        color: COLORS.signal, lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false, priceScaleId: "osc-pane",
      });
      const sigShifted = sig.map((v, i) => (m[i] === null || v === null) ? null : v);
      s2.setData(toLine(cs, sigShifted)); created.push(s2);
      const h = chart.addHistogramSeries({ priceScaleId: "osc-pane", priceLineVisible: false, lastValueVisible: false });
      const hp = [];
      for (let i = 0; i < cs.length; i++) {
        if (m[i] === null || sig[i] === null) continue;
        const v = m[i] - sig[i];
        hp.push({ time: cs[i].time, value: v, color: v >= 0 ? "rgba(38,166,154,0.6)" : "rgba(239,83,80,0.6)" });
      }
      h.setData(hp); created.push(h);
    }
    return created;
  }

  function isOn(id) { return !!active[id]; }

  function toggle(id) {
    if (!DEFS[id]) return;
    if (active[id]) {
      active[id].forEach(s => { try { chart.removeSeries(s); } catch (e) {} });
      delete active[id];
      // RSI/MACD share the oscillator pane — mutually exclusive
    } else {
      if (DEFS[id].kind === "oscpane") {
        ["rsi", "macd"].forEach(o => {
          if (o !== id && active[o]) {
            active[o].forEach(s => { try { chart.removeSeries(s); } catch (e) {} });
            delete active[o];
          }
        });
      }
      const made = build(id);
      if (made.length) active[id] = made;
    }
    layoutPanes();
    persist();
    renderMenu();
  }

  function persist() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(Object.keys(active).reduce((o, k) => (o[k] = 1, o), {}))); }
    catch (e) {}
  }

  function applyAll() {
    removeAllSilent();
    Object.keys(wantOn).forEach(id => {
      if (DEFS[id]) {
        if (DEFS[id].kind === "oscpane") {
          ["rsi", "macd"].forEach(o => { if (o !== id) delete wantOn[o]; });
        }
        const made = build(id);
        if (made.length) active[id] = made;
        else delete wantOn[id];
      }
    });
    layoutPanes();
    persist();
    renderMenu();
  }

  function removeAllSilent() {
    Object.values(active).flat().forEach(s => { try { chart.removeSeries(s); } catch (e) {} });
    active = {};
  }

  // ---------- menu UI ----------
  function renderMenu() {
    const menu = document.getElementById("ind-menu");
    if (!menu) return;
    menu.innerHTML = Object.keys(DEFS).map(id =>
      `<button class="ind-item ${active[id] ? "on" : ""}" onclick="toggleIndicator('${id}')">
         <span class="ind-dot" style="background:${COLORS[id] || "#888"}"></span>${DEFS[id].label}
       </button>`
    ).join("");
  }

  function toggleMenu() {
    const menu = document.getElementById("ind-menu");
    if (menu) menu.classList.toggle("open");
  }

  // ---------- public ----------
  window.toggleIndicator = toggle;
  window.clearIndicators = removeAll;
  window.applyIndicators = applyAll;
  window.renderIndicatorMenu = renderMenu;
  window.toggleIndicatorMenu = toggleMenu;
  window._wantIndicators = wantOn;
})();
