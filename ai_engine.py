"""AI indicator generator via Hugging Face (free serverless inference).

Flow: user describes an indicator (Hebrew/English) -> LLM writes JavaScript
conforming to our chart overlay contract -> validated -> embedded on the chart.

Contract the model must follow:
    function compute(candles) -> { overlays: [{name, color, width, values:[{time,value}]}] }

Needs env HF_API_TOKEN (free Hugging Face account). Optional HF_MODEL override.
Free tier = rate limits + occasional cold starts; the client shows progress.
"""

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
- You may implement: SMA, EMA, RSI, MACD, Bollinger, ATR, Stochastic, VWAP (needs volume; if volume missing return empty), Donchian, Keltner, or any custom math the user describes.

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
