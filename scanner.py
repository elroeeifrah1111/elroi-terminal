"""
Scanner engine — run an indicator/strategy scan over a whole ticker list.

Two script languages:
  * python — user code gets a pandas DataFrame `df` (columns:
    time,Open,High,Low,Close,Volume), a vectorized indicator library `ta`,
    plus `pd`, `np`, `SYMBOL`, `INTERVAL`. The script either defines
        def scan(df): ... -> {"signal": bool, "score": float, "note": str}
    or sets a top-level `result = {...}` dict.
  * pine — the existing Pine subset interpreter; a symbol "matches" when the
    script fired at least one alert()/alertcondition().

Security model: user code NEVER runs in the server process. Every symbol is
executed in a short-lived forked child with a wall-clock timeout (the child
is SIGTERM/SIGKILLed on timeout), an address-space cap (RLIMIT_AS) and a
CPU-time cap, stdout/stderr silenced, plus an AST pre-check rejecting blocked
imports, dunder attribute access and dangerous builtins.

Candles are fetched in bulk with yf.download (chunked + threaded), the same
pattern the AI-Scanner uses — one request per ~400 symbols instead of one
per symbol.
"""

from __future__ import annotations

import ast
import concurrent.futures
import logging
import math
import multiprocessing
import os
import pickle
import queue
import re
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

try:
    from pine_engine import PineError as _PineError
    from pine_engine import run_pine as _pine_run
    _pine_import_error: Optional[Exception] = None
except Exception as _e:  # pine engine missing — pine scans report it per symbol
    _pine_run = None
    _PineError = Exception
    _pine_import_error = _e

from strategy_engine import backtest_from_signals

logger = logging.getLogger("scanner")

BATCH_CHUNK = 100          # symbols per yf.download request (memory-safe on 512MB)
DOWNLOAD_WORKERS = 2       # parallel chunk downloads (512MB instance)
PER_SYMBOL_TIMEOUT = 15    # wall-clock seconds per symbol (child is killed after)
SCAN_WORKERS = 2           # concurrent sandbox child processes (512MB instance)
SCAN_CHILD_MEM_HEADROOM = 768 * 1024 * 1024  # extra address space per child
MAX_SYMBOLS_INTRADAY = 2000  # safety cap for intraday batch scans


# =====================================================================
# Vectorized indicator library (pandas) exposed to scan scripts as `ta`
# =====================================================================
def _s(x) -> pd.Series:
    return x if isinstance(x, pd.Series) else pd.Series(x, dtype=float)


class TA:
    """ta.sma(close, 20) etc. — all return pandas Series aligned with df."""

    @staticmethod
    def sma(s, n: int) -> pd.Series:
        return _s(s).rolling(n, min_periods=1).mean()

    @staticmethod
    def ema(s, n: int) -> pd.Series:
        return _s(s).ewm(span=n, adjust=False, min_periods=1).mean()

    @staticmethod
    def rma(s, n: int) -> pd.Series:
        return _s(s).ewm(alpha=1 / n, adjust=False, min_periods=1).mean()

    @staticmethod
    def wma(s, n: int) -> pd.Series:
        s = _s(s)
        w = np.arange(1, n + 1, dtype=float)
        return s.rolling(n).apply(lambda a: float(np.dot(a, w) / w.sum()), raw=True)

    @staticmethod
    def rsi(s, n: int = 14) -> pd.Series:
        s = _s(s)
        d = s.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=1).mean()
        rs = up / dn.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def macd(s, fast: int = 12, slow: int = 26, signal: int = 9):
        m = TA.ema(s, fast) - TA.ema(s, slow)
        sig = m.ewm(span=signal, adjust=False, min_periods=1).mean()
        return m, sig, m - sig

    @staticmethod
    def atr(h, l, c, n: int = 14) -> pd.Series:
        h, l, c = _s(h), _s(l), _s(c)
        pc = c.shift(1)
        tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / n, adjust=False, min_periods=1).mean()

    @staticmethod
    def bbands(s, n: int = 20, mult: float = 2.0):
        mid = TA.sma(s, n)
        sd = _s(s).rolling(n, min_periods=1).std()
        return mid + mult * sd, mid, mid - mult * sd

    @staticmethod
    def stoch(h, l, c, k: int = 14, d: int = 3, smooth: int = 3):
        h, l, c = _s(h), _s(l), _s(c)
        ll = l.rolling(k, min_periods=1).min()
        hh = h.rolling(k, min_periods=1).max()
        rng = (hh - ll).replace(0, np.nan)
        kk = 100 * (c - ll) / rng
        kk = kk.rolling(smooth, min_periods=1).mean()
        return kk, kk.rolling(d, min_periods=1).mean()

    @staticmethod
    def obv(c, v) -> pd.Series:
        c, v = _s(c), _s(v)
        direction = np.sign(c.diff()).fillna(0)
        return (direction * v).cumsum()

    @staticmethod
    def roc(s, n: int = 10) -> pd.Series:
        s = _s(s)
        return 100 * (s - s.shift(n)) / s.shift(n).replace(0, np.nan)

    @staticmethod
    def mom(s, n: int = 10) -> pd.Series:
        s = _s(s)
        return s - s.shift(n)

    @staticmethod
    def cci(h, l, c, n: int = 20) -> pd.Series:
        tp = (_s(h) + _s(l) + _s(c)) / 3
        ma = tp.rolling(n, min_periods=1).mean()
        md = (tp - ma).abs().rolling(n, min_periods=1).mean().replace(0, np.nan)
        return (tp - ma) / (0.015 * md)

    @staticmethod
    def mfi(h, l, c, v, n: int = 14) -> pd.Series:
        h, l, c, v = _s(h), _s(l), _s(c), _s(v)
        tp = (h + l + c) / 3
        mf = tp * v
        pos = mf.where(tp > tp.shift(1), 0).rolling(n, min_periods=1).sum()
        neg = mf.where(tp < tp.shift(1), 0).rolling(n, min_periods=1).sum().replace(0, np.nan)
        return 100 - 100 / (1 + pos / neg)

    @staticmethod
    def willr(h, l, c, n: int = 14) -> pd.Series:
        h, l, c = _s(h), _s(l), _s(c)
        hh = h.rolling(n, min_periods=1).max()
        ll = l.rolling(n, min_periods=1).min()
        return -100 * (hh - c) / (hh - ll).replace(0, np.nan)

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        v = df["Volume"].replace(0, np.nan)
        return (tp * v).cumsum() / v.cumsum()

    @staticmethod
    def highest(s, n: int) -> pd.Series:
        return _s(s).rolling(n, min_periods=1).max()

    @staticmethod
    def lowest(s, n: int) -> pd.Series:
        return _s(s).rolling(n, min_periods=1).min()

    @staticmethod
    def stdev(s, n: int) -> pd.Series:
        return _s(s).rolling(n, min_periods=1).std()

    @staticmethod
    def change(s, n: int = 1) -> pd.Series:
        s = _s(s)
        return s - s.shift(n)

    @staticmethod
    def crossover(a, b) -> pd.Series:
        a, b = _s(a), _s(b)
        return (a > b) & (a.shift(1) <= b.shift(1))

    @staticmethod
    def crossunder(a, b) -> pd.Series:
        a, b = _s(a), _s(b)
        return (a < b) & (a.shift(1) >= b.shift(1))

    @staticmethod
    def rising(s, n: int = 1) -> pd.Series:
        s = _s(s)
        return s > s.shift(n)

    @staticmethod
    def falling(s, n: int = 1) -> pd.Series:
        s = _s(s)
        return s < s.shift(n)


# =====================================================================
# Bulk candle download — one yfinance request per ~400 symbols
# =====================================================================
def _download_chunk(args: Tuple[List[str], str, str]) -> Dict[str, List[Dict]]:
    symbols, period, interval = args
    out: Dict[str, List[Dict]] = {}
    try:
        data = yf.download(
            symbols if len(symbols) > 1 else symbols[0],
            period=period,
            interval=interval,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
    except Exception as e:
        logger.warning("chunk download failed (%d symbols): %s", len(symbols), e)
        return out
    if data is None or data.empty:
        return out
    cols = getattr(data, "columns", None)
    multi = cols is not None and getattr(cols, "nlevels", 1) > 1
    for sym in symbols:
        try:
            df = data[sym] if (multi and len(symbols) > 1) else data
            if df is None or df.empty:
                continue
            if multi and len(symbols) == 1:
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            candles = []
            for idx, row in df.iterrows():
                try:
                    ts = int(idx.timestamp())
                    o, h, l, c = (float(row["Open"]), float(row["High"]),
                                  float(row["Low"]), float(row["Close"]))
                except (AttributeError, TypeError, ValueError, KeyError):
                    continue
                if not (o == o and h == h and l == l and c == c):
                    continue
                vol = row.get("Volume", 0)
                try:
                    vol = float(vol)
                    if not (vol == vol) or vol < 0:
                        vol = 0.0
                except (TypeError, ValueError):
                    vol = 0.0
                candles.append({"time": ts, "open": round(o, 2), "high": round(h, 2),
                                "low": round(l, 2), "close": round(c, 2),
                                "volume": vol})
            if candles:
                out[sym] = candles
        except Exception:
            continue
    try:
        del data
    except Exception:
        pass
    import gc as _gc
    _gc.collect()
    return out


def batch_load_candles(symbols: List[str], period: str = "1y",
                       interval: str = "1d") -> Dict[str, List[Dict]]:
    """Bulk-download candles for many symbols. Returns {symbol: [candles]}.

    Symbols with no data are simply absent from the result — the caller may
    fall back to per-symbol loading for those.
    """
    symbols = [s for s in dict.fromkeys(symbols or []) if s]
    if not symbols:
        return {}
    chunks = [symbols[i:i + BATCH_CHUNK] for i in range(0, len(symbols), BATCH_CHUNK)]
    args = [(ch, period, interval) for ch in chunks]
    t0 = time.time()
    out: Dict[str, List[Dict]] = {}
    if len(chunks) == 1:
        out.update(_download_chunk(args[0]))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(chunks))) as ex:
            for part in ex.map(_download_chunk, args):
                out.update(part)
    logger.info("batch_load_candles: %d/%d symbols, %d chunks, %.1fs",
                len(out), len(symbols), len(chunks), time.time() - t0)
    return out


def candles_to_df(candles: List[Dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles).rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("time").reset_index(drop=True)


# =====================================================================
# Python scan sandbox
# =====================================================================
_BLOCKED_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib",
    "ctypes", "inspect", "io", "pickle", "marshal", "threading",
    "multiprocessing", "signal", "gc", "code", "pty", "ssl", "urllib",
    "http", "ftplib", "smtplib", "telnetlib", "webbrowser", "pkgutil",
    "runpy", "trace", "traceback",
}

_ALLOWED_MODULES = {
    "pandas": pd, "numpy": np, "math": __import__("math"),
    "datetime": __import__("datetime"), "statistics": __import__("statistics"),
    "re": re, "json": __import__("json"), "collections": __import__("collections"),
    "itertools": __import__("itertools"), "functools": __import__("functools"),
}

_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "print": print, "range": range, "round": round, "set": set, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "isinstance": isinstance,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
}


def _guarded_import(name, *args, **kwargs):
    base = name.split(".")[0]
    if base in _BLOCKED_MODULES:
        raise ImportError(f"import of '{name}' is not allowed in scan scripts")
    if base in _ALLOWED_MODULES:
        return _ALLOWED_MODULES[base]
    raise ImportError(f"import of '{name}' is not allowed in scan scripts")


# IMPORT_NAME looks __import__ up in builtins (not globals) — register here.
_SAFE_BUILTINS["__import__"] = _guarded_import


def _exec_one(payload: Tuple[str, str, str, List[Dict], str]) -> Dict[str, Any]:
    """Run the user script for one symbol. Returns a normalized result dict."""
    code, symbol, interval, candles, language = payload
    base = {"symbol": symbol, "signal": False, "score": 0.0, "note": "", "error": ""}
    if len(candles) < 20:
        base["error"] = "אין מספיק נרות"
        return base
    if language == "pine":
        return _exec_pine_one(code, symbol, interval, candles, base)
    if language == "pyind":
        return _exec_pyind_one(code, symbol, interval, candles, base)
    if language == "pystrat":
        return _exec_pystrat_one(code, symbol, interval, candles, base)
    return _exec_python_one(code, symbol, interval, candles, base)


def _exec_pyind_one(code: str, symbol: str, interval: str,
                    candles: List[Dict], base: Dict[str, Any]) -> Dict[str, Any]:
    """Python chart indicator. Code defines indicator(df) (or result) returning
    a dict {plot_name: series} plus optional {"markers": [{bar, side, text}]}."""
    df = candles_to_df(candles)
    ns = _sandbox_ns(df, symbol, interval)
    try:
        exec(compile(code, "<indicator>", "exec"), ns)
    except Exception as e:
        base["error"] = f"שגיאת קוד: {e}"
        return base
    raw = None
    fn = ns.get("indicator")
    if callable(fn):
        try:
            raw = fn(df)
        except Exception as e:
            base["error"] = f"שגיאה ב-indicator(): {e}"
            return base
    elif "result" in ns:
        raw = ns["result"]
    if raw is None:
        base["error"] = "הקוד חייב להגדיר indicator(df) או result"
        return base
    return _normalize_indicator(raw, base, len(df))


def _normalize_indicator(raw: Any, base: Dict[str, Any], n: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        base["error"] = "indicator() חייב להחזיר dict מהצורה {שם_קו: סדרה}"
        return base
    plots: Dict[str, List[Optional[float]]] = {}
    for name, vals in raw.items():
        if name == "markers":
            continue
        if len(plots) >= 20:
            break
        sname = str(name)[:40]
        try:
            s = pd.Series(vals)
        except Exception:
            continue
        if len(s) != n:
            base["error"] = f"הסדרה '{sname}' באורך {len(s)} — נדרש {n} (כמספר הנרות)"
            return base
        out: List[Optional[float]] = []
        for v in s:
            try:
                f = float(v)
                out.append(None if not math.isfinite(f) else f)
            except (TypeError, ValueError):
                out.append(None)
        plots[sname] = out
    markers: List[Dict[str, Any]] = []
    mraw = raw.get("markers")
    if isinstance(mraw, list):
        for m in mraw[:500]:
            if not isinstance(m, dict):
                continue
            try:
                bar = int(m.get("bar", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= bar < n:
                side = str(m.get("side", "buy")).lower()
                markers.append({"bar": bar,
                                "side": "sell" if side == "sell" else "buy",
                                "text": str(m.get("text", ""))[:30]})
    base["plots"] = plots
    base["markers"] = markers
    return base


def _exec_pystrat_one(code: str, symbol: str, interval: str,
                      candles: List[Dict], base: Dict[str, Any]) -> Dict[str, Any]:
    """Python strategy. Code defines strategy(df) (or result) returning a
    signal series aligned with df: 1 = enter long, -1 = exit, 0 = hold."""
    df = candles_to_df(candles)
    ns = _sandbox_ns(df, symbol, interval)
    try:
        exec(compile(code, "<strategy>", "exec"), ns)
    except Exception as e:
        base["error"] = f"שגיאת קוד: {e}"
        return base
    raw = None
    fn = ns.get("strategy")
    if callable(fn):
        try:
            raw = fn(df)
        except Exception as e:
            base["error"] = f"שגיאה ב-strategy(): {e}"
            return base
    elif "result" in ns:
        raw = ns["result"]
    if raw is None:
        base["error"] = "הקוד חייב להגדיר strategy(df) או result"
        return base
    try:
        s = pd.Series(raw)
    except Exception:
        base["error"] = "strategy() חייב להחזיר סדרת סיגנלים (1/-1/0)"
        return base
    if len(s) != len(df):
        base["error"] = f"מספר הסיגנלים ({len(s)}) לא תואם למספר הנרות ({len(df)})"
        return base
    sigs = []
    for v in s:
        try:
            iv = int(float(v))
        except (TypeError, ValueError):
            iv = 0
        sigs.append(1 if iv > 0 else (-1 if iv < 0 else 0))
    base["signals"] = sigs
    return base


def _sandbox_ns(df: pd.DataFrame, symbol: str, interval: str) -> Dict[str, Any]:
    """Restricted namespace for user code (shared by scan/indicator/strategy)."""
    return {
        "__builtins__": dict(_SAFE_BUILTINS),
        "pd": pd, "np": np, "ta": TA,
        "df": df, "SYMBOL": symbol, "INTERVAL": interval,
    }


def _exec_python_one(code: str, symbol: str, interval: str,
                     candles: List[Dict], base: Dict[str, Any]) -> Dict[str, Any]:
    df = candles_to_df(candles)
    ns = _sandbox_ns(df, symbol, interval)
    try:
        exec(compile(code, "<scan>", "exec"), ns)
    except Exception as e:
        base["error"] = f"שגיאת קוד: {e}"
        return base
    raw = None
    fn = ns.get("scan")
    if callable(fn):
        try:
            raw = fn(df)
        except Exception as e:
            base["error"] = f"שגיאה ב-scan(): {e}"
            return base
    elif "result" in ns:
        raw = ns["result"]
    if raw is None:
        base["error"] = "הקוד חייב להגדיר scan(df) או result"
        return base
    return _normalize_result(raw, base)


def _normalize_result(raw: Any, base: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw, dict):
        sig = raw.get("signal", False)
        base["signal"] = bool(sig) if not isinstance(sig, float) else bool(sig)
        try:
            base["score"] = float(raw.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            base["score"] = 0.0
        note = raw.get("note", "")
        base["note"] = str(note)[:200] if note is not None else ""
    elif isinstance(raw, bool):
        base["signal"] = raw
    elif isinstance(raw, (int, float)):
        base["signal"] = bool(raw)
        base["score"] = float(raw)
    else:
        base["error"] = "scan() חייב להחזיר dict עם signal"
    if not math.isfinite(base["score"]):
        base["score"] = 0.0
    return base


def _exec_pine_one(code: str, symbol: str, interval: str,
                   candles: List[Dict], base: Dict[str, Any]) -> Dict[str, Any]:
    if _pine_run is None:
        base["error"] = f"מנוע Pine לא זמין: {_pine_import_error}"
        return base
    # The chart engine requires a plot()/strategy for rendering; a scan script
    # may legitimately contain only alertcondition() — append an invisible
    # dummy plot so such scripts still evaluate their alerts.
    scan_code = code
    if (not re.search(r"(?m)^\s*plot\s*\(", code)
            and "strategy.entry" not in code and "strategy(" not in code):
        scan_code = code + "\nplot(close, \"__scan__\")\n"
    try:
        res = _pine_run(candles, scan_code, symbol=symbol, interval=interval)
    except Exception as e:
        # PineError or anything else — report per symbol, don't kill the scan
        base["error"] = str(e)[:200]
        return base
    alerts = res.get("alerts") or []
    # A scanner cares about "matching NOW": only alerts fired on the last few
    # candles count (a condition true 3 months ago is not a match).
    cutoff = candles[-3]["time"] if len(candles) >= 3 else candles[0]["time"]
    recent = [a for a in alerts
              if isinstance(a, dict) and a.get("time", 0) >= cutoff]
    if recent:
        base["signal"] = True
        base["score"] = float(len(recent))
        last = recent[-1]
        msg = last.get("message", "") if isinstance(last, dict) else str(last)
        base["note"] = str(msg)[:200] if msg else f"{len(recent)} התראות אחרונות"
    return base


# =====================================================================
# Hardened sandbox: user code runs in a short-lived forked child process
# =====================================================================
_BLOCKED_NAMES = {
    "eval", "exec", "compile", "open", "input", "exit", "quit", "help",
}


class _ScanCodeValidator(ast.NodeVisitor):
    """AST pre-check — rejects dangerous constructs before any execution."""

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            base = (a.name or "").split(".")[0]
            if base in _BLOCKED_MODULES or base not in _ALLOWED_MODULES:
                raise ValueError(f"ייבוא '{a.name}' אינו מותר בסריקות")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = (node.module or "").split(".")[0]
        if base in _BLOCKED_MODULES or base not in _ALLOWED_MODULES:
            raise ValueError(f"ייבוא '{node.module}' אינו מותר בסריקות")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Blocks the ().__class__.__bases__... escape hatch.
        if node.attr.startswith("__"):
            raise ValueError("גישה למאפיינים פנימיים אינה מותרת בסריקות")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__") or node.id in _BLOCKED_NAMES:
            raise ValueError(f"שימוש בשם '{node.id}' אינו מותר בסריקות")
        self.generic_visit(node)


def _validate_python_ast(code: str) -> None:
    if len(code) > 200_000:
        raise ValueError("הקוד ארוך מדי (מעל 200KB)")
    try:
        tree = ast.parse(code, filename="<scan>")
    except SyntaxError as e:
        raise ValueError(f"שגיאת תחביר: {e}") from e
    _ScanCodeValidator().visit(tree)


def _self_vsz_bytes() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def _sandbox_child_main(result_path: str, code: str, symbol: str, interval: str,
                        candles: List[Dict], language: str) -> None:
    """Child-process entry point. Never called in the server process.

    FD SAFETY: a forked child inherits the request worker's file descriptors
    (live client sockets, DB handles, ...). If the child exits normally,
    interpreter cleanup closes those inherited FDs, which can sever the
    client's own connection mid-response. So the child closes every FD >= 3
    first, reports back only through a result file opened by path, and leaves
    via os._exit() to skip all cleanup of inherited objects.
    """
    try:
        os.closerange(3, 65536)
    except Exception:
        pass
    base = {"symbol": symbol, "signal": False, "score": 0.0, "note": "", "error": ""}
    try:
        sys.dont_write_bytecode = True
        # Crash diagnostics: if the child dies on a native fault (SIGSEGV
        # after fork), faulthandler leaves a traceback the parent reports.
        try:
            import faulthandler as _fh
            _fh.enable(file=open(f"/tmp/pybox_fault_{os.getpid()}.log", "w",
                                 encoding="utf-8"))
        except Exception:
            pass
        # A forked child of a threaded server can inherit broken native
        # thread-pool state (OpenBLAS/OpenMP); keep user code single-threaded.
        try:
            for _k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                       "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                       "VECLIB_MAXIMUM_THREADS"):
                os.environ.setdefault(_k, "1")
            import threadpoolctl as _tc
            _tc.threadpool_limits(limits=1)
        except Exception:
            pass
        # Memory cap: fail with MemoryError inside the child instead of
        # OOMing the whole 512MB instance.
        try:
            import resource as _resource
            vsz = _self_vsz_bytes()
            mem_cap = (vsz + SCAN_CHILD_MEM_HEADROOM) if vsz > 0 else 2 * 1024 ** 3
            _resource.setrlimit(_resource.RLIMIT_AS, (mem_cap, mem_cap))
            # Backstop for orphaned children (parent died): CPU time kills them.
            cpu = PER_SYMBOL_TIMEOUT + 60
            _resource.setrlimit(_resource.RLIMIT_CPU, (cpu, cpu + 30))
        except Exception:
            pass
        # Silence the child: user print() must not spam server logs.
        try:
            _devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(_devnull, 1)
            os.dup2(_devnull, 2)
        except Exception:
            pass
        try:
            res = _exec_one((code, symbol, interval, candles, language))
        except Exception as e:  # noqa: BLE001 — never let the child die silent
            base["error"] = str(e)[:200] or "שגיאה לא ידועה"
            res = base
        try:
            with open(result_path, "wb") as f:
                pickle.dump(res, f)
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        try:
            base["error"] = f"שגיאת סביבת הרצה: {e}"[:200]
            with open(result_path, "wb") as f:
                pickle.dump(base, f)
        except Exception:
            pass
    os._exit(0)


def _terminate_proc(p: "multiprocessing.Process") -> None:
    try:
        if p.is_alive():
            p.terminate()
            p.join(2)
        if p.is_alive():
            p.kill()
            p.join(2)
    except Exception:
        pass


def _blank_result(symbol: str) -> Dict[str, Any]:
    return {"symbol": symbol, "signal": False, "score": 0.0, "note": "", "error": ""}


def _attach_market_info(r: Dict[str, Any], candles: List[Dict]) -> Dict[str, Any]:
    if candles:
        last = candles[-1]
        prev = candles[-2] if len(candles) > 1 else candles[-1]
        r["price"] = last.get("close")
        try:
            pc = prev.get("close")
            r["change_pct"] = round(100 * (last["close"] - pc) / pc, 2) if pc else 0.0
        except (TypeError, ZeroDivisionError):
            r["change_pct"] = 0.0
    else:
        r["price"] = None
        r["change_pct"] = 0.0
    return r


def _read_child_fault_log(pid) -> str:
    """Tail of a crashed sandbox child's faulthandler dump (best effort)."""
    if not pid:
        return ""
    path = f"/tmp/pybox_fault_{pid}.log"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()
    except OSError:
        return ""
    try:
        os.remove(path)
    except OSError:
        pass
    tail = " ".join(tail.split())[-1200:]
    return tail


def _collect_child(a: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch the child's result; describe crashes with a clean error."""
    p, result_path, sym = a["p"], a["result_path"], a["sym"]
    res = None
    try:
        with open(result_path, "rb") as f:
            payload = pickle.load(f)
        if isinstance(payload, dict):
            res = payload
    except Exception:
        res = None
    finally:
        try:
            os.unlink(result_path)
        except Exception:
            pass
    if not isinstance(res, dict):
        res = _blank_result(sym)
        if p.exitcode not in (0, None):
            res["error"] = f"תהליך הסריקה קרס (קוד {p.exitcode})"
            diag = _read_child_fault_log(getattr(p, "pid", None))
            if diag:
                res["error"] += f" | {diag}"
        else:
            res["error"] = "התהליך הסתיים ללא תוצאה"
    return res


def _run_isolated(code: str, items: List[Tuple[str, List[Dict]]], language: str,
                  interval: str,
                  progress_cb: Optional[Callable[[int, int], None]]) -> List[Dict[str, Any]]:
    """Run one scan script per symbol, each in its own child process.

    A hanging or exploding script can only kill its own child — the parent
    enforces a wall-clock timeout and reaps it. The server process itself
    never executes user code.
    """
    try:
                # forkserver: workers are forked from a clean single-threaded server
             # process — safe when the caller is a FastAPI threadpool thread.
     ctx = multiprocessing.get_context("forkserver")
    except (ValueError, AttributeError) as e:
        raise RuntimeError("הרצת סריקות דורשת סביבת Linux") from e
    total = len(items)
    results: List[Dict[str, Any]] = []
    done = 0
    idx = 0
    active: List[Dict[str, Any]] = []
    while idx < total or active:
        while idx < total and len(active) < SCAN_WORKERS:
            sym, candles = items[idx]
            idx += 1
            fd, result_path = tempfile.mkstemp(prefix="scan_sym_")
            os.close(fd)
            p = ctx.Process(target=_sandbox_child_main,
                            args=(result_path, code, sym, interval, candles, language))
            p.start()
            active.append({"p": p, "result_path": result_path, "sym": sym,
                           "start": time.time(), "killed": False})
        time.sleep(0.05)
        still: List[Dict[str, Any]] = []
        for a in active:
            p = a["p"]
            if p.is_alive() and time.time() - a["start"] > PER_SYMBOL_TIMEOUT:
                _terminate_proc(p)
                a["killed"] = True
            if p.is_alive():
                still.append(a)
                continue
            if a["killed"]:
                res = _blank_result(a["sym"])
                res["error"] = f"חריגת זמן ({PER_SYMBOL_TIMEOUT} שנ׳)"
                try:
                    os.unlink(a["result_path"])
                except Exception:
                    pass
            else:
                res = _collect_child(a)
            try:
                p.close()
            except Exception:
                pass
            results.append(res)
            done += 1
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
        active = still
    return results


def run_scan(code: str, candles_by_symbol: Dict[str, List[Dict]],
             language: str = "python", interval: str = "1d",
             progress_cb: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
    """Run the scan script over all symbols in hardened child processes.

    Returns a list of result dicts: symbol/signal/score/note/error/price/change_pct.
    """
    items = list(candles_by_symbol.items())
    if language == "python":
        try:
            _validate_python_ast(code)
        except ValueError as e:
            return [_attach_market_info({**_blank_result(sym), "error": str(e)}, candles)
                    for sym, candles in items]
    results = _run_isolated(code, items, language, interval, progress_cb)
    by_symbol = {sym: candles for sym, candles in items}
    out = [_attach_market_info(r, by_symbol.get(r.get("symbol"), [])) for r in results]
    # matches first (by score desc), then the rest alphabetically
    matched = sorted([r for r in out if r.get("signal")],
                     key=lambda r: (-r.get("score", 0), r["symbol"]))
    rest = sorted([r for r in out if not r.get("signal")], key=lambda r: r["symbol"])
    return matched + rest


# =====================================================================
# Python chart indicator & strategy (single symbol, hardened sandbox)
# =====================================================================
_INDICATOR_COLORS = ["#2962ff", "#ff6d00", "#089981", "#f23645",
                     "#9c27b0", "#00bcd4", "#ffab00", "#5c6bc0"]


def run_python_indicator(code: str, candles: List[Dict],
                         symbol: str = "") -> Dict[str, Any]:
    """Run a Python indicator script for chart overlay.

    Returns {"plots": [{name, color, values (index-aligned)}],
             "markers": [{time, position, color, shape, text}], "error": ""}.
    The plots shape matches /api/pine/run so the chart UI reuses its renderer.
    """
    try:
        _validate_python_ast(code)
    except ValueError as e:
        return {"plots": [], "markers": [], "error": str(e)}
    results = _run_isolated(code, [(symbol or "SYM", candles)], "pyind", "1d", None)
    r = results[0] if results else {}
    if r.get("error"):
        return {"plots": [], "markers": [], "error": r["error"]}
    plots = [
        {"name": name, "color": _INDICATOR_COLORS[i % len(_INDICATOR_COLORS)],
         "values": vals}
        for i, (name, vals) in enumerate((r.get("plots") or {}).items())
    ]
    markers = []
    for m in r.get("markers") or []:
        t = candles[m["bar"]]["time"]
        if m["side"] == "sell":
            markers.append({"time": t, "position": "aboveBar", "color": "#f23645",
                            "shape": "arrowDown", "text": m["text"] or "Sell"})
        else:
            markers.append({"time": t, "position": "belowBar", "color": "#089981",
                            "shape": "arrowUp", "text": m["text"] or "Buy"})
    return {"plots": plots, "markers": markers, "error": ""}


def run_python_strategy(code: str, candles: List[Dict], symbol: str = "",
                        initial_capital: float = 10000.0,
                        commission_pct: float = 0.1) -> Dict[str, Any]:
    """Run a Python strategy script and backtest its signals.

    Returns the same metrics/trades/markers/equity_curve structure as the
    built-in strategies (strategy_engine.backtest_from_signals).
    Raises ValueError with a Hebrew message on any user-code problem.
    """
    try:
        _validate_python_ast(code)
    except ValueError as e:
        raise ValueError(str(e))
    results = _run_isolated(code, [(symbol or "SYM", candles)], "pystrat", "1d", None)
    r = results[0] if results else {}
    if r.get("error"):
        raise ValueError(r["error"])
    signals = r.get("signals") or []
    return backtest_from_signals(
        candles, signals,
        initial_capital=initial_capital,
        commission_pct=commission_pct,
        strategy_id="python_custom",
        params={},
    )


PYTHON_EXAMPLE = '''# דוגמת סריקה: RSI נמוך + מחיר מעל SMA50
# df: נרות (time/open/high/low/close/volume) · ta: אינדיקטורים · SYMBOL · INTERVAL
def scan(df):
    c = df["close"]
    rsi = ta.rsi(c, 14).iloc[-1]
    sma50 = ta.sma(c, 50).iloc[-1]
    price = c.iloc[-1]
    if rsi < 30 and price > sma50:
        return {"signal": True,
                "score": round(30 - rsi, 1),
                "note": f"RSI {rsi:.1f} מתחת ל-30 ומעל SMA50"}
    return {"signal": False}
'''

PINE_SCAN_NOTE = ("Pine: סימול נחשב תואם אם הסקריפט ירה alert() או alertcondition() — "
                  "השתמש ב-alertcondition כדי להגדיר את תנאי הסריקה.")

PY_INDICATOR_EXAMPLE = '''# אינדיקטור Python לגרף — מחזיר dict של {שם_קו: סדרה}
# df: נרות · ta: אינדיקטורים · pd/np · SYMBOL · INTERVAL
def indicator(df):
    c = df["close"]
    upper, mid, lower = ta.bbands(c, 20, 2.0)
    return {
        "BB Upper": upper,
        "BB Mid": mid,
        "BB Lower": lower,
        "RSI x10": ta.rsi(c, 14) * 10,   # קנה מידה להשוואה ויזואלית
    }
'''

PY_STRATEGY_EXAMPLE = '''# אסטרטגיית Python לבקטסט — מחזיר סדרת סיגנלים:
#  1 = כניסה ללונג, -1 = יציאה, 0 = החזקה
def strategy(df):
    c = df["close"]
    fast = ta.ema(c, 9)
    slow = ta.ema(c, 21)
    rsi = ta.rsi(c, 14)
    long = ta.crossover(fast, slow) & (rsi > 50)
    ex = ta.crossunder(fast, slow)
    sig = pd.Series(0, index=df.index)
    sig[long] = 1
    sig[ex] = -1
    return sig
'''


PY_SRFLIP_INDICATOR_EXAMPLE = '''# =====================================================================
# S/R Flip Zone + Rounding Bottom + Cup & Handle  —  המרה מ-Pine v6 ל-Python
# =====================================================================
# איך משתמשים:
#   1. באפליקציה: לשונית הסקריפטים (📜) → בורר 🐍 Python → הדבק את הקוד → ▶ הרץ
#   2. כדוגמה מובנית באפליקציה: לשונית הסקריפטים → "📋 S/R Flip"
#      (אינדיקטור), לשונית הסורק → "📋 S/R Flip" (סריקה).
#
# מה הקוד עושה:
#   indicator(df) — מקבל df עם נרות (time/open/high/low/close/volume),
#   מחזיר dict של {שם_קו: סדרה} + "markers" לסמני קנייה/מכירה על הגרף.
#   זמינים: ta.* (sma/ema/rsi/macd/atr/bbands/stoch/cci/mfi/highest/lowest/
#                  crossover/crossunder/falling), pd, np, SYMBOL, INTERVAL.
#
# הערה: ציורי קווים/קופסאות/תוויות מה-Pine לא קיימים ב-Python —
#   במקומם מקבלים את הקווים (גבולות האזור, מחיר מוחלק, פרבולה) והסיגנלים
#   כסמנים על הגרף. הלוגיקה מנסה לשחזר את ה-Pine, אבל לא אומתה מול
#   TradingView — רצוי להשוות סיגנלים על אותו סימול/טווח לפני שימוש.
# =====================================================================

# ================= 1. פרמטרים: S/R Flip Zone =================
PIVOT_LEN         = 5      # אורך פיבוט (נרות בכל צד לאישור שיא/שפל)
ATR_LEN           = 14     # אורך ATR
ZONE_WIDTH_ATR    = 0.35   # חצי-רוחב אזור במכפלות ATR
MIN_SUP_TOUCHES   = 2      # מינימום נגיעות תמיכה
MIN_FLIP_RETESTS  = 2      # מינימום בדיקות-חוזרות של התנגדות
CONFIRM_BARS      = 2      # נרות אישור
TREND_LEN         = 34     # אורך מגמת ירידה לקונטקסט
LOOKBACK          = 500    # לוקבאק פיבוטים
MAX_ZONES         = 30     # מקסימום אזורים במעקב
MIN_TOTAL_TOUCHES = 4      # מינימום פיבוטים כולל

# ================= 2. פרמטרים: Rounding Bottom =================
ROUND_WINDOW        = 60     # חלון פרבולה
SMOOTH_LEN          = 11     # אורך החלקה
MIN_R2              = 0.80   # ציון R² מינימלי
MIN_CURVATURE       = 0.0005 # עקמומיות מינימלית (a)
PRIOR_DROP_PCT      = 50.0   # ירידה קודמת מינימלית (%)
PRIOR_DROP_LOOKBACK = 250    # לוקבאק לירידה הקודמת

# ================= 3. פרמטרים: Cup & Handle =================
C_PIVOT_LEN        = 3      # לוקבאק פיבוטים לשפות/שפלים
MIN_CUP_BARS       = 15     # אורך ספל מינימלי (נרות)
MAX_CUP_BARS       = 120    # אורך ספל מקסימלי (נרות)
RIM_TOL            = 0.04   # הפרש גובה שפות מקסימלי (0.04 = 4%)
MIN_DEPTH          = 0.10   # עומק ספל מינימלי (0.10 = 10%)
MAX_DEPTH          = 0.99   # עומק ספל מקסימלי
MAX_HANDLE_RETRACE = 0.618  # תיקון ידית מקסימלי (0.618 = 61.8%)

# ================= 4. תצוגה =================
SHOW_FLIP_ZONE  = True   # הצג אזור היפוך
SHOW_CENTER     = False  # הצג מרכז אזור
SHOW_ROUND_FIT  = True   # הצג התאמה מעוגלת
SHOW_SIGNALS    = True   # הצג סיגנלים
MINTICK         = 0.01   # טיק מינימלי (במקום syminfo.mintick)


def _pivot_series(h, l, left, right):
    """שחזור ta.pivothigh / ta.pivotlow: מחזיר (ph, pl) — מערכי numpy עם NaN,
    כאשר הערך מופיע בנר האישור (pivot bar = i - right)."""
    import numpy as np
    n = len(h)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    w = left + right + 1
    for i in range(w - 1, n):
        pb = i - right
        wh = h[pb - left:pb + right + 1]
        wl = l[pb - left:pb + right + 1]
        mh = np.nanmax(wh)
        ml = np.nanmin(wl)
        if h[pb] >= mh:
            ph[i] = h[pb]
        if l[pb] <= ml:
            pl[i] = l[pb]
    return ph, pl


def _new_zone(center, width, pbar, is_sup, downtrend):
    return {
        "center": center, "width": width, "touches": 1,
        "sup": 1 if is_sup else 0, "res": 0 if is_sup else 1,
        "first_bar": pbar, "last_bar": pbar,
        "downtrend_origin": bool(is_sup and downtrend),
        "broken": False, "break_bar": None, "retests": 0,
        "qualified": False, "qualified_bar": None,
        "hold": 0, "confirmed": False, "confirmed_bar": None,
    }


def indicator(df):
    import numpy as np

    n = len(df)
    close = df["close"].to_numpy(dtype=float)
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)

    # ---- סדרות מחושבות מראש (וקטורי) ----
    atr_s    = ta.atr(df["high"], df["low"], df["close"], ATR_LEN).to_numpy(dtype=float)
    trend    = ta.ema(df["close"], TREND_LEN)
    falling  = (trend < trend.shift(TREND_LEN)).to_numpy()  # כמו ta.falling(trend, TREND_LEN)
    falling  = np.nan_to_num(falling.astype(float), nan=0.0).astype(bool)
    smoothed = ta.sma(df["close"], SMOOTH_LEN).to_numpy(dtype=float)
    wmin_a   = ta.lowest(ta.sma(df["close"], SMOOTH_LEN), ROUND_WINDOW).to_numpy(dtype=float)
    wmax_a   = ta.highest(ta.sma(df["close"], SMOOTH_LEN), ROUND_WINDOW).to_numpy(dtype=float)
    prior_high = ta.highest(df["high"], PRIOR_DROP_LOOKBACK).to_numpy(dtype=float)
    pat_low    = ta.lowest(df["low"], ROUND_WINDOW).to_numpy(dtype=float)

    ph, pl     = _pivot_series(high, low, PIVOT_LEN, PIVOT_LEN)      # לאזורי S/R
    cph, cpl   = _pivot_series(high, low, C_PIVOT_LEN, C_PIVOT_LEN)  # ל-Cup & Handle

    # ---- פלט ----
    up = [None] * n
    lo = [None] * n
    ce = [None] * n
    sm = [None] * n
    ft = [None] * n
    markers = []

    zones = []

    # ---- מצב Cup & Handle ----
    phB, phP, plB, plP = [], [], [], []
    b2_p = None
    active_neckline = None
    pattern_active = False
    breakout_confirmed = False
    prev_round_setup = False

    for i in range(n):
        # ---------- ניקוי אזורים ישנים ----------
        if zones:
            zones = [z for z in zones if i - z["last_bar"] <= LOOKBACK]

        # ---------- קיבוץ פיבוטים לאזורי S/R ----------
        for ptype in (0, 1):  # 0 = שפל (תמיכה), 1 = שיא (התנגדות)
            pv = pl[i] if ptype == 0 else ph[i]
            if np.isnan(pv):
                continue
            is_sup = (ptype == 0)
            pbar = i - PIVOT_LEN
            down_at_pivot = bool(falling[pbar]) if pbar >= 0 else False
            a = atr_s[pbar] if pbar >= 0 else np.nan
            pwidth = max((a if not np.isnan(a) else MINTICK) * ZONE_WIDTH_ATR,
                         MINTICK * 2.0)
            best, best_d = -1, float("inf")
            for zi, z in enumerate(zones):
                d = abs(pv - z["center"])
                if d <= max(z["width"], pwidth) and d < best_d:
                    best, best_d = zi, d
            if best >= 0:
                z = zones[best]
                t = z["touches"]
                z["center"] = (z["center"] * t + pv) / (t + 1)
                z["width"] = max(z["width"], pwidth)
                z["touches"] = t + 1
                z["sup"] += 1 if is_sup else 0
                z["res"] += 0 if is_sup else 1
                z["first_bar"] = min(z["first_bar"], pbar)
                z["last_bar"] = pbar
                if is_sup and down_at_pivot:
                    z["downtrend_origin"] = True
            else:
                if len(zones) >= MAX_ZONES:
                    wi = min(range(len(zones)),
                             key=lambda k: zones[k]["touches"]
                             + min(zones[k]["sup"], zones[k]["res"]) * 2.0)
                    zones.pop(wi)
                zones.append(_new_zone(pv, pwidth, pbar, is_sup, down_at_pivot))

        # ---------- שבירת תמיכה / הסמכה מחדש ----------
        for z in zones:
            zc, zw = z["center"], z["width"]
            zup, zlo = zc + zw, zc - zw
            crossed_below = close[i] < zlo and i > 0 and close[i - 1] >= zlo
            if (not z["broken"] and z["downtrend_origin"]
                    and z["sup"] >= MIN_SUP_TOUCHES and z["sup"] > z["res"]
                    and crossed_below):
                z["broken"] = True
                z["break_bar"] = i
                if SHOW_SIGNALS:
                    markers.append({"bar": i, "side": "sell", "text": "Break"})
            elif z["broken"] and not z["qualified"]:
                piv_after = (i - PIVOT_LEN) > z["break_bar"]
                rej = (not np.isnan(ph[i]) and piv_after
                       and zlo <= ph[i] <= zup
                       and i - PIVOT_LEN >= 0
                       and close[i - PIVOT_LEN] < zc)
                if rej:
                    z["retests"] += 1
                if z["retests"] >= MIN_FLIP_RETESTS and close[i] > zup:
                    z["qualified"] = True
                    z["qualified_bar"] = i
            if z["qualified"]:
                if close[i] >= zlo:
                    z["hold"] += 1
                    if z["hold"] >= CONFIRM_BARS and not z["confirmed"]:
                        z["confirmed"] = True
                        z["confirmed_bar"] = i
                else:
                    z["hold"] = 0
                    z["confirmed"] = False
                    z["confirmed_bar"] = None

        # ---------- בחירת האזור הטוב ביותר ----------
        sel, sel_score = -1, None
        for zi, z in enumerate(zones):
            if (z["qualified"] and z["sup"] >= MIN_SUP_TOUCHES
                    and z["retests"] >= MIN_FLIP_RETESTS
                    and z["touches"] >= MIN_TOTAL_TOUCHES):
                age = i - z["last_bar"]
                recency = 1.0 - min(age / LOOKBACK, 1.0)
                origin_b = 5.0 if z["downtrend_origin"] else 0.0
                score = (z["touches"] + min(z["sup"], z["retests"]) * 3.0
                         + origin_b + recency)
                if sel_score is None or score > sel_score:
                    sel, sel_score = zi, score
        if sel >= 0:
            z = zones[sel]
            c_, w_ = z["center"], z["width"]
            if SHOW_FLIP_ZONE:
                up[i], lo[i] = c_ + w_, c_ - w_
                if SHOW_CENTER:
                    ce[i] = c_
            if SHOW_SIGNALS:
                if z["qualified_bar"] == i:
                    markers.append({"bar": i, "side": "buy", "text": "Reclaim"})
                if z["confirmed_bar"] == i:
                    markers.append({"bar": i, "side": "buy", "text": "Confirm"})

        # ---------- התאמת פרבולה: Rounding Bottom ----------
        if i >= ROUND_WINDOW + SMOOTH_LEN - 1:
            sb = i - ROUND_WINDOW + 1
            if not np.isnan(smoothed[sb]):
                wmin, wmax = wmin_a[i], wmax_a[i]
                if not np.isnan(wmin) and not np.isnan(wmax) and wmax > wmin:
                    y = smoothed[sb:i + 1]
                    yn = (y - wmin) / (wmax - wmin)
                    x = np.arange(ROUND_WINDOW, dtype=float)
                    a_, b_, c_ = np.polyfit(x, yn, 2)  # y = a*x^2 + b*x + c
                    vx = -b_ / (2.0 * a_) if a_ != 0 else np.nan
                    fit = a_ * x * x + b_ * x + c_
                    mean = yn.mean()
                    sse = ((yn - fit) ** 2).sum()
                    sst = ((yn - mean) ** 2).sum()
                    r2 = 1.0 - sse / sst if sst > 0 else 0.0
                    valid = (a_ >= MIN_CURVATURE and r2 >= MIN_R2
                             and ROUND_WINDOW * 0.33 < vx < ROUND_WINDOW * 0.66)
                    phv = prior_high[i]
                    plv = pat_low[i]
                    drop = (phv - plv) / phv * 100.0 if phv > 0 and not np.isnan(plv) else 0.0
                    setup = bool(valid and drop >= PRIOR_DROP_PCT)
                    if setup and SHOW_ROUND_FIT:
                        sm[i] = float(smoothed[i])
                        fnorm = a_ * (ROUND_WINDOW - 1) ** 2 + b_ * (ROUND_WINDOW - 1) + c_
                        ft[i] = float(wmin + fnorm * (wmax - wmin))
                    if setup and SHOW_SIGNALS and not prev_round_setup:
                        markers.append({"bar": i, "side": "buy", "text": "Round"})
                    prev_round_setup = setup

        # ---------- Cup & Handle: איסוף פיבוטים ----------
        ccph = cph[i]
        ccpl = cpl[i]
        if not np.isnan(ccph):
            phB.append(i - C_PIVOT_LEN)
            phP.append(float(ccph))
            if len(phB) > 50:
                phB.pop(0); phP.pop(0)
        if not np.isnan(ccpl):
            plB.append(i - C_PIVOT_LEN)
            plP.append(float(ccpl))
            if len(plB) > 50:
                plB.pop(0); plP.pop(0)

        # ---------- Cup & Handle: זיהוי (בכל שיא-פיבוט חדש) ----------
        if len(phB) >= 3 and len(plB) >= 2 and not np.isnan(ccph):
            pR3_bar, pR3_p = phB[-1], phP[-1]
            pR2_bar, pR2_p = phB[-2], phP[-2]
            for k in range(len(phB) - 3, max(0, len(phB) - 8) - 1, -1):
                pR1_bar, pR1_p = phB[k], phP[k]
                cup_bars = pR2_bar - pR1_bar
                if not (MIN_CUP_BARS <= cup_bars <= MAX_CUP_BARS):
                    continue
                avg_neck = (pR1_p + pR2_p) / 2.0
                if avg_neck <= 0:
                    continue
                if abs(pR1_p - pR2_p) / avg_neck > RIM_TOL:
                    continue
                lc_p = lc_b = None
                for jb, jp in zip(plB, plP):
                    if pR1_bar < jb < pR2_bar and (lc_p is None or jp < lc_p):
                        lc_p, lc_b = jp, jb
                lh_p = lh_b = None
                for jb, jp in zip(plB, plP):
                    if pR2_bar < jb < pR3_bar and (lh_p is None or jp < lh_p):
                        lh_p, lh_b = jp, jb
                if lc_p is None or lh_p is None:
                    continue
                depth = (avg_neck - lc_p) / avg_neck
                depth_val = avg_neck - lc_p
                handle_pb = pR2_p - lh_p
                if (MIN_DEPTH <= depth <= MAX_DEPTH and lh_p > lc_p
                        and handle_pb <= depth_val * MAX_HANDLE_RETRACE):
                    b2_p = lh_p
                    active_neckline = avg_neck
                    pattern_active = True
                    breakout_confirmed = False
                    if SHOW_SIGNALS:
                        markers.append({"bar": i, "side": "buy", "text": "C&H"})
                    break

        # ---------- Cup & Handle: מעקב פריצה ----------
        if pattern_active and not breakout_confirmed and b2_p is not None:
            if close[i] < b2_p:
                pattern_active = False
            elif i > 0 and close[i] > active_neckline >= close[i - 1]:
                breakout_confirmed = True
                if SHOW_SIGNALS:
                    markers.append({"bar": i, "side": "buy", "text": "C&H BO"})

    # ---------- הרכבת התוצאה ----------
    out = {}
    if SHOW_FLIP_ZONE:
        out["Flip zone upper"] = up
        out["Flip zone lower"] = lo
        if SHOW_CENTER:
            out["Flip zone center"] = ce
    if SHOW_ROUND_FIT:
        out["Smoothed"] = sm
        out["Quad fit"] = ft
    if SHOW_SIGNALS:
        out["markers"] = markers
    return out
'''


PY_SRFLIP_SCAN_EXAMPLE = (PY_SRFLIP_INDICATOR_EXAMPLE + '''

# ================= 5. עטיפת סריקה =================
# הסורק מריץ את scan(df) על כל סימול ברשימה.
# signal=True אם אחד הדפוסים (S/R Flip / Rounding / C&H) אותר ב-3 הנרות האחרונים.
def scan(df):
    out = indicator(df)
    markers = out.get("markers") or []
    n = len(df)
    recent = [m for m in markers
              if isinstance(m, dict) and m.get("bar", -1) >= n - 3]
    if not recent:
        return {"signal": False, "score": 0.0, "note": ""}
    names = sorted({str(m.get("text", "?")) for m in recent})
    return {"signal": True,
            "score": float(len(names)),
            "note": "דפוסים ב-3 נרות אחרונים: " + ", ".join(names)}
''')
PY_BASEBO_INDICATOR_EXAMPLE = '''# =====================================================================
# Base Breakout — הסטאפ של אלרואי
# =====================================================================
# שלבי התבנית:
#  1. אזור תמיכה (נגיעה אחת מינימום בירידה) -> נשבר כלפי מטה -> הופך
#     להתנגדות ממושכת.
#  2. התבססות ארוכה תחת האזור: 3+ תחתיות מעוגלות (U) בעומקים משתנים
#     (U-ים מקוננים), הבודקות את ההתנגדות שוב ושוב (2+ דחיות).
#  3. פריצה אמיתית: סגירה מעל האזור.
#  4. אישור: התבססות מעל האזור + בדיקת האזור כתמיכה, כשווליום המוכרים
#     דועך בבדיקה (אין היצע) -> סיגנל "BASE_BO".
# בנפרד (טרייד אחר, מתויג בנפרד): זיהוי C&H קלאסי ("C&H" / "C&H BO").
#
# indicator(df) — df עם נרות (time/open/high/low/close/volume).
# markers: "BASE_WATCH" (בסיס נבנה, עוד אין פריצה), "Breakout" (פריצה),
#          "BASE_BO" (הטרייד: פריצה + בדיקת תמיכה עם ווליום מוכרים דועך),
#          "Round" (תחתית מעוגלת), "C&H" / "C&H BO" (טרייד אחר).
# זמינים: ta.* (sma/ema/atr/lowest/highest), pd, np, SYMBOL, INTERVAL.
# =====================================================================

# ================= פרמטרים =================
PIVOT_LEN        = 5      # אורך פיבוט (נרות בכל צד)
ATR_LEN          = 14
ZONE_WIDTH_ATR   = 0.35   # חצי-רוחב אזור במכפלות ATR
MIN_SUP_TOUCHES  = 1      # נגיעות תמיכה לפני השבירה (מינימום: נגיעה אחת בירידה)
MIN_RES_RETESTS  = 2      # דחיות מההתנגדות אחרי ההיפוך
MIN_ROUNDS       = 3      # תחתיות מעוגלות תחת האזור
MIN_BASE_BARS    = 60     # אורך בסיס מינימלי (נרות) — "ארוך יחסית לטיים-פריים"
POST_BO_BARS     = 20     # חלון לבדיקת תמיכה אחרי פריצה
MIN_RETEST_BARS  = 3      # מינימום נרות התבססות לפני בדיקת תמיכה
TREND_LEN        = 34
LOOKBACK         = 500
MAX_ZONES        = 30

ROUND_WINDOWS       = (30, 60, 120)  # רב-סקאלה: תחתית בכל אורך גל, על כל ההיסטוריה
ROUND_STRIDE        = 2              # דגימת חלון כל 2 נרות (חפיפה מספיקה)
ROUND_DEDUP_BARS    = 15             # מיזוג זיהוי כפול של אותה תחתית (נרות)
ROUND_DEDUP_PCT     = 0.05           # מיזוג זיהוי כפול של אותה תחתית (מחיר)
MIN_R2              = 0.80
MIN_CURVATURE       = 0.0005
MIN_COS_R2          = 0.70   # סף R² להתאמת קוסינוס (שכבת סינון נוספת)
MAX_SIN_RATIO       = 0.60   # |B|/|A| מקסימלי — שומר שהתחתית נשארת במרכז
PRIOR_DROP_PCT      = 50.0
PRIOR_DROP_LOOKBACK = 250

C_PIVOT_LEN        = 3
MIN_CUP_BARS       = 15
MAX_CUP_BARS       = 120
RIM_TOL            = 0.04
MIN_DEPTH          = 0.10
MAX_DEPTH          = 0.99
MAX_HANDLE_RETRACE = 0.618

SHOW_SIGNALS = True
MINTICK      = 0.01


def _pivot_series(h, l, left, right):
    import numpy as np
    n = len(h)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    w = left + right + 1
    for i in range(w - 1, n):
        pb = i - right
        wh = h[pb - left:pb + right + 1]
        wl = l[pb - left:pb + right + 1]
        if h[pb] >= np.nanmax(wh):
            ph[i] = h[pb]
        if l[pb] <= np.nanmin(wl):
            pl[i] = l[pb]
    return ph, pl


def _bb_new_zone(center, width, pbar, is_sup, downtrend):
    return {
        "center": center, "width": width, "touches": 1,
        "sup": 1 if is_sup else 0, "res": 0 if is_sup else 1,
        "first_bar": pbar, "last_bar": pbar,
        "downtrend_origin": bool(is_sup and downtrend),
        "broken": False, "break_bar": None, "retests": 0,
        "bo_bar": None, "bo_done": False, "bo_dead": False,
        "watch_fired": False,
    }


def indicator(df):
    import numpy as np

    n = len(df)
    op    = df["open"].to_numpy(dtype=float)
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    vol   = (df["volume"].to_numpy(dtype=float)
             if "volume" in df.columns else np.full(n, np.nan))

    atr_s    = ta.atr(df["high"], df["low"], df["close"], ATR_LEN).to_numpy(dtype=float)
    trend    = ta.ema(df["close"], TREND_LEN)
    falling  = (trend < trend.shift(TREND_LEN)).to_numpy()
    falling  = np.nan_to_num(falling.astype(float), nan=0.0).astype(bool)
    _sm, _wmin, _wmax, _patlow, _smsz = {}, {}, {}, {}, {}
    for _W in ROUND_WINDOWS:
        _sz = max(5, _W // 6)
        _smsz[_W] = _sz
        _s = ta.sma(df["close"], _sz).to_numpy(dtype=float)
        _sm[_W] = _s
        _wmin[_W] = ta.lowest(_s, _W).to_numpy(dtype=float)
        _wmax[_W] = ta.highest(_s, _W).to_numpy(dtype=float)
        _patlow[_W] = ta.lowest(df["low"], _W).to_numpy(dtype=float)
    _RW_DESC = tuple(sorted(ROUND_WINDOWS, reverse=True))
    prior_high = ta.highest(df["high"], PRIOR_DROP_LOOKBACK).to_numpy(dtype=float)

    ph, pl   = _pivot_series(high, low, PIVOT_LEN, PIVOT_LEN)
    cph, cpl = _pivot_series(high, low, C_PIVOT_LEN, C_PIVOT_LEN)

    markers = []
    seen = set()

    def _mark(bar, side, text):
        key = (int(bar), text)
        if key not in seen:
            seen.add(key)
            markers.append({"bar": int(bar), "side": side, "text": text})

    zones = []
    round_events = []  # (bar, trough_price)

    # ---- מצב C&H (טרייד נפרד) ----
    phB, phP, plB, plP = [], [], [], []
    b2_p = None
    active_neckline = None
    pattern_active = False
    breakout_confirmed = False

    for i in range(n):
        if zones:
            zones = [z for z in zones if i - z["last_bar"] <= LOOKBACK]
        if round_events and round_events[0][0] < i - LOOKBACK:
            round_events = [e for e in round_events if e[0] >= i - LOOKBACK]

        # ---------- קיבוץ פיבוטים לאזורים ----------
        for ptype in (0, 1):  # 0 = שפל (תמיכה), 1 = שיא (התנגדות)
            pv = pl[i] if ptype == 0 else ph[i]
            if np.isnan(pv):
                continue
            is_sup = (ptype == 0)
            pbar = i - PIVOT_LEN
            down_at_pivot = bool(falling[pbar]) if pbar >= 0 else False
            a = atr_s[pbar] if pbar >= 0 else np.nan
            pwidth = max((a if not np.isnan(a) else MINTICK) * ZONE_WIDTH_ATR,
                         MINTICK * 2.0)
            best, best_d = -1, float("inf")
            for zi, z in enumerate(zones):
                d = abs(pv - z["center"])
                if d <= max(z["width"], pwidth) and d < best_d:
                    best, best_d = zi, d
            if best >= 0:
                z = zones[best]
                t = z["touches"]
                z["center"] = (z["center"] * t + pv) / (t + 1)
                z["width"] = max(z["width"], pwidth)
                z["touches"] = t + 1
                z["sup"] += 1 if is_sup else 0
                z["res"] += 0 if is_sup else 1
                z["first_bar"] = min(z["first_bar"], pbar)
                z["last_bar"] = pbar
                if is_sup and down_at_pivot:
                    z["downtrend_origin"] = True
            else:
                if len(zones) >= MAX_ZONES:
                    wi = min(range(len(zones)),
                             key=lambda k: zones[k]["touches"])
                    zones.pop(wi)
                zones.append(_bb_new_zone(pv, pwidth, pbar, is_sup,
                                          down_at_pivot))

        # ---------- שבירה / דחיות / פריצה / בדיקת תמיכה ----------
        for z in zones:
            zc, zw = z["center"], z["width"]
            zup, zlo = zc + zw, zc - zw
            if (not z["broken"] and z["downtrend_origin"]
                    and z["sup"] >= MIN_SUP_TOUCHES and z["sup"] > z["res"]
                    and i > 0 and close[i] < zlo and close[i - 1] >= zlo):
                z["broken"] = True
                z["break_bar"] = i
            if not z["broken"]:
                continue
            if z["bo_bar"] is None:
                piv_after = (i - PIVOT_LEN) > z["break_bar"]
                if (not np.isnan(ph[i]) and piv_after and zlo <= ph[i] <= zup
                        and i - PIVOT_LEN >= 0
                        and close[i - PIVOT_LEN] < zc):
                    z["retests"] += 1
            bb_lim = z["bo_bar"] if z["bo_bar"] is not None else i
            rounds = sum(1 for (b, t) in round_events
                         if z["break_bar"] < b <= bb_lim and t < zlo)
            base_bars = bb_lim - z["break_bar"]
            base_ready = (z["retests"] >= MIN_RES_RETESTS
                          and rounds >= MIN_ROUNDS and base_bars >= MIN_BASE_BARS)
            if (SHOW_SIGNALS and not z["watch_fired"]
                    and z["bo_bar"] is None and base_ready):
                z["watch_fired"] = True
                _mark(i, "buy", "BASE_WATCH")
            if (z["bo_bar"] is None and base_ready
                    and i > 0 and close[i] > zup and close[i - 1] <= zup):
                z["bo_bar"] = i
                if SHOW_SIGNALS:
                    _mark(i, "buy", "Breakout")
            bb = z["bo_bar"]
            if bb is not None and not z["bo_done"] and not z["bo_dead"]:
                if close[i] < zlo:
                    z["bo_dead"] = True
                elif MIN_RETEST_BARS <= i - bb <= POST_BO_BARS:
                    if low[i] <= zup and close[i] >= zlo:
                        s0, s1 = bb + 1, i + 1
                        sv = vol[s0:s1]
                        scls = close[s0:s1]
                        sopn = op[s0:s1]
                        valid = ~np.isnan(sv)
                        seller = valid & (scls < sopn)
                        if int(np.sum(seller)) >= 2:
                            svv = sv[seller]
                            xs = np.arange(len(svv), dtype=float)
                            slope = float(np.polyfit(xs, svv, 1)[0])
                            bo_vol = vol[bb]
                            weaker = (not np.isnan(bo_vol)
                                      and float(np.mean(svv)) < float(bo_vol))
                            if slope < 0 and weaker:
                                z["bo_done"] = True
                                _mark(i, "buy", "BASE_BO")

        # ---------- תחתית מעוגלת (פרבולה + קוסינוס, רב-סקאלה) ----------
        # סורק את כל ההיסטוריה בחלונות בגדלים שונים — תופס U קצר וארוך
        for W in _RW_DESC:
            sm_ = _smsz[W]
            if i < W + sm_ - 1 or (i % ROUND_STRIDE):
                continue
            sb = i - W + 1
            smv = _sm[W]
            if np.isnan(smv[sb]):
                continue
            wmin, wmax = _wmin[W][i], _wmax[W][i]
            if np.isnan(wmin) or np.isnan(wmax) or wmax <= wmin:
                continue
            y = smv[sb:i + 1]
            yn = (y - wmin) / (wmax - wmin)
            x = np.arange(W, dtype=float)
            a_, b_, c_ = np.polyfit(x, yn, 2)  # y = a*x^2 + b*x + c
            vx = -b_ / (2.0 * a_) if a_ != 0 else np.nan
            fit = a_ * x * x + b_ * x + c_
            mean = yn.mean()
            sse = ((yn - fit) ** 2).sum()
            sst = ((yn - mean) ** 2).sum()
            r2 = 1.0 - sse / sst if sst > 0 else 0.0
            valid = (a_ >= MIN_CURVATURE and r2 >= MIN_R2
                     and W * 0.33 < vx < W * 0.66)
            # שכבת קוסינוס: האם החלון נשלט בידי סווינג נקי אחד
            # (גל יחיד בתדר החלון) ולא רעש/קיטועים
            th = 2.0 * np.pi * x / W
            Xc = np.column_stack([np.ones(W), np.cos(th), np.sin(th)])
            coef = np.linalg.lstsq(Xc, yn, rcond=None)[0]
            A_, B_ = float(coef[1]), float(coef[2])
            fitc = Xc @ coef
            ssec = ((yn - fitc) ** 2).sum()
            r2c = 1.0 - ssec / sst if sst > 0 else 0.0
            cos_ok = (A_ > 0 and r2c >= MIN_COS_R2
                      and abs(B_) <= MAX_SIN_RATIO * abs(A_))
            phv = prior_high[i]
            plv = _patlow[W][i]
            drop = ((phv - plv) / phv * 100.0
                    if phv > 0 and not np.isnan(plv) else 0.0)
            setup = bool(valid and cos_ok and drop >= PRIOR_DROP_PCT)
            if setup:
                vbar = sb + int(round(vx))
                trough = wmin + (a_ * vx * vx + b_ * vx + c_) * (wmax - wmin)
                dup = False
                for (eb, et) in round_events:
                    if (abs(eb - vbar) <= ROUND_DEDUP_BARS
                            and abs(et - trough)
                            <= ROUND_DEDUP_PCT * max(abs(et), 1e-9)):
                        dup = True
                        break
                if not dup:
                    round_events.append((vbar, float(trough)))
                    if SHOW_SIGNALS:
                        _mark(i, "buy", "Round")

        # ---------- C&H: איסוף פיבוטים (טרייד נפרד) ----------
        ccph = cph[i]
        ccpl = cpl[i]
        if not np.isnan(ccph):
            phB.append(i - C_PIVOT_LEN)
            phP.append(float(ccph))
            if len(phB) > 50:
                phB.pop(0); phP.pop(0)
        if not np.isnan(ccpl):
            plB.append(i - C_PIVOT_LEN)
            plP.append(float(ccpl))
            if len(plB) > 50:
                plB.pop(0); plP.pop(0)

        # ---------- C&H: זיהוי (בכל שיא-פיבוט חדש) ----------
        if len(phB) >= 3 and len(plB) >= 2 and not np.isnan(ccph):
            pR3_bar, pR3_p = phB[-1], phP[-1]
            pR2_bar, pR2_p = phB[-2], phP[-2]
            for k in range(len(phB) - 3, max(0, len(phB) - 8) - 1, -1):
                pR1_bar, pR1_p = phB[k], phP[k]
                cup_bars = pR2_bar - pR1_bar
                if not (MIN_CUP_BARS <= cup_bars <= MAX_CUP_BARS):
                    continue
                avg_neck = (pR1_p + pR2_p) / 2.0
                if avg_neck <= 0:
                    continue
                if abs(pR1_p - pR2_p) / avg_neck > RIM_TOL:
                    continue
                lc_p = lc_b = None
                for jb, jp in zip(plB, plP):
                    if pR1_bar < jb < pR2_bar and (lc_p is None or jp < lc_p):
                        lc_p, lc_b = jp, jb
                lh_p = lh_b = None
                for jb, jp in zip(plB, plP):
                    if pR2_bar < jb < pR3_bar and (lh_p is None or jp < lh_p):
                        lh_p, lh_b = jp, jb
                if lc_p is None or lh_p is None:
                    continue
                depth = (avg_neck - lc_p) / avg_neck
                depth_val = avg_neck - lc_p
                handle_pb = pR2_p - lh_p
                if (MIN_DEPTH <= depth <= MAX_DEPTH and lh_p > lc_p
                        and handle_pb <= depth_val * MAX_HANDLE_RETRACE):
                    b2_p = lh_p
                    active_neckline = avg_neck
                    pattern_active = True
                    breakout_confirmed = False
                    if SHOW_SIGNALS:
                        _mark(i, "buy", "C&H")
                    break

        # ---------- C&H: מעקב פריצה ----------
        if pattern_active and not breakout_confirmed and b2_p is not None:
            if close[i] < b2_p:
                pattern_active = False
            elif i > 0 and close[i] > active_neckline >= close[i - 1]:
                breakout_confirmed = True
                if SHOW_SIGNALS:
                    _mark(i, "buy", "C&H BO")

    if SHOW_SIGNALS:
        return {"markers": markers}
    return {"markers": markers}
'''


PY_BASEBO_SCAN_EXAMPLE = (PY_BASEBO_INDICATOR_EXAMPLE + '''

# ================= עטיפת סריקה =================
# signal=True אם BASE_BO / BASE_WATCH / Breakout / C&H / C&H BO
# אותרו ב-3 הנרות האחרונים.
# משקלים: BASE_BO (הטרייד של אלרואי) גבוה ביותר; C&H מתויג בנפרד (טרייד אחר).
def scan(df):
    out = indicator(df)
    markers = out.get("markers") or []
    n = len(df)
    recent = [m for m in markers
              if isinstance(m, dict) and m.get("bar", -1) >= n - 3]
    if not recent:
        return {"signal": False, "score": 0.0, "note": ""}
    weights = {"BASE_BO": 3.0, "BASE_WATCH": 2.0, "C&H BO": 1.5,
               "Breakout": 1.0, "C&H": 1.0, "Round": 0.5}
    names = sorted({str(m.get("text", "?")) for m in recent})
    score = max([weights.get(t, 0.5) for t in names])
    return {"signal": True,
            "score": float(score),
            "note": "דפוסים ב-3 נרות אחרונים: " + ", ".join(names)}
''')
