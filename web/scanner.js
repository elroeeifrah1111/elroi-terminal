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
      '    <option value="1d" selected>יום</option><option value="1wk">שבוע</option>' +
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
      '<div id="scan-results" class="sc-results"></div>';
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
        `<option value="preset:${esc(l.id)}">${esc(l.label_he || l.label)} (${l.count})</option>`).join("") +
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

  /* ---------------- examples ---------------- */
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
    if (!code) { showToast("כתוב קוד סריקה קודם"); return; }
    const btn = $("scan-run-btn");
    btn.disabled = true;
    btn.textContent = "⏳ סורק…";
    $("scan-summary").textContent = "טוען נתונים ומריץ… (רשימות גדולות לוקחות דקה-שתיים)";
    $("scan-results").innerHTML = "";
    $("scan-nav").classList.add("hidden");
    let data = null, lastErr = null;
    for (let attempt = 0; attempt < 2 && !data; attempt++) {
      if (attempt > 0) {
        $("scan-summary").textContent = "החיבור נקטע, מנסה שוב…";
        await new Promise(r => setTimeout(r, 3000));
      }
      try {
        const res = await fetch("/api/scan/run", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source: scanSourceObj(),
            interval: $("scan-interval").value,
            language: $("scan-lang").value,
            code: code,
          }),
        });
        const j = await res.json();
        if (!res.ok) throw new Error(j.detail || "שגיאה בסריקה");
        data = j;
      } catch (e) { lastErr = e; }
    }
    if (data) renderScanResults(data);
    else {
      $("scan-summary").textContent = "שגיאה: " + (lastErr && lastErr.message);
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
      const note = r.error
        ? '<span class="sc-err">' + esc(r.error) + "</span>"
        : esc(r.note || "");
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
})();
