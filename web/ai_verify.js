/* AI chart verification — merged into Elroi Terminal.
 * Captures the visible chart, sends it with symbol/timeframe/entry/direction
 * to POST /api/ai/verify, and renders the verdict in a modal.
 * Expects the global Elroi Terminal state: currentSymbol, currentPeriod,
 * currentInterval, showToast. Button #ai-verify-btn wired at the end.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  /* ---------- chart screenshot: composite all LWC canvases ---------- */
  function captureChartB64() {
    const container = $("chart");
    if (!container) return null;
    const canvases = container.querySelectorAll("canvas");
    if (!canvases.length) return null;
    const rect = container.getBoundingClientRect();
    if (rect.width < 50 || rect.height < 50) return null;
    const dpr = window.devicePixelRatio || 1;
    const out = document.createElement("canvas");
    out.width = Math.round(rect.width * dpr);
    out.height = Math.round(rect.height * dpr);
    const ctx = out.getContext("2d");
    const cs = getComputedStyle(document.body);
    ctx.fillStyle = "#131722";
    try { ctx.fillStyle = cs.getPropertyValue("--bg").trim() || "#131722"; } catch (e) {}
    ctx.fillRect(0, 0, out.width, out.height);
    canvases.forEach(cv => {
      try {
        const r = cv.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        ctx.drawImage(cv,
          (r.left - rect.left) * dpr, (r.top - rect.top) * dpr,
          r.width * dpr, r.height * dpr);
      } catch (e) { /* tainted / detached canvas — skip layer */ }
    });
    const url = out.toDataURL("image/jpeg", 0.85);
    return url.split(",")[1] || null;
  }

  function currentTimeframe() {
    try {
      const r = (typeof currentPeriod !== "undefined" ? currentPeriod : "ALL");
      const t = (typeof currentInterval !== "undefined" ? currentInterval : "1d");
      return r + " / " + t;
    } catch (e) { return ""; }
  }

  function currentSym() {
    try { return (typeof currentSymbol !== "undefined" ? currentSymbol : ""); }
    catch (e) { return ""; }
  }

  /* ---------- modal ---------- */
  function ensureModal() {
    if ($("aiv-modal")) return;
    const wrap = document.createElement("div");
    wrap.innerHTML =
      '<div id="aiv-modal" class="aiv-modal hidden">' +
      '  <div class="aiv-card">' +
      '    <div class="aiv-head"><b>🤖 AI — בדיקת גרף</b>' +
      '      <button id="aiv-close" class="tb-btn">✕</button></div>' +
      '    <div class="aiv-row">' +
      '      <label>סימול <input id="aiv-symbol" class="aiv-in"></label>' +
      '      <label>טיים־פריים <input id="aiv-tf" class="aiv-in" readonly></label>' +
      '    </div>' +
      '    <div class="aiv-row">' +
      '      <label>מחיר כניסה <input id="aiv-entry" class="aiv-in" inputmode="decimal" placeholder="למשל 182.4"></label>' +
      '      <label>כיוון <select id="aiv-dir" class="aiv-in">' +
      '        <option value="long">לונג 🟢</option>' +
      '        <option value="short">שורט 🔴</option>' +
      '      </select></label>' +
      '    </div>' +
      '    <div class="aiv-preview"><img id="aiv-img" alt="צילום הגרף"></div>' +
      '    <button id="aiv-run" class="tb-btn aiv-run">🔍 בדוק עם AI</button>' +
      '    <div id="aiv-result" class="aiv-result"></div>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(wrap.firstElementChild);
    $("aiv-close").addEventListener("click", closeModal);
    $("aiv-modal").addEventListener("click", e => {
      if (e.target.id === "aiv-modal") closeModal();
    });
    $("aiv-run").addEventListener("click", runVerify);
  }

  function openModal() {
    ensureModal();
    const b64 = captureChartB64();
    if (!b64) {
      if (window.showToast) showToast("לא הצלחתי לצלם את הגרף — טען גרף קודם");
      return;
    }
    $("aiv-img").src = "data:image/jpeg;base64," + b64;
    $("aiv-img").dataset.b64 = b64;
    $("aiv-symbol").value = currentSym();
    $("aiv-tf").value = currentTimeframe();
    $("aiv-entry").value = "";
    $("aiv-result").innerHTML = "";
    $("aiv-result").className = "aiv-result";
    $("aiv-modal").classList.remove("hidden");
  }

  function closeModal() { $("aiv-modal").classList.add("hidden"); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

 /* מחכה שהגרף הראשי יטען סימול חדש (אחרי pickSymbol) */
function waitForChartSymbol(sym, timeoutMs) {
  const t0 = Date.now();
  return new Promise((resolve) => {
    (function poll() {
      const s = ($("tb-symbol").textContent || "").trim().toUpperCase();
      const price = ($("tb-price").textContent || "").trim();
      const loading = price === "טוען..." || price === "—";
      if (s === sym && !loading) return resolve(true);
      if (Date.now() - t0 > timeoutMs) return resolve(false);
      setTimeout(poll, 400);
    })();
  });
}
 async function runVerify() {
    const btn = $("aiv-run");
    const box = $("aiv-result");
    const entry = parseFloat(($("aiv-entry").value || "").trim());
    if (!isFinite(entry)) {
      box.className = "aiv-result aiv-err";
      box.textContent = "הזן מחיר כניסה מספרי";
      return;
    }
      box.className = "aiv-result aiv-err";
      box.textContent = "הזן סימול";
      return;
    }
    const sym = ($("aiv-symbol").value || "").trim().toUpperCase(); if (!sym) { box.textContent = "הזן סימול"; return; } if (typeof pickSymbol === "function" && sym !== currentSym()) { btn.disabled = true; try { pickSymbol(sym); } catch (e) {} const loaded = await waitForChartSymbol(sym, 30000); btn.disabled = false; if (!loaded) { box.textContent = "הגרף לא נטען"; return; } const b64 = captureChartB64(); if (!b64) { box.textContent = "לא הצלחתי לצלם"; return; } $("aiv-img").src = "data:image/jpeg;base64," + b64; $("aiv-img").dataset.b64 = b64; $("aiv-tf").value = currentTimeframe(); } if (typeof pickSymbol === "function" && sym !== currentSym()) { btn.disabled = true; try { pickSymbol(sym); } catch (e) {} const loaded = await waitForChartSymbol(sym, 30000); btn.disabled = false; if (!loaded) { box.textContent = "הגרף לא נטען"; return; } const b64 = captureChartB64(); if (!b64) { box.textContent = "לא הצלחתי לצלם"; return; } $("aiv-img").src = "data:image/jpeg;base64," + b64; $("aiv-img").dataset.b64 = b64; $("aiv-tf").value = currentTimeframe(); }    const payload = {
      symbol: sym,
      timeframe: ($("aiv-tf").value || "").trim(),
      entry_price: entry,
      direction: $("aiv-dir").value,
      image_b64: $("aiv-img").dataset.b64,
    };
    btn.disabled = true;
    btn.textContent = "⏳ ה-AI בודק את הגרף…";
    box.className = "aiv-result";
    box.innerHTML = '<div class="aiv-spin">מנתח את הגרף…</div>';
    let data = null, errMsg = "";
    for (let attempt = 0; attempt < 2 && !data; attempt++) {
      if (attempt > 0) {
        box.innerHTML = '<div class="aiv-spin">החיבור נקטע, מנסה שוב…</div>';
        await new Promise(r => setTimeout(r, 2500));
      }
      try {
        const res = await fetch("/api/ai/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const j = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(j.detail || ("שגיאת שרת " + res.status));
        data = j;
      } catch (e) { errMsg = e.message || "שגיאת רשת"; }
    }
    btn.disabled = false;
    btn.textContent = "🔍 בדוק עם AI";
    if (!data) {
      box.className = "aiv-result aiv-err";
      box.textContent = "הבדיקה נכשלה: " + errMsg;
      return;
    }
    renderResult(box, data);
  }

  function verdictBadge(v) {
    v = String(v || "").toLowerCase();
    if (v === "bullish" || v === "buy" || v === "long")
      return '<span class="aiv-badge aiv-bull">🟢 שורי</span>';
    if (v === "bearish" || v === "sell" || v === "short")
      return '<span class="aiv-badge aiv-bear">🔴 דובי</span>';
    return '<span class="aiv-badge aiv-flat">⚪ ניטרלי</span>';
  }

  function renderResult(box, d) {
    const conf = d.confidence != null ? Math.round(Number(d.confidence) * 100) : null;
    const reasons = (d.reasons || []).map(r => "<li>" + esc(r) + "</li>").join("");
    const risks = (d.risks || []).map(r => "<li>" + esc(r) + "</li>").join("");
    const ev = (d.visual_evidence || []).map(r => "<li>" + esc(r) + "</li>").join("");
    box.className = "aiv-result";
    box.innerHTML =
      '<div class="aiv-verdict">' + verdictBadge(d.verdict) +
      (conf != null ? ' <span class="aiv-conf">ביטחון: ' + conf + '%</span>' : "") +
      (d.provider ? ' <span class="aiv-prov">דרך ' + esc(d.provider) + '</span>' : "") +
      "</div>" +
      (reasons ? "<b>נימוקים:</b><ul>" + reasons + "</ul>" : "") +
      (d.invalidation ? "<b>פסילה:</b> " + esc(d.invalidation) + "<br>" : "") +
      (risks ? "<b>סיכונים:</b><ul>" + risks + "</ul>" : "") +
      (ev ? '<b>מה נראה בגרף:</b><ul class="aiv-ev">' + ev + "</ul>" : "");
  }

  /* ---------- wire the toolbar button ---------- */
  function wire() {
    const btn = $("ai-verify-btn");
    if (btn && !btn._aivWired) {
      btn._aivWired = true;
      btn.addEventListener("click", openModal);
    }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
