"""Standalone charting platform backend (TradingView-style).

Free-tier design:
- Crypto candles/quotes: Coinbase public API -> Kraken fallback (real-time, no key).
- Crypto live stream:  WebSocket proxy to Coinbase WS feed (no key).
- Forex daily: Frankfurter (ECB); intraday FX via yfinance.
- Stocks: yfinance (+ Nasdaq/Stooq/Nasdaq fallbacks could be added later).
- No database in v1: layouts/drawings persist in the browser (localStorage).
- Optional Supabase (free tier): when SUPABASE_URL + SUPABASE_SERVICE_KEY are
  set, alerts/drawings/layouts/watchlists/AI indicators sync per user and the
  alert engine evaluates every user's alerts. Without it, everything keeps
  working locally exactly as before.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import supa
from markets import (
    detect_market,
    fetch_crypto_candles,
    fetch_crypto_quote,
    fetch_fx_candles,
    fetch_fx_quote,
    fetch_orderbook,
    market_lists,
    normalize_symbol,
)
from alerts_engine import AlertStore, SupabaseAlertStorage, recent_triggers
import ai_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("charts")

# Local-mode store (alerts.json). When Supabase is configured, one store
# per user is created on demand (see store_for).
alert_store = AlertStore()


def get_user_id(request: Request) -> str:
    """Resolve the caller's user id.

    - Supabase not configured -> "local" (current behaviour, no login needed).
    - Configured + valid Bearer token -> that user's id (cloud sync).
    - Configured but no/invalid token -> "local" fallback: the app keeps
      working fully without login (alerts stored server-side in alerts.json);
      signing in upgrades to per-user cloud sync.
    """
    if not supa.is_configured():
        return "local"
    auth = request.headers.get("authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    uid = supa.auth_user_id(token) if token else None
    return uid or "local"


def store_for(user_id: str) -> AlertStore:
    if user_id == "local":
        return alert_store
    return AlertStore(user_id=user_id,
                      storage=SupabaseAlertStorage(user_id))


async def _alert_loop():
    """Background evaluation of rule-based alerts every 5 minutes."""
    await asyncio.sleep(60)  # let the server settle first
    while True:
        try:
            if supa.is_configured():
                # Cheap heartbeat: keeps the free Supabase project from
                # pausing after ~7 days of inactivity, even with no alerts.
                supa.heartbeat()
                # Local (not-logged-in) alerts keep being evaluated too.
                try:
                    fired = alert_store.evaluate_all(load_candles)
                    if fired:
                        logger.info("alert loop fired %d local", len(fired))
                except Exception as exc:
                    logger.warning("alert loop local error: %s", exc)
                for uid in supa.alert_user_ids():
                    try:
                        store_for(uid).evaluate_all(load_candles)
                    except Exception as exc:
                        logger.warning("alert loop user %s error: %s", uid, exc)
            else:
                fired = alert_store.evaluate_all(load_candles)
                if fired:
                    logger.info("alert loop fired %d", len(fired))
        except Exception as exc:
            logger.warning("alert loop error: %s", exc)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_alert_loop())
    yield
    task.cancel()


app = FastAPI(title="Charts", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# ----------------------------------------------------------------------------
# Candle cache (memory-bounded, same pattern as trading-alerts)
# ----------------------------------------------------------------------------
_candle_cache: Dict[str, tuple] = {}
_CACHE_TTL = {"1m": 60, "5m": 120, "15m": 300, "30m": 600, "1h": 900}
_DEFAULT_TTL = 1800
_MAX_CACHE_ENTRIES = 300


def _cache_get(key: str, ttl: int):
    item = _candle_cache.get(key)
    if item and time.time() - item[0] < ttl:
        return item[1]
    return None


def _cache_set(key: str, value):
    if len(_candle_cache) >= _MAX_CACHE_ENTRIES:
        oldest = min(_candle_cache, key=lambda k: _candle_cache[k][0])
        del _candle_cache[oldest]
    _candle_cache[key] = (time.time(), value)


_PERIOD_DAYS = {
    "1D": 2, "5D": 7, "1M": 35, "3M": 100, "6M": 200,
    "YTD": 300, "1Y": 400, "5Y": 1900, "ALL": 3650,
}
_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")
_INTRADAY = ("1m", "5m", "15m", "30m", "1h")


def _yf_interval(interval: str) -> str:
    return {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "60m", "1d": "1d", "1wk": "1wk", "1mo": "1mo"}[interval]


def _yfinance_candles(symbol: str, days: int, interval: str) -> Optional[dict]:
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        df = yf.Ticker(symbol).history(
            period=f"{min(days, 730)}d", interval=_yf_interval(interval))
    except Exception as exc:
        logger.warning("yfinance failed for %s: %s", symbol, exc)
        return None
    if df is None or df.empty:
        return None
    candles = []
    for ts, row in df.iterrows():
        try:
            o, h, l, c = (float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if o != o or h != h or l != l or c != c:
            continue
        candle = {"time": int(ts.timestamp()), "open": o, "high": h,
                  "low": l, "close": c}
        try:
            v = float(row["Volume"])
            if v == v and v > 0:
                candle["volume"] = v
        except (KeyError, TypeError, ValueError):
            pass
        candles.append(candle)
    if not candles:
        return None
    candles.sort(key=lambda c: c["time"])
    return {"symbol": symbol, "candles": candles,
            "current_price": candles[-1]["close"], "source": "yahoo"}


def load_candles(symbol: str, period: str, interval: str) -> dict:
    symbol = (symbol or "").strip().upper()
    if interval not in _INTERVALS:
        interval = "1d"
    days = _PERIOD_DAYS.get(period, 400)
    market = detect_market(symbol)
    dsymbol = normalize_symbol(symbol)
    key = f"{dsymbol}:{period}:{interval}"
    ttl = _CACHE_TTL.get(interval, _DEFAULT_TTL)
    cached = _cache_get(key, ttl)
    if cached:
        return cached

    result = None
    if market == "crypto":
        try:
            result = fetch_crypto_candles(dsymbol, days, interval)
        except Exception as exc:
            logger.warning("crypto candles failed %s: %s", dsymbol, exc)
    elif market == "fx" and interval not in _INTRADAY:
        try:
            result = fetch_fx_candles(dsymbol, days, interval)
        except Exception as exc:
            logger.warning("fx candles failed %s: %s", dsymbol, exc)
    if result is None:
        # yfinance covers stocks + intraday FX/crypto fallback
        try:
            result = _yfinance_candles(dsymbol, days, interval)
        except Exception as exc:
            logger.warning("yfinance candles failed %s: %s", dsymbol, exc)

    if not result or not result.get("candles"):
        raise ValueError(f"לא נמצאו נתונים עבור {symbol}")
    _cache_set(key, result)
    return result


def get_quote(symbol: str) -> Optional[float]:
    symbol = (symbol or "").strip().upper()
    market = detect_market(symbol)
    if market == "crypto":
        q = fetch_crypto_quote(symbol)
        if q:
            return q
    elif market == "fx":
        q = fetch_fx_quote(symbol)
        if q:
            return q
    try:
        import yfinance as yf
        info = yf.Ticker(normalize_symbol(symbol)).fast_info
        px = info.get("last_price") or info.get("lastPrice")
        return float(px) if px else None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Symbol search (v1: curated universe, no heavy index)
# ----------------------------------------------------------------------------
_POPULAR_STOCKS = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"),
    ("TSLA", "Tesla"), ("AMZN", "Amazon"), ("GOOGL", "Alphabet"),
    ("META", "Meta"), ("AMD", "AMD"), ("NFLX", "Netflix"),
    ("PLTR", "Palantir"), ("COIN", "Coinbase"), ("MSTR", "MicroStrategy"),
    ("SPY", "S&P 500 ETF"), ("QQQ", "Nasdaq 100 ETF"), ("IWM", "Russell 2000 ETF"),
    ("DIA", "Dow Jones ETF"), ("TLT", "20Y Treasury ETF"), ("GLD", "Gold ETF"),
    ("USO", "Oil ETF"), ("XLF", "Financials ETF"), ("SMH", "Semiconductors ETF"),
]


def _search_universe() -> List[dict]:
    ml = market_lists()
    out = [{"symbol": s, "name": n, "market": "stock"} for s, n in _POPULAR_STOCKS]
    out += [{"symbol": x["symbol"], "name": x["name"], "market": "crypto"}
            for x in ml["crypto"]]
    out += [{"symbol": x["symbol"], "name": x["name"], "market": "fx"}
            for x in ml["fx"]]
    return out


_YAHOO_SEARCH_CACHE: Dict[str, tuple] = {}
_YAHOO_SEARCH_UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}


def _yahoo_search(q: str):
    """Fallback חינמי לחיפוש מניות שלא ברשימה המקומית (ללא מפתח)."""
    now = time.time()
    ent = _YAHOO_SEARCH_CACHE.get(q)
    if ent and now - ent[0] < 300:
        return ent[1]
    out = []
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 12, "newsCount": 0},
            headers=_YAHOO_SEARCH_UA, timeout=10)
        if r.ok:
            for qt in r.json().get("quotes", [])[:12]:
                sym = (qt.get("symbol") or "").upper()
                if not sym:
                    continue
                qtype = (qt.get("quoteType") or "").upper()
                mkt = "crypto" if qtype == "CRYPTOCURRENCY" else \
                      "fx" if qtype == "CURRENCY" else "stock"
                name = qt.get("shortname") or qt.get("longname") or sym
                out.append({"symbol": sym, "name": name, "market": mkt})
    except Exception as exc:
        logger.debug("yahoo search failed for %s: %s", q, exc)
    _YAHOO_SEARCH_CACHE[q] = (now, out)
    return out


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}


@app.get("/api/candles")
def api_candles(symbol: str, period: str = "1Y", interval: str = "1d"):
    try:
        return load_candles(symbol, period, interval)
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@app.get("/api/quote")
def api_quote(symbol: str):
    q = get_quote(symbol)
    if q is None:
        return JSONResponse(status_code=404, content={"error": "אין מחיר"})
    return {"symbol": normalize_symbol(symbol.strip().upper()), "price": q}


@app.get("/api/quotes")
def api_quotes(symbols: str):
    """Batch quotes: /api/quotes?symbols=AAPL,BTC-USD,EURUSD=X"""
    out = []
    for s in (symbols or "").split(",")[:40]:
        s = s.strip()
        if not s:
            continue
        q = get_quote(s)
        out.append({"symbol": normalize_symbol(s.upper()),
                    "price": q, "market": detect_market(s.upper())})
    return {"quotes": out}


@app.get("/api/markets")
def api_markets():
    return market_lists()


@app.get("/api/orderbook")
def api_orderbook(symbol: str):
    """Level 2 visual depth — crypto only (Coinbase -> Kraken, free, no key)."""
    ob = fetch_orderbook(symbol)
    if ob is None:
        return JSONResponse(status_code=404, content={"error": "עומק שוק זמין לקריפטו בלבד"})
    return ob


@app.get("/api/search")
def api_search(q: str):
    q = (q or "").strip().upper()
    if len(q) < 1:
        return {"results": []}
    hits = [x for x in _search_universe()
            if q in x["symbol"] or q in x["name"].upper()][:15]
    # מניות שלא ברשימה המקומית — חיפוש Yahoo חינמי כגיבוי
    if len(hits) < 8:
        for y in _yahoo_search(q):
            if not any(h["symbol"] == y["symbol"] for h in hits):
                hits.append(y)
            if len(hits) >= 15:
                break
    return {"results": hits[:15]}


# ----------------------------------------------------------------------------
# Public config (safe to expose: anon key is meant for the browser)
# ----------------------------------------------------------------------------
@app.get("/api/config")
def api_config():
    return {
        "supabase_url": supa.supabase_url() if supa.is_configured() else "",
        "supabase_anon_key": supa.anon_key() if supa.is_configured() else "",
    }


# ----------------------------------------------------------------------------
# Cloud sync (Supabase): drawings, layouts, watchlists, AI indicators
# ----------------------------------------------------------------------------
@app.get("/api/sync/pull")
def api_sync_pull(request: Request):
    uid = get_user_id(request)
    if uid == "local":
        return {"drawings": [], "layouts": [], "watchlists": [],
                "indicators": [], "alerts": []}
    return {
        "drawings": supa.select("drawings", {"user_id": uid},
                                order="updated_at.desc", limit=500),
        "layouts": supa.select("chart_layouts", {"user_id": uid},
                               order="updated_at.desc", limit=100),
        "watchlists": supa.select("watchlists", {"user_id": uid},
                                  order="updated_at.desc", limit=20),
        "indicators": supa.select("custom_indicators", {"user_id": uid},
                                  order="updated_at.desc", limit=200),
        "alerts": store_for(uid).list(),
    }


@app.post("/api/sync/push")
def api_sync_push(request: Request, payload: dict):
    uid = get_user_id(request)
    if uid == "local":
        return {"ok": True, "synced": False}
    body = payload or {}
    supa.sync_drawings(uid, body.get("drawings"))
    supa.sync_layouts(uid, body.get("layouts"))
    supa.sync_watchlists(uid, body.get("watchlists"))
    supa.sync_indicators(uid, body.get("indicators"))
    return {"ok": True, "synced": True}


# ----------------------------------------------------------------------------
# Rule-based alerts API
# ----------------------------------------------------------------------------
@app.get("/api/alerts/meta")
def api_alerts_meta():
    return AlertStore.meta()


@app.get("/api/alerts")
def api_alerts_list(request: Request):
    return {"alerts": store_for(get_user_id(request)).list()}


@app.post("/api/alerts")
def api_alerts_create(request: Request, payload: dict):
    try:
        return store_for(get_user_id(request)).create(payload or {})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.patch("/api/alerts/{aid}")
@app.put("/api/alerts/{aid}")
def api_alerts_update(aid: str, request: Request, payload: dict):
    try:
        a = store_for(get_user_id(request)).update(aid, payload or {})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if not a:
        return JSONResponse(status_code=404, content={"error": "לא נמצא"})
    return a


@app.delete("/api/alerts/{aid}")
def api_alerts_delete(aid: str, request: Request):
    if store_for(get_user_id(request)).delete(aid):
        return {"ok": True}
    return JSONResponse(status_code=404, content={"error": "לא נמצא"})


@app.get("/api/alerts/triggers")
def api_alerts_triggers(request: Request):
    return {"triggers": list(reversed(recent_triggers(get_user_id(request))))}


@app.post("/api/alerts/evaluate")
def api_alerts_evaluate(request: Request):
    store = store_for(get_user_id(request))
    fired = store.evaluate_all(load_candles)
    return {"fired": fired, "count": len(fired)}


# ----------------------------------------------------------------------------
# AI indicator generator (Hugging Face)
# ----------------------------------------------------------------------------
@app.post("/api/ai/indicator")
def api_ai_indicator(payload: dict):
    prompt = ((payload or {}).get("prompt") or "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "חסר תיאור"})
    try:
        return ai_engine.generate_indicator(prompt)
    except ai_engine.AINotConfigured as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/api/ai/status")
def api_ai_status():
    return {"configured": bool(os.environ.get("HF_API_TOKEN", "").strip()),
            "models": ai_engine.HF_MODELS}


# ----------------------------------------------------------------------------
# Live crypto stream: WebSocket proxy -> Coinbase WS (free, no key)
# ----------------------------------------------------------------------------
@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        params = dict(websocket.query_params)
        symbol = normalize_symbol(params.get("symbol", "BTC-USD"))
        if detect_market(symbol) != "crypto":
            await websocket.send_json({"error": "live stream supports crypto only"})
            await websocket.close()
            return
        product = symbol
        import websockets as ws_lib
        async with ws_lib.connect("wss://ws-feed.exchange.coinbase.com",
                                  ping_interval=20) as upstream:
            await upstream.send(json.dumps({
                "type": "subscribe",
                "product_ids": [product],
                "channels": ["ticker"],
            }))
            while True:
                try:
                    raw = await asyncio.wait_for(upstream.recv(), timeout=30)
                except asyncio.TimeoutError:
                    await upstream.ping()
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") != "ticker":
                    continue
                try:
                    price = float(msg["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                await websocket.send_json({
                    "symbol": product, "price": price,
                    "time": msg.get("time"),
                })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("ws stream error: %s", exc)
        try:
            await websocket.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Static frontend
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def serve_index():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
