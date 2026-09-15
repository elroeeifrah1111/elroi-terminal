"""Supabase helper (free tier) for Elroi Terminal.

Design notes:
- No extra dependency: talks to Supabase over plain HTTPS with `requests`
  (already in requirements.txt) instead of the official supabase-py client.
  This keeps the free-tier deploy light and avoids a heavy dependency tree.
- The server always uses the SERVICE key (bypasses RLS); the browser only
  ever sees the ANON key and uses it solely for auth (magic link).
- Everything degrades gracefully: when SUPABASE_URL / SUPABASE_SERVICE_KEY
  are missing, is_configured() is False and the app keeps its current
  localStorage + alerts.json behaviour.
"""

import logging
import os
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("charts.supa")

_TIMEOUT = 15


def supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def anon_key() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "").strip()


def service_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_KEY", "").strip()


def is_configured() -> bool:
    """True only when URL + service key are present."""
    return bool(supabase_url() and service_key())


def _auth_headers() -> dict:
    return {"apikey": anon_key()}


def _svc_headers(prefer: str = "return=representation") -> dict:
    key = service_key()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def auth_user_id(token: str) -> Optional[str]:
    """Validate a Supabase JWT (from the browser) via /auth/v1/user.

    Returns the user's id, or None when Supabase isn't configured,
    the token is missing/invalid, or the call fails.
    """
    token = (token or "").strip()
    if not is_configured() or not token:
        return None
    try:
        r = requests.get(
            f"{supabase_url()}/auth/v1/user",
            headers={**_auth_headers(),
                     "Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json().get("id")
        logger.warning("supabase auth check -> %s", r.status_code)
    except Exception as exc:
        logger.warning("supabase auth check failed: %s", exc)
    return None


# ----------------------------------------------------------------------------
# PostgREST helpers (service key)
# ----------------------------------------------------------------------------

def _rest_url(table: str) -> str:
    return f"{supabase_url()}/rest/v1/{table}"


def select(table: str, filters: Optional[Dict[str, str]] = None,
           order: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
    """Simple select; filters are {column: value} translated to eq. predicates."""
    if not is_configured():
        return []
    params = {}
    for col, val in (filters or {}).items():
        params[col] = f"eq.{val}"
    if order:
        params["order"] = order
    if limit:
        params["limit"] = str(limit)
    params["select"] = "*"
    try:
        r = requests.get(_rest_url(table), headers=_svc_headers(prefer=""),
                         params=params, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
        logger.warning("supabase select %s -> %s %s", table, r.status_code,
                       r.text[:200])
    except Exception as exc:
        logger.warning("supabase select %s failed: %s", table, exc)
    return []


def insert_rows(table: str, rows: List[dict]) -> List[dict]:
    if not is_configured() or not rows:
        return []
    try:
        r = requests.post(_rest_url(table), headers=_svc_headers(),
                          json=rows, timeout=_TIMEOUT)
        if r.status_code in (200, 201):
            data = r.json()
            return data if isinstance(data, list) else []
        logger.warning("supabase insert %s -> %s %s", table, r.status_code,
                       r.text[:200])
    except Exception as exc:
        logger.warning("supabase insert %s failed: %s", table, exc)
    return []


def upsert_rows(table: str, rows: List[dict], on_conflict: str) -> List[dict]:
    """Upsert using a unique constraint named by on_conflict (comma cols)."""
    if not is_configured() or not rows:
        return []
    try:
        r = requests.post(
            _rest_url(table),
            headers=_svc_headers("resolution=merge-duplicates,return=representation"),
            params={"on_conflict": on_conflict},
            json=rows, timeout=_TIMEOUT)
        if r.status_code in (200, 201):
            data = r.json()
            return data if isinstance(data, list) else []
        logger.warning("supabase upsert %s -> %s %s", table, r.status_code,
                       r.text[:200])
    except Exception as exc:
        logger.warning("supabase upsert %s failed: %s", table, exc)
    return []


def delete_rows(table: str, filters: Dict[str, str]) -> bool:
    if not is_configured():
        return False
    params = {col: f"eq.{val}" for col, val in filters.items()}
    try:
        r = requests.delete(_rest_url(table), headers=_svc_headers(prefer=""),
                            params=params, timeout=_TIMEOUT)
        if r.status_code in (200, 204):
            return True
        logger.warning("supabase delete %s -> %s %s", table, r.status_code,
                       r.text[:200])
    except Exception as exc:
        logger.warning("supabase delete %s failed: %s", table, exc)
    return False


# ----------------------------------------------------------------------------
# Sync helpers used by /api/sync/push
# ----------------------------------------------------------------------------

_MAX = {"drawings": 300, "layouts": 50, "watchlists": 20, "indicators": 100}


def _clean_str(v, n: int = 200) -> str:
    return str(v or "")[:n]


def sync_drawings(user_id: str, items: list) -> None:
    rows = []
    for it in (items or [])[:_MAX["drawings"]]:
        if not isinstance(it, dict):
            continue
        symbol = _clean_str(it.get("symbol"), 40).upper()
        if not symbol:
            continue
        rows.append({
            "user_id": user_id,
            "symbol": symbol,
            "timeframe": _clean_str(it.get("timeframe") or "1d", 10),
            "drawings": it.get("drawings") if isinstance(it.get("drawings"), list) else [],
        })
    if rows:
        upsert_rows("drawings", rows, "user_id,symbol,timeframe")


def sync_layouts(user_id: str, items: list) -> None:
    rows = []
    for it in (items or [])[:_MAX["layouts"]]:
        if not isinstance(it, dict):
            continue
        name = _clean_str(it.get("name"), 120)
        if not name:
            continue
        rows.append({
            "user_id": user_id,
            "name": name,
            "layout": it.get("layout") if isinstance(it.get("layout"), dict) else {},
            "is_default": bool(it.get("is_default")),
        })
    delete_rows("chart_layouts", {"user_id": user_id})
    if rows:
        insert_rows("chart_layouts", rows)


def sync_watchlists(user_id: str, items: list) -> None:
    rows = []
    for it in (items or [])[:_MAX["watchlists"]]:
        if not isinstance(it, dict):
            continue
        name = _clean_str(it.get("name") or "default", 120)
        syms = it.get("symbols")
        rows.append({
            "user_id": user_id,
            "name": name,
            "symbols": [str(s)[:40] for s in syms] if isinstance(syms, list) else [],
        })
    delete_rows("watchlists", {"user_id": user_id})
    if rows:
        insert_rows("watchlists", rows)


def sync_indicators(user_id: str, items: list) -> None:
    rows = []
    for it in (items or [])[:_MAX["indicators"]]:
        if not isinstance(it, dict):
            continue
        name = _clean_str(it.get("name"), 120)
        code = it.get("code")
        if not name or not isinstance(code, str) or not code.strip():
            continue
        rows.append({
            "user_id": user_id,
            "name": name,
            "description": _clean_str(it.get("description"), 500),
            "code": code[:200000],
        })
    delete_rows("custom_indicators", {"user_id": user_id})
    if rows:
        insert_rows("custom_indicators", rows)


# ----------------------------------------------------------------------------
# Alert-engine support
# ----------------------------------------------------------------------------

def alert_user_ids() -> List[str]:
    """Distinct user ids that own alerts (for the evaluation loop)."""
    rows = select("alerts", limit=1000)
    seen = []
    for r in rows:
        uid = r.get("user_id")
        if uid and uid not in seen:
            seen.append(uid)
    return seen


def heartbeat() -> bool:
    """Cheap query run on every alert-loop cycle, even with no alerts.

    The Supabase free tier pauses projects after ~7 days without activity;
    this keeps the project 'alive' at zero cost.
    """
    if not is_configured():
        return False
    try:
        r = requests.get(
            _rest_url("alerts"),
            headers=_svc_headers(prefer=""),
            params={"select": "id", "limit": "1"},
            timeout=_TIMEOUT,
        )
        return r.status_code == 200
    except Exception as exc:
        logger.warning("supabase heartbeat failed: %s", exc)
        return False
