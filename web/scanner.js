/* Scanner panel — merged from trading-alerts into Elroi Terminal.
 * Injects a "🔍 סורק" tab into the bottom panel and wires the full scan UI:
 * run scan (Python/Pine), S/R Flip + פריצת בסיס examples, saved/scheduled
 * scans with Telegram alerts, results navigation, alert creation per row.
 * Uses Elroi Terminal globals: pickSymbol, openAlertAtPrice, openAlertBuilder,
 * addToWatchlist, showToast.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  let scanMeta = null;
  let scanResults = [];
  let scanMatches = [];
  let scanNavIdx = 0;

  /* ---------------- inject tab + page ---------------- */
  function inject() {
    if ($("bp-scanner")) return;
    const tabs = document.querySelector(".bp-tabs");
    const body = $("bp-body");
    if (!tabs || !body) return;
    const collapse = $("bp-collapse");
    const tab = document.createElement("button");
    tab.className = "bp-tab";
    tab.dataset.bp = "scanner";
    tab.textContent = "🔍 סורק";
    tabs.insertBefore(tab, collapse);
    const page = document.createElement("div");
    page.className = "bp-page hidden";
    page.id = "bp-scanner";
    page.innerHTML =
      '<div class="sc-hint">סורק רשימות: בחר רשימה + טווח נרות, כתוב לוגיקה, והרץ על כל הסימולים. ' +
      '<b>Python</b> מומלץ (df עם הנרות, ta עם אינדיקטורים, SYMBOL). <b>Pine</b>: סימול תואם = הסקריפט ירה alert().</div>' +
      '<div class="sc-toolbar">' +
      '  <select id="scan-source" style="flex:1;min-width:120px"></select>' +
      '  <input id="scan-custom" class="sc-in hidden" placeholder="סימולים: AAPL, MSFT, TSLA…">' +
      '  <select id="scan-interval">' +
      '    <option value="5m">5 דקות</option><option value="15m">15 דקות</option>' +
      '    <option value="30m">30 דקות</option><option value="1h">שעה</option>' +
      '    <option value="1d" selected>יום</option><option value="1wk">שבוע</option><option value="1mo">חודש</option>' +
      '  </select>' +
      '  <select id="scan-lang">' +
      '    <option value="python">Python</option><option value="pine">Pine</option>' +
      '  </select>' +
      '  <button class="tb-btn" id="scan-run-btn">▶ הרץ סריקה</button>' +
      '  <button class="tb-btn" id="scan-ex-btn">📋 דוגמה</button>' +
      '  <button class="tb-btn" id="scan-srflip-btn">📋 S/R Flip</button>' +
      '  <button class="tb-btn" id="scan-basebo-btn">📋 פריצת בסיס</button>' +
      '</div>' +
      '<textarea id="scan-code" class="sc-code" placeholder="def scan(df): ..."></textarea>' +
      '<div class="sc-toolbar">' +
      '  <input type="text" id="scan-name" class="sc-in" placeholder="שם הסריקה">' +
      '  <input type="number" id="scan-schedule" class="sc-in" placeholder="כל X דקות" min="0" ' +
      '    title="0 = הרצה חד-פעמית; למשל 60 = כל שעה + התראת טלגרם על תוצאות חדשות">' +
      '  <button class="tb-btn" id="scan-save-btn">💾 שמור</button>' +
      '  <select id="scan-saved"><option value="">— סריקות שמורות —</option></select>' +
      '  <button class="tb-btn" id="scan-load-btn">📂 טען</button>' +
      '  <button class="tb-btn" id="scan-run-saved-btn">▶ הרץ</button>' +
      '  <button class="tb-btn" id="scan-toggle-btn">⏯️</button>' +
      '  <button class="tb-btn" id="scan-del-btn">🗑️</button>' +
      '</div>' +
      '<div id="scan-nav" class="sc-nav hidden">' +
      '  <button class="tb-btn" id="scan-prev">‹ הקודם</button>' +
      '  <b id="scan-counter"></b>' +
      '  <button class="tb-btn" id="scan-next">הבא ›</button>' +
      '  <span style="flex:1"></span>' +
      '  <button class="tb-btn" id="scan-addwl">➕ תואמים למעקב</button>' +
      '</div>' +
      '<div id="scan-summary" class="sc-note"></div>' +
      '<div id="scan-results" class="sc-results"></div>' +
      '<div class="sc-toolbar" style="margin-top:10px">' +
      '  <b>🎯 ממצאי פריצת בסיס</b><span style="flex:1"></span>' +
      '  <button class="tb-btn" id="bb-refresh">🔄 רענן</button>' +
      '  <button class="tb-btn" id="bb-mkwl">📋 צור רשימת מעקב מהממצאים</button>' +
      '  <button class="tb-btn" id="bb-addwl">➕ הוסף למעקב הנוכחי</button>' +
      '  <select id="bb-wl-load"><option value="">— רשימות שנוצרו —</option></select>' +
      '  <button class="tb-btn" id="bb-wl-open">📂 טען רשימה</button>' +
      '</div>' +
      '<div id="bb-findings" class="sc-results"></div>';
    body.appendChild(page);

    $("scan-run-btn").addEventListener("click", runScan);
    $("scan-ex-btn").addEventListener("click", loadScanExample);
    $("scan-srflip-btn").addEventListener("click", loadSrflipScanExample);
    $("scan-basebo-btn").addEventListener("click", loadBaseboScanExample);
    $("scan-save-btn").addEventListener("click", saveScan);
    $("scan-load-btn").addEventListener("click", loadSavedScanToEditor);
    $("scan-run-saved-btn").addEventListener("click", runSavedScanNow);
    $("scan-toggle-btn").addEventListener("click", toggleSavedScan);
    $("scan-del-btn").addEventListener("click", deleteSavedScan);
    $("scan-prev").addEventListener("click", () => scanStep(-1));
    $("scan-next").addEventListener("click", () => scanStep(1));
    $("scan-addwl").addEventListener("click", scanAddAllToWatchlist);
    $("bb-refresh").addEventListener("click", loadBaseboFindings);
    $("bb-mkwl").addEventListener("click", bbCreateWatchlist);
    $("bb-addwl").addEventListener("click", bbAddToCurrentWatchlist);
    $("bb-wl-open").addEventListener("click", bbLoadNamedWatchlist);
    $("scan-lang").addEventListener("change", () => loadScanExample());
    $("scan-source").addEventListener("change", () => {
      $("scan-custom").classList.toggle("hidden",
        $("scan-source").value !== "custom:");
    });
  }

  /* ---------------- sources ---------------- */
  async function refreshScanSources() {
    const sel = $("scan-source");
    try {
      const lists = await fetch("/api/ticker-lists").then(r => r.json());
      sel.innerHTML = (lists || []).map(l =>
        `<option value="preset:${esc(l.id)}" data-count="${l.count}">${esc(l.label_he || l.label)} (${l.count})</option>`).join("") +
        '<option value="custom:">✏️ סימולים מותאמים</option>';
    } catch (e) {
      sel.innerHTML = '<option value="preset:sp500">S&P 500</option><option value="custom:">✏️ סימולים מותאמים</option>';
    }
  }

  function scanSourceObj() {
    const v = ($("scan-source").value || "preset:sp500").split(":");
    if (v[0] === "custom")
      return { type: "custom", symbols: ($("scan-custom").value || "").split(/[\s,;]+/) };
    return { type: "preset", id: v.slice(1).join(":") };
  }

  /* Symbol count for the currently selected scan source; used to warn before running very large scans. Returns null when unknown. */ function scanSymbolCount() { const sel = $("scan-source"); const v = (sel.value || "preset:sp500").split(":"); if (v[0] === "custom") return (($("scan-custom").value || "").split(/[\s,;]+/).filter(Boolean)).length; const opt = sel.selectedOptions && sel.selectedOptions[0]; const n = opt && opt.dataset ? parseInt(opt.dataset.count, 10) : NaN; return isNaN(n) ? null : n; } /* ---------------- examples ---------------- */
  async function ensureMeta() {
    if (!scanMeta) {
      try { scanMeta = await fetch("/api/scan/meta").then(r => r.json()); } catch (e) {}
    }
    return scanMeta;
  }

  async function loadScanExample() {
    const lang = $("scan-lang").value;
    if (lang === "pine") {
      const m = await ensureMeta();
      $("scan-code").value = (m && m.pine_note) || "";
      return;
    }
    const m = await ensureMeta();
    $("scan-code").value = (m && m.python_example) || "";
  }

  async function loadSrflipScanExample() {
    $("scan-lang").value = "python";
    const m = await ensureMeta();
    if (m && m.py_srflip_scan_example) {
      $("scan-code").value = m.py_srflip_scan_example;
      showToast("📋 סריקת S/R Flip נטענה — לחץ ▶ הרץ סריקה");
    } else showToast("טעינת הדוגמה נכשלה");
  }

  async function loadBaseboScanExample() {
    $("scan-lang").value = "python";
    const m = await ensureMeta();
    if (m && m.py_basebo_scan_example) {
      $("scan-code").value = m.py_basebo_scan_example;
      showToast("📋 סריקת פריצת בסיס נטענה — לחץ ▶ הרץ סריקה");
    } else showToast("טעינת הדוגמה נכשלה");
  }

  /* ---------------- run ---------------- */
  async function runScan() {
    const code = $("scan-code").value.trim();
    if (!code) { /* Fix: empty-code validation — show a clear error in the scan results area and don't send the request. */ $("scan-summary").textContent = "⚠️ קוד הסריקה ריק — הזן קוד Python או Pine ולחץ שוב על ▶ הרץ סריקה."; $("scan-results").innerHTML = ""; showToast("נא להזין קוד סריקה"); return; }
    const symCount = scanSymbolCount(); /* Fix: warn before scanning large symbol lists (default "All US" = 6654). */ if (symCount != null && symCount > 100 && !confirm("הסריקה תכלול " + symCount + " סמלים ועשויה לקחת זמן רב. להמשיך?")) return; const btn = $("scan-run-btn");
    btn.disabled = true;
    btn.textContent = "⏳ סורק…";
    $("scan-summary").textContent = "סורק… 0%" + (symCount != null ? " (" + symCount + " סימולים)" : "");
    $("scan-results").innerHTML = "";
    $("scan-nav").classList.add("hidden");
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    try {
      // 1. Start the scan — the server returns immediately with a job_id.
      const startRes = await fetch("/api/scan/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: scanSourceObj(),
          interval: $("scan-interval").value,
          language: $("scan-lang").value,
          code: code,
        }),
      });
      const started = await startRes.json();
      if (!startRes.ok) throw new Error(started.detail || "שגיאה בסריקה");
      const jobId = started.job_id;
      if (!jobId) throw new Error("שגיאה בסריקה");
      // 2. Poll the job every 2s until it finishes or fails.
      let job = null, fails = 0;
      for (;;) {
        await sleep(2000);
        try {
          const pr = await fetch("/api/scan/job/" + encodeURIComponent(jobId));
          job = await pr.json();
          if (!pr.ok) throw new Error(job.detail || "שגיאה בסריקה");
        } catch (e) {
          if (++fails > 5) throw e;
          $("scan-summary").textContent = "החיבור נקטע, מנסה שוב…";
          continue;
        }
        fails = 0;
        if (job.status === "done" || job.status === "error") break;
        const pct = Math.round(job.progress || 0);
        $("scan-summary").textContent = "סורק… " + pct + "%" +
          (job.symbol_count ? " (" + job.symbol_count + " סימולים)" : "");
        btn.textContent = "⏳ סורק… " + pct + "%";
        // Streaming: the server publishes partial results per chunk —
        // render them live, then restore the progress line on top.
        if (job.results && Array.isArray(job.results.results) &&
            job.results.results.length) {
          renderScanResults(job.results);
          $("scan-summary").textContent = "סורק… " + pct + "% · נמצאו בינתיים " +
            (job.results.matched || 0) + " תואמים";
          btn.textContent = "⏳ סורק… " + pct + "%";
        }
      }
      if (job.status === "error") throw new Error(job.error || "הסריקה נכשלה");
      renderScanResults(job.results);
    } catch (e) {
      $("scan-summary").textContent = "שגיאה: " + (e && e.message);
      showToast("הסריקה נכשלה");
    }
    btn.disabled = false;
    btn.textContent = "▶ הרץ סריקה";
  }

  function fmtPrice(p) {
    if (p == null || isNaN(p)) return "—";
    p = Number(p);
    if (p >= 1000) return p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (p >= 100) return p.toFixed(2);
    if (p >= 1) return p.toFixed(4);
    return p.toFixed(6);
  }

  function renderScanResults(data) {
    scanResults = data.results || [];
    scanMatches = scanResults.filter(r => r.signal);
    scanNavIdx = 0;
    const miss = (data.missing && data.missing.length) ? " · ללא נתונים: " + data.missing.length : "";
    $("scan-summary").textContent =
      "נסרקו " + data.scanned + " סימולים · נמצאו " + data.matched + " תואמים · " +
      data.elapsed_seconds + " שניות" + miss;
    const nav = $("scan-nav");
    if (scanMatches.length) { nav.classList.remove("hidden"); updateScanCounter(); }
    else nav.classList.add("hidden");
    const box = $("scan-results");
    const rows = scanResults.slice(0, 300).map(r => {
      const chg = (r.change_pct == null || r.change_pct === "") ? "" :
        '<span class="sc-chg ' + (r.change_pct >= 0 ? "up" : "down") + '">' +
        (r.change_pct >= 0 ? "+" : "") + r.change_pct + "%</span>";
      const price = (r.price != null) ? fmtPrice(r.price) : "—";
      const sig = r.signal ? "✅ " : "";
      const boTxt = (r.details && r.details.breakout_price != null)
        ? " · פריצה " + fmtPrice(r.details.breakout_price) : "";
      const note = r.error
        ? '<span class="sc-err">' + esc(r.error) + "</span>"
        : esc((r.note || "") + boTxt);
      const pArg = (r.price != null) ? Number(r.price) : "null";
      return '<div class="sc-row">' + sig +
        '<span class="sc-sym" data-sym="' + esc(r.symbol) + '">' + esc(r.symbol) + "</span>" +
        '<span class="sc-price">' + price + "</span>" + chg +
        '<span class="sc-note">' + note + "</span>" +
        '<button class="tb-btn sc-bell" data-sym="' + esc(r.symbol) + '" data-price="' + pArg +
        '" title="צור התראת מחיר">🔔</button></div>';
    }).join("");
    box.innerHTML = rows || '<div class="sc-note">אין תוצאות</div>';
    if (scanResults.length > 300)
      box.innerHTML += '<div class="sc-note">מוצגות 300 שורות ראשונות מתוך ' + scanResults.length + "</div>";
    box.querySelectorAll(".sc-sym").forEach(el =>
      el.addEventListener("click", () => scanLoadSymbol(el.dataset.sym)));
    box.querySelectorAll(".sc-bell").forEach(el =>
      el.addEventListener("click", () => scanMakeAlert(el.dataset.sym,
        el.dataset.price === "null" ? null : Number(el.dataset.price))));
  }

  function updateScanCounter() {
    $("scan-counter").textContent = scanMatches.length
      ? (scanNavIdx + 1) + "/" + scanMatches.length + " · " + scanMatches[scanNavIdx].symbol : "";
  }

  function scanStep(d) {
    if (!scanMatches.length) return;
    scanNavIdx = (scanNavIdx + d + scanMatches.length) % scanMatches.length;
    updateScanCounter();
    scanLoadSymbol(scanMatches[scanNavIdx].symbol, true);
  }

  function scanLoadSymbol(sym, fromNav) {
    if (typeof pickSymbol === "function") pickSymbol(sym);
    if (!fromNav) {
      const i = scanMatches.findIndex(r => r.symbol === sym);
      if (i >= 0) { scanNavIdx = i; updateScanCounter(); }
    }
  }

  async function scanMakeAlert(sym, price) {
    scanLoadSymbol(sym, true);
    try {
      if (price != null && typeof openAlertAtPrice === "function") await openAlertAtPrice(price);
      else if (typeof openAlertBuilder === "function") await openAlertBuilder();
      showToast("🔔 " + sym + " — עדכן תנאי ולחץ שמור");
    } catch (e) { showToast("פתיחת בונה ההתראות נכשלה"); }
  }

  function scanAddAllToWatchlist() {
    const syms = scanMatches.map(r => r.symbol);
    if (!syms.length) return;
    if (!confirm("להוסיף " + syms.length + " סימולים תואמים לרשימת המעקב?")) return;
    let n = 0;
    syms.forEach(s => { try { addToWatchlist(s); n++; } catch (e) {} });
    showToast("➕ נוספו " + n + " סימולים למעקב");
  }

  /* ---------------- Base Breakout findings (saved scheduled scans) --------
     Shows the latest matches of the saved scans (the daily Base Breakout
     scan) as a list in the terminal, with per-row chart opening (chart +
     Base Breakout indicator overlays incl. zone lines and the volume
     average on the volume pane), a TradingView link, and watchlist
     creation from the findings. */
  let bbFindings = [];
  let bbSeries = [];
  let bbPriceLines = [];
  const BB_PERIOD = { "5m": "5d", "15m": "1mo", "30m": "1mo", "1h": "1mo",
                      "1d": "1y", "1wk": "max", "1mo": "max" };

  async function loadBaseboFindings() {
    const box = $("bb-findings");
    if (!box) return;
    try {
      const scans = await fetch("/api/scans").then(r => r.json());
      bbFindings = [];
      (scans || []).forEach(s => {
        const matched = (s.last_matches || []).filter(Boolean);
        if (!matched.length) return;
        const bySym = {};
        (s.last_results || []).forEach(r => { if (r && r.symbol) bySym[r.symbol] = r; });
        matched.forEach(sym => {
          const r = bySym[sym] || {};
          bbFindings.push({
            symbol: sym, price: r.price, change_pct: r.change_pct,
            note: r.note || "", details: r.details || null,
            interval: s.interval || "1d", scanName: s.name || "",
          });
        });
      });
      renderBaseboFindings();
    } catch (e) {
      box.innerHTML = '<div class="sc-note">טעינת הממצאים נכשלה</div>';
    }
  }

  const BB_CAT_GROUPS = [
    { key: "confirmed", title: "🎯 מאושרות" },
    { key: "retest", title: "🟡 בבדיקת תמיכה / מדשדשות" },
    { key: "fresh", title: "🟢 פריצות טריות" },
    { key: "", title: "📌 ממצאים נוספים" },
  ];
  const BB_CAT_HE = { confirmed: "מאושרת", retest: "בבדיקת תמיכה",
                      fresh: "פריצה טריה" };

  function renderBaseboFindings() {
    const box = $("bb-findings");
    if (!bbFindings.length) {
      box.innerHTML = '<div class="sc-note">אין ממצאים עדיין - הסריקה המתוזמנת תמלא את הרשימה בריצה הבאה</div>';
      return;
    }
    const rowHtml = (f, i) => {
      const chg = (f.change_pct == null || f.change_pct === "") ? "" :
        '<span class="sc-chg ' + (f.change_pct >= 0 ? "up" : "down") + '">' +
        (f.change_pct >= 0 ? "+" : "") + f.change_pct + "%</span>";
      const det = f.details || {};
      const catTag = det.category
        ? '<span class="sc-note">[' + esc(BB_CAT_HE[det.category] || det.category) + "]</span>" : "";
      const bo = det.breakout_price != null
        ? '<span class="sc-note">פריצה: ' + fmtPrice(det.breakout_price) +
          (det.zone_low != null ? " · אזור " + fmtPrice(det.zone_low) + "-" + fmtPrice(det.zone_high) : "") +
          (det.vol_ratio ? " · ווליום ×" + det.vol_ratio : "") + "</span>" : "";
      const tv = "https://www.tradingview.com/chart/?symbol=" + encodeURIComponent(f.symbol);
      return '<div class="sc-row">🎯 ' +
        '<span class="sc-sym" data-bb="' + i + '">' + esc(f.symbol) + "</span>" +
        '<span class="sc-price">' + (f.price != null ? fmtPrice(f.price) : "-") + "</span>" + chg +
        '<span class="sc-note">' + esc(f.interval) + "</span>" + catTag + bo +
        '<button class="tb-btn" data-bbchart="' + i + '" title="פתח גרף עם סימוני פריצת בסיס">📈 גרף</button>' +
        '<a class="tb-btn" href="' + tv + '" target="_blank" rel="noopener" title="TradingView">TV</a></div>';
    };
    let html = "";
    BB_CAT_GROUPS.forEach(g => {
      const idxs = [];
      bbFindings.forEach((f, i) => {
        const cat = (f.details && f.details.category) || "";
        if (cat === g.key) idxs.push(i);
      });
      if (!idxs.length) return;
      html += '<div style="font-weight:bold;padding:10px 4px 2px">' +
        g.title + " (" + idxs.length + ")</div>";
      html += idxs.map(i => rowHtml(bbFindings[i], i)).join("");
    });
    box.innerHTML = html;
    box.querySelectorAll("[data-bb]").forEach(el =>
      el.addEventListener("click", () => openBaseboChart(bbFindings[Number(el.dataset.bb)])));
    box.querySelectorAll("[data-bbchart]").forEach(el =>
      el.addEventListener("click", () => openBaseboChart(bbFindings[Number(el.dataset.bbchart)])));
  }

  function bbClearOverlays() {
    const ch = window.chart;
    bbSeries.forEach(s => { try { ch.removeSeries(s); } catch (e) {} });
    bbSeries = [];
    if (window.candleSeries) bbPriceLines.forEach(pl => {
      try { candleSeries.removePriceLine(pl); } catch (e) {}
    });
    bbPriceLines = [];
  }

  async function openBaseboChart(f) {
    if (!f) return;
    const iv = f.interval || "1d";
    try { if (typeof setTfInterval === "function") setTfInterval(iv); } catch (e) {}
    scanLoadSymbol(f.symbol, true);
    showToast("📈 " + f.symbol + " — טוען סימוני פריצת בסיס…");
    // Wait until the chart shows this symbol's candles, then run the
    // Base Breakout indicator on it (zone lines + markers + VolAvg50).
    const t0 = Date.now();
    while (Date.now() - t0 < 9000) {
      await new Promise(r => setTimeout(r, 400));
      let ready = false;
      try {
        ready = (typeof currentSymbol !== "undefined" && currentSymbol === f.symbol &&
                 typeof lastCandles !== "undefined" && lastCandles && lastCandles.length > 50);
      } catch (e) {}
      if (ready) break;
    }
    try {
      const meta = await ensureMeta();
      const code = meta && meta.py_basebo_indicator_example;
      if (!code) { showToast("קוד האינדיקטור לא נטען"); return; }
      const res = await fetch("/api/py/indicator", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: f.symbol, period: BB_PERIOD[iv] || "1y",
                               interval: iv, code: code }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "שגיאה");
      bbClearOverlays();
      const ch = window.chart;
      const candles = (typeof lastCandles !== "undefined" && lastCandles) || [];
      let hasVol = false;
      (data.plots || []).forEach(p => {
        const opts = { color: p.color || "#2962ff", lineWidth: 1,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false, title: p.name };
        if (p.pane === "vol") { opts.priceScaleId = "vol-pane"; hasVol = true; }
        const ls = ch.addLineSeries(opts);
        const pts = [];
        (p.values || []).forEach((v, i2) => {
          if (v !== null && v !== undefined && candles[i2])
            pts.push({ time: candles[i2].time, value: v });
        });
        ls.setData(pts);
        bbSeries.push(ls);
      });
      if (hasVol) {
        try {
          ch.priceScale("vol-pane").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
          ch.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.28 } });
        } catch (e) {}
      }
      if ((data.markers || []).length && window.candleSeries) {
        try { candleSeries.setMarkers(data.markers); } catch (e) {}
      }
      (data.hlines || []).forEach(h => {
        try {
          bbPriceLines.push(candleSeries.createPriceLine({
            price: h.price, color: h.color || "#f5b301", lineWidth: 1,
            lineStyle: 2, axisLabelVisible: true, title: h.title || "",
          }));
        } catch (e) {}
      });
      showToast("🎯 " + f.symbol + " — סימוני פריצת הבסיס על הגרף");
    } catch (e) {
      showToast("הפעלת סימוני פריצת הבסיס נכשלה: " + e.message);
    }
  }

  /* Named watchlists from findings: stored locally (the app keeps one
     live watchlist; creating a list loads its symbols into it, batched
     through the existing addToWatchlist + sync push). */
  function bbNamedLists() {
    try { return JSON.parse(localStorage.getItem("basebo_watchlists") || "{}"); }
    catch (e) { return {}; }
  }

  function bbRefreshNamedSelect() {
    const sel = $("bb-wl-load");
    if (!sel) return;
    const names = Object.keys(bbNamedLists());
    sel.innerHTML = '<option value="">— רשימות שנוצרו —</option>' +
      names.map(n => '<option value="' + esc(n) + '">' + esc(n) + "</option>").join("");
  }

  function bbSetCurrentWatchlist(syms) {
    try { if (Array.isArray(window.watchlist)) window.watchlist.length = 0; } catch (e) {}
    let n = 0;
    (syms || []).forEach(s => {
      try { addToWatchlist(s); n++; } catch (e) {}
    });
    try { if (typeof refreshWatchlist === "function") refreshWatchlist(); } catch (e) {}
    try { if (typeof schedulePush === "function") schedulePush(); } catch (e) {}
    return n;
  }

  function bbCreateWatchlist() {
    const syms = [...new Set(bbFindings.map(f => f.symbol))];
    if (!syms.length) { showToast("אין ממצאים ליצירת רשימה"); return; }
    const name = "פריצת בסיס - " + new Date().toLocaleDateString("he-IL");
    const all = bbNamedLists();
    all[name] = syms;
    try { localStorage.setItem("basebo_watchlists", JSON.stringify(all)); } catch (e) {}
    bbRefreshNamedSelect();
    const n = bbSetCurrentWatchlist(syms);
    showToast("📋 נוצרה רשימת מעקב '" + name + "' עם " + n + " סימולים");
  }

  function bbAddToCurrentWatchlist() {
    const syms = [...new Set(bbFindings.map(f => f.symbol))];
    if (!syms.length) { showToast("אין ממצאים"); return; }
    let n = 0;
    syms.forEach(s => { try { addToWatchlist(s); n++; } catch (e) {} });
    showToast("➕ נוספו " + n + " סימולים לרשימת המעקב");
  }

  function bbLoadNamedWatchlist() {
    const name = $("bb-wl-load").value;
    if (!name) { showToast("בחר רשימה שנוצרה"); return; }
    const syms = bbNamedLists()[name] || [];
    const n = bbSetCurrentWatchlist(syms);
    showToast("📂 הרשימה '" + name + "' נטענה (" + n + " סימולים)");
  }

  /* ---------------- saved scans ---------------- */
  function currentScanPayload() {
    return {
      name: $("scan-name").value.trim(),
      source: scanSourceObj(),
      interval: $("scan-interval").value,
      language: $("scan-lang").value,
      code: $("scan-code").value,
      schedule_minutes: parseInt($("scan-schedule").value || "0", 10) || 0,
    };
  }

  async function loadSavedScans() {
    try {
      const list = await fetch("/api/scans").then(r => r.json());
      $("scan-saved").innerHTML = '<option value="">— סריקות שמורות —</option>' + (list || []).map(s =>
        '<option value="' + s.id + '">' + esc(s.name) +
        (s.schedule_minutes ? " (כל " + s.schedule_minutes + " דק׳)" : "") +
        (s.active ? "" : " ⏸") + "</option>").join("");
    } catch (e) {}
  }

  async function saveScan() {
    const p = currentScanPayload();
    if (!p.name) { showToast("תן שם לסריקה"); return; }
    if (!p.code.trim()) { showToast("אין קוד לשמירה"); return; }
    try {
      const res = await fetch("/api/scans", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(p),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "שגיאה");
      showToast(p.schedule_minutes
        ? "💾 נשמר — ירוץ כל " + p.schedule_minutes + " דקות + טלגרם על חדשים"
        : "💾 הסריקה נשמרה");
      $("scan-name").value = "";
      loadSavedScans();
    } catch (e) { showToast("השמירה נכשלה: " + e.message); }
  }

  async function loadSavedScanToEditor() {
    const id = $("scan-saved").value;
    if (!id) { showToast("בחר סריקה שמורה"); return; }
    try {
      const s = await fetch("/api/scans/" + id).then(r => r.json());
      $("scan-code").value = s.code || "";
      $("scan-lang").value = s.language || "python";
      $("scan-interval").value = s.interval || "1d";
      $("scan-name").value = s.name || "";
      $("scan-schedule").value = s.schedule_minutes || "";
      const key = s.source.type === "custom" ? "custom:" : "preset:" + s.source.id;
      const srcSel = $("scan-source");
      if ([...srcSel.options].some(o => o.value === key)) srcSel.value = key;
      if (s.source.type === "custom") {
        $("scan-custom").classList.remove("hidden");
        $("scan-custom").value = (s.source.symbols || []).join(", ");
      }
      showToast("📂 הסריקה נטענה לעריכה");
    } catch (e) { showToast("הטעינה נכשלה"); }
  }

  async function runSavedScanNow() {
    const id = $("scan-saved").value;
    if (!id) { showToast("בחר סריקה שמורה"); return; }
    $("scan-summary").textContent = "מריץ סריקה שמורה…";
    $("scan-results").innerHTML = "";
    try {
      const res = await fetch("/api/scans/" + id + "/run", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "שגיאה");
      renderScanResults(data);
      loadBaseboFindings();
    } catch (e) { $("scan-summary").textContent = "שגיאה: " + e.message; }
  }

  async function toggleSavedScan() {
    const id = $("scan-saved").value;
    if (!id) { showToast("בחר סריקה שמורה"); return; }
    try {
      await fetch("/api/scans/" + id + "/toggle", { method: "POST" });
      loadSavedScans();
      showToast("⏯️ מצב הסריקה הוחלף");
    } catch (e) { showToast("הפעולה נכשלה"); }
  }

  async function deleteSavedScan() {
    const id = $("scan-saved").value;
    if (!id) { showToast("בחר סריקה שמורה"); return; }
    if (!confirm("למחוק את הסריקה השמורה?")) return;
    try {
      await fetch("/api/scans/" + id, { method: "DELETE" });
      loadSavedScans();
      showToast("🗑️ נמחק");
    } catch (e) { showToast("המחיקה נכשלה"); }
  }

  /* ---------------- boot ----------------
     Inject synchronously at parse time (this script loads after app.js but
     before DOMContentLoaded): app.js wires .bp-tab clicks in its boot, so
     our tabs must already exist. Data loading is async anyway. */
  inject();
  refreshScanSources();
  loadSavedScans();
  loadBaseboFindings();
  bbRefreshNamedSelect();

  /* Live findings: while the scanner page is on screen, refresh the Base
     Breakout findings every 30s — a running scheduled pass streams its
     matches into the panel in near-real time. */
  let bbAutoBusy = false;
  setInterval(() => {
    const page = $("bp-scanner");
    if (!page || page.classList.contains("hidden")) return;
    if (document.visibilityState && document.visibilityState !== "visible") return;
    if (bbAutoBusy) return;
    bbAutoBusy = true;
    Promise.resolve(loadBaseboFindings()).finally(() => { bbAutoBusy = false; });
  }, 30000);
})();
