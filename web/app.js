/* ============================================================
   Charts — standalone TradingView-style terminal (v1)
   Vanilla JS + Lightweight Charts. Drawings, layouts, watchlist,
   live crypto stream, client-side alerts — all free-tier.
   ============================================================ */
"use strict";

/* ---------------- state ---------------- */
var chart = null;
var candleSeries = null;
var lastCandles = [];
var baseCandles = [];
var currentSymbol = "AAPL";
var currentPeriod = "ALL";
var currentInterval = "1d";
var chartType = "candles";
var currentSource = "";

var drawingMode = "cursor";
var pendingPoint = null;
/* תצוגה מקדימה של ציור — שכבת SVG מעל הגרף (בלי סדרות בספרייה).
   הסיבה: יצירת LineSeries עם נקודה בודדת בלחיצה הראשונה הקפיאה את הטאב
   עד מוות (לולאת main-thread בספריית הגרפים, ללא exception). ה-SVG בטוח לחלוטין. */
var previewSvg = null;
function previewLayer() {
  if (previewSvg) return previewSvg;
  previewSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  previewSvg.setAttribute("id", "draw-preview-layer");
  previewSvg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:6;";
  $("chart").appendChild(previewSvg);
  return previewSvg;
}
function clearPreview() {
  if (previewSvg) previewSvg.innerHTML = "";
}
function drawPreview(p1, p2) {
  // p1 = נקודה ראשונה (עיגול כחול כמו ב-TradingView); p2 = מיקום הסמן; p2=null -> רק עיגול
  if (!candleSeries || !chart) return;
  const svg = previewLayer();
  const x1 = chart.timeScale().timeToCoordinate(p1.time);
  const y1 = candleSeries.priceToCoordinate(p1.price);
  let html = "";
  if (x1 !== null && y1 !== null && isFinite(x1) && isFinite(y1)) {
    html += `<circle cx="${x1.toFixed(1)}" cy="${y1.toFixed(1)}" r="5" fill="#2962ff" stroke="#fff" stroke-width="2"/>`;
  }
  if (p2) {
    const x2 = chart.timeScale().timeToCoordinate(p2.time);
    const y2 = candleSeries.priceToCoordinate(p2.price);
    if ([x1, y1, x2, y2].every(v => v !== null && v !== undefined && isFinite(v))) {
      const color = DRAW_COLORS[drawingMode] || "#2962ff";
      html += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${color}" stroke-width="1.5" stroke-dasharray="7 5"/>`;
    }
  }
  svg.innerHTML = html;
}
function drawPreviewHline(price) {
  if (!candleSeries || !chart) return;
  const svg = previewLayer();
  const y = candleSeries.priceToCoordinate(price);
  const w = $("chart").getBoundingClientRect().width;
  svg.innerHTML = (y !== null && isFinite(y) && w > 0)
    ? `<line x1="0" y1="${y.toFixed(1)}" x2="${w.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${DRAW_COLORS.hline}" stroke-width="1.5" stroke-dasharray="7 5"/>`
    : "";
}
var magnetOn = true;
var drawings = [];          // {id,type,visible,color,points:[{time,price}],text}
var drawSeq = 1;

var liveWS = null;
var livePrice = null;
var prevClose = null;

var watchlist = [];
var wpTab = "all";
var quoteTimer = null;
var prevCloses = {};

var serverAlerts = [];
var alertMeta = null;

var layouts = {};

var aiLibrary = [];   // {id, name, code, active}
var aiOverlays = [];  // live series on chart

var supa = { enabled: false, client: null, token: null, user: null, pushTimer: null };
var lastSyncAt = 0;   // epoch ms of last successful sync (last-write-wins)

window.chart = null; // (set after init)

/* ---------------- helpers ---------------- */
function $(id) { return document.getElementById(id); }

function showToast(msg, ms) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms || 2600);
}
window.showToast = showToast;

function fmtPrice(p) {
  if (p === null || p === undefined || isNaN(p)) return "—";
  p = Number(p);
  if (p >= 1000) return p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (p >= 100) return p.toFixed(2);
  if (p >= 1) return p.toFixed(4);
  return p.toFixed(6);
}

function fmtPct(p) {
  if (p === null || p === undefined || isNaN(p)) return "";
  const s = (p >= 0 ? "+" : "") + p.toFixed(2) + "%";
  return s;
}

function uid(prefix) { return (prefix || "d") + Date.now().toString(36) + Math.floor(Math.random() * 999); }

function saveLocal(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
function loadLocal(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }

/* fetch עם Bearer token של סופאבייס כשמחוברים (ללא טוקן = בקשה רגילה) */
function authFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  if (supa.token) opts.headers["Authorization"] = "Bearer " + supa.token;
  return fetch(url, opts);
}
function authNeededToast() {
  if (supa.enabled && !supa.token) showToast("התחבר כדי להשתמש בהתראות בענן 👤");
}

const DRAW_COLORS = { trend: "#3b82f6", hline: "#f59e0b", fib: "#a855f7", text: "#22c55e" };
const DRAW_NAMES = { trend: "קו מגמה", hline: "קו אופקי", fib: "פיבונאצ'י", text: "טקסט" };

/* ---------------- chart init ---------------- */
function initChart() {
  if (typeof LightweightCharts === "undefined")
    throw new Error("chart-lib-missing");
  chart = LightweightCharts.createChart($("chart"), {
    layout: {
      background: { type: "solid", color: "#0e1220" },
      textColor: "#8a93a8",
      fontFamily: "-apple-system,'Segoe UI',Roboto,Arial,sans-serif",
      fontSize: 11,
    },
    grid: {
      vertLines: { color: "rgba(38,45,66,.55)" },
      horzLines: { color: "rgba(38,45,66,.55)" },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: "#3b82f6", labelBackgroundColor: "#3b82f6" },
      horzLine: { color: "#3b82f6", labelBackgroundColor: "#3b82f6" },
    },
    rightPriceScale: { borderColor: "#262d42" },
    timeScale: { borderColor: "#262d42", timeVisible: true, secondsVisible: false },
  });
  window.chart = chart;

  new ResizeObserver(entries => {
    const r = entries[0].contentRect;
    chart.applyOptions({ width: r.width, height: r.height });
  }).observe($("chart"));

  chart.subscribeClick(onChartClick);
  chart.subscribeCrosshairMove(onCrosshairMove);
  buildMainSeries();
}

function mainSeriesOpts() {
  return {
    priceLineVisible: true,
    lastValueVisible: true,
    priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
  };
}

function buildMainSeries() {
  if (candleSeries) { try { chart.removeSeries(candleSeries); } catch (e) {} candleSeries = null; }
  const o = mainSeriesOpts();
  if (chartType === "line") {
    candleSeries = chart.addLineSeries({ ...o, color: "#3b82f6", lineWidth: 2 });
  } else if (chartType === "area") {
    candleSeries = chart.addAreaSeries({ ...o, lineColor: "#3b82f6", topColor: "rgba(59,130,246,.45)", bottomColor: "rgba(59,130,246,.02)", lineWidth: 2 });
  } else if (chartType === "bars") {
    candleSeries = chart.addBarSeries({ ...o, upColor: "#22c55e", downColor: "#ef4444" });
  } else if (chartType === "baseline") {
    const base = lastCandles.length ? lastCandles[0].close : 0;
    candleSeries = chart.addBaselineSeries({ ...o, baseValue: { type: "price", price: base }, topLineColor: "#22c55e", bottomLineColor: "#ef4444", topFillColor1: "rgba(34,197,94,.3)", bottomFillColor1: "rgba(239,68,68,.3)" });
  } else {
    candleSeries = chart.addCandlestickSeries({ ...o, upColor: "#22c55e", downColor: "#ef4444", wickUpColor: "#22c55e", wickDownColor: "#ef4444", borderVisible: false });
  }
  renderTextMarkers();
}

function heikinAshi(src) {
  const out = [];
  let po = null, pc = null;
  for (const c of src) {
    const hc = (c.open + c.high + c.low + c.close) / 4;
    const ho = po === null ? (c.open + c.close) / 2 : (po + pc) / 2;
    const hh = Math.max(c.high, ho, hc), hl = Math.min(c.low, ho, hc);
    out.push({ time: c.time, open: ho, high: hh, low: hl, close: hc });
    po = ho; pc = hc;
  }
  return out;
}

function seriesData() {
  if (chartType === "heikin") return heikinAshi(baseCandles);
  if (chartType === "line" || chartType === "area" || chartType === "baseline")
    return baseCandles.map(c => ({ time: c.time, value: c.close }));
  return baseCandles;
}

/* ---------------- data loading ---------------- */
/* טווח זמן ואינטרוול נרות — כמו ב-TradingView: שני בוררים נפרדים.
   שילובים לא הגיוניים (נר דקה על 5 שנים) מותאמים אוטומטית. */
const RANGE_ORDER = ["1D", "5D", "1M", "3M", "6M", "YTD", "1Y", "5Y", "ALL"];
const IV_MAX_RANGE = { "1m": "5D", "5m": "3M", "15m": "3M", "30m": "3M", "1h": "1Y" };

function setRange(period) {
  if (RANGE_ORDER.indexOf(period) === -1) return;
  const maxR = IV_MAX_RANGE[currentInterval];
  if (maxR && RANGE_ORDER.indexOf(period) > RANGE_ORDER.indexOf(maxR)) {
    currentPeriod = maxR;
    showToast(`טווח הותאם ל־${maxR} לאינטרוול ${currentInterval}`);
  } else {
    currentPeriod = period;
  }
  document.querySelectorAll("#range-group .tf-btn").forEach(x =>
    x.classList.toggle("active", x.dataset.range === currentPeriod));
  loadChart();
}

function setTfInterval(iv) {
  currentInterval = iv;
  document.querySelectorAll("#tf-group .tf-btn").forEach(x =>
    x.classList.toggle("active", x.dataset.tf === iv));
  // אם הטווח הנוכחי גדול מדי לאינטרוול החדש — להתאים אוטומטית
  const maxR = IV_MAX_RANGE[iv];
  if (maxR && RANGE_ORDER.indexOf(currentPeriod) > RANGE_ORDER.indexOf(maxR)) {
    currentPeriod = maxR;
    document.querySelectorAll("#range-group .tf-btn").forEach(x =>
      x.classList.toggle("active", x.dataset.range === currentPeriod));
    showToast(`טווח הותאם ל־${maxR} לאינטרוול ${iv}`);
  }
  loadChart();
}

async function loadChart() {
  if (!chart) return; // ספריית הגרפים לא נטענה — הבאנר כבר מוצג
  $("chart-error").classList.add("hidden");
  $("tb-symbol").textContent = currentSymbol;
  $("tb-price").textContent = "טוען...";
  $("tb-chg").textContent = "";
  if (window.clearIndicators) clearIndicators();
  clearDrawingSeries();
  clearAIOverlays();
  stopLive();

  try {
    const r = await fetch(`/api/candles?symbol=${encodeURIComponent(currentSymbol)}&period=${currentPeriod}&interval=${currentInterval}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "שגיאה בטעינה");
    // סינון הגנתי: נר עם ערך לא תקין מקריס את ספריית הגרפים ("Value is null")
    baseCandles = (data.candles || []).filter(c => c &&
      Number.isFinite(+c.time) &&
      Number.isFinite(+c.open) && Number.isFinite(+c.high) &&
      Number.isFinite(+c.low) && Number.isFinite(+c.close));
    lastCandles = baseCandles;
    currentSource = data.source || "";
    buildMainSeries();
    candleSeries.setData(seriesData());
    chart.timeScale().fitContent();
    if (window.applyIndicators) applyIndicators();
    renderDrawings();
    renderAIOverlays();
    renderAlertLines();

    const px = data.current_price;
    livePrice = px;
    if (baseCandles.length >= 2) prevClose = baseCandles[baseCandles.length - 2].close;
    updateTopbar(px);
    updateBadge();
    startLiveIfCrypto();
  } catch (e) {
    showToast("שגיאה: " + e.message);
    $("tb-price").textContent = "—";
    $("chart-error").classList.remove("hidden");
  }
}

function updateTopbar(px) {
  $("tb-price").textContent = fmtPrice(px);
  const chgEl = $("tb-chg");
  if (px && prevClose) {
    const p = (px - prevClose) / prevClose * 100;
    chgEl.textContent = fmtPct(p);
    chgEl.className = "tb-chg " + (p >= 0 ? "up" : "down");
  } else chgEl.textContent = "";
}

function updateBadge() {
  const b = $("data-badge");
  const s = (currentSource || "").toLowerCase();
  if (s.includes("coinbase") || s.includes("kraken")) b.textContent = "⚡ קריפטו · חי";
  else if (s.includes("frankfurter")) b.textContent = "🏦 פורקס · ECB יומי";
  else if (s.includes("yahoo") || s.includes("nasdaq")) b.textContent = "📊 Yahoo · דיליי ~15 דק׳";
  else b.textContent = "📊 " + (currentSource || "—");
}

function setTimeframe(tf) { setTfInterval(tf); } // תאימות לאחור

/* ---------------- live crypto stream ---------------- */
function isCrypto(sym) {
  const s = (sym || "").toUpperCase().replace(/[\/-]/g, "");
  return /USDT$|USDC$|USD$/.test(s) && !s.endsWith("=X") && s.length > 3;
}

function startLiveIfCrypto() {
  if (!isCrypto(currentSymbol)) return;
  try {
    // באתר HTTPS חייבים wss — אחרת הדפדפן חוסם (mixed content)
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    liveWS = new WebSocket(`${proto}${location.host}/ws/stream?symbol=${encodeURIComponent(currentSymbol)}`);
  } catch (e) { return; }
  liveWS.onmessage = ev => {
    try {
      const m = JSON.parse(ev.data);
      if (typeof m.price !== "number") return;
      livePrice = m.price;
      $("tb-price").textContent = fmtPrice(m.price);
      if (prevClose) {
        const p = (m.price - prevClose) / prevClose * 100;
        const chgEl = $("tb-chg");
        chgEl.textContent = fmtPct(p);
        chgEl.className = "tb-chg " + (p >= 0 ? "up" : "down");
      }
      // עדכון הנר האחרון
      if ((chartType === "candles" || chartType === "heikin" || chartType === "bars") && lastCandles.length) {
        const last = lastCandles[lastCandles.length - 1];
        const upd = {
          time: last.time, open: last.open,
          high: Math.max(last.high, m.price), low: Math.min(last.low, m.price),
          close: m.price,
        };
        // לא שולחים לספרייה עדכון עם ערך לא תקין — זה מה שקרס ("Value is null")
        if (![upd.time, upd.open, upd.high, upd.low, upd.close].every(v => typeof v === "number" && isFinite(v))) return;
        last.close = m.price;
        last.high = upd.high;
        last.low = upd.low;
        candleSeries.update(chartType === "heikin" ? heikinAshi(baseCandles).slice(-1)[0] : upd);
      } else if (lastCandles.length) {
        candleSeries.update({ time: lastCandles[lastCandles.length - 1].time, value: m.price });
      }
    } catch (e) {}
  };
  liveWS.onerror = () => stopLive();
  liveWS.onclose = () => { liveWS = null; };
}

function stopLive() {
  if (liveWS) { try { liveWS.close(); } catch (e) {} liveWS = null; }
}

/* ---------------- crosshair / OHLC legend ---------------- */
function onCrosshairMove(param) {
  lastCrossParam = param || null;
  // תצוגה מקדימה של ציור: קו מגמה/פיבונאצ'י = קו מקווקו חי מהנקודה הראשונה לסמן;
  // קו אופקי = קו מקווקו אופקי עוקב. (שכבת SVG — לא סדרת גרף.)
  if (pendingPoint && (drawingMode === "trend" || drawingMode === "fib")) {
    const pt = priceAtClick(param);
    if (pt && isFinite(pt.price) && typeof pt.time === "number") drawPreview(pendingPoint, pt);
    else clearPreview();
  } else if (drawingMode === "hline") {
    const pt = priceAtClick(param);
    if (pt && isFinite(pt.price)) drawPreviewHline(pt.price);
    else clearPreview();
  }
  const el = $("ohlc-legend");
  let c = null;
  if (param && param.time) c = lastCandles.find(x => x.time === param.time);
  if (!c && lastCandles.length) c = lastCandles[lastCandles.length - 1];
  if (!c) { el.innerHTML = ""; return; }
  const up = c.close >= c.open;
  const col = up ? "var(--up)" : "var(--down)";
  el.innerHTML =
    `<span>O <b>${fmtPrice(c.open)}</b></span><span>H <b>${fmtPrice(c.high)}</b></span>` +
    `<span>L <b>${fmtPrice(c.low)}</b></span>` +
    `<span>C <b style="color:${col}">${fmtPrice(c.close)}</b></span>` +
    (c.volume ? `<span>Vol <b>${Number(c.volume).toLocaleString("en-US", { maximumFractionDigits: 0 })}</b></span>` : "");
}

/* ---------------- גרירת קווי התראה על הגרף ---------------- */
var alertLines = [];   // {alertId, line, side}
var alertDragActive = false;

function alertDragPrice(alert) {
  // מחזיר את המחיר הקבוע של ההתראה (אם יש) — רק אותו אפשר לגרור.
  // הערה: השרת (alerts_engine) מתייחס ל-condition ללא type כאל "rule",
  // ובונה ההתראות לא שומר type — לכן מקבלים גם type חסר.
  const c = alert && alert.condition;
  if (!c || c.type === "change_pct") return null;
  if (c.left && c.left.kind === "value" && isFinite(+c.left.value))
    return { price: +c.left.value, side: "left" };
  if (c.right && c.right.kind === "value" && isFinite(+c.right.value))
    return { price: +c.right.value, side: "right" };
  return null;
}

function renderAlertLines() {
  try {
    alertLines.forEach(o => { try { candleSeries.removePriceLine(o.line); } catch (e) {} });
  } catch (e) {}
  alertLines = [];
  if (!candleSeries || typeof candleSeries.createPriceLine !== "function") return;
  (serverAlerts || []).forEach(a => {
    if (!a.active || a.symbol !== currentSymbol) return;
    const dp = alertDragPrice(a);
    if (!dp) return;
    const line = candleSeries.createPriceLine({
      price: dp.price,
      color: "#f59e0b",
      lineWidth: 2,
      lineStyle: 2, // dashed
      axisLabelVisible: true,
      title: "⏰ " + (a.name || "").slice(0, 24),
    });
    alertLines.push({ alertId: a.id, line, side: dp.side });
  });
}

function alertLineAt(yPx) {
  // מוצא קו התראה קרוב לנקודת המגע (עד ~12px)
  for (const o of alertLines) {
    try {
      const y = candleSeries.priceToCoordinate(o.line.options().price);
      if (y != null && Math.abs(y - yPx) <= 12) return o;
    } catch (e) {}
  }
  return null;
}

function wireAlertDrag() {
  const el = $("chart");
  if (!el || el._alertDragWired) return;
  el._alertDragWired = true;
  let cand = null, dragging = false, sx = 0, sy = 0, lastPrice = null;

  const pos = e => {
    const r = el.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  el.addEventListener("pointerdown", e => {
    if (drawingMode !== "cursor" || e.button === 2) return;
    const p = pos(e);
    const hit = alertLineAt(p.y);
    if (!hit) return;
    cand = hit; dragging = false; sx = p.x; sy = p.y; lastPrice = null;
    // נטרול גלילה/זום מיד — כדי שגרירה לא תזיז את הגרף (במיוחד במובייל)
    chart.applyOptions({ handleScroll: false, handleScale: false });
  });

  el.addEventListener("pointermove", e => {
    if (!cand) return;
    const p = pos(e);
    if (!dragging && Math.hypot(p.x - sx, p.y - sy) > 5) {
      dragging = true;
      alertDragActive = true;   // מבטל long-press של תפריט ההקשר
    }
    if (dragging) {
      try {
        const pr = candleSeries.coordinateToPrice(p.y);
        if (pr != null && isFinite(pr)) {
          lastPrice = pr;
          cand.line.applyOptions({ price: pr });
        }
      } catch (err) {}
    }
  });

  const end = async e => {
    if (!cand) return;
    const wasDragging = dragging, o = cand, px = lastPrice;
    cand = null; dragging = false;
    alertDragActive = false;
    chart.applyOptions({ handleScroll: true, handleScale: true });
    if (wasDragging) {
      if (px != null) {
        const alert = (serverAlerts || []).find(a => a.id === o.alertId);
        if (alert) {
          const cond = JSON.parse(JSON.stringify(alert.condition));
          const tgt = o.side === "left" ? cond.left : cond.right;
          if (tgt) tgt.value = Math.round(px * 100) / 100;
          try {
            const r = await authFetch(`/api/alerts/${o.alertId}`, {
              method: "PUT", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ condition: cond }),
            });
            if (r.ok) showToast("⏰ ההתראה עודכנה ל־" + fmtPrice(px));
            else showToast("שגיאה בעדכון ההתראה");
          } catch (err) { showToast("שגיאה בעדכון ההתראה"); }
          loadAlerts();
        }
      }
    }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", () => {
    if (cand) { cand = null; dragging = false; alertDragActive = false;
      chart.applyOptions({ handleScroll: true, handleScale: true }); }
  });
}

/* ---------------- תפריט הקשר של הסמן (הפלוס) ---------------- */
let lastCrossParam = null;
let ctxOpenedAt = 0;

function snapToCandle(price, time) {
  if (magnetOn && time) {
    const c = lastCandles.find(x => x.time === time);
    if (c) {
      const cands = [c.open, c.high, c.low, c.close];
      price = cands.reduce((a, b) => Math.abs(b - price) < Math.abs(a - price) ? b : a);
    }
  }
  return price;
}

function priceAtClientXY(clientX, clientY, raw) {
  if (!candleSeries) return null;
  const rect = $("chart").getBoundingClientRect();
  let price;
  try { price = candleSeries.coordinateToPrice(clientY - rect.top); }
  catch (e) { return null; }
  if (price === null || price === undefined || isNaN(price)) return null;
  const t = lastCrossParam && lastCrossParam.time;
  // תפריט ההקשר (התראה/קו אופקי) משתמש במחיר המדויק של הסמן — בלי הצמדת מגנט,
  // כמו ב-TradingView. המגנט נשאר רק לציורים.
  return { time: t, price: raw ? price : snapToCandle(price, t) };
}

function closeCtxMenu() {
  const m = $("ctx-menu");
  if (m) m.classList.remove("open");
  // ניתוק מעקב המחיר החי של התפריט
  if (ctxMoveOff) { try { ctxMoveOff(); } catch (e) {} ctxMoveOff = null; }
  ctxPt = null;
}
let ctxPt = null;   // הנקודה העדכנית ביותר של תפריט ההקשר (מתעדכנת בזמן תנועה)
let ctxMoveOff = null;

function trackCtxPrice() {
  // בזמן שהתפריט פתוח, המחיר המוצג עוקב אחרי הסמן/האצבע בזמן אמת,
  // כדי שלחיצה על "התראה במחיר זה" תיצור במחיר העדכני — לא במחיר שנתפס בפתיחה.
  const el = $("chart");
  const upd = e => {
    if (!ctxPt) return;
    let cx, cy;
    if (e.clientX !== undefined) { cx = e.clientX; cy = e.clientY; }
    else if (e.touches && e.touches.length) { cx = e.touches[0].clientX; cy = e.touches[0].clientY; }
    else return;
    const pt = priceAtClientXY(cx, cy, true);
    if (pt && isFinite(pt.price)) {
      ctxPt = pt;
      const pe = document.querySelector("#ctx-menu .ctx-price");
      if (pe) pe.textContent = fmtPrice(pt.price);
    }
  };
  el.addEventListener("pointermove", upd);
  el.addEventListener("touchmove", upd, { passive: true });
  ctxMoveOff = () => {
    el.removeEventListener("pointermove", upd);
    el.removeEventListener("touchmove", upd);
  };
}

function showCtxMenu(clientX, clientY, pt) {
  const m = $("ctx-menu");
  if (!m) return;
  closeCtxMenu(); // ניקוי מעקב קודם, אם היה
  ctxPt = pt;
  m.innerHTML =
    `<div class="ctx-price">${fmtPrice(pt.price)}</div>` +
    `<button id="ctx-alert">⏰ התראה במחיר זה</button>` +
    `<button id="ctx-hline">📏 קו אופקי כאן</button>`;
  m.classList.add("open");
  // מיקום ליד הסמן — תמיד בתוך המסך
  const mw = 230, mh = 160;
  const vw = window.innerWidth, vh = window.innerHeight;
  let left = Math.max(8, Math.min(clientX - 24, vw - mw - 8));
  let top = clientY + 10;
  if (top + mh > vh - 8) top = Math.max(8, clientY - mh - 10);
  m.style.left = left + "px";
  m.style.right = "auto";
  m.style.top = top + "px";
  ctxOpenedAt = Date.now();
  trackCtxPrice();
  $("ctx-alert").addEventListener("click", () => {
    const live = ctxPt && isFinite(ctxPt.price) ? ctxPt.price : pt.price;
    closeCtxMenu();
    openAlertAtPrice(live);
  });
  $("ctx-hline").addEventListener("click", () => {
    const live = ctxPt || pt;
    closeCtxMenu();
    if (live.time) {
      addDrawing({ type: "hline", points: [{ time: live.time, price: live.price }] });
      setTool("cursor");
      showToast("קו אופקי נוסף 📏");
    } else showToast("לא נמצא נר בנקודה");
  });
}

async function openAlertAtPrice(price) {
  await openAlertBuilder();
  try {
    $("ab-ctype").value = "rule";
    $("ab-left-kind").value = "price";
    $("ab-right-kind").value = "value";
    syncBuilderKinds();
    const rv = $("ab-right-value");
    if (rv) rv.value = String(Number(price.toPrecision(6)));
    const opSel = $("ab-op");
    if (opSel && opSel.options.length) {
      const opts = Array.from(opSel.options);
      const cross = opts.find(o => /cross/i.test(o.value));
      opSel.value = (cross || opts[0]).value;
    }
    $("ab-name").value = `${currentSymbol} @ ${fmtPrice(price)}`;
  } catch (e) { /* טופס נשאר בריק — המשתמש ימלא */ }
}

function wireCtxMenu() {
  const el = $("chart");
  if (!el || el._ctxWired) return;
  el._ctxWired = true;
  // דסקטופ: לחיצה ימנית
  el.addEventListener("contextmenu", ev => {
    ev.preventDefault();
    const pt = priceAtClientXY(ev.clientX, ev.clientY, true);
    if (pt) showCtxMenu(ev.clientX, ev.clientY, pt);
  });
  // מובייל: לחיצה ארוכה
  let lpTimer = null, lpX = 0, lpY = 0;
  el.addEventListener("touchstart", ev => {
    if (ev.touches.length !== 1) return;
    const t = ev.touches[0]; lpX = t.clientX; lpY = t.clientY;
    clearTimeout(lpTimer);
    lpTimer = setTimeout(() => {
      if (alertDragActive) return;   // גרירת קו התראה — לא לפתוח תפריט
      const pt = priceAtClientXY(lpX, lpY, true);
      if (pt) showCtxMenu(lpX, lpY, pt);
    }, 550);
  }, { passive: true });
  el.addEventListener("touchmove", ev => {
    if (!ev.touches.length) return;
    const t = ev.touches[0];
    if (Math.hypot(t.clientX - lpX, t.clientY - lpY) > 12) clearTimeout(lpTimer);
  }, { passive: true });
  el.addEventListener("touchend", () => clearTimeout(lpTimer));
  el.addEventListener("touchcancel", () => clearTimeout(lpTimer));
}

/* ---------------- drawings ---------------- */
function setTool(tool) {
  drawingMode = tool;
  pendingPoint = null;
  clearPreview();
  document.querySelectorAll(".tool-btn[data-tool]").forEach(b =>
    b.classList.toggle("active", b.dataset.tool === tool));
  const hint = $("draw-hint");
  const hints = {
    trend: "קו מגמה: לחץ על נקודת התחלה ואז נקודת סיום",
    hline: "קו אופקי: לחץ על הגרף למיקום הקו",
    fib: "פיבונאצ'י: לחץ על נקודת התחלה ואז נקודת סיום",
    text: "טקסט: לחץ על הגרף למיקום",
  };
  if (hints[tool]) { hint.textContent = hints[tool] + " · Esc לביטול"; hint.classList.add("show"); }
  else hint.classList.remove("show");
  chart.applyOptions({ handleScroll: tool === "cursor", handleScale: tool === "cursor" });
}


function priceAtClick(param) {
  if (!param || !param.point || !candleSeries) return null;
  let price;
  try { price = candleSeries.coordinateToPrice(param.point.y); }
  catch (e) { return null; }
  if (price === null || price === undefined || isNaN(price)) return null;
  if (magnetOn && param.time) {
    const c = lastCandles.find(x => x.time === param.time);
    if (c) {
      const cands = [c.open, c.high, c.low, c.close];
      price = cands.reduce((a, b) => Math.abs(b - price) < Math.abs(a - price) ? b : a);
    }
  }
  return { time: param.time, price };
}

function onChartClick(param) {
  if (drawingMode === "cursor" || !param || !param.time) return;
  const pt = priceAtClick(param);
  if (!pt) return;

  if (drawingMode === "hline") {
    addDrawing({ type: "hline", points: [pt] });
    setTool("cursor");
  } else if (drawingMode === "text") {
    const txt = prompt("טקסט לסימון:");
    if (txt) addDrawing({ type: "text", points: [pt], text: txt });
    setTool("cursor");
  } else if (drawingMode === "trend" || drawingMode === "fib") {
    if (!pendingPoint) {
      pendingPoint = pt;
      $("draw-hint").textContent = "נקודה ראשונה נבחרה — גרור/הזז לנקודת הסיום ולחץ · Esc לביטול";
      // עיגול כחול בנקודה הראשונה (כמו ב-TradingView); הקו המקווקו יעקוב אחרי הסמן
      drawPreview(pt, null);
    } else {
      addDrawing({ type: drawingMode, points: [pendingPoint, pt] });
      pendingPoint = null;
      clearPreview();
      setTool("cursor");
    }
  }
}

function addDrawing(d) {
  d.id = uid("dw");
  d.visible = true;
  d.color = d.color || DRAW_COLORS[d.type] || "#3b82f6";
  drawings.push(d);
  renderDrawings();
  renderObjList();
  persistDrawings();
}

function clearDrawingSeries() {
  for (const d of drawings) {
    if (d._series) { d._series.forEach(s => { try { chart.removeSeries(s); } catch (e) {} }); d._series = null; }
    if (d._plines && candleSeries) { d._plines.forEach(pl => { try { candleSeries.removePriceLine(pl); } catch (e) {} }); d._plines = null; }
  }
  renderTextMarkers();
}

function barStep() {
  if (lastCandles.length >= 2)
    return lastCandles[lastCandles.length - 1].time - lastCandles[lastCandles.length - 2].time;
  return 86400;
}

function renderDrawings() {
  clearDrawingSeries();
  for (const d of drawings) {
    if (!d.visible || !d.points || !d.points.length) continue;
    d._series = []; d._plines = [];
    try {
      if (d.type === "trend" && d.points.length === 2) {
        const [a, b] = d.points;
        const dt = (b.time - a.time) || barStep();
        const slope = (b.price - a.price) / dt;
        const ext = b.time + barStep() * 60;
        const s = chart.addLineSeries({
          color: d.color, lineWidth: 1, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false,
        });
        s.setData([
          { time: a.time, value: a.price },
          { time: b.time, value: b.price },
          { time: ext, value: b.price + slope * (ext - b.time) },
        ]);
        d._series.push(s);
      } else if (d.type === "hline" && d.points.length === 1) {
        const pl = candleSeries.createPriceLine({
          price: d.points[0].price, color: d.color, lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true, title: fmtPrice(d.points[0].price),
        });
        d._plines.push(pl);
      } else if (d.type === "fib" && d.points.length === 2) {
        const [a, b] = d.points;
        const hi = Math.max(a.price, b.price), lo = Math.min(a.price, b.price);
        const diff = hi - lo || 1;
        [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1].forEach(lv => {
          const pl = candleSeries.createPriceLine({
            price: hi - diff * lv, color: d.color, lineWidth: 1,
            lineStyle: lv === 0 || lv === 1 ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.Dotted,
            axisLabelVisible: true, title: (lv * 100).toFixed(1) + "%",
          });
          d._plines.push(pl);
        });
      }
    } catch (e) {}
  }
  renderTextMarkers();
}

function renderTextMarkers() {
  if (!candleSeries) return;
  try {
    const marks = drawings
      .filter(d => d.visible && d.type === "text" && d.points.length)
      .map(d => ({
        time: d.points[0].time, position: "aboveBar",
        color: d.color, shape: "circle", text: d.text || "",
      }));
    candleSeries.setMarkers(marks);
  } catch (e) {}
}

function deleteDrawing(id) {
  const i = drawings.findIndex(d => d.id === id);
  if (i >= 0) {
    const [d] = drawings.splice(i, 1);
    if (d._series) d._series.forEach(s => { try { chart.removeSeries(s); } catch (e) {} });
    if (d._plines && candleSeries) d._plines.forEach(pl => { try { candleSeries.removePriceLine(pl); } catch (e) {} });
    renderTextMarkers();
    renderObjList();
    persistDrawings();
  }
}

function toggleDrawing(id) {
  const d = drawings.find(x => x.id === id);
  if (d) { d.visible = !d.visible; renderDrawings(); renderObjList(); persistDrawings(); }
}

function persistDrawings() {
  saveLocal("charts_drawings_" + currentSymbol,
    drawings.map(d => ({ type: d.type, visible: d.visible, color: d.color, points: d.points, text: d.text })));
  schedulePush();
}

function restoreDrawings() {
  const raw = loadLocal("charts_drawings_" + currentSymbol, []);
  drawings = raw.map(d => ({ ...d, id: uid("dw") }));
  renderDrawings();
  renderObjList();
}

/* ---------------- object tree ---------------- */
function renderObjList() {
  const el = $("obj-list");
  let html = "";
  if (!drawings.length) {
    html = `<div class="obj-empty">אין ציורים עדיין — בחר כלי מסרגל הציור משמאל</div>`;
  } else {
    html = drawings.map(d => `
    <div class="obj-row">
      <span class="sw" style="background:${d.color}"></span>
      <span class="nm">${DRAW_NAMES[d.type] || d.type}${d.text ? " · " + escapeHtml(d.text) : ""}</span>
      <button class="mini" onclick="toggleDrawing('${d.id}')">${d.visible ? "👁" : "🚫"}</button>
      <button class="mini del" onclick="deleteDrawing('${d.id}')">✕</button>
    </div>`).join("");
  }
  el.innerHTML = html + renderAISection();
}
window.toggleDrawing = toggleDrawing;
window.deleteDrawing = deleteDrawing;

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------------- symbol search ---------------- */
let searchTimer = null, searchSel = 0;

function openSearch() {
  $("search-modal").classList.remove("hidden");
  $("search-input").value = "";
  $("search-results").innerHTML = "";
  searchSel = 0;
  setTimeout(() => $("search-input").focus(), 30);
}
function closeSearch() { $("search-modal").classList.add("hidden"); }

async function doSearch(q) {
  const box = $("search-results");
  if (!q.trim()) { box.innerHTML = ""; return; }
  try {
    const r = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await r.json();
    const res = data.results || [];
    searchSel = 0;
    box.innerHTML = res.length ? res.map((x, i) => `
      <div class="sr-row${i === 0 ? " sel" : ""}" data-sym="${escapeHtml(x.symbol)}">
        <span class="sr-sym">${escapeHtml(x.symbol)}</span>
        <span class="sr-name">${escapeHtml(x.name)}</span>
        <span class="sr-mkt">${x.market === "crypto" ? "קריפטו" : x.market === "fx" ? "פורקס" : "מניה"}</span>
      </div>`).join("")
      : `<div class="obj-empty">לא נמצאו תוצאות</div>`;
    box.querySelectorAll(".sr-row").forEach(row =>
      row.addEventListener("click", () => pickSymbol(row.dataset.sym)));
  } catch (e) {}
}

function pickSymbol(sym) {
  closeSearch();
  currentSymbol = sym.toUpperCase();
  restoreDrawings();
  loadChart();
  updateL2Btn();
  if (l2Open) loadL2();
}

/* ---------------- watchlist ---------------- */
const DEFAULT_WL = ["AAPL", "NVDA", "TSLA", "MSFT", "BTC-USD", "ETH-USD", "SOL-USD", "EURUSD=X", "GBPUSD=X", "USDJPY=X"];

function wlMarket(sym) {
  const s = sym.toUpperCase();
  if (s.endsWith("=X")) return "fx";
  const flat = s.replace(/[\/-]/g, "");
  if (/USDT$|USDC$|USD$/.test(flat) && flat.length > 3) return "crypto";
  return "stock";
}

async function refreshWatchlist() {
  const el = $("wp-list");
  const syms = watchlist.filter(s => wpTab === "all" || wlMarket(s) === wpTab);
  if (!syms.length) { el.innerHTML = `<div class="obj-empty">הרשימה ריקה — הוסף סימול למעלה</div>`; return; }

  // מחיר קודם לחישוב שינוי (פעם אחת לסימול)
  const needPrev = syms.filter(s => !(s in prevCloses));
  await Promise.all(needPrev.map(async s => {
    try {
      const r = await fetch(`/api/candles?symbol=${encodeURIComponent(s)}&period=5D&interval=1d`);
      const d = await r.json();
      if (r.ok && d.candles && d.candles.length >= 2)
        prevCloses[s] = d.candles[d.candles.length - 2].close;
    } catch (e) {}
  }));

  let quotes = {};
  try {
    const r = await fetch("/api/quotes?symbols=" + encodeURIComponent(syms.join(",")));
    const d = await r.json();
    (d.quotes || []).forEach(q => { quotes[q.symbol] = q.price; });
  } catch (e) {}

  el.innerHTML = syms.map(s => {
    const px = quotes[s];
    const pc = prevCloses[s];
    const chg = (px && pc) ? (px - pc) / pc * 100 : null;
    return `
    <div class="wp-row${s === currentSymbol ? " active" : ""}" data-sym="${escapeHtml(s)}">
      <div class="wp-main">
        <div class="wp-sym">${escapeHtml(s)}</div>
        <div class="wp-name">${wlMarket(s) === "crypto" ? "קריפטו" : wlMarket(s) === "fx" ? "פורקס" : "מניה"}</div>
      </div>
      <div class="wp-px">
        <div>${px ? fmtPrice(px) : "—"}</div>
        <div class="wp-chg ${chg === null ? "" : chg >= 0 ? "up" : "down"}">${chg === null ? "" : fmtPct(chg)}</div>
      </div>
      <button class="wp-x" data-del="${escapeHtml(s)}">✕</button>
    </div>`;
  }).join("");

  el.querySelectorAll(".wp-row").forEach(row =>
    row.addEventListener("click", ev => {
      if (ev.target.dataset.del) return;
      pickSymbol(row.dataset.sym);
    }));
  el.querySelectorAll("[data-del]").forEach(btn =>
    btn.addEventListener("click", ev => {
      ev.stopPropagation();
      watchlist = watchlist.filter(s => s !== btn.dataset.del);
      saveLocal("charts_watchlist", watchlist);
      refreshWatchlist();
      schedulePush();
    }));
}

function addToWatchlist(sym) {
  sym = (sym || "").trim().toUpperCase().replace(/\s+/g, "");
  if (!sym) return;
  // נרמול בסיסי
  if (/^[A-Z]{6}$/.test(sym)) sym = sym + "=X";
  else if (/^[A-Z]{2,10}USDT?$/.test(sym) && !sym.endsWith("=X")) {
    const base = sym.replace(/USDT?$/, "");
    if (base) sym = base + "-USD";
  }
  if (!watchlist.includes(sym)) {
    watchlist.push(sym);
    saveLocal("charts_watchlist", watchlist);
    refreshWatchlist();
    schedulePush();
    showToast(sym + " נוסף לרשימת המעקב");
  }
}

/* ---------------- alerts: rule-based, server-side ---------------- */
async function getAlertMeta() {
  if (alertMeta) return alertMeta;
  const r = await authFetch("/api/alerts/meta");
  alertMeta = await r.json();
  return alertMeta;
}

function describeCondClient(cond) {
  const meta = alertMeta || { indicators: {}, operators: {} };
  const opName = o => {
    if (!o) return "";
    if (o.kind === "price") return "מחיר";
    if (o.kind === "value") return String(o.value);
    if (o.kind === "indicator") {
      const lb = (meta.indicators[o.name] || {}).label || o.name;
      const ps = Object.entries(o.params || {}).map(([k, v]) => v).join(",");
      return ps ? `${lb}(${ps})` : lb;
    }
    return "";
  };
  if (cond.type === "change_pct")
    return `שינוי ${cond.period} נרות ${cond.operator === "greater_than" ? "מעל" : "מתחת"} ${cond.value}%`;
  return `${opName(cond.left)} ${meta.operators[cond.operator] || cond.operator} ${opName(cond.right)}`;
}

async function loadAlerts() {
  try {
    const r = await authFetch("/api/alerts");
    if (r.status === 401) { authNeededToast(); serverAlerts = []; }
    else serverAlerts = (await r.json()).alerts || [];
  } catch (e) { serverAlerts = []; }
  renderServerAlerts();
}

async function renderServerAlerts() {
  await getAlertMeta().catch(() => {});
  const el = $("alert-list");
  const active = serverAlerts.filter(a => a.active);
  $("alert-count").textContent = active.length || "";
  let html = "";
  if (!serverAlerts.length) {
    html = `<div class="obj-empty">אין התראות — לחץ ⏰ התראה בסרגל העליון לבניית חוק</div>`;
  } else {
    const singles = serverAlerts.filter(a => !a.group_id);
    const groups = {};
    serverAlerts.filter(a => a.group_id).forEach(a => {
      (groups[a.group_id] = groups[a.group_id] || []).push(a);
    });
    html = Object.entries(groups).map(([gid, arr]) => {
      const act = arr.filter(a => a.active).length;
      const gname = arr[0].group_name || "רשימת מעקב";
      return `
      <div class="obj-row">
        <span class="sw" style="background:${act ? "var(--warn)" : "var(--muted)"}"></span>
        <span class="nm">📋 <b>${escapeHtml(gname)}</b> · ${arr.length} סמלים (${act} פעילות)
          <br><span style="color:var(--muted);font-size:11.5px">${escapeHtml(describeCondClient(arr[0].condition))}</span></span>
        <button class="mini" onclick="toggleAlertGroup('${gid}')" title="הפעל/השהה את כל הקבוצה">${act ? "⏸" : "▶"}</button>
        <button class="mini del" onclick="deleteAlertGroup('${gid}')" title="מחק את כל הקבוצה">✕</button>
      </div>`;
    }).join("") + singles.map(a => `
      <div class="obj-row">
        <span class="sw" style="background:${a.active ? "var(--warn)" : "var(--muted)"}"></span>
        <span class="nm"><b>${escapeHtml(a.symbol)}</b> · ${escapeHtml(a.name)}
          <br><span style="color:var(--muted);font-size:11.5px">${escapeHtml(describeCondClient(a.condition))} · ${(alertMeta.frequencies || {})[a.frequency] || ""}${a.trigger_count ? " · הופעלה " + a.trigger_count + "×" : ""}</span></span>
        <button class="mini" onclick="toggleServerAlert('${a.id}')">${a.active ? "⏸" : "▶"}</button>
        <button class="mini del" onclick="deleteServerAlert('${a.id}')">✕</button>
      </div>`).join("");
  }
  try {
    const r = await authFetch("/api/alerts/triggers");
    const tr = r.ok ? (await r.json()).triggers || [] : [];
    if (tr.length) {
      html += `<div class="menu-sep"></div>` + tr.slice(0, 8).map(t => `
        <div class="obj-row">
          <span class="sw" style="background:var(--up)"></span>
          <span class="nm">🔔 <b>${escapeHtml(t.symbol)}</b> · ${escapeHtml(t.name)}
            <br><span style="color:var(--muted);font-size:11.5px">${fmtPrice(t.price)} · ${new Date(t.time).toLocaleString("he-IL")}</span></span>
        </div>`).join("");
    }
  } catch (e) {}
  el.innerHTML = html;
  renderAlertLines();
}

window.toggleServerAlert = async id => {
  const a = serverAlerts.find(x => x.id === id);
  if (!a) return;
  await authFetch(`/api/alerts/${id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: !a.active }),
  });
  loadAlerts();
};

window.toggleAlertGroup = async gid => {
  const arr = serverAlerts.filter(x => x.group_id === gid);
  if (!arr.length) return;
  const toActive = arr.some(a => !a.active);
  await authFetch("/api/alerts/bulk", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ group_id: gid, alert: { active: toActive } }),
  });
  loadAlerts();
};

window.deleteAlertGroup = async gid => {
  const arr = serverAlerts.filter(x => x.group_id === gid);
  const r = await authFetch(`/api/alerts/bulk?group_id=${encodeURIComponent(gid)}`, { method: "DELETE" });
  showToast(r.ok ? `🗑 נמחקו ${arr.length} התראות` : "שגיאה במחיקה");
  loadAlerts();
};

window.deleteServerAlert = async id => {
  const a = serverAlerts.find(x => x.id === id);
  const r = await authFetch(`/api/alerts/${id}`, { method: "DELETE" });
  showToast(r.ok ? `🗑 ההתראה ${a ? a.symbol : ""} נמחקה` : "שגיאה במחיקה");
  loadAlerts();
};

/* ---- alert builder modal ---- */
function fillBuilderSelects() {
  const meta = alertMeta;
  const indOpts = Object.entries(meta.indicators || {})
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`).join("");
  $("ab-left-ind").innerHTML = indOpts;
  $("ab-right-ind").innerHTML = indOpts;
  $("ab-op").innerHTML = Object.entries(meta.operators || {})
    .map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  $("ab-freq").innerHTML = Object.entries(meta.frequencies || {})
    .map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  syncBuilderKinds();
}

function syncBuilderKinds() {
  const rule = $("ab-ctype").value === "rule";
  $("ab-rule-sec").classList.toggle("hidden", !rule);
  $("ab-pct-sec").classList.toggle("hidden", rule);
  const lk = $("ab-left-kind").value;
  $("ab-left-ind").style.display = lk === "indicator" ? "" : "none";
  $("ab-left-n").style.display = lk === "indicator" ? "" : "none";
  const rk = $("ab-right-kind").value;
  $("ab-right-value").style.display = rk === "value" ? "" : "none";
  $("ab-right-ind").style.display = rk === "indicator" ? "" : "none";
  $("ab-right-n").style.display = rk === "indicator" ? "" : "none";
}

async function openAlertBuilder() {
  try { await getAlertMeta(); } catch (e) { showToast("שגיאה בטעינת מנוע ההתראות"); return; }
  fillBuilderSelects();
  $("ab-symbol").textContent = currentSymbol;
  $("ab-name").value = "";
  $("ab-error").textContent = "";
  if ($("ab-watchlist")) $("ab-watchlist").checked = false;
  if ($("ab-interval")) $("ab-interval").value = "5";
  $("alert-modal").classList.remove("hidden");
}
function closeAlertBuilder() { $("alert-modal").classList.add("hidden"); }

function indOperand(selId, nId) {
  const p = {};
  const n = parseInt($(nId).value, 10);
  if (n >= 2 && n <= 300) p.n = n;
  return { kind: "indicator", name: $(selId).value, params: p };
}

async function submitAlert() {
  const errEl = $("ab-error");
  errEl.textContent = "";
  let condition;
  if ($("ab-ctype").value === "change_pct") {
    condition = {
      type: "change_pct",
      operator: $("ab-pct-op").value,
      period: Math.max(1, parseInt($("ab-period").value, 10) || 1),
      value: parseFloat($("ab-pct-value").value),
    };
  } else {
    const lk = $("ab-left-kind").value;
    const left = lk === "price" ? { kind: "price" } : indOperand("ab-left-ind", "ab-left-n");
    const rk = $("ab-right-kind").value;
    const right = rk === "value"
      ? { kind: "value", value: parseFloat($("ab-right-value").value) }
      : rk === "price" ? { kind: "price" } : indOperand("ab-right-ind", "ab-right-n");
    condition = { left, operator: $("ab-op").value, right };
  }
  let expires_at = null;
  const expH = parseInt($("ab-exp").value, 10);
  if (expH) expires_at = new Date(Date.now() + expH * 3600e3).toISOString();
  const payload = {
    symbol: currentSymbol,
    name: $("ab-name").value.trim(),
    condition,
    frequency: $("ab-freq").value,
    interval_min: parseInt($("ab-interval").value, 10) || 5,
    expires_at,
  };
  const useWatchlist = $("ab-watchlist") && $("ab-watchlist").checked && watchlist.length > 0;
  try {
    let r, d;
    if (useWatchlist) {
      r = await authFetch("/api/alerts/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols: watchlist,
          alert: { ...payload, group_name: "רשימת מעקב · " + payload.name },
        }),
      });
      d = await r.json();
      if (r.status === 401) { authNeededToast(); return; }
      if (!r.ok) throw new Error(d.error || "שגיאה");
      closeAlertBuilder();
      loadAlerts();
      showToast(`📋 נוצרו ${d.created} התראות לרשימה` + (d.skipped && d.skipped.length ? ` (${d.skipped.length} כפילויות דולגו)` : ""));
    } else {
      r = await authFetch("/api/alerts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      d = await r.json();
      if (r.status === 401) { authNeededToast(); return; }
      if (!r.ok) throw new Error(d.error || "שגיאה");
      closeAlertBuilder();
      loadAlerts();
      showToast("⏰ ההתראה נוצרה" + (d.name ? ": " + d.name : ""));
    }
  } catch (e) {
    errEl.textContent = e.message;
  }
}

/* ---------------- layouts ---------------- */
function collectLayout() {
  let inds = {};
  try { inds = JSON.parse(localStorage.getItem("tv_builtin_indicators") || "{}"); } catch (e) {}
  return {
    symbol: currentSymbol, period: currentPeriod, interval: currentInterval,
    chartType, indicators: Object.keys(inds),
    drawings: drawings.map(d => ({ type: d.type, visible: d.visible, color: d.color, points: d.points, text: d.text })),
    watchlist: [...watchlist],
  };
}

function applyLayout(name) {
  const L = layouts[name];
  if (!L) return;
  currentSymbol = L.symbol || "AAPL";
  currentPeriod = L.period || "ALL";
  currentInterval = L.interval || "1d";
  chartType = L.chartType || "candles";
  document.querySelectorAll("#tf-group .tf-btn").forEach(x =>
    x.classList.toggle("active", x.dataset.tf === (currentInterval || "1d")));
  document.querySelectorAll("#range-group .tf-btn").forEach(x =>
    x.classList.toggle("active", x.dataset.range === (currentPeriod || "ALL")));
  if (L.watchlist) { watchlist = L.watchlist; saveLocal("charts_watchlist", watchlist); }
  try {
    const o = {};
    (L.indicators || []).forEach(id => { o[id] = 1; });
    localStorage.setItem("tv_builtin_indicators", JSON.stringify(o));
  } catch (e) {}
  // ציורים ישוחזרו אחרי טעינת הנתונים
  window._pendingLayoutDrawings = L.drawings || [];
  restoreDrawings();
  loadChart().then(() => {
    if (window._pendingLayoutDrawings && window._pendingLayoutDrawings.length) {
      drawings = window._pendingLayoutDrawings.map(d => ({ ...d, id: uid("dw") }));
      window._pendingLayoutDrawings = null;
      renderDrawings(); renderObjList();
    }
  });
  showToast("פריסה '" + name + "' נטענה");
}

function renderLayoutList() {
  const el = $("layout-list");
  const names = Object.keys(layouts);
  el.innerHTML = names.length ? names.map(n => `
    <button onclick="applyLayout('${escapeHtml(n)}')">📐 ${escapeHtml(n)}</button>`).join("")
    : `<div class="obj-empty" style="padding:8px">אין פריסות שמורות</div>`;
}
window.applyLayout = applyLayout;

/* ---------------- menus ---------------- */
function closeAllMenus() {
  document.querySelectorAll(".menu.open").forEach(m => m.classList.remove("open"));
}
function wireMenu(btnId, menuId, onOpen) {
  $(btnId).addEventListener("click", ev => {
    ev.stopPropagation();
    const m = $(menuId);
    const was = m.classList.contains("open");
    closeAllMenus();
    if (!was) { if (onOpen) onOpen(); m.classList.add("open"); positionMenuMobile($(btnId), m); }
  });
}

/* במובייל התפריטים יושבים בתוך שורה נגללת (overflow-x) — ממקמים אותם כ-fixed
   מתחת לכפתור כדי שלא ייחתכו */
window.positionMenuMobile = function(btn, menu) {
  if (!btn || !menu) return;
  if (window.innerWidth > 860) {
    menu.style.position = ""; menu.style.top = ""; menu.style.right = ""; menu.style.left = "";
    return;
  }
  const r = btn.getBoundingClientRect();
  const mw = Math.min(260, window.innerWidth - 16);
  menu.style.position = "fixed";
  menu.style.top = (r.bottom + 6) + "px";
  menu.style.left = "auto";
  menu.style.minWidth = mw + "px";
  let right = window.innerWidth - r.right;
  right = Math.max(8, Math.min(right, window.innerWidth - mw - 8));
  menu.style.right = right + "px";
};

/* ---------------- AI indicator engine (Hugging Face) ---------------- */
const AI_DENY = ["fetch(", "XMLHttpRequest", "eval(", "Function(", "import(",
  "localStorage", "sessionStorage", "document.", "window.", "cookie", "postMessage"];

function validateAICode(code) {
  if (!code || code.indexOf("function compute") === -1)
    return "הקוד חייב להגדיר function compute(candles)";
  const low = code.toLowerCase();
  for (const bad of AI_DENY)
    if (low.indexOf(bad.toLowerCase()) !== -1) return "פעולה אסורה בקוד: " + bad;
  return "";
}

function runAICode(code, candles) {
  const err = validateAICode(code);
  if (err) throw new Error(err);
  const factory = new Function(code + "\nreturn compute;");
  const compute = factory();
  if (typeof compute !== "function") throw new Error("compute אינה פונקציה");
  // עותק עמוק — קוד ה-AI לא יוכל להשחית את נתוני הגרף הראשי.
  // שים לב: 400 הנרות האחרונים (העדכניים), לא הראשונים.
  const src = (candles && candles.length ? candles : lastCandles).slice(-400);
  const sample = src.map(c => Object.assign({}, c));
  const res = compute(sample);
  if (!res || !Array.isArray(res.overlays)) throw new Error("הפונקציה חייבת להחזיר { overlays: [...] }");
  for (const o of res.overlays) {
    if (!Array.isArray(o.values)) throw new Error("כל overlay חייב להכיל values");
    // סינון נקודות לא תקינות (null/NaN) במקום כשלון שקט של כל האינדיקטור
    const clean = [];
    for (const p of o.values) {
      if (p && typeof p.time === "number" && isFinite(p.time) &&
          typeof p.value === "number" && isFinite(p.value))
        clean.push({ time: p.time, value: p.value });
    }
    if (!clean.length) throw new Error("אין ערכים תקינים ב-overlay: " + (o.name || ""));
    o.values = clean;
  }
  return res;
}

function clearAIOverlays() {
  aiOverlays.forEach(s => { try { chart.removeSeries(s); } catch (e) {} });
  aiOverlays = [];
}

function renderAIOverlays() {
  clearAIOverlays();
  for (const item of aiLibrary) {
    if (!item.active) continue;
    try {
      const res = runAICode(item.code, lastCandles);
      for (const o of res.overlays) {
        const s = chart.addLineSeries({
          color: o.color || "#22d3ee", lineWidth: o.width || 1,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        s.setData(o.values);
        aiOverlays.push(s);
      }
    } catch (e) {
      console.warn("AI overlay failed:", item.name, e.message);
      showToast("⚠ אינדיקטור '" + item.name + "' לא הוטמע: " + e.message);
    }
  }
}

function openAIModal() {
  $("ai-prompt").value = "";
  $("ai-code-paste").value = "";
  $("ai-error").textContent = "";
  $("ai-status").textContent = "";
  $("ai-code-wrap").classList.add("hidden");
  $("ai-modal").classList.remove("hidden");
}
function closeAIModal() { $("ai-modal").classList.add("hidden"); }

async function generateAIIndicator() {
  const prompt = $("ai-prompt").value.trim();
  const errEl = $("ai-error"), stEl = $("ai-status");
  errEl.textContent = "";
  if (!prompt) { errEl.textContent = "כתוב תיאור לאינדיקטור"; return; }
  $("ai-generate").disabled = true;
  $("ai-generate").textContent = "⏳ יוצר קוד...";
  stEl.textContent = "המודל כותב את הקוד (יכול לקחת עד דקה בקריאה ראשונה)...";
  try {
    const r = await fetch("/api/ai/indicator", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "שגיאה ביצירה");
    showAICode(d.name || "אינדיקטור AI", d.code);
    stEl.textContent = "הקוד נוצר ✓ — בדוק אותו ולחץ הטמע בגרף";
  } catch (e) {
    errEl.textContent = e.message;
    stEl.textContent = "";
  } finally {
    $("ai-generate").disabled = false;
    $("ai-generate").textContent = "⚙ צור קוד";
  }
}

function showAICode(name, code) {
  $("ai-name").value = name;
  $("ai-code-view").textContent = code;
  $("ai-code-view").dataset.code = code;
  $("ai-code-wrap").classList.remove("hidden");
}

function embedAICode(save) {
  const errEl = $("ai-error");
  errEl.textContent = "";
  let code, name;
  const pasted = $("ai-code-paste").value.trim();
  if (pasted && !$("ai-paste-sec").classList.contains("hidden")) {
    code = pasted;
    name = $("ai-name").value.trim() || "אינדיקטור מותאם";
  } else {
    code = $("ai-code-view").dataset.code || "";
    name = $("ai-name").value.trim() || "אינדיקטור AI";
  }
  try {
    runAICode(code, lastCandles); // ולידציה לפני הטמעה
  } catch (e) {
    errEl.textContent = "הקוד לא עבר ולידציה: " + e.message;
    return;
  }
  const item = { id: uid("ai"), name, code, active: true };
  aiLibrary.forEach(x => { x.active = false; });
  aiLibrary.push(item);
  saveAILibrary();
  renderAIOverlays();
  renderObjList();
  closeAIModal();
  showToast("✨ '" + name + "' הוטמע בגרף" + (save ? " ונשמר לספרייה" : ""));
}

function saveAILibrary() { saveLocal("charts_ai_library", aiLibrary); schedulePush(); }
function loadAILibrary() { aiLibrary = loadLocal("charts_ai_library", []); }

window.toggleAIItem = id => {
  const it = aiLibrary.find(x => x.id === id);
  if (it) {
    aiLibrary.forEach(x => { if (x.id !== id) x.active = false; });
    it.active = !it.active;
    saveAILibrary(); renderAIOverlays(); renderObjList();
  }
};
window.deleteAIItem = id => {
  aiLibrary = aiLibrary.filter(x => x.id !== id);
  saveAILibrary(); renderAIOverlays(); renderObjList();
};

function renderAISection() {
  if (!aiLibrary.length) return "";
  return `<div class="menu-sep"></div>
    <div class="obj-row"><span class="nm" style="font-weight:700">✨ אינדיקטורים AI</span></div>` +
    aiLibrary.map(it => `
      <div class="obj-row">
        <span class="sw" style="background:#22d3ee"></span>
        <span class="nm">${escapeHtml(it.name)}</span>
        <button class="mini" onclick="toggleAIItem('${it.id}')">${it.active ? "👁" : "🚫"}</button>
        <button class="mini del" onclick="deleteAIItem('${it.id}')">✕</button>
      </div>`).join("");
}

/* ---------------- supabase: auth + cloud sync ---------------- */
function showAuthOverlay() { $("auth-modal").classList.remove("hidden"); }
function hideAuthOverlay() { $("auth-modal").classList.add("hidden"); }

function updateAuthBtn() {
  const b = $("auth-btn");
  if (!supa.enabled) { b.classList.add("hidden"); return; }
  b.classList.remove("hidden");
  if (supa.user) {
    b.textContent = "👤✓";
    b.title = "מחובר: " + (supa.user.email || "") + " — לחץ להתנתקות";
  } else {
    b.textContent = "👤";
    b.title = "התחברות לסנכרון ענן";
  }
}

async function initSupabase() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    if (!cfg.supabase_url || !cfg.supabase_anon_key || !window.supabase) return;
    supa.client = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);
    supa.enabled = true;
    lastSyncAt = loadLocal("charts_last_sync", 0);
    updateAuthBtn();
    const { data } = await supa.client.auth.getSession();
    if (data && data.session) {
      await onSignedIn(data.session);
    } else {
      showAuthOverlay();
    }
    supa.client.auth.onAuthStateChange((ev, session) => {
      if (session) onSignedIn(session);
      else onSignedOut();
    });
  } catch (e) {}
}

async function onSignedIn(session) {
  supa.token = session.access_token;
  supa.user = session.user;
  hideAuthOverlay();
  updateAuthBtn();
  await pullAndMerge();
  loadAlerts();
}

function onSignedOut() {
  supa.token = null;
  supa.user = null;
  updateAuthBtn();
}

async function sendMagicLink() {
  const email = $("auth-email").value.trim();
  const errEl = $("auth-error"), stEl = $("auth-status");
  errEl.textContent = "";
  stEl.textContent = "";
  if (!email || email.indexOf("@") < 0) { errEl.textContent = "כתובת אימייל לא תקינה"; return; }
  $("auth-send").disabled = true;
  try {
    const { error } = await supa.client.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: location.origin + "/" },
    });
    if (error) throw error;
    stEl.textContent = "📧 קישור נשלח למייל — לחץ עליו והדף יתחבר אוטומטית";
  } catch (e) {
    errEl.textContent = e.message || "שגיאה בשליחת הקישור";
  } finally {
    $("auth-send").disabled = false;
  }
}

async function signOutCloud() {
  try { await supa.client.auth.signOut(); } catch (e) {}
  onSignedOut();
  showToast("התנתקת מהסנכרון — הנתונים המקומיים נשמרו");
}

/* pull מהשרת ומיזוג ל-state המקומי (last-write-wins לפי updated_at) */
async function pullAndMerge() {
  let d;
  try {
    const r = await authFetch("/api/sync/pull");
    if (!r.ok) return;
    d = await r.json();
  } catch (e) { return; }

  const newer = row => {
    try { return new Date(row.updated_at).getTime() > lastSyncAt; } catch (e) { return false; }
  };

  let touched = false;
  (d.drawings || []).forEach(row => {
    if (row.symbol && newer(row)) {
      saveLocal("charts_drawings_" + row.symbol, row.drawings || []);
      touched = true;
    }
  });
  if (touched) { restoreDrawings(); renderDrawings(); renderObjList(); }

  const srvLayouts = {};
  (d.layouts || []).forEach(row => { if (row.name && newer(row)) srvLayouts[row.name] = row.layout || {}; });
  if (Object.keys(srvLayouts).length) {
    layouts = Object.assign({}, layouts, srvLayouts);
    saveLocal("charts_layouts", layouts);
    renderLayoutList();
  }

  const wl = (d.watchlists || [])[0];
  if (wl && Array.isArray(wl.symbols) && wl.symbols.length && newer(wl)) {
    watchlist = wl.symbols.filter(s => typeof s === "string");
    saveLocal("charts_watchlist", watchlist);
    refreshWatchlist();
  }

  const rows = (d.indicators || []).filter(x => x.name && x.code && newer(x));
  if (rows.length) {
    const prevActive = {};
    aiLibrary.forEach(x => { if (x.active) prevActive[x.name] = 1; });
    const merged = {};
    aiLibrary.forEach(x => { merged[x.name] = x; });
    rows.forEach(x => {
      merged[x.name] = { id: x.id || uid("ai"), name: x.name, code: x.code, active: !!prevActive[x.name] };
    });
    aiLibrary = Object.values(merged);
    saveAILibrary();
    renderAIOverlays();
    renderObjList();
  }

  lastSyncAt = Date.now();
  saveLocal("charts_last_sync", lastSyncAt);
  showToast("☁ הסנכרון הושלם");
}

/* push עם debounce — נקרא אחרי כל שינוי מקומי */
function schedulePush() {
  if (!supa.enabled || !supa.token) return;
  clearTimeout(supa.pushTimer);
  supa.pushTimer = setTimeout(pushNow, 2000);
}

async function pushNow() {
  if (!supa.enabled || !supa.token) return;
  const drawings = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.indexOf("charts_drawings_") === 0) {
        const arr = loadLocal(k, []);
        if (arr && arr.length)
          drawings.push({ symbol: k.slice("charts_drawings_".length), timeframe: currentInterval, drawings: arr });
      }
    }
  } catch (e) {}
  const payload = {
    drawings: drawings.slice(0, 300),
    layouts: Object.keys(layouts).slice(0, 50).map(n => ({ name: n, layout: layouts[n] })),
    watchlists: [{ name: "default", symbols: watchlist.slice(0, 200) }],
    indicators: aiLibrary.slice(0, 100).map(x => ({ name: x.name, code: x.code, description: "" })),
  };
  try {
    const r = await authFetch("/api/sync/push", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (r.ok) { lastSyncAt = Date.now(); saveLocal("charts_last_sync", lastSyncAt); }
  } catch (e) {}
}

/* ---------------- Level 2 — עומק שוק ויזואלי (קריפטו) ---------------- */
let l2Open = false;
let l2Timer = null;

function updateL2Btn() {
  const b = $("l2-btn");
  const show = isCrypto(currentSymbol);
  b.classList.toggle("hidden", !show);
  if (!show && l2Open) toggleL2(false);
}

function toggleL2(force) {
  l2Open = typeof force === "boolean" ? force : !l2Open;
  $("l2-panel").classList.toggle("hidden", !l2Open);
  $("l2-btn").classList.toggle("active", l2Open);
  if (l2Timer) { clearInterval(l2Timer); l2Timer = null; }
  if (l2Open) {
    loadL2();
    l2Timer = setInterval(loadL2, 5000);
  }
}

function fmtAmt(v) {
  if (v == null || isNaN(v)) return "—";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (a >= 1) return String(+v.toFixed(4));
  return String(+v.toPrecision(4));
}

async function loadL2() {
  if (!l2Open || !isCrypto(currentSymbol)) return;
  try {
    const r = await fetch(`/api/orderbook?symbol=${encodeURIComponent(currentSymbol)}`);
    if (!r.ok) throw new Error("no data");
    renderL2(await r.json());
  } catch (e) {
    $("l2-sub").textContent = "אין נתוני עומק כרגע";
  }
}

function renderL2(d) {
  const bids = (d.bids || []).slice(0, 15);
  const asks = (d.asks || []).slice(0, 15);
  if (!bids.length || !asks.length) { $("l2-sub").textContent = "אין נתוני עומק כרגע"; return; }
  const bestBid = bids[0][0], bestAsk = asks[0][0];
  $("l2-sub").textContent = "ספרד: " + fmtPrice(bestAsk - bestBid);
  $("l2-mid").textContent = fmtPrice((bestAsk + bestBid) / 2);
  $("l2-src").textContent = "מקור: " + (d.source || "");

  const mkRow = (p, s, ct, cls, maxT) => {
    const w = Math.max(2, Math.min(100, (ct / maxT) * 100));
    return '<div class="l2-row ' + cls + '"><div class="bar" style="width:' + w.toFixed(1) + '%"></div>' +
      '<span class="p">' + fmtPrice(p) + '</span><span class="s">' + fmtAmt(s) + '</span><span class="t">' + fmtAmt(ct) + '</span></div>';
  };
  // asks: מוצג גבוה->נמוך, מצטבר מהצד הטוב (הנמוך)
  const dispA = asks.slice().reverse();
  let runA = 0;
  const cumA = new Array(dispA.length);
  for (let i = dispA.length - 1; i >= 0; i--) { runA += dispA[i][1]; cumA[i] = runA; }
  const maxA = runA || 1;
  $("l2-asks").innerHTML = dispA.map((r, i) => mkRow(r[0], r[1], cumA[i], "l2-ask", maxA)).join("");
  // bids: מוצג גבוה->נמוך, מצטבר מהצד הטוב (הגבוה)
  let runB = 0;
  const cumB = bids.map(r => runB += r[1]);
  const maxB = runB || 1;
  $("l2-bids").innerHTML = bids.map((r, i) => mkRow(r[0], r[1], cumB[i], "l2-bid", maxB)).join("");
}

/* ---------------- boot ---------------- */
// תג שגיאה זעיר לאבחון (מופיע רק אם יש שגיאה לא מטופלת)
// שומר את המחסנית ב-window.__lastError כדי לאפשר אבחון מרחוק
window.addEventListener("error", ev => {
  try {
    window.__lastError = {
      message: ev.message || "unknown",
      stack: (ev.error && ev.error.stack) ? String(ev.error.stack).slice(0, 2000) : "",
      file: ev.filename || "",
      line: ev.lineno || 0,
      col: ev.colno || 0,
      time: new Date().toISOString(),
    };
    if ($("js-err-badge")) return;
    const b = document.createElement("div");
    b.id = "js-err-badge";
    b.textContent = "⚠";
    b.title = "שגיאה: " + (ev.message || "unknown") +
      (ev.filename ? "\n" + String(ev.filename).split("/").pop() + ":" + (ev.lineno || "?") : "") +
      (window.__lastError.stack ? "\n" + window.__lastError.stack.slice(0, 1500) : "");
    b.style.cssText = "position:fixed;bottom:6px;left:6px;z-index:9999;background:#7f1d1d;color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:13px;cursor:help";
    document.body.appendChild(b);
  } catch (e) {}
});

function chartLibPresent() {
  return typeof LightweightCharts !== "undefined";
}

// טעינה דינמית של ספריית הגרפים כגיבוי (אם תג ה-script הסטטי נכשל)
function loadChartLibDynamic() {
  return new Promise(resolve => {
    if (chartLibPresent()) { resolve(true); return; }
    const s = document.createElement("script");
    s.src = "/static/vendor/lightweight-charts.standalone.production.js?dyn=" + Date.now();
    s.onload = () => resolve(chartLibPresent());
    s.onerror = () => resolve(false);
    const parent = document.head || document.documentElement;
    if (!parent) { resolve(false); return; }
    parent.appendChild(s);
  });
}

async function initChartWithRecovery() {
  try {
    initChart();
    return;
  } catch (firstErr) {
    // הספרייה קיימת אבל האתחול נכשל — מציגים את השגיאה האמיתית
    if (chartLibPresent()) { showChartLibError(firstErr); return; }
    // ניסיון שיקום: טעינה דינמית ואז אתחול מחדש
    const ok = await loadChartLibDynamic();
    if (ok) {
      try { initChart(); loadChart(); return; }
      catch (e2) { showChartLibError(e2); return; }
    }
    showChartLibError(firstErr);
  }
}

function showChartLibError(err) {
  // הבאנר מכסה רק את אזור הגרף עצמו — הפאנל התחתון (התראות וכו') נשאר נגיש.
  // מציג את השגיאה האמיתית כדי לאפשר אבחון (לא מניחים שזו בעיית רשת).
  const chartEl = $("chart");
  if (!chartEl || $("chart-lib-err")) return;
  const msg = err && err.message ? String(err.message) : "unknown";
  const diag = "lib:" + (chartLibPresent() ? "yes" : "no") +
    " | tag:" + (document.querySelector('script[src*="vendor/lightweight-charts"]') ? "yes" : "no");
  const d = document.createElement("div");
  d.id = "chart-lib-err";
  d.style.cssText = "position:absolute;inset:0;display:flex;flex-direction:column;gap:10px;align-items:center;justify-content:center;background:#0e1220;color:#e5e7eb;z-index:50;text-align:center;padding:20px";
  d.innerHTML = '<div style="font-size:15px">⚠ הגרף לא נטען</div>' +
    '<div dir="ltr" style="font-size:11px;color:#8a93a8;max-width:100%;overflow-wrap:anywhere">' +
    escapeHtml(msg) + "<br>" + escapeHtml(diag) + "</div>" +
    '<button id="chart-lib-retry" class="tb-btn" style="font-size:14px">🔄 נסה שוב</button>';
  chartEl.style.position = "relative";
  chartEl.appendChild(d);
  $("chart-lib-retry").addEventListener("click", () => location.reload());
}

function boot() {
  watchlist = loadLocal("charts_watchlist", [...DEFAULT_WL]);
  layouts = loadLocal("charts_layouts", {});
  loadAILibrary();

  // הגרף: אתחול עם ניסיון שיקום עצמי אם הספרייה לא נטענה
  initChartWithRecovery();
  wireCtxMenu();
  wireAlertDrag();
  $("chart-retry").addEventListener("click", () => loadChart());

  // סרגל עליון — אינטרוול נרות וטווח זמן נפרדים
  document.querySelectorAll("#tf-group .tf-btn").forEach(b =>
    b.addEventListener("click", () => setTfInterval(b.dataset.tf)));
  document.querySelectorAll("#range-group .tf-btn").forEach(b =>
    b.addEventListener("click", () => setRange(b.dataset.range)));

  wireMenu("ctype-btn", "ctype-menu");
  $("ctype-menu").querySelectorAll("[data-ct]").forEach(b =>
    b.addEventListener("click", () => {
      chartType = b.dataset.ct;
      $("ctype-label").textContent = b.textContent.trim().split(" ").slice(1).join(" ") || b.textContent.trim();
      closeAllMenus();
      loadChart();
    }));

  $("ind-btn").addEventListener("click", ev => {
    ev.stopPropagation();
    const m = $("ind-menu");
    const was = m.classList.contains("open");
    closeAllMenus();
    if (!was) { if (window.renderIndicatorMenu) renderIndicatorMenu(); m.classList.add("open"); positionMenuMobile($("ind-btn"), m); }
  });

  $("alert-btn").addEventListener("click", openAlertBuilder);

  $("ab-close").addEventListener("click", closeAlertBuilder);
  $("alert-modal").addEventListener("click", ev => { if (ev.target.id === "alert-modal") closeAlertBuilder(); });
  ["ab-ctype", "ab-left-kind", "ab-right-kind"].forEach(id =>
    $(id).addEventListener("change", syncBuilderKinds));
  $("ab-submit").addEventListener("click", submitAlert);

  // AI אינדיקטור
  $("ai-btn").addEventListener("click", openAIModal);
  $("ai-close").addEventListener("click", closeAIModal);
  $("ai-modal").addEventListener("click", ev => { if (ev.target.id === "ai-modal") closeAIModal(); });
  document.querySelectorAll(".ab-tab[data-at]").forEach(t =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".ab-tab[data-at]").forEach(x => x.classList.toggle("active", x === t));
      const gen = t.dataset.at === "gen";
      $("ai-gen-sec").classList.toggle("hidden", !gen);
      $("ai-paste-sec").classList.toggle("hidden", gen);
    }));
  $("ai-generate").addEventListener("click", generateAIIndicator);
  $("ai-embed").addEventListener("click", () => embedAICode(false));
  $("ai-save").addEventListener("click", () => embedAICode(true));

  // עומק שוק (Level 2)
  $("l2-btn").addEventListener("click", () => toggleL2());
  $("l2-close").addEventListener("click", () => toggleL2(false));

  wireMenu("layout-btn", "layout-menu", renderLayoutList);
  $("layout-save").addEventListener("click", () => {
    const name = prompt("שם הפריסה:");
    if (name) {
      layouts[name] = collectLayout();
      saveLocal("charts_layouts", layouts);
      renderLayoutList();
      schedulePush();
      showToast("פריסה '" + name + "' נשמרה");
    }
    closeAllMenus();
  });

  $("symbol-btn").addEventListener("click", openSearch);
  $("search-modal").addEventListener("click", ev => { if (ev.target.id === "search-modal") closeSearch(); });
  $("search-input").addEventListener("input", ev => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => doSearch(ev.target.value), 220);
  });
  $("search-input").addEventListener("keydown", ev => {
    if (ev.key === "Enter") {
      const first = document.querySelector(".sr-row");
      if (first) pickSymbol(first.dataset.sym);
    } else if (ev.key === "Escape") closeSearch();
  });

  // סרגל ציור
  document.querySelectorAll(".tool-btn[data-tool]").forEach(b =>
    b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("magnet-btn").addEventListener("click", () => {
    magnetOn = !magnetOn;
    $("magnet-btn").classList.toggle("active", magnetOn);
    showToast(magnetOn ? "מגנט הופעל 🧲" : "מגנט כובה");
  });
  $("clear-drawings").addEventListener("click", () => {
    if (!drawings.length) return;
    if (confirm("למחוק את כל הציורים על הגרף?")) {
      clearDrawingSeries();
      drawings = [];
      renderObjList();
      persistDrawings();
    }
  });

  // פאנל מעקב
  document.querySelectorAll(".wp-tab").forEach(b =>
    b.addEventListener("click", () => {
      wpTab = b.dataset.wp;
      document.querySelectorAll(".wp-tab").forEach(x => x.classList.toggle("active", x === b));
      refreshWatchlist();
    }));
  const addSym = () => { addToWatchlist($("wp-add").value); $("wp-add").value = ""; };
  $("wp-add-btn").addEventListener("click", addSym);
  $("wp-add").addEventListener("keydown", ev => { if (ev.key === "Enter") addSym(); });

  // פאנל תחתון
  document.querySelectorAll(".bp-tab[data-bp]").forEach(b =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".bp-tab[data-bp]").forEach(x => x.classList.toggle("active", x === b));
      $("bp-objects").classList.toggle("hidden", b.dataset.bp !== "objects");
      $("bp-alerts").classList.toggle("hidden", b.dataset.bp !== "alerts");
    }));
  $("bp-collapse").addEventListener("click", () => {
    const p = $("bottom-panel");
    p.classList.toggle("collapsed");
    $("bp-collapse").textContent = p.classList.contains("collapsed") ? "▴" : "▾";
  });

  // מקלדת
  document.addEventListener("keydown", ev => {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA") return;
    const k = ev.key.toLowerCase();
    if (k === "escape") {
      if (!$("search-modal").classList.contains("hidden")) closeSearch();
      else if (drawingMode !== "cursor") setTool("cursor");
      closeAllMenus();
    }
    else if (k === "v") setTool("cursor");
    else if (k === "t") setTool("trend");
    else if (k === "h") setTool("hline");
    else if (k === "f") setTool("fib");
    else if (k === "x") setTool("text");
  });
  document.addEventListener("click", ev => {
    if (Date.now() - ctxOpenedAt < 500) return; // קליק שחרור אחרי לחיצה ארוכה
    if (!ev.target.closest(".dropdown") && !ev.target.closest("#ctx-menu")) { closeAllMenus(); closeCtxMenu(); }
  });

  // כפתור מעקב במובייל
  if (window.innerWidth <= 860) {
    const b = document.createElement("button");
    b.className = "tb-btn"; b.textContent = "📋";
    b.title = "רשימת מעקב";
    b.addEventListener("click", () => $("watchpanel").classList.toggle("open"));
    $("topbar").insertBefore(b, $("data-badge"));
  }

  // התחברות וסנכרון ענן (סופאבייס)
  $("auth-btn").addEventListener("click", () => {
    if (!supa.enabled) return;
    if (supa.user) { if (confirm("להתנתק מהסנכרון?")) signOutCloud(); }
    else showAuthOverlay();
  });
  $("auth-close").addEventListener("click", hideAuthOverlay);
  $("auth-modal").addEventListener("click", ev => { if (ev.target.id === "auth-modal") hideAuthOverlay(); });
  $("auth-send").addEventListener("click", sendMagicLink);
  $("auth-email").addEventListener("keydown", ev => { if (ev.key === "Enter") sendMagicLink(); });
  initSupabase();

  loadAlerts();
  restoreDrawings();
  loadChart();
  refreshWatchlist();
  updateL2Btn();
  quoteTimer = setInterval(refreshWatchlist, 20000);
}

document.addEventListener("DOMContentLoaded", boot);
