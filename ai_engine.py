"""Deterministic templates for well-known indicators (no LLM call).

When the user asks for a standard indicator (RSI, SMA, ...), we return
hand-written, mathematically correct JavaScript — instant, free, and always
right. The LLM is only used for genuinely custom requests.
"""

import re

# (key, hebrew name, aliases regex, default params, builder)
def _num(prompt, default):
    m = re.search(r"(\d+(?:\.\d+)?)", prompt)
    try:
        return float(m.group(1)) if m else default
    except ValueError:
        return default


def _tpl_rsi(n=14):
    n = int(n)
    return f"""// name: RSI {{n}}
function compute(candles) {{
  const n = {n}, out = [];
  let gain = 0, loss = 0;
  for (let i = 1; i < candles.length; i++) {{
    const d = candles[i].close - candles[i-1].close;
    const g = d > 0 ? d : 0, l = d < 0 ? -d : 0;
    if (i <= n) {{ gain += g; loss += l; }}
    else {{ gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n; }}
    if (i >= n) out.push({{ time: candles[i].time, value: loss === 0 ? 100 : 100 - 100 / (1 + gain / loss) }});
  }}
  return {{ overlays: [{{ name: "RSI " + n + " (אוסצילטור)", color: "#7e57c2", width: 1, values: out }}] }};
}}""".replace("{n}", str(n))


def _tpl_sma(n=20):
    n = int(n)
    return f"""// name: SMA {n}
function compute(candles) {{
  const n = {n}, out = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {{
    sum += candles[i].close;
    if (i >= n) sum -= candles[i - n].close;
    if (i >= n - 1) out.push({{ time: candles[i].time, value: sum / n }});
  }}
  return {{ overlays: [{{ name: "SMA " + n, color: "#3b82f6", width: 1, values: out }}] }};
}}"""


def _tpl_ema(n=20):
    n = int(n)
    return f"""// name: EMA {n}
function compute(candles) {{
  const n = {n}, k = 2 / (n + 1), out = [];
  let prev = null;
  for (let i = 0; i < candles.length; i++) {{
    prev = prev === null ? candles[i].close : candles[i].close * k + prev * (1 - k);
    if (i >= n - 1) out.push({{ time: candles[i].time, value: prev }});
  }}
  return {{ overlays: [{{ name: "EMA " + n, color: "#f59e0b", width: 1, values: out }}] }};
}}"""


def _tpl_macd(fast=12, slow=26, sig=9):
    fast, slow, sig = int(fast), int(slow), int(sig)
    return f"""// name: MACD {{f}},{{s}},{{g}}
function compute(candles) {{
  const cl = candles.map(c => c.close);
  function ema(vals, n) {{
    const k = 2 / (n + 1), out = new Array(vals.length).fill(null);
    let p = null;
    for (let i = 0; i < vals.length; i++) {{
      p = p === null ? vals[i] : vals[i] * k + p * (1 - k);
      if (i >= n - 1) out[i] = p;
    }}
    return out;
  }}
  const eF = ema(cl, {fast}), eS = ema(cl, {slow});
  const m = cl.map((_, i) => (eF[i] === null || eS[i] === null) ? null : eF[i] - eS[i]);
  const sg = ema(m.map(v => v === null ? 0 : v), {sig});
  const ml = [], sl = [];
  for (let i = 0; i < candles.length; i++) {{
    if (m[i] === null) continue;
    ml.push({{ time: candles[i].time, value: m[i] }});
    if (sg[i] !== null) sl.push({{ time: candles[i].time, value: sg[i] }});
  }}
  return {{ overlays: [
    {{ name: "MACD (אוסצילטור)", color: "#3b82f6", width: 1, values: ml }},
    {{ name: "Signal", color: "#f59e0b", width: 1, values: sl }},
  ] }};
}}""".replace("{f}", str(fast)).replace("{s}", str(slow)).replace("{g}", str(sig))


def _tpl_bb(n=20, k=2):
    n, k = int(n), float(k)
    return f"""// name: רצועות בולינגר {{n}}
function compute(candles) {{
  const n = {n}, mult = {k}, mid = [], up = [], lo = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {{
    sum += candles[i].close;
    if (i >= n) sum -= candles[i - n].close;
    if (i >= n - 1) {{
      const m = sum / n;
      let v = 0;
      for (let j = i - n + 1; j <= i; j++) v += (candles[j].close - m) * (candles[j].close - m);
      const sd = Math.sqrt(v / n), t = candles[i].time;
      mid.push({{ time: t, value: m }});
      up.push({{ time: t, value: m + mult * sd }});
      lo.push({{ time: t, value: m - mult * sd }});
    }}
  }}
  return {{ overlays: [
    {{ name: "BB mid", color: "#3b82f6", width: 1, values: mid }},
    {{ name: "BB upper", color: "#f59e0b", width: 1, values: up }},
    {{ name: "BB lower", color: "#f59e0b", width: 1, values: lo }},
  ] }};
}}""".replace("{n}", str(n))


def _tpl_atr(n=14):
    n = int(n)
    return f"""// name: ATR {{n}}
function compute(candles) {{
  const n = {n}, out = [];
  let prev = null;
  for (let i = 0; i < candles.length; i++) {{
    const c = candles[i];
    const tr = prev === null ? c.high - c.low :
      Math.max(c.high - c.low, Math.abs(c.high - prev), Math.abs(c.low - prev));
    prev = c.close;
    if (i === n - 1) {{
      let s = 0;
      for (let j = 1; j <= n; j++) {{
        const cj = candles[j], pp = candles[j-1].close;
        s += Math.max(cj.high - cj.low, Math.abs(cj.high - pp), Math.abs(cj.low - pp));
      }}
      out._a = s / n;
    }} else if (i >= n) {{
      out._a = (out._a * (n - 1) + tr) / n;
      out.push({{ time: c.time, value: out._a }});
    }}
  }}
  return {{ overlays: [{{ name: "ATR " + n + " (אוסצילטור)", color: "#22d3ee", width: 1, values: out }}] }};
}}""".replace("{n}", str(n))


def _tpl_stoch(n=14, k=3, d=3):
    n, k, d = int(n), int(k), int(d)
    return f"""// name: סטוקסטיק {{n}},{{k}},{{d}}
function compute(candles) {{
  const n = {n}, kk = {k}, dd = {d};
  const raw = [];
  for (let i = 0; i < candles.length; i++) {{
    if (i < n - 1) {{ raw.push(null); continue; }}
    let hi = -Infinity, lo = Infinity;
    for (let j = i - n + 1; j <= i; j++) {{
      hi = Math.max(hi, candles[j].high); lo = Math.min(lo, candles[j].low);
    }}
    raw.push(hi === lo ? 50 : 100 * (candles[i].close - lo) / (hi - lo));
  }}
  function smaArr(v, p) {{
    const o = new Array(v.length).fill(null);
    let s = 0;
    for (let i = 0; i < v.length; i++) {{
      if (v[i] === null) continue;
      s += v[i];
      const from = Math.max(0, i - p + 1);
      let cnt = 0; for (let j = from; j <= i; j++) if (v[j] !== null) cnt++;
      if (cnt >= p) {{
        let ss = 0; for (let j = i - p + 1; j <= i; j++) ss += v[j];
        o[i] = ss / p;
      }}
    }}
    return o;
  }}
  const kL = smaArr(raw, kk), dL = smaArr(kL.map(v => v), dd);
  const ko = [], ddo = [];
  for (let i = 0; i < candles.length; i++) {{
    if (kL[i] !== null) ko.push({{ time: candles[i].time, value: kL[i] }});
    if (dL[i] !== null) ddo.push({{ time: candles[i].time, value: dL[i] }});
  }}
  return {{ overlays: [
    {{ name: "%K (אוסצילטור)", color: "#3b82f6", width: 1, values: ko }},
    {{ name: "%D (אוסצילטור)", color: "#f59e0b", width: 1, values: ddo }},
  ] }};
}}""".replace("{n}", str(n)).replace("{k}", str(k)).replace("{d}", str(d))


def _tpl_vwap():
    return """// name: VWAP
function compute(candles) {
  const out = [];
  let pv = 0, v = 0, day = null;
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    if (!c.volume) return { overlays: [] };
    const d = new Date(c.time * 1000).getUTCDate();
    if (d !== day) { pv = 0; v = 0; day = d; }
    const tp = (c.high + c.low + c.close) / 3;
    pv += tp * c.volume; v += c.volume;
    out.push({ time: c.time, value: pv / v });
  }
  return { overlays: [{ name: "VWAP", color: "#b71c1c", width: 1, values: out }] };
}"""


def _tpl_donchian(n=20):
    n = int(n)
    return f"""// name: דונצ'יאן {{n}}
function compute(candles) {{
  const n = {n}, up = [], lo = [], mid = [];
  for (let i = 0; i < candles.length; i++) {{
    if (i < n - 1) continue;
    let hi = -Infinity, lw = Infinity;
    for (let j = i - n + 1; j <= i; j++) {{
      hi = Math.max(hi, candles[j].high); lw = Math.min(lw, candles[j].low);
    }}
    const t = candles[i].time;
    up.push({{ time: t, value: hi }}); lo.push({{ time: t, value: lw }});
    mid.push({{ time: t, value: (hi + lw) / 2 }});
  }}
  return {{ overlays: [
    {{ name: "Donchian up", color: "#22c55e", width: 1, values: up }},
    {{ name: "Donchian lo", color: "#ef4444", width: 1, values: lo }},
    {{ name: "Donchian mid", color: "#787b86", width: 1, values: mid }},
  ] }};
}}""".replace("{n}", str(n))


def _tpl_supertrend(n=10, mult=3):
    n, mult = int(n), float(mult)
    return f"""// name: סופרטרנד {{n}},{{m}}
function compute(candles) {{
  const n = {n}, mult = {mult};
  const tr = [], atr = [];
  for (let i = 0; i < candles.length; i++) {{
    const c = candles[i];
    const t = i === 0 ? c.high - c.low :
      Math.max(c.high - c.low, Math.abs(c.high - candles[i-1].close), Math.abs(c.low - candles[i-1].close));
    tr.push(t);
  }}
  let s = 0;
  for (let i = 0; i < tr.length; i++) {{
    s += tr[i];
    if (i >= n) s -= tr[i - n];
    atr.push(i >= n - 1 ? s / n : null);
  }}
  const out = [];
  let dir = 1, st = 0;
  for (let i = 0; i < candles.length; i++) {{
    if (atr[i] === null) continue;
    const c = candles[i], hl2 = (c.high + c.low) / 2;
    const up = hl2 - mult * atr[i], dn = hl2 + mult * atr[i];
    if (i > 0 && atr[i-1] !== null) {{
      if (c.close > st) dir = 1; else if (c.close < st) dir = -1;
    }}
    st = dir === 1 ? up : dn;
    out.push({{ time: c.time, value: st }});
  }}
  return {{ overlays: [{{ name: "Supertrend", color: "#a855f7", width: 2, values: out }}] }};
}}""".replace("{n}", str(n)).replace("{m}", str(mult))


# Order matters: first match wins. \b guards against substring false positives.
KNOWN = [
    ("rsi", re.compile(r"\brsi\b|אר[\s\-]?אס[\s\-]?איי|relative\s*strength", re.I),
     lambda p: ("RSI", _tpl_rsi(_num(p, 14)))),
    ("macd", re.compile(r"\bmacd\b|מקדי", re.I),
     lambda p: ("MACD", _tpl_macd(12, 26, 9))),
    ("bollinger", re.compile(r"\bbollinger\b|\bbb\b|בולינגר|רצועות", re.I),
     lambda p: ("Bollinger", _tpl_bb(20, 2))),
    ("stochastic", re.compile(r"\bstochastic\b|סטוקסטיק", re.I),
     lambda p: ("Stochastic", _tpl_stoch(14, 3, 3))),
    ("supertrend", re.compile(r"\bsupertrend\b|סופר\s*טרנד", re.I),
     lambda p: ("Supertrend", _tpl_supertrend(10, 3))),
    ("vwap", re.compile(r"\bvwap\b", re.I),
     lambda p: ("VWAP", _tpl_vwap())),
    ("donchian", re.compile(r"\bdonchian\b|דונצ'?יאן", re.I),
     lambda p: ("Donchian", _tpl_donchian(int(_num(p, 20))))),
    ("atr", re.compile(r"\batr\b", re.I),
     lambda p: ("ATR", _tpl_atr(_num(p, 14)))),
    ("ema", re.compile(r"\bema\b|אקספוננציאלי|exponential", re.I),
     lambda p: ("EMA", _tpl_ema(_num(p, 20)))),
    ("sma", re.compile(r"\bsma\b|ממוצע נע|moving average|\bma\b", re.I),
     lambda p: ("SMA", _tpl_sma(_num(p, 20)))),
]


def match_known(prompt: str):
    """Returns (name, code) for a standard indicator request, else None."""
    p = (prompt or "").strip()
    if not p:
        return None
    for _key, rx, build in KNOWN:
        if rx.search(p):
            name, code = build(p)
            return {"name": name, "code": code, "model": "builtin"}
    return None


import logging
import os
import re

import requests

logger = logging.getLogger("charts.ai")

HF_MODEL = os.environ.get("HF_MODEL", "Qwen/Qwen3-Coder-30B-A3B-Instruct")
# Ranked fallback: best free code models first. HF_MODEL can override with a
# comma-separated list. The generator tries each in order until one answers.
HF_MODELS = [m.strip() for m in os.environ.get("HF_MODELS", "").split(",") if m.strip()] or [
    HF_MODEL,
    "moonshotai/Kimi-K2-Instruct",
    "Qwen/Qwen2.5-Coder-32B-Instruct",
]
_ROUTER_URL = "https://router.huggingface.co/v1/chat/completions"
# Models that failed this process (not supported by any free provider) are skipped
_DEAD_MODELS: set = set()

SYSTEM_PROMPT = """You are an expert financial indicator developer. You write JavaScript indicator code for a charting platform.

TARGET API (follow strictly):
- Define exactly one function: function compute(candles)
- Input: candles = array of {time (unix seconds), open, high, low, close, volume (number or null)}
- Output: { overlays: [ {name, color, width, values} ] } where values = [{time, value}] aligned 1:1 with input candles by time.
- overlays are drawn on the main price chart.

RULES:
- Pure computation only. No network, no DOM, no storage, no eval, no Function constructor.
- values must be finite numbers. Skip warmup bars (do not pad with nulls).
- Guard short inputs: if candles.length < 60, return { overlays: [] }.
- Colors as hex strings, e.g. "#3b82f6".
- PREFER WELL-KNOWN INDICATORS. Implement one of these unless the user explicitly
  describes different math: SMA, EMA, RSI, MACD, Bollinger Bands, ATR, Stochastic,
  VWAP (needs volume; if volume missing return empty), Donchian, Keltner, Supertrend,
  Parabolic SAR, Ichimoku. If the request is vague ("something good", "smart lines",
  "AI prediction"), implement the closest standard indicator and name it accordingly.
- NO RANDOM-LOOKING OUTPUT. Every overlay must be a smooth, deterministic function
  of price/volume with clear financial meaning. Never plot raw per-bar differences,
  unsmoothed oscillators, or invented wavy lines — the result must look like a
  professional trading indicator, not noise. Smooth with moving averages where needed.
- SCALE: price overlays must track the price scale (values near the candle prices,
  e.g. moving averages, bands). Oscillators must be normalized (e.g. 0-100 for RSI /
  Stochastic) and their name must say they are oscillators.
- At most 3 overlays per indicator. Each overlay gets a distinct, readable color.

EXAMPLE (SMA 20 + SMA 50):
// name: ממוצעים נעים
function compute(candles) {
  function sma(n) {
    const out = [];
    let sum = 0;
    for (let i = 0; i < candles.length; i++) {
      sum += candles[i].close;
      if (i >= n) sum -= candles[i - n].close;
      if (i >= n - 1) out.push({ time: candles[i].time, value: sum / n });
    }
    return out;
  }
  return { overlays: [
    { name: "SMA 20", color: "#3b82f6", width: 1, values: sma(20) },
    { name: "SMA 50", color: "#f59e0b", width: 1, values: sma(50) },
  ] };
}

RESPONSE FORMAT:
- First line: // name: <short name in the user's language>
- Then ONLY the JavaScript code. No explanations, no markdown fences.

EXAMPLE 2 (Bollinger Bands 20, 2):
// name: רצועות בולינגר
function compute(candles) {
  const n = 20, k = 2;
  const mid = [], up = [], lo = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close;
    if (i >= n) sum -= candles[i - n].close;
    if (i >= n - 1) {
      const m = sum / n;
      let v = 0;
      for (let j = i - n + 1; j <= i; j++) v += (candles[j].close - m) * (candles[j].close - m);
      const sd = Math.sqrt(v / n), t = candles[i].time;
      mid.push({ time: t, value: m });
      up.push({ time: t, value: m + k * sd });
      lo.push({ time: t, value: m - k * sd });
    }
  }
  return { overlays: [
    { name: "BB mid", color: "#3b82f6", width: 1, values: mid },
    { name: "BB upper", color: "#f59e0b", width: 1, values: up },
    { name: "BB lower", color: "#f59e0b", width: 1, values: lo },
  ] };
}
"""

_DENY = ["fetch(", "XMLHttpRequest", "eval(", "Function(", "import(",
         "localStorage", "sessionStorage", "document.", "window.",
         "cookie", "postMessage"]


class AINotConfigured(RuntimeError):
    pass


def _token() -> str:
    tok = os.environ.get("HF_API_TOKEN", "").strip()
    if not tok:
        raise AINotConfigured(
            "חסר HF_API_TOKEN — צור טוקן חינמי ב-huggingface.co/settings/tokens והגדר אותו כמשתנה סביבה")
    return tok


def _strip_fences(code: str) -> str:
    code = code.strip()
    m = re.match(r"^```(?:javascript|js)?\s*\n([\s\S]*?)\n```$", code)
    if m:
        return m.group(1).strip()
    return code


def validate_code(code: str) -> str:
    """Returns error message or '' if the code looks safe/valid."""
    if "function compute" not in code:
        return "הקוד לא מכיל function compute"
    low = code.lower()
    for bad in _DENY:
        if bad.lower() in low:
            return f"הקוד מכיל פעולה אסורה: {bad}"
    return ""


def generate_indicator(user_prompt: str, timeout: int = 120) -> dict:
    """Ask the LLM to write an indicator. Returns {name, code, model}.

    Tries HF_MODELS in order (best first); a model that the free tier does
    not serve is skipped automatically for the rest of the process.
    """
    token = _token()
    user_prompt = (user_prompt or "").strip()
    if not user_prompt:
        raise ValueError("תיאור ריק")
    if len(user_prompt) > 2000:
        user_prompt = user_prompt[:2000]

    # Fast path: standard indicator -> deterministic, correct, free, instant.
    known = match_known(user_prompt)
    if known:
        logger.info("AI indicator: builtin template for %r", known["name"])
        return known

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    errors = []
    for model in HF_MODELS:
        if model in _DEAD_MODELS:
            continue
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Write an indicator for this request:\n{user_prompt}"},
            ],
            "max_tokens": 1500,
            "temperature": 0.2,
        }
        try:
            r = requests.post(_ROUTER_URL, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            errors.append(f"{model}: שגיאת תקשורת ({exc})")
            continue

        if r.status_code == 401:
            raise RuntimeError("טוקן Hugging Face לא תקין (401)")
        if r.status_code == 429:
            raise RuntimeError("מכסת החינם של Hugging Face מוצתה זמנית — נסה שוב בעוד דקה")
        if r.status_code in (400, 404, 410, 422, 501, 503):
            # model not served on the free tier -> remember and try the next one
            _DEAD_MODELS.add(model)
            errors.append(f"{model}: לא זמין במסלול החינמי ({r.status_code})")
            logger.warning("HF model unavailable, falling back: %s (%s)", model, r.status_code)
            continue
        if r.status_code >= 400:
            errors.append(f"{model}: שגיאה {r.status_code}")
            continue

        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            errors.append(f"{model}: תשובה לא צפויה")
            continue

        code = _strip_fences(text)
        name = "אינדיקטור AI"
        m = re.match(r"\s*//\s*name\s*:\s*(.+)", code)
        if m:
            name = m.group(1).strip()[:60]

        err = validate_code(code)
        if err:
            errors.append(f"{model}: קוד לא תקין ({err})")
            continue

        return {"name": name, "code": code, "model": model}

    raise RuntimeError("כל המודלים נכשלו: " + "; ".join(errors[:3]))
