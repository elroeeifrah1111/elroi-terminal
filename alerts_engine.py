"""Rule-based alerts engine (server-side, TradingView-style).

Conditions combine:  OPERAND  OPERATOR  OPERAND
  operand  = price | indicator(name, params)
  operator = crosses_above | crosses_down | greater_than | less_than

Plus: change_pct over N bars vs a value.

Frequency: once | once_per_bar | always
Expiry: optional ISO datetime -> auto-deactivate.
Notify: Telegram (env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) + trigger log.

Persistence: alerts.json next to this file (ephemeral on Render free —
same caveat as the rest of the free tier).
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import supa

logger = logging.getLogger("charts.alerts")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(BASE_DIR, "alerts.json")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# ----------------------------------------------------------------------------
# Pure-python indicators (aligned to candle list; None where undefined)
# ----------------------------------------------------------------------------

def _closes(candles: List[dict]) -> List[float]:
    return [float(c["close"]) for c in candles]


def sma(vals: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    k = 2 / (n + 1)
    prev = None
    for i, v in enumerate(vals):
        prev = v if prev is None else v * k + prev * (1 - k)
        if i >= n - 1:
            out[i] = prev
    return out


def rsi(vals: List[float], n: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) < n + 1:
        return out
    gain = loss = 0.0
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        g, l = (d, 0.0) if d > 0 else (0.0, -d)
        if i <= n:
            gain += g
            loss += l
            if i == n:
                gain /= n
                loss /= n
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l) / n
        if i >= n:
            out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def macd(vals: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9):
    line_raw = ema(vals, fast)
    slow_raw = ema(vals, slow)
    line: List[Optional[float]] = [None] * len(vals)
    for i in range(len(vals)):
        if line_raw[i] is not None and slow_raw[i] is not None:
            line[i] = line_raw[i] - slow_raw[i]
    filled = [v if v is not None else 0.0 for v in line]
    sig_raw = ema(filled, signal)
    sig: List[Optional[float]] = [None] * len(vals)
    hist: List[Optional[float]] = [None] * len(vals)
    for i in range(len(vals)):
        if line[i] is not None and i >= slow - 1 + signal - 1:
            sig[i] = sig_raw[i]
            hist[i] = line[i] - sig_raw[i]
    return line, sig, hist


def bollinger(vals: List[float], n: int = 20, mult: float = 2.0):
    mid = sma(vals, n)
    upper: List[Optional[float]] = [None] * len(vals)
    lower: List[Optional[float]] = [None] * len(vals)
    for i in range(len(vals)):
        if mid[i] is None:
            continue
        var = sum((vals[j] - mid[i]) ** 2 for j in range(i - n + 1, i + 1)) / n
        sd = var ** 0.5
        upper[i] = mid[i] + mult * sd
        lower[i] = mid[i] - mult * sd
    return upper, mid, lower


INDICATORS = {
    "sma":      {"label": "SMA", "params": {"n": 20}, "fn": lambda c, p: sma(_closes(c), int(p.get("n", 20)))},
    "ema":      {"label": "EMA", "params": {"n": 50}, "fn": lambda c, p: ema(_closes(c), int(p.get("n", 20)))},
    "rsi":      {"label": "RSI", "params": {"n": 14}, "fn": lambda c, p: rsi(_closes(c), int(p.get("n", 14)))},
    "macd_line":   {"label": "MACD Line", "params": {}, "fn": lambda c, p: macd(_closes(c))[0]},
    "macd_signal": {"label": "MACD Signal", "params": {}, "fn": lambda c, p: macd(_closes(c))[1]},
    "macd_hist":   {"label": "MACD Hist", "params": {}, "fn": lambda c, p: macd(_closes(c))[2]},
    "bb_upper": {"label": "BB Upper", "params": {"n": 20}, "fn": lambda c, p: bollinger(_closes(c), int(p.get("n", 20)), float(p.get("mult", 2)))[0]},
    "bb_mid":   {"label": "BB Basis", "params": {"n": 20}, "fn": lambda c, p: bollinger(_closes(c), int(p.get("n", 20)), float(p.get("mult", 2)))[1]},
    "bb_lower": {"label": "BB Lower", "params": {"n": 20}, "fn": lambda c, p: bollinger(_closes(c), int(p.get("n", 20)), float(p.get("mult", 2)))[2]},
    "vwap":     {"label": "VWAP", "params": {}, "fn": None},  # session-based; not for alerts v1
}

OPERATORS = {
    "crosses_above": "חוצה מעל",
    "crosses_below": "חוצה מתחת",
    "greater_than": "גדול מ־",
    "less_than": "קטן מ־",
}

FREQUENCIES = {
    "once": "פעם אחת",
    "once_per_bar": "פעם לנר",
    "always": "כל בדיקה",
}


# ----------------------------------------------------------------------------
# Condition evaluation
# ----------------------------------------------------------------------------

def _operand_value(operand: dict, candles: List[dict]) -> Optional[float]:
    """Latest value of an operand: {'kind':'price'} or {'kind':'indicator',...}."""
    kind = operand.get("kind", "price")
    if kind == "price":
        return float(candles[-1]["close"])
    if kind == "indicator":
        name = operand.get("name")
        spec = INDICATORS.get(name)
        if not spec or not spec["fn"]:
            return None
        series = spec["fn"](candles, operand.get("params", {}))
        for v in reversed(series):
            if v is not None:
                return float(v)
        return None
    if kind == "value":
        try:
            return float(operand.get("value"))
        except (TypeError, ValueError):
            return None
    return None


def _operand_prev(operand: dict, candles: List[dict]) -> Optional[float]:
    kind = operand.get("kind", "price")
    if kind == "price":
        return float(candles[-2]["close"])
    if kind == "indicator":
        name = operand.get("name")
        spec = INDICATORS.get(name)
        if not spec or not spec["fn"]:
            return None
        series = spec["fn"](candles, operand.get("params", {}))
        seen = 0
        for v in reversed(series):
            if v is not None:
                seen += 1
                if seen == 2:
                    return float(v)
        return None
    if kind == "value":
        try:
            return float(operand.get("value"))
        except (TypeError, ValueError):
            return None
    return None


def eval_condition(cond: dict, candles: List[dict]) -> bool:
    """Single rule -> True/False. cond: {left, operator, right}."""
    if len(candles) < 3:
        return False
    ctype = cond.get("type", "rule")
    if ctype == "change_pct":
        # {type:'change_pct', operator, period, value}
        period = max(1, int(cond.get("period", 1)))
        if len(candles) <= period:
            return False
        now = float(candles[-1]["close"])
        then = float(candles[-1 - period]["close"])
        pct = (now - then) / then * 100 if then else 0
        try:
            val = float(cond.get("value", 0))
        except (TypeError, ValueError):
            return False
        op = cond.get("operator", "greater_than")
        if op == "greater_than":
            return pct > val
        if op == "less_than":
            return pct < val
        return False

    left = _operand_value(cond.get("left", {"kind": "price"}), candles)
    right = _operand_value(cond.get("right", {"kind": "value", "value": 0}), candles)
    if left is None or right is None:
        return False
    op = cond.get("operator", "crosses_above")
    if op == "greater_than":
        return left > right
    if op == "less_than":
        return left < right
    # crossing needs previous bar
    lp = _operand_prev(cond.get("left", {"kind": "price"}), candles)
    rp = _operand_prev(cond.get("right", {"kind": "value", "value": 0}), candles)
    if lp is None or rp is None:
        return False
    if op == "crosses_above":
        return lp <= rp and left > right
    if op == "crosses_below":
        return lp >= rp and left < right
    return False


def describe_condition(cond: dict) -> str:
    def op_str(o):
        k = o.get("kind", "price")
        if k == "price":
            return "מחיר"
        if k == "value":
            return str(o.get("value", ""))
        if k == "indicator":
            label = INDICATORS.get(o.get("name"), {}).get("label", o.get("name"))
            p = o.get("params", {})
            ps = ",".join(f"{k}={v}" for k, v in p.items())
            return f"{label}({ps})" if ps else label
        return k
    if cond.get("type") == "change_pct":
        return (f"שינוי {cond.get('period', 1)} נרות "
                f"{OPERATORS.get(cond.get('operator'), '')} {cond.get('value')}%")
    return (f"{op_str(cond.get('left', {}))} "
            f"{OPERATORS.get(cond.get('operator'), '')} "
            f"{op_str(cond.get('right', {}))}")


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text}, timeout=10)
        return r.status_code == 200
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False


# ----------------------------------------------------------------------------
# Store + evaluation
# ----------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- trigger log (per user, in memory; local mode also persists to file) ----
_TRIGGER_LOGS: Dict[str, List[dict]] = {}


def log_trigger(user_id: str, trig: dict) -> None:
    log = _TRIGGER_LOGS.setdefault(user_id, [])
    log.append(trig)
    del log[:-200]


def recent_triggers(user_id: str, limit: int = 200) -> List[dict]:
    return _TRIGGER_LOGS.get(user_id, [])[-limit:]


def seed_triggers(user_id: str, trigs: List[dict]) -> None:
    _TRIGGER_LOGS[user_id] = list(trigs or [])[-200:]


# ----------------------------------------------------------------------------
# Storage backends: JSON file (local mode) vs Supabase (cloud mode)
# ----------------------------------------------------------------------------

class JsonAlertStorage:
    """alerts.json next to this file (ephemeral on Render free)."""

    def __init__(self, path: str = STORE_PATH):
        self.path = path

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return {"alerts": data.get("alerts", []),
                    "triggers": data.get("triggers", [])[-200:]}
        except (FileNotFoundError, ValueError):
            return {"alerts": [], "triggers": []}

    def save(self, user_id: str, alerts: List[dict],
             triggers: List[dict]) -> List[dict]:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"alerts": alerts, "triggers": triggers[-200:]},
                          f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("alert store save failed: %s", exc)
        return alerts


def _market_of(symbol: str) -> str:
    try:
        from markets import detect_market
        return detect_market(symbol or "")
    except Exception:
        return ""


def _row_to_alert(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "symbol": row.get("symbol") or "",
        "name": row.get("name") or "",
        "condition": row.get("rule") or {},
        "frequency": row.get("frequency") or "once",
        "expires_at": row.get("expires_at"),
        "active": bool(row.get("active", True)),
        "created_at": row.get("created_at"),
        "last_trigger": row.get("last_triggered_at"),
        "last_bar": row.get("last_bar"),
        "trigger_count": row.get("trigger_count") or 0,
    }


def _alert_to_row(alert: dict, user_id: str) -> dict:
    row = {
        "user_id": user_id,
        "name": str(alert.get("name") or "")[:200],
        "symbol": str(alert.get("symbol") or "")[:40],
        "market": _market_of(alert.get("symbol") or ""),
        "rule": alert.get("condition") or {},
        "frequency": alert.get("frequency") or "once",
        "expires_at": alert.get("expires_at"),
        "active": bool(alert.get("active", True)),
        "last_triggered_at": alert.get("last_trigger"),
        "last_bar": alert.get("last_bar"),
        "trigger_count": int(alert.get("trigger_count") or 0),
    }
    aid = alert.get("id")
    if aid and _UUID_RE.match(str(aid)):
        row["id"] = str(aid)
    # created_at is DB-managed on insert
    return row


class SupabaseAlertStorage:
    """Alerts table in Supabase (persistent across deploys/restarts)."""

    def __init__(self, user_id: str):
        self.user_id = user_id

    def load(self) -> dict:
        rows = supa.select("alerts", {"user_id": self.user_id},
                           order="created_at.desc", limit=500)
        return {"alerts": [_row_to_alert(r) for r in rows], "triggers": []}

    def save(self, user_id: str, alerts: List[dict],
             triggers: List[dict]) -> List[dict]:
        # Replace-all is simple and safe for a single user's alert list.
        supa.delete_rows("alerts", {"user_id": user_id})
        rows = [_alert_to_row(a, user_id) for a in alerts]
        if rows:
            inserted = supa.insert_rows("alerts", rows)
            if inserted:
                return [_row_to_alert(r) for r in inserted]
            logger.warning("supabase alert save returned nothing; "
                           "keeping in-memory copy")
        return alerts


class AlertStore:
    def __init__(self, user_id: str = "local", storage=None):
        self.user_id = user_id
        self.storage = storage or JsonAlertStorage()
        self.alerts: List[dict] = []
        self.load()

    # ---- persistence ----
    def load(self):
        data = self.storage.load() or {}
        self.alerts = data.get("alerts", [])
        seed_triggers(self.user_id, data.get("triggers", []))

    def _persist(self):
        self.alerts = (self.storage.save(
            self.user_id, self.alerts, recent_triggers(self.user_id)) or [])

    # Backwards-compatible alias (local mode used store.save()).
    def save(self):
        self._persist()

    # ---- CRUD ----
    def validate_condition(self, cond: dict) -> Optional[str]:
        if cond.get("type") == "change_pct":
            if cond.get("operator") not in ("greater_than", "less_than"):
                return "operator לא תקין לשינוי באחוזים"
            try:
                float(cond.get("value", "x"))
            except (TypeError, ValueError):
                return "value חייב להיות מספר"
            return None
        for side in ("left", "right"):
            op = cond.get(side, {})
            k = op.get("kind", "price")
            if k == "indicator":
                spec = INDICATORS.get(op.get("name"))
                if not spec or not spec["fn"]:
                    return f"אינדיקטור לא נתמך: {op.get('name')}"
            elif k == "value":
                try:
                    float(op.get("value", "x"))
                except (TypeError, ValueError):
                    return "ערך חייב להיות מספר"
            elif k != "price":
                return f"סוג אופרנד לא תקין: {k}"
        if cond.get("operator") not in OPERATORS:
            return "operator לא תקין"
        return None

    def create(self, data: dict) -> dict:
        cond = data.get("condition") or {}
        err = self.validate_condition(cond)
        if err:
            raise ValueError(err)
        freq = data.get("frequency", "once")
        if freq not in FREQUENCIES:
            raise ValueError("frequency לא תקין")
        alert = {
            "id": f"al_{int(time.time() * 1000)}_{len(self.alerts)}",
            "symbol": str(data.get("symbol", "")).strip().upper(),
            "name": str(data.get("name", "") or describe_condition(cond))[:80],
            "condition": cond,
            "frequency": freq,
            "expires_at": data.get("expires_at"),
            "active": True,
            "created_at": _now_iso(),
            "last_trigger": None,
            "last_bar": None,
            "trigger_count": 0,
        }
        if not alert["symbol"]:
            raise ValueError("חסר סימול")
        self.alerts.append(alert)
        self._persist()
        return self.alerts[-1] if self.alerts else alert

    def update(self, aid: str, data: dict) -> Optional[dict]:
        a = self.get(aid)
        if not a:
            return None
        if "active" in data:
            a["active"] = bool(data["active"])
        if "name" in data:
            a["name"] = str(data["name"])[:80]
        if "condition" in data:
            err = self.validate_condition(data["condition"])
            if err:
                raise ValueError(err)
            a["condition"] = data["condition"]
        if "frequency" in data:
            if data["frequency"] not in FREQUENCIES:
                raise ValueError("frequency לא תקין")
            a["frequency"] = data["frequency"]
        if "expires_at" in data:
            a["expires_at"] = data["expires_at"]
        self._persist()
        return a

    def delete(self, aid: str) -> bool:
        n = len(self.alerts)
        self.alerts = [a for a in self.alerts if a["id"] != aid]
        if len(self.alerts) != n:
            self._persist()
            return True
        return False

    def get(self, aid: str) -> Optional[dict]:
        return next((a for a in self.alerts if a["id"] == aid), None)

    def list(self) -> List[dict]:
        return sorted(self.alerts, key=lambda a: a["created_at"], reverse=True)

    # ---- evaluation ----
    def evaluate_all(self, fetch_candles: Callable) -> List[dict]:
        """Run one pass over active alerts. Returns triggered alerts."""
        fired = []
        now = datetime.now(timezone.utc)
        changed = False
        for a in self.alerts:
            if not a.get("active"):
                continue
            exp = a.get("expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(exp) < now:
                        a["active"] = False
                        changed = True
                        continue
                except ValueError:
                    pass
            try:
                data = fetch_candles(a["symbol"], "3M", "1d")
            except Exception as exc:
                logger.debug("alert fetch failed %s: %s", a["symbol"], exc)
                continue
            candles = (data or {}).get("candles") or []
            if len(candles) < 3:
                continue
            bar_time = candles[-1]["time"]
            freq = a.get("frequency", "once")
            if freq == "once" and a.get("trigger_count", 0) > 0:
                a["active"] = False
                changed = True
                continue
            if freq == "once_per_bar" and a.get("last_bar") == bar_time:
                continue
            try:
                hit = eval_condition(a["condition"], candles)
            except Exception as exc:
                logger.warning("alert eval failed %s: %s", a["id"], exc)
                continue
            if not hit:
                continue
            # ---- trigger ----
            a["trigger_count"] = a.get("trigger_count", 0) + 1
            a["last_trigger"] = _now_iso()
            a["last_bar"] = bar_time
            if freq == "once":
                a["active"] = False
            price = candles[-1]["close"]
            trig = {
                "alert_id": a["id"], "symbol": a["symbol"], "name": a["name"],
                "condition": describe_condition(a["condition"]),
                "price": price, "time": a["last_trigger"],
            }
            log_trigger(self.user_id, trig)
            text = (f"🔔 התראה: {a['name']}\n"
                    f"{a['symbol']} — {describe_condition(a['condition'])}\n"
                    f"מחיר: {price}")
            sent = send_telegram(text)
            trig["telegram_sent"] = sent
            fired.append(trig)
            changed = True
            logger.info("alert fired: %s %s @ %s (tg=%s)", a["id"], a["symbol"], price, sent)
        if changed:
            self._persist()
        return fired

    @staticmethod
    def meta() -> dict:
        return {
            "indicators": {k: {"label": v["label"], "params": v["params"]}
                           for k, v in INDICATORS.items() if v["fn"]},
            "operators": OPERATORS,
            "frequencies": FREQUENCIES,
        }
