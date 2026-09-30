/* Strategy backtest tab — merged from trading-alerts into Elroi Terminal.
 * Injects a "📊 אסטרטגיה" tab: built-in strategies with tunable params,
 * backtest on the current symbol, and parameter optimization.
 * Uses Elroi Terminal globals: currentSymbol, currentPeriod, currentInterval,
 * candleSeries, showToast. Markers are drawn on the candle series.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  let strategies = [], currentStrategy = null;

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

  /* ---------------- inject tab + page ---------------- */
  function inject() {
    if ($("bp-strategy")) return;
    const tabs = document.querySelector(".bp-tabs");
    const body = $("bp-body");
    if (!tabs || !body) return;
    const tab = document.createElement("button");
    tab.className = "bp-tab";
    tab.dataset.bp = "strategy";
    tab.textContent = "📊 אסטרטגיה";
    tabs.insertBefore(tab, $("bp-collapse"));
    const page = document.createElement("div");
    page.className = "bp-page hidden";
    page.id = "bp-strategy";
    page.innerHTML =
      '<div class="sc-toolbar">' +
      '  <select id="st-select" style="min-width:180px"></select>' +
      '  <button class="tb-btn" id="st-run-btn">▶ בקטסט</button>' +
      '  <button class="tb-btn" id="st-opt-btn">⚡ אופטימיזציה</button>' +
      "</div>" +
      '<div class="sc-hint" id="st-hint"></div>' +
      '<div class="st-params" id="st-params"></div>' +
      '<div id="st-results" class="sc-results"></div>' +
      '<div id="st-opt" class="sc-results"></div>';
    body.appendChild(page);
    $("st-select").addEventListener("change", renderStrategyParams);
    $("st-run-btn").addEventListener("click", runBacktest);
    $("st-opt-btn").addEventListener("click", runOptimize);
  }

  async function loadStrategies() {
    try {
      const data = await fetch("/api/strategies").then(r => r.json());
      strategies = data.strategies || [];
      $("st-select").innerHTML = strategies.map(s =>
        '<option value="' + esc(s.id) + '">' + esc(s.name_he) + "</option>").join("") +
        '<option value="__python__">🐍 Python מותאם אישית</option>';
      renderStrategyParams();
    } catch (e) { $("st-hint").textContent = "טעינת האסטרטגיות נכשלה"; }
  }

  function renderStrategyParams() {
    const id = $("st-select").value;
    if (id === "__python__") {
      currentStrategy = { id: "__python__", name_he: "Python מותאם אישית", params: {} };
      $("st-hint").textContent =
        "כתוב strategy(df) שמחזיר סיגנלים: 1 = כניסה ללונג, ‎-1‎ = יציאה, 0 = החזקה · נבדק על " +
        curSym() + " (" + curPeriod() + "/" + curInterval() + ") · df/ta/pd/np זמינים";
      $("st-params").innerHTML =
        '<textarea id="st-py-code" class="sc-code" placeholder="def strategy(df): ..."></textarea>' +
        '<div class="sc-toolbar"><button class="tb-btn" id="st-py-ex">📋 דוגמה</button></div>';
      $("st-py-ex").addEventListener("click", async () => {
        try {
          const meta = await fetch("/api/scan/meta").then(r => r.json());
          if (meta && meta.py_strategy_example) {
            $("st-py-code").value = meta.py_strategy_example;
            showToast("📋 דוגמת אסטרטגיית Python נטענה");
          }
        } catch (e) { showToast("טעינת הדוגמה נכשלה"); }
      });
      $("st-results").innerHTML = "";
      $("st-opt").innerHTML = "";
      return;
    }
    currentStrategy = strategies.find(s => s.id === id);
    if (!currentStrategy) return;
    $("st-hint").textContent =
      currentStrategy.description_he + " · נבדק על " + curSym() +
      " (" + curPeriod() + "/" + curInterval() + ")";
    $("st-params").innerHTML =
      Object.entries(currentStrategy.params).map(([name, spec]) =>
        '<label class="st-param">' + esc(spec.label_he) + ": " +
        '<input type="number" id="st-p-' + esc(name) + '" value="' + spec.default + '"' +
        ' min="' + spec.min + '" max="' + spec.max + '" step="' + spec.step + '">' +
        "</label>").join("");
    $("st-results").innerHTML = "";
    $("st-opt").innerHTML = "";
  }

  function collectStrategyParams() {
    const p = {};
    Object.keys(currentStrategy.params).forEach(name => {
      const el = $("st-p-" + name);
      if (el) p[name] = parseFloat(el.value);
    });
    return p;
  }

  function renderBacktestMetrics(data) {
    if (window.renderBacktestMetrics) return window.renderBacktestMetrics(data);
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
      '<div class="st-metric"><span>נרות נבדקו</span><b>' + data.candles + "</b></div></div>";
  }

  async function runBacktest() {
    const box = $("st-results");
    $("st-opt").innerHTML = "";
    const payload = {
      symbol: curSym(), period: curPeriod(), interval: curInterval(),
      initial_capital: 10000, commission_pct: 0.1,
    };
    let url;
    if (currentStrategy && currentStrategy.id === "__python__") {
      const code = ($("st-py-code") || {}).value || "";
      if (!code.trim()) { showToast("כתוב קוד אסטרטגיה קודם"); return; }
      url = "/api/py/backtest";
      payload.code = code;
      box.innerHTML = '<div class="sc-note">מריץ בקטסט Python…</div>';
    } else {
      if (!currentStrategy) { showToast("בחר אסטרטגיה"); return; }
      url = "/api/backtest";
      payload.strategy_id = currentStrategy.id;
      payload.params = collectStrategyParams();
      box.innerHTML = '<div class="sc-note">מריץ בקטסט…</div>';
    }
    try {
      const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "שגיאה");
      try { candleSeries.setMarkers(data.markers || []); } catch (e) { console.error(e); }
      box.innerHTML = renderBacktestMetrics(data) +
        '<div class="sc-note">סימוני קנייה/מכירה הוצגו על הגרף · ' + esc(data.symbol) +
        " · טווח " + esc(data.period) + " · נרות " + esc(data.interval) + "</div>";
    } catch (e) {
      box.innerHTML = '<div class="sc-err">❌ ' + esc(e.message) + "</div>";
    }
  }

  async function runOptimize() {
    if (currentStrategy && currentStrategy.id === "__python__") {
      showToast("אופטימיזציה נתמכת רק באסטרטגיות המובנות");
      return;
    }
    if (!currentStrategy) { showToast("בחר אסטרטגיה"); return; }
    const box = $("st-opt");
    $("st-results").innerHTML = "";
    box.innerHTML = '<div class="sc-note">מחפש פרמטרים מיטביים…</div>';
    try {
      const res = await fetch("/api/optimize", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: curSym(), period: curPeriod(), interval: curInterval(),
          strategy_id: currentStrategy.id, metric: "profit_factor",
          params: collectStrategyParams(),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "שגיאה");
      if (!data.top || !data.top.length) {
        box.innerHTML = '<div class="sc-note">לא נמצאו שילובים תקפים — נסה טווח זמן ארוך יותר</div>';
        return;
      }
      const paramNames = Object.keys(data.top[0].params);
      const labels = paramNames.map(n => (currentStrategy.params[n] || {}).label_he || n);
      box.innerHTML = '<div class="sc-note">נבדקו ' + data.combos_tested + " שילובים (תקפים: " +
        data.combos_valid + ") · דירוג לפי פרופיט פקטור · " + esc(data.symbol) + " · " +
        esc(data.period) + "/" + esc(data.interval) + "</div>" +
        '<table class="st-table"><thead><tr>' +
        labels.map(l => "<th>" + esc(l) + "</th>").join("") +
        "<th>תשואה</th><th>PF</th><th>עסקאות</th><th></th></tr></thead><tbody>" +
        data.top.map(r =>
          "<tr>" + paramNames.map(n => "<td>" + esc(r.params[n]) + "</td>").join("") +
          '<td class="' + (r.metrics.total_return_pct >= 0 ? "up" : "down") + '">' +
          r.metrics.total_return_pct + "%</td>" +
          "<td>" + (r.metrics.profit_factor === null ? "—" : r.metrics.profit_factor) + "</td>" +
          "<td>" + r.metrics.num_trades + "</td>" +
          "<td><button class=\"tb-btn\" data-params='" + JSON.stringify(r.params) +
          "' onclick=\"applyOptParams(this)\">החל</button></td></tr>").join("") +
        "</tbody></table>";
    } catch (e) {
      box.innerHTML = '<div class="sc-err">❌ ' + esc(e.message) + "</div>";
    }
  }

  window.applyOptParams = function (btn) {
    try {
      const params = JSON.parse(btn.dataset.params);
      Object.entries(params).forEach(([name, v]) => {
        const el = $("st-p-" + name);
        if (el) el.value = v;
      });
      runBacktest();
    } catch (e) { showToast("החלת הפרמטרים נכשלה"); }
  };

  /* ---------------- boot ----------------
     Inject synchronously at parse time so app.js sees our tab when it
     wires .bp-tab clicks in its DOMContentLoaded boot. */
  inject();
  loadStrategies();
})();
