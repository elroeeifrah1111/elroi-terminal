/* Pine / Python sandbox — merged from trading-alerts into Elroi Terminal.
 * Injects a "📜 Pine" tab into the bottom panel: paste Pine Script (v5/v6) or
 * Python indicator code, run it on the current symbol with the chart's
 * range/interval, overlay plots + markers + hlines on the chart.
 * Uses Elroi Terminal globals: currentSymbol, currentPeriod, currentInterval,
 * lastCandles, candleSeries, window.chart, showToast, loadChart.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  const PINE_EXAMPLE = `//@version=5
strategy("EMA Cross + RSI Filter", overlay=true)
fast = input.int(9, "Fast EMA")
slow = input.int(21, "Slow EMA")
rsiLen = input.int(14, "RSI Length")

fastEma = ta.ema(close, fast)
slowEma = ta.ema(close, slow)
r = ta.rsi(close, rsiLen)

longCond = ta.crossover(fastEma, slowEma) and r > 50
exitCond = ta.crossunder(fastEma, slowEma)

strategy.entry("Long", strategy.long, when=longCond)
strategy.close("Long", when=exitCond)

plot(fastEma, "Fast EMA", color=color.blue)
plot(slowEma, "Slow EMA", color=color.orange)`;

  const PINE_HINT_HTML = 'הדבק קוד Pine Script (v5 / v6) — ירוץ על <b id="pine-symbol-label">AAPL</b> ' +
    'עם אותו טווח/אינטרוול של הגרף. <span style="color:var(--accent)">נתמך:</span> ta.* (ema/sma/rsi/macd/atr/…) · ' +
    'math.* · input.* · plot/plotshape/hline · strategy.entry/close/exit (TP/SL) · alert()/alertcondition() · ' +
    'if/else · for/while/switch · var · פונקציות f(x) =&gt; … · strategy.position_size';
  const PY_HINT_HTML = 'כתוב אינדיקטור <b>Python</b> — מחזיר <span dir="ltr">dict</span> של {שם_קו: סדרה} · ' +
    'ירוץ על <b id="pine-symbol-label">AAPL</b> עם אותו טווח/אינטרוול של הגרף · <span dir="ltr">df</span> (נרות) · ' +
    '<span dir="ltr">ta.*</span> (sma/ema/rsi/macd/atr/bbands/stoch/…) · <span dir="ltr">pd/np</span> · ' +
    'אופציונלי: <span dir="ltr">"markers": [{bar, side, text}]</span> לסמני קנייה/מכירה';

  let sbSeries = [];
  let sbPriceLines = [];
  let scanMetaCache = null;

  function clearSandboxOverlays() {
    try {
      const ch = window.chart;
      sbSeries.forEach(s => { try { ch.removeSeries(s); } catch (e) {} });
    } catch (e) {}
    sbSeries = [];
    try {
      sbPriceLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch (e) {} });
    } catch (e) {}
    sbPriceLines = [];
  }
  window.clearSandboxOverlays = clearSandboxOverlays;

  /* ---------------- inject tab + page ---------------- */
  function inject() {
    if ($("bp-pine")) return;
    const tabs = document.querySelector(".bp-tabs");
    const body = $("bp-body");
    if (!tabs || !body) return;
    const tab = document.createElement("button");
    tab.className = "bp-tab";
    tab.dataset.bp = "pine";
    tab.textContent = "📜 Pine";
    tabs.insertBefore(tab, $("bp-collapse"));
    const page = document.createElement("div");
    page.className = "bp-page hidden";
    page.id = "bp-pine";
    page.innerHTML =
      '<div class="sc-hint" id="pine-hint"></div>' +
      '<textarea id="pine-code" class="sc-code" style="min-height:140px" ' +
      ' placeholder="//@version=5&#10;indicator(&quot;My Script&quot;, overlay=true)&#10;..."></textarea>' +
      '<div class="sc-toolbar">' +
      '  <label class="sc-radio"><input type="radio" name="pine-lang" value="pine" checked> Pine</label>' +
      '  <label class="sc-radio"><input type="radio" name="pine-lang" value="python"> 🐍 Python</label>' +
      '  <input type="text" id="pine-name" class="sc-in" placeholder="שם הסקריפט">' +
      '  <button class="tb-btn" id="pine-run-btn">▶ הרץ</button>' +
      '  <button class="tb-btn" id="pine-clear-btn" title="נקה שכבות מהגרף">🧹 נקה גרף</button>' +
      '  <button class="tb-btn" id="pine-save-btn">💾 שמור</button>' +
      '  <button class="tb-btn" id="pine-ex-btn">📋 דוגמה</button>' +
      '  <button class="tb-btn" id="pine-srflip-btn">📋 S/R Flip</button>' +
      '  <button class="tb-btn" id="pine-basebo-btn">📋 פריצת בסיס</button>' +
      '  <select id="pine-saved"><option value="">— שמורים —</option></select>' +
      '  <button class="tb-btn" id="pine-del-btn">🗑️</button>' +
      '</div>' +
      '<div id="pine-results" class="sc-results"></div>';
    body.appendChild(page);
    $("pine-hint").innerHTML = PINE_HINT_HTML;
    document.querySelectorAll('input[name="pine-lang"]').forEach(r =>
      r.addEventListener("change", onPineLangChange));
    $("pine-run-btn").addEventListener("click", runPine);
    $("pine-clear-btn").addEventListener("click", () => {
      clearSandboxOverlays();
      $("pine-results").innerHTML = "";
      showToast("🧹 שכבות הסקריפט נוקו מהגרף");
    });
    $("pine-save-btn").addEventListener("click", savePineScript);
    $("pine-ex-btn").addEventListener("click", loadPineExample);
    $("pine-srflip-btn").addEventListener("click", loadSrflipIndicatorExample);
    $("pine-basebo-btn").addEventListener("click", loadBaseboIndicatorExample);
    $("pine-del-btn").addEventListener("click", deletePineScript);
    $("pine-saved").addEventListener("change", loadPineSaved);
    refreshPineSaved();
    // נקה שכבות כשהגרף נטען מחדש (סימול/טווח חדש)
    try {
      if (typeof loadChart === "function" && !loadChart._sbWrapped) {
        const orig = loadChart;
        const wrapped = async function () {
          clearSandboxOverlays();
          return orig.apply(this, arguments);
        };
        wrapped._sbWrapped = true;
        loadChart = wrapped;
      }
    } catch (e) {}
  }

  function pineLang() {
    const el = document.querySelector('input[name="pine-lang"]:checked');
    return el ? el.value : "pine";
  }

  function setPineLang(lang) {
    document.querySelectorAll('input[name="pine-lang"]').forEach(r => {
      r.checked = (r.value === lang);
    });
    onPineLangChange();
  }

  function onPineLangChange() {
    $("pine-hint").innerHTML = pineLang() === "python" ? PY_HINT_HTML : PINE_HINT_HTML;
    const lbl = $("pine-symbol-label");
    if (lbl) lbl.textContent = curSym();
  }

  function curSym() {
    try { return (typeof currentSymbol !== "undefined" && currentSymbol) || "AAPL"; }
    catch (e) { return "AAPL"; }
  }
  function curPeriod() {
    try { return (typeof currentPeriod !== "undefined" && currentPeriod) || "ALL"; }
    catch (e) { return "ALL"; }
  }
  function curInterval() {
    try { return (typeof currentInterval !== "undefined" && currentInterval) || "1d"; }
    catch (e) { return "1d"; }
  }

  async function ensureMeta() {
    if (!scanMetaCache) {
      try { scanMetaCache = await fetch("/api/scan/meta").then(r => r.json()); } catch (e) {}
    }
    return scanMetaCache;
  }

  /* ---------------- run ---------------- */
  async function runPine() {
    const code = $("pine-code").value;
    const lang = pineLang();
    if (!code.trim()) { showToast(lang === "python" ? "כתוב קוד Python קודם" : "הדבק קוד Pine Script קודם"); return; }
    const box = $("pine-results");
    box.innerHTML = '<div class="sc-note">מריץ ' + (lang === "python" ? "Python" : "Pine") + "…</div>";
    try {
      const res = await fetch(lang === "python" ? "/api/py/indicator" : "/api/pine/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: curSym(), period: curPeriod(), interval: curInterval(), code: code
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "שגיאה");
      clearSandboxOverlays();
      const ch = window.chart;
      const candles = (typeof lastCandles !== "undefined" && lastCandles) || [];
      (data.plots || []).forEach(p => {
        const ls = ch.addLineSeries({
          color: p.color || "#2962ff",
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          title: p.name,
        });
        const pts = [];
        (p.values || []).forEach((v, i) => {
          if (v !== null && v !== undefined && candles[i]) {
            pts.push({ time: candles[i].time, value: v });
          }
        });
        ls.setData(pts);
        sbSeries.push(ls);
      });
      if ((data.markers || []).length) {
        try { candleSeries.setMarkers(data.markers); } catch (e) { console.error(e); }
      }
      (data.hlines || []).forEach(h => {
        try {
          const pl = candleSeries.createPriceLine({
            price: h.price,
            color: h.color || "#787b86",
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: h.title || "",
          });
          sbPriceLines.push(pl);
        } catch (e) { console.error(e); }
      });
      box.innerHTML = renderPineResults(data);
    } catch (e) {
      box.innerHTML = '<div class="sc-err">❌ ' + esc(e.message) + "</div>";
    }
  }

  function renderPineResults(data) {
    const kindHe = data.kind === "strategy" ? "אסטרטגיה" : "אינדיקטור";
    const verTxt = data.pine_version ? " · Pine v" + data.pine_version : "";
    let html = '<div class="sc-note">📜 <b>' + esc(data.title) + "</b> (" + kindHe + verTxt + ") · " +
      data.num_bars + " נרות · " + esc(data.symbol) + " · " + esc(data.period) + "/" + esc(data.interval) + "</div>";
    if (data.kind === "strategy" && data.metrics) {
      html += renderBacktestMetrics(data);
    } else {
      const n = (data.plots || []).length;
      html += '<div class="sc-note">הוצגו ' + n + " קווים על הגרף" +
        ((data.markers || []).length ? " ו-" + data.markers.length + " סמנים" : "") + "</div>";
    }
    if (data.notes && data.notes.length) {
      html += '<div class="sc-note">שים לב: ' + data.notes.map(esc).join(" · ") + "</div>";
    }
    if ((data.alerts || []).length) {
      const shown = data.alerts.slice(0, 20);
      html += '<div class="sc-note">🔔 <b>' + data.alerts.length + " התראות</b>" +
        (data.alerts.length > 20 ? " (מוצגות 20)" : "") + ":<br>" +
        shown.map(a => {
          const t = a.time ? new Date(a.time * 1000).toLocaleDateString("he-IL") : "";
          return '<span class="sc-note">' + esc(t) + "</span> " + esc(a.message);
        }).join("<br>") + "</div>";
    }
    return html;
  }

  function renderBacktestMetrics(data) {
    const m = data.metrics;
    const pf = m.profit_factor === null ? "—" : (m.profit_factor >= 999 ? "∞" : m.profit_factor);
    const cls = v => v >= 0 ? "up" : "down";
    return '<div class="st-metrics">' +
      '<div class="st-metric"><span>תשואת אסטרטגיה</span><b class="' + cls(m.total_return_pct) + '">' + m.total_return_pct + '%</b></div>' +
      '<div class="st-metric"><span>קנה-והחזק</span><b class="' + cls(m.buy_hold_pct) + '">' + m.buy_hold_pct + '%</b></div>' +
      '<div class="st-metric"><span>עסקאות</span><b>' + m.num_trades + "</b></div>" +
      '<div class="st-metric"><span>אחוז הצלחה</span><b>' + m.win_rate_pct + '%</b></div>' +
      '<div class="st-metric"><span>פרופיט פקטור</span><b>' + pf + "</b></div>" +
      '<div class="st-metric"><span>דראודאון מקס׳</span><b class="down">-' + m.max_drawdown_pct + '%</b></div>' +
      '<div class="st-metric"><span>שארפ</span><b>' + m.sharpe + "</b></div>" +
      '<div class="st-metric"><span>נרות נבדקו</span><b>' + data.candles + "</b></div>" +
      "</div>" +
      '<div class="sc-note">סימוני קנייה/מכירה הוצגו על הגרף · ' + esc(data.symbol) +
      " · טווח " + esc(data.period) + " · נרות " + esc(data.interval) + "</div>";
  }
  window.renderBacktestMetrics = renderBacktestMetrics;

  /* ---------------- examples ---------------- */
  async function loadPineExample() {
    if (pineLang() === "python") {
      const meta = await ensureMeta();
      if (meta && meta.py_indicator_example) {
        $("pine-code").value = meta.py_indicator_example;
        showToast("📋 דוגמת Python נטענה — לחץ ▶ הרץ");
        return;
      }
    }
    $("pine-code").value = PINE_EXAMPLE;
    showToast("📋 דוגמה נטענה — לחץ ▶ הרץ");
  }

  async function loadSrflipIndicatorExample() {
    setPineLang("python");
    const meta = await ensureMeta();
    if (meta && meta.py_srflip_indicator_example) {
      $("pine-code").value = meta.py_srflip_indicator_example;
      showToast("📋 S/R Flip + Rounding + C&H נטען — לחץ ▶ הרץ");
    } else showToast("טעינת הדוגמה נכשלה");
  }

  async function loadBaseboIndicatorExample() {
    setPineLang("python");
    const meta = await ensureMeta();
    if (meta && meta.py_basebo_indicator_example) {
      $("pine-code").value = meta.py_basebo_indicator_example;
      showToast("📋 פריצת בסיס (הסטאפ של אלרואי) נטענה — לחץ ▶ הרץ");
    } else showToast("טעינת הדוגמה נכשלה");
  }

  /* ---------------- saved scripts (localStorage) ---------------- */
  function getPineScripts() {
    try { return JSON.parse(localStorage.getItem("pine_scripts") || "[]"); }
    catch (e) { return []; }
  }

  function refreshPineSaved() {
    const sel = $("pine-saved");
    if (!sel) return;
    const scripts = getPineScripts();
    sel.innerHTML = '<option value="">— שמורים —</option>' +
      scripts.map((s, i) => '<option value="' + i + '">' + esc(s.name) + "</option>").join("");
  }

  function savePineScript() {
    const name = $("pine-name").value.trim() || prompt("שם הסקריפט:");
    const code = $("pine-code").value;
    if (!name || !code.trim()) { showToast("צריך שם וקוד"); return; }
    const scripts = getPineScripts();
    scripts.push({ name: name, code: code, lang: pineLang() });
    localStorage.setItem("pine_scripts", JSON.stringify(scripts));
    $("pine-name").value = "";
    refreshPineSaved();
    showToast('💾 "' + name + '" נשמר');
  }

  function loadPineSaved() {
    const sel = $("pine-saved");
    if (sel.value === "") return;
    const s = getPineScripts()[parseInt(sel.value, 10)];
    if (s) {
      $("pine-code").value = s.code;
      $("pine-name").value = s.name;
      if (s.lang === "python" || s.lang === "pine") setPineLang(s.lang);
    }
  }

  function deletePineScript() {
    const sel = $("pine-saved");
    if (sel.value === "") { showToast("בחר סקריפט שמור למחיקה"); return; }
    const scripts = getPineScripts();
    const removed = scripts.splice(parseInt(sel.value, 10), 1);
    localStorage.setItem("pine_scripts", JSON.stringify(scripts));
    refreshPineSaved();
    showToast('🗑️ "' + (removed[0] && removed[0].name) + '" נמחק');
  }

  /* ---------------- boot ----------------
     Inject synchronously at parse time so app.js sees our tab when it
     wires .bp-tab clicks in its DOMContentLoaded boot. */
  inject();
})();
