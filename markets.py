"""Multi-market data layer: crypto + forex via free providers.

- Crypto: Coinbase Exchange public API (free, no key, real-time) -> Kraken fallback.
- Forex:  Frankfurter (ECB daily rates, free) for daily/weekly/monthly candles.
          Intraday forex goes through yfinance (EURUSD=X) in main.py.
- Stocks: untouched — handled by the existing providers in main.py.

All fetchers return the same dict shape as the stock fallbacks:
    {"symbol": ..., "candles": [{"time","open","high","low","close"}], "current_price": ..., "source": ...}
or None on failure. Memory-bounded: candle counts are capped.
"""

import logging
import time
from calendar import timegm
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("trading-alerts.markets")

_UA = {"User-Agent": "trading-alerts/1.0"}

FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "SEK", "NOK", "MXN", "ZAR", "SGD", "HKD",
}

CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "TRX", "LINK",
    "AVAX", "XLM", "LTC", "DOT", "NEAR", "UNI", "ATOM", "APT",
    "ARB", "OP", "INJ", "SUI", "AAVE", "MKR", "CRV", "GRT",
    "SAND", "MANA", "FIL", "HBAR", "VET", "ALGO", "ICP", "FET",
    "RENDER", "JASMY", "BONK", "WIF", "PYTH", "JUP", "ONDO", "SEI",
}
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")

TOP_CRYPTO = [
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
    ("XRP-USD", "Ripple"), ("DOGE-USD", "Dogecoin"), ("ADA-USD", "Cardano"),
    ("TRX-USD", "Tron"), ("LINK-USD", "Chainlink"), ("AVAX-USD", "Avalanche"),
    ("XLM-USD", "Stellar"), ("LTC-USD", "Litecoin"), ("DOT-USD", "Polkadot"),
]

TOP_FX = [
    ("EURUSD=X", "EUR/USD"), ("GBPUSD=X", "GBP/USD"),
    ("USDJPY=X", "USD/JPY"), ("USDCHF=X", "USD/CHF"),
    ("AUDUSD=X", "AUD/USD"), ("USDCAD=X", "USD/CAD"),
    ("NZDUSD=X", "NZD/USD"), ("EURGBP=X", "EUR/GBP"),
    ("EURJPY=X", "EUR/JPY"), ("GBPJPY=X", "GBP/JPY"),
]


def detect_market(symbol: str) -> str:
    """Classify a (cleaned, uppercased) symbol: 'crypto' | 'fx' | 'stock'."""
    s = (symbol or "").strip().upper()
    if not s:
        return "stock"
    core = s[:-2] if s.endswith("=X") else s
    if s.endswith("=X") and len(core) == 6 and core[:3] in FIAT and core[3:] in FIAT:
        return "fx"
    flat = s.replace("/", "").replace("-", "")
    for q in _CRYPTO_QUOTES:
        if flat.endswith(q) and flat[: -len(q)] in CRYPTO_BASES:
            return "crypto"
    if s in CRYPTO_BASES:
        return "crypto"
    nosep = core.replace("/", "")
    if len(nosep) == 6 and nosep[:3] in FIAT and nosep[3:] in FIAT:
        return "fx"
    return "stock"


def normalize_symbol(symbol: str) -> str:
    """Canonical provider form: 'BTC-USD' for crypto, 'EURUSD=X' for forex."""
    s = (symbol or "").strip().upper()
    market = detect_market(s)
    if market == "crypto":
        base = s
        for q in _CRYPTO_QUOTES:
            if s.endswith(q):
                base = s[: -len(q)]
                break
        base = base.replace("-", "").replace("/", "")
        if not base:
            return s
        return f"{base}-USD"
    if market == "fx":
        core = s[:-2] if s.endswith("=X") else s.replace("/", "")
        return f"{core}=X"
    return s


def market_lists() -> Dict[str, List[Dict[str, str]]]:
    return {
        "crypto": [{"symbol": s, "name": n} for s, n in TOP_CRYPTO],
        "fx": [{"symbol": s, "name": n} for s, n in TOP_FX],
    }


_COINBASE_GRAN = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "1d": 86400, "1wk": 86400, "1mo": 86400,
}
_KRAKEN_INTERVAL = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "1d": 1440, "1wk": 10080, "1mo": 21600,
}
_MAX_CANDLES = 1000


def _to_candles(rows: List[dict]) -> List[dict]:
    out = []
    for r in rows:
        try:
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if o != o or h != h or l != l or c != c:
            continue
        candle = {"time": int(r["time"]), "open": o, "high": h, "low": l, "close": c}
        try:
            v = float(r.get("volume", 0))
            if v == v and v > 0:
                candle["volume"] = v
        except (TypeError, ValueError):
            pass
        out.append(candle)
    out.sort(key=lambda c: c["time"])
    return out


def _coinbase_klines(product: str, gran: int, days: int) -> List[dict]:
    """Newest-first klines pages -> oldest-first candle dicts. Bounded."""
    need = max(50, min(_MAX_CANDLES, int(days * 86400 / gran) + 2))
    url = f"https://api.exchange.coinbase.com/products/{product}/candles"
    rows: List[dict] = []
    end = None
    pages = 0
    while len(rows) < need and pages < 4:
        params = {"granularity": gran, "limit": 300}
        if end is not None:
            params["end"] = end
        resp = requests.get(url, params=params, headers=_UA, timeout=15)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for b in batch:
            rows.append({
                "time": int(b[0]), "low": float(b[1]), "high": float(b[2]),
                "open": float(b[3]), "close": float(b[4]),
                "volume": float(b[5]),
            })
        oldest = int(batch[-1][0])
        end = oldest - 1
        pages += 1
        if len(batch) < 300:
            break
        time.sleep(0.15)
    return rows[:need]


def _kraken_pair(product: str) -> str:
    base = product.split("-")[0]
    if base == "BTC":
        base = "XBT"
    return f"{base}USD"


def _kraken_klines(product: str, interval: str) -> List[dict]:
    mins = _KRAKEN_INTERVAL.get(interval, 1440)
    pair = _kraken_pair(product)
    url = "https://api.kraken.com/0/public/OHLC"
    resp = requests.get(url, params={"pair": pair, "interval": mins}, headers=_UA, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(";".join(payload["error"]))
    result = payload.get("result", {})
    key = next((k for k in result if k != "last"), None)
    if not key:
        return []
    rows = []
    for k in result[key][-_MAX_CANDLES:]:
        row = {
            "time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
        }
        try:
            row["volume"] = float(k[6])
        except (IndexError, TypeError, ValueError):
            pass
        rows.append(row)
    return rows


def _aggregate(candles: List[dict], interval: str) -> List[dict]:
    """Fold daily candles into weekly / monthly."""
    if interval not in ("1wk", "1mo") or not candles:
        return candles
    buckets: Dict[str, dict] = {}
    for c in candles:
        dt = datetime.utcfromtimestamp(c["time"])
        key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}" if interval == "1wk" else f"{dt.year}-{dt.month:02d}"
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"time": c["time"], "open": c["open"], "high": c["high"],
                            "low": c["low"], "close": c["close"],
                            "volume": c.get("volume", 0)}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["time"] = min(b["time"], c["time"])
            b["volume"] = b.get("volume", 0) + c.get("volume", 0)
    result = sorted(buckets.values(), key=lambda c: c["time"])
    for b in result:
        if not b.get("volume"):
            b.pop("volume", None)
    return result


def fetch_crypto_candles(symbol: str, days: int, interval: str) -> Optional[dict]:
    """symbol canonical 'BTC-USD'. Coinbase primary, Kraken fallback."""
    product = normalize_symbol(symbol)
    if detect_market(product) != "crypto":
        return None
    rows: List[dict] = []
    source = "coinbase"
    try:
        gran = _COINBASE_GRAN.get(interval, 86400)
        rows = _coinbase_klines(product, gran, days)
    except Exception as exc:
        logger.warning("Coinbase candles failed for %s: %s", product, exc)
        rows = []
    if not rows:
        try:
            rows = _kraken_klines(product, interval)
            source = "kraken"
        except Exception as exc:
            logger.warning("Kraken candles failed for %s: %s", product, exc)
            return None
    if not rows:
        return None
    candles = _aggregate(_to_candles(rows), interval)
    if not candles:
        return None
    return {
        "symbol": product,
        "candles": candles,
        "current_price": candles[-1]["close"],
        "source": source,
    }


def fetch_crypto_quote(symbol: str) -> Optional[float]:
    product = normalize_symbol(symbol)
    try:
        resp = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker",
            headers=_UA, timeout=10,
        )
        resp.raise_for_status()
        price = float(resp.json().get("price", 0))
        return price or None
    except Exception as exc:
        logger.debug("Coinbase quote failed for %s: %s", product, exc)
        return None


def _frankfurter_range(base: str, quote: str, days: int) -> Optional[dict]:
    end = datetime.utcnow().date()
    start = end - timedelta(days=days + 10)
    url = f"https://api.frankfurter.app/{start}..{end}"
    resp = requests.get(url, params={"from": base, "to": quote}, headers=_UA, timeout=15)
    resp.raise_for_status()
    return resp.json().get("rates", {})


def fetch_fx_candles(symbol: str, days: int, interval: str) -> Optional[dict]:
    """symbol canonical 'EURUSD=X'. Daily-based; intraday returns None."""
    norm = normalize_symbol(symbol)
    if detect_market(norm) != "fx" or interval in ("1m", "5m", "15m", "30m", "1h"):
        return None
    core = norm[:-2]
    base, quote = core[:3], core[3:]
    try:
        rates = _frankfurter_range(base, quote, days)
    except Exception as exc:
        logger.warning("Frankfurter candles failed for %s: %s", norm, exc)
        return None
    if not rates:
        return None
    candles = []
    for day in sorted(rates):
        try:
            ts = timegm(datetime.strptime(day, "%Y-%m-%d").timetuple())
            price = float(rates[day][quote])
        except (KeyError, TypeError, ValueError):
            continue
        candles.append({"time": ts, "open": price, "high": price, "low": price, "close": price})
    candles = _aggregate(candles, interval)
    if not candles:
        return None
    return {
        "symbol": norm,
        "candles": candles,
        "current_price": candles[-1]["close"],
        "source": "frankfurter",
    }


def fetch_fx_quote(symbol: str) -> Optional[float]:
    norm = normalize_symbol(symbol)
    if detect_market(norm) != "fx":
        return None
    core = norm[:-2]
    base, quote = core[:3], core[3:]
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": base, "to": quote}, headers=_UA, timeout=10,
        )
        resp.raise_for_status()
        price = float(resp.json()["rates"][quote])
        return price or None
    except Exception as exc:
        logger.debug("Frankfurter quote failed for %s: %s", norm, exc)
        return None
