"""Visual AI chart verification — free-providers-only cascade.

Tries, in order:
  1. Gemini        gemini-2.5-flash, gemini-2.0-flash        (GEMINI_API_KEY)
  2. Z.AI          glm-4.6v-flash  (permanently free vision)  (ZAI_API_KEY)
  3. Groq          llama-4-scout-17b vision, qwen3.6-27b      (GROQ_API_KEY)
  4. OpenRouter    gemma-4-26b-a4b-it:free, gemma-4-31b-it    (OPENROUTER_API_KEY)
  5. Pollinations  openai (keyless, last resort)

Only free tiers / free models. No billing, no paid models.
Returns per-provider errors so failures are diagnosable.
"""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request

TIMEOUT = 30  # seconds per single attempt

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


# ---------------------------------------------------------------- prompt

def build_prompt(symbol, timeframe, entry_price, direction):
    return f"""You are a professional technical analyst. Analyze the attached stock chart image.

Trade setup:
- Symbol: {symbol}
- Timeframe: {timeframe}
- Entry price: {entry_price}
- Direction: {direction} (long = betting price goes UP from entry, short = betting price goes DOWN from entry)

Look at the chart: trend, support/resistance zones, chart patterns, candlestick signals,
volume if visible, and whether price action supports the trade direction from the entry price.

Reply with ONLY a JSON object (no markdown, no extra text) with exactly these fields:
{{
  "verdict": "confirm" | "warn" | "reject",
  "confidence": <0-100 integer>,
  "reasons": ["<short reason 1>", "<short reason 2>", ...],
  "invalidation": <price number where the setup is proven wrong, or null>,
  "risks": ["<short risk 1>", ...],
  "visual_evidence": ["<what you see on the chart 1>", ...]
}}

Rules:
- "confirm" only if the chart clearly supports the direction from the entry price.
- "warn" if mixed signals.
- "reject" if the chart contradicts the direction.
- confidence must reflect how clear the chart signals are.
- invalidation must be a realistic price level visible on/near the chart, or null if none.
- Keep every string under 120 characters."""


# ---------------------------------------------------------------- helpers

def _downscale_image(image_bytes, max_side=1024):
    """Downscale to keep base64 payloads small (Groq limit 4MB base64)."""
    if not _HAS_PIL:
        return image_bytes, "image/png"
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/png"


def _http_post(url, payload, headers, timeout=TIMEOUT):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _extract_json(text):
    """Pull the first {...} JSON object out of model output."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _normalize(obj):
    """Validate / coerce the model JSON into our contract."""
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict", "")).lower().strip()
    if verdict not in ("confirm", "warn", "reject"):
        return None
    try:
        conf = int(obj.get("confidence", 0))
    except Exception:
        conf = 0
    conf = max(0, min(100, conf))

    def _strs(v):
        if not isinstance(v, list):
            return []
        return [str(x)[:140] for x in v[:6]]

    inv = obj.get("invalidation")
    try:
        inv = float(inv) if inv is not None else None
    except Exception:
        inv = None
    return {
        "verdict": verdict,
        "confidence": conf,
        "reasons": _strs(obj.get("reasons")),
        "invalidation": inv,
        "risks": _strs(obj.get("risks")),
        "visual_evidence": _strs(obj.get("visual_evidence")),
    }


# ---------------------------------------------------------------- providers

def _openai_compat_call(base_url, api_key, model, image_b64, mime, prompt, extra_headers=None):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 900,
        "temperature": 0.2,
    }
    return _http_post(url, payload, headers)


def _parse_openai_compat(status, body):
    if status != 200:
        return None, f"http_{status}: {body[:200]}"
    try:
        data = json.loads(body)
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        return None, f"bad_response: {e} / {body[:200]}"
    norm = _normalize(_extract_json(text))
    if not norm:
        return None, f"bad_json: {text[:200]}"
    return norm, None


def _call_gemini(api_key, model, image_b64, mime, prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={api_key}")
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime, "data": image_b64}},
                {"text": prompt},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900},
    }
    status, body = _http_post(url, payload, {"Content-Type": "application/json"})
    if status != 200:
        return None, f"http_{status}: {body[:200]}"
    try:
        data = json.loads(body)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return None, f"bad_response: {e} / {body[:200]}"
    norm = _normalize(_extract_json(text))
    if not norm:
        return None, f"bad_json: {text[:200]}"
    return norm, None


def _call_pollinations(image_b64, mime, prompt):
    # Keyless. POST /openai is OpenAI-compatible.
    url = "https://text.pollinations.ai/openai"
    payload = {
        "model": "openai",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 900,
    }
    status, body = _http_post(url, payload, {"Content-Type": "application/json"})
    return _parse_openai_compat(status, body)


# (name, models, env_key, caller)
# caller(api_key_or_None, model, image_b64, mime, prompt) -> (result|None, error|None)
def _provider_chain():
    return [
        ("gemini",
         ["gemini-2.5-flash", "gemini-2.0-flash"],
         "GEMINI_API_KEY", _call_gemini),
        ("zai",
         ["glm-4.6v-flash"],
         "ZAI_API_KEY",
         lambda key, model, b64, mime, prompt: _parse_openai_compat(*_openai_compat_call(
             "https://open.bigmodel.cn/api/paas/v4", key, model, b64, mime, prompt))),
        ("groq",
         ["meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3.6-27b"],
         "GROQ_API_KEY",
         lambda key, model, b64, mime, prompt: _parse_openai_compat(*_openai_compat_call(
             "https://api.groq.com/openai/v1", key, model, b64, mime, prompt))),
        ("openrouter",
         ["google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free"],
         "OPENROUTER_API_KEY",
         lambda key, model, b64, mime, prompt: _parse_openai_compat(*_openai_compat_call(
             "https://openrouter.ai/api/v1", key, model, b64, mime, prompt,
             extra_headers={"HTTP-Referer": "https://elroi-terminal.onrender.com",
                            "X-Title": "Elroi Terminal"}))),
        ("pollinations",
         ["openai"],
         None,
         lambda _key, model, b64, mime, prompt: _call_pollinations(b64, mime, prompt)),
    ]


# ---------------------------------------------------------------- audit

_AUDIT = []
_AUDIT_MAX = 50
_audit_lock = None


def _get_lock():
    global _audit_lock
    if _audit_lock is None:
        import threading
        _audit_lock = threading.Lock()
    return _audit_lock


def log_verification(entry: dict):
    with _get_lock():
        _AUDIT.append(entry)
        del _AUDIT[:-_AUDIT_MAX]


def get_audit(limit: int = 20):
    with _get_lock():
        items = list(_AUDIT[-limit:])
    return {"verifications": items}


# ---------------------------------------------------------------- cascade

def verify_chart(image_bytes, symbol, timeframe, entry_price, direction):
    """Run the cascade. Returns dict with ok=True + analysis, or ok=False + errors."""
    started = time.time()
    prompt = build_prompt(symbol, timeframe, entry_price, direction)
    img, mime = _downscale_image(image_bytes)
    image_b64 = base64.b64encode(img).decode("ascii")

    errors = {}
    tried = []
    for name, models, env_key, caller in _provider_chain():
        api_key = os.environ.get(env_key) if env_key else None
        if env_key and not api_key:
            errors[name] = "no_api_key"
            continue
        for model in models:
            tried.append(f"{name}/{model}")
            try:
                result, err = caller(api_key, model, image_b64, mime, prompt)
            except Exception as e:
                result, err = None, f"{type(e).__name__}: {e}"
            if result:
                result.update({
                    "ok": True,
                    "provider": name,
                    "model": model,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "entry_price": entry_price,
                    "direction": direction,
                    "elapsed_s": round(time.time() - started, 1),
                })
                return result
            errors[f"{name}/{model}"] = err or "unknown"
    return {
        "ok": False,
        "error": "all AI providers failed",
        "errors": errors,
        "tried": tried,
        "elapsed_s": round(time.time() - started, 1),
    }


def status():
    """Which providers are configured (no secrets exposed)."""
    info = []
    for name, models, env_key, _ in _provider_chain():
        info.append({
            "provider": name,
            "models": models,
            "configured": bool(os.environ.get(env_key)) if env_key else True,
        })
    return {"providers": info}
