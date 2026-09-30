"""
pine_engine.py — Pine Script v5 subset interpreter (safe, dependency-free).

Lets users paste real Pine Script code and run it as an indicator (plot()
overlays on the chart) or as a strategy (strategy.entry()/strategy.close()
backtest with Buy/Sell markers) — the same way TradingView does, but
server-side on the free tier.

This is a *subset* interpreter, not a full Pine compiler. Supported:
  - indicator()/strategy() headers (title, overlay)
  - input.int/float/bool/string/source/color/timeframe (defaults)
  - ta.ema/sma/rma/wma/rsi/macd/stdev/variance/atr/tr/highest/lowest/
    highestbars/lowestbars/change/mom/roc/cum/crossover/crossunder/vwap/
    cci/mfi/wpr/stoch/linreg/correlation/barssince/valuewhen/supertrend
  - math.abs/max/min/sum/avg/floor/ceil/round/sqrt/pow/exp/log/sign
  - nz(), na(), fixnan()
  - plot(), plotshape(), plotchar(), hline()  (bgcolor/fill are ignored)
  - alert(), alertcondition() — נאספות ומוצגות בתוצאות
  - strategy.entry()/strategy.close()/strategy.order() with when=
  - strategy.exit() עם profit/loss (בטיקים, $0.01 למניות), limit/stop (מחיר), from_entry
  - strategy() params: initial_capital, commission_type/value (percent)
  - if/else blocks (strategy.* calls + masked assignments + alert() inside)
  - for i = 0 to 9 [by 2] / downto, while (תנאי סקלרי), switch כביטוי בהשמה
  - single-line functions:  f(x) => x * 2
  - tuple unpacking:  [a, b] = ta.macd(...)
  - := reassignment, var, השמה רקורסיבית x := ...x[1]... (עד 2000 נרות),
    ?: ternary, close[1] history referencing
  - dayofweek (1=ראשון..7=שבת) עם dayofweek.monday וכו', str.tostring()

Everything else raises PineError with a Hebrew explanation of what to change.
Series are plain Python lists, None means `na`. No eval(), no imports —
expressions go through an AST whitelist, so pasted code cannot touch the
server.
"""

import ast
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


class PineError(Exception):
    """User-facing error (Hebrew message)."""
    pass


# =====================================================================
# Series helpers — series are Python lists, None means `na`
# =====================================================================

def _is_series(v: Any) -> bool:
    return isinstance(v, list)


def _contains_str(v: Any) -> bool:
    """True if v is a string or a series whose first known value is a string."""
    if isinstance(v, str):
        return True
    if _is_series(v):
        for x in v:
            if x is None:
                continue
            return isinstance(x, str)
    return False


def _as_series(v: Any, n: int) -> List:
    return v if _is_series(v) else [v] * n


def _truthy(x: Any) -> bool:
    return x is not None and x is not False and x != 0


def _as_bool_series(v: Any, n: int) -> List[bool]:
    return [_truthy(x) for x in _as_series(v, n)]


def _valid_window(s: List, i: int, length: int) -> bool:
    return (
        i - length + 1 >= 0
        and all(x is not None for x in s[i - length + 1 : i + 1])
    )


# =====================================================================
# ta.* — None-aware indicator implementations (Pine semantics)
# =====================================================================

def p_ema(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1 or n == 0:
        return out
    k = 2.0 / (length + 1)
    prev = None
    seeded = False
    for i in range(n):
        if not seeded:
            if _valid_window(s, i, length):
                prev = sum(s[i - length + 1 : i + 1]) / length
                out[i] = prev
                seeded = True
            continue
        v = s[i]
        if v is None:
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def p_sma(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1 or n == 0:
        return out
    for i in range(n):
        if _valid_window(s, i, length):
            out[i] = sum(s[i - length + 1 : i + 1]) / length
    return out


def p_rma(s: List, length: int) -> List:
    """Wilder's smoothing (Pine's ta.rma)."""
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1 or n == 0:
        return out
    prev = None
    seeded = False
    for i in range(n):
        if not seeded:
            if _valid_window(s, i, length):
                prev = sum(s[i - length + 1 : i + 1]) / length
                out[i] = prev
                seeded = True
            continue
        v = s[i]
        if v is None:
            continue
        prev = (prev * (length - 1) + v) / length
        out[i] = prev
    return out


def p_wma(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1 or n == 0:
        return out
    denom = length * (length + 1) / 2.0
    for i in range(n):
        if _valid_window(s, i, length):
            w = s[i - length + 1 : i + 1]
            out[i] = sum(x * (j + 1) for j, x in enumerate(w)) / denom
    return out


def p_rsi(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1 or n < 2:
        return out
    avg_g = avg_l = None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        if s[i] is None or s[i - 1] is None:
            gains.append(None); losses.append(None)  # type: ignore
            continue
        d = s[i] - s[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
        if len([g for g in gains if g is not None]) == length and avg_g is None:
            gg = [g for g in gains if g is not None][-length:]
            ll = [l for l in losses if l is not None][-length:]
            avg_g = sum(gg) / length
            avg_l = sum(ll) / length
            out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        elif avg_g is not None and gains[-1] is not None:
            avg_g = (avg_g * (length - 1) + gains[-1]) / length
            avg_l = (avg_l * (length - 1) + losses[-1]) / length
            out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def p_stdev(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1 or n == 0:
        return out
    for i in range(n):
        if _valid_window(s, i, length):
            w = s[i - length + 1 : i + 1]
            mean = sum(w) / length
            out[i] = math.sqrt(sum((x - mean) ** 2 for x in w) / length)
    return out


def p_tr(h: List, l: List, c: List) -> List:
    n = len(c); out: List = [None] * n
    for i in range(n):
        if h[i] is None or l[i] is None or c[i] is None:
            continue
        if i == 0 or c[i - 1] is None:
            out[i] = h[i] - l[i]
        else:
            out[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return out


def p_atr(h: List, l: List, c: List, length: int) -> List:
    tr = p_tr(h, l, c)
    return p_rma(tr, length)


def p_highest(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1:
        return out
    for i in range(n):
        if i - length + 1 < 0:
            continue
        w = [x for x in s[i - length + 1 : i + 1] if x is not None]
        out[i] = max(w) if w else None
    return out


def p_lowest(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    if length < 1:
        return out
    for i in range(n):
        if i - length + 1 < 0:
            continue
        w = [x for x in s[i - length + 1 : i + 1] if x is not None]
        out[i] = min(w) if w else None
    return out


def p_change(s: List, length: int = 1) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    for i in range(n):
        if i - length >= 0 and s[i] is not None and s[i - length] is not None:
            out[i] = s[i] - s[i - length]
    return out


def p_mom(s: List, length: int) -> List:
    return p_change(s, length)


def p_roc(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    for i in range(n):
        if i - length >= 0 and s[i] is not None and s[i - length] is not None and s[i - length] != 0:
            out[i] = (s[i] - s[i - length]) / abs(s[i - length]) * 100
    return out


def p_crossover(a: List, b: List) -> List[bool]:
    n = len(a); out = [False] * n
    for i in range(1, n):
        if a[i - 1] is None or b[i - 1] is None or a[i] is None or b[i] is None:
            continue
        out[i] = a[i - 1] <= b[i - 1] and a[i] > b[i]
    return out


def p_crossunder(a: List, b: List) -> List[bool]:
    n = len(a); out = [False] * n
    for i in range(1, n):
        if a[i - 1] is None or b[i - 1] is None or a[i] is None or b[i] is None:
            continue
        out[i] = a[i - 1] >= b[i - 1] and a[i] < b[i]
    return out


def p_macd(s: List, fast: int, slow: int, signal: int) -> Tuple[List, List, List]:
    ef, es = p_ema(s, fast), p_ema(s, slow)
    n = len(s)
    macd_line: List = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None:
            macd_line[i] = ef[i] - es[i]
    sig_raw = p_ema([x for x in macd_line if x is not None], signal)
    signal_line: List = [None] * n
    first = next((i for i, x in enumerate(macd_line) if x is not None), None)
    if first is not None:
        for j, v in enumerate(sig_raw):
            if v is not None and first + j < n:
                signal_line[first + j] = v
    hist: List = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, hist


def p_vwap(src: List, vol: List, times: List[int]) -> List:
    """Session (daily) VWAP."""
    n = len(src); out: List = [None] * n
    pv = 0.0; vv = 0.0; day = None
    from datetime import datetime, timezone
    for i in range(n):
        d = datetime.fromtimestamp(times[i], tz=timezone.utc).date()
        if d != day:
            day = d; pv = 0.0; vv = 0.0
        v = vol[i] if vol[i] else 1.0
        if src[i] is None:
            continue
        pv += src[i] * v; vv += v
        out[i] = pv / vv if vv else None
    return out


def p_cci(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    for i in range(n):
        if not _valid_window(s, i, length):
            continue
        win = s[i - length + 1 : i + 1]
        sma = sum(win) / length
        md = sum(abs(x - sma) for x in win) / length
        out[i] = (s[i] - sma) / (0.015 * md) if md else 0.0
    return out


def p_mfi(h: List, l: List, c: List, vol: List, length: int) -> List:
    n = len(c); out: List = [None] * n; length = int(length)
    tp = [None if a is None or b is None or d is None else (a + b + d) / 3
          for a, b, d in zip(h, l, c)]
    for i in range(n):
        if i < length or any(tp[i - j] is None for j in range(length + 1)):
            continue
        pos = neg = 0.0
        for j in range(i - length + 1, i + 1):
            v = vol[j] if vol[j] else 1.0
            mf = tp[j] * v
            if tp[j] > tp[j - 1]:
                pos += mf
            elif tp[j] < tp[j - 1]:
                neg += mf
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return out


def p_wpr(h: List, l: List, c: List, length: int) -> List:
    n = len(c); out: List = [None] * n; length = int(length)
    for i in range(n):
        if not (_valid_window(h, i, length) and _valid_window(l, i, length)) or c[i] is None:
            continue
        hh = max(h[i - length + 1 : i + 1]); ll = min(l[i - length + 1 : i + 1])
        out[i] = (hh - c[i]) / (hh - ll) * -100 if hh != ll else 0.0
    return out


def p_stoch(c: List, h: List, l: List, k: int, d: int, smooth: int) -> Tuple[List, List]:
    n = len(c); kk: List = [None] * n
    k, d, smooth = int(k), int(d), int(smooth)
    for i in range(n):
        if not (_valid_window(h, i, k) and _valid_window(l, i, k)) or c[i] is None:
            continue
        hh = max(h[i - k + 1 : i + 1]); ll = min(l[i - k + 1 : i + 1])
        kk[i] = (c[i] - ll) / (hh - ll) * 100 if hh != ll else 0.0
    kk = p_sma([x if x is not None else float("nan") for x in kk], smooth)
    kk = [None if x is None or (isinstance(x, float) and math.isnan(x)) else x for x in kk]
    dd = p_sma([x if x is not None else float("nan") for x in kk], d)
    dd = [None if x is None or (isinstance(x, float) and math.isnan(x)) else x for x in dd]
    return kk, dd


def p_linreg(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    sx = length * (length - 1) / 2
    sxx = (length - 1) * length * (2 * length - 1) / 6
    denom = length * sxx - sx * sx
    for i in range(n):
        if not _valid_window(s, i, length):
            continue
        win = s[i - length + 1 : i + 1]
        sy = sum(win); sxy = sum(j * v for j, v in enumerate(win))
        if not denom:
            out[i] = win[-1]; continue
        slope = (length * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / length
        out[i] = intercept + slope * (length - 1)
    return out


def p_correlation(a: List, b: List, length: int) -> List:
    n = len(a); out: List = [None] * n; length = int(length)
    for i in range(n):
        if not (_valid_window(a, i, length) and _valid_window(b, i, length)):
            continue
        wa = a[i - length + 1 : i + 1]; wb = b[i - length + 1 : i + 1]
        ma = sum(wa) / length; mb = sum(wb) / length
        cov = sum((x - ma) * (y - mb) for x, y in zip(wa, wb))
        va = sum((x - ma) ** 2 for x in wa); vb = sum((y - mb) ** 2 for y in wb)
        out[i] = cov / math.sqrt(va * vb) if va and vb else 0.0
    return out


def p_variance(s: List, length: int) -> List:
    sd = p_stdev(s, length)
    return [None if x is None else x * x for x in sd]


def p_cum(s: List) -> List:
    out: List = []
    tot = 0.0; started = False
    for x in s:
        if x is None:
            out.append(tot if started else None)
        else:
            tot += x; started = True
            out.append(tot)
    return out


def p_barssince(cond: List) -> List:
    n = len(cond); out: List = [None] * n
    last = None
    for i in range(n):
        if cond[i]:
            last = i
        out[i] = None if last is None else i - last
    return out


def p_valuewhen(cond: List, src: List, occurrence: int) -> List:
    n = len(cond); out: List = [None] * n
    occurrence = int(occurrence)
    hits = [i for i in range(n) if cond[i]]
    for i in range(n):
        past = [j for j in hits if j <= i]
        if len(past) > occurrence:
            out[i] = src[past[-1 - occurrence]]
    return out


def p_highestbars(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    for i in range(n):
        if not _valid_window(s, i, length):
            continue
        win = s[i - length + 1 : i + 1]
        out[i] = win.index(max(win)) - (length - 1)
    return out


def p_lowestbars(s: List, length: int) -> List:
    n = len(s); out: List = [None] * n; length = int(length)
    for i in range(n):
        if not _valid_window(s, i, length):
            continue
        win = s[i - length + 1 : i + 1]
        out[i] = win.index(min(win)) - (length - 1)
    return out


def p_supertrend(h: List, l: List, c: List, factor: float, atr_len: int) -> Tuple[List, List]:
    n = len(c)
    atr = p_atr(h, l, c, int(atr_len))
    st: List = [None] * n
    direction: List = [1] * n
    prev_upper = prev_lower = None
    prev_st = None; prev_dir = 1
    for i in range(n):
        if c[i] is None or atr[i] is None or h[i] is None or l[i] is None:
            continue
        basic_upper = (h[i] + l[i]) / 2 + factor * atr[i]
        basic_lower = (h[i] + l[i]) / 2 - factor * atr[i]
        upper = basic_upper if (prev_upper is None or basic_upper < prev_upper or (prev_st is not None and c[i - 1] is not None and c[i - 1] > prev_upper)) else prev_upper
        lower = basic_lower if (prev_lower is None or basic_lower > prev_lower or (prev_st is not None and c[i - 1] is not None and c[i - 1] < prev_lower)) else prev_lower
        if prev_st is None:
            d = 1
        elif prev_dir == 1 and c[i] < lower:
            d = -1
        elif prev_dir == -1 and c[i] > upper:
            d = 1
        else:
            d = prev_dir
        st[i] = lower if d == 1 else upper
        direction[i] = d
        prev_upper, prev_lower, prev_st, prev_dir = upper, lower, st[i], d
    return st, direction


# math.* — elementwise, None propagates
def _m2(fn):
    def f(a, b, n):
        la, lb = _as_series(a, n), _as_series(b, n)
        return [None if x is None or y is None else fn(x, y) for x, y in zip(la, lb)]
    return f


def _m1(fn):
    def f(a, n):
        return [None if x is None else fn(x) for x in _as_series(a, n)]
    return f


# =====================================================================
# Namespaces & constants
# =====================================================================

COLORS = {
    "red": "#f23645", "green": "#089981", "blue": "#2962ff",
    "orange": "#ff9800", "purple": "#7e57c2", "yellow": "#ffee58",
    "black": "#000000", "white": "#ffffff", "gray": "#787b86",
    "aqua": "#00bcd4", "teal": "#00897b", "lime": "#c0ca33",
    "maroon": "#880e4f", "navy": "#1a237e", "olive": "#827717",
    "silver": "#b0b0b0", "fuchsia": "#e040fb",
}


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _DOW(list):
    """סדרת dayofweek (1=ראשון..7=שבת) עם קבועים dayofweek.sunday וכו'."""


# =====================================================================
# Runner — parses statements, evaluates expressions safely via AST
# =====================================================================

_ASSIGN_RE = re.compile(r":=|=(?![=>])")
_FUNCDEF_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(([^)]*)\)\s*=>\s*(.+)$", re.S)
_FOR_RE = re.compile(r"^for\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s+(to|downto)\s+(.+?)(?:\s+by\s+(.+))?$", re.S | re.I)
_FOR_IN_RE = re.compile(r"^for\s+[A-Za-z_]\w*\s+in\b", re.I)
_SWITCH_ASSIGN_RE = re.compile(r"^(?:var\s+)?([A-Za-z_]\w*)\s*(:?=)\s*switch\b\s*(.*)$", re.I | re.S)


def _strip_comment(line: str) -> str:
    out, instr = [], None
    i = 0
    while i < len(line):
        c = line[i]
        if instr:
            out.append(c)
            if c == instr and line[i - 1] != "\\":
                instr = None
        elif c in "\"'":
            instr = c
            out.append(c)
        elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _paren_depth(text: str) -> int:
    """עומק סוגריים לא סגורים, תוך התעלמות ממחרוזות."""
    depth, instr = 0, None
    i = 0
    while i < len(text):
        c = text[i]
        if instr:
            if c == instr and text[i - 1] != "\\":
                instr = None
        elif c in "\"'":
            instr = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        i += 1
    return depth


def _join_continuations(lines):
    """איחוד שורות המשך: שורה עם סוגריים לא סגורים מתחברת לבאות אחריה."""
    out = []
    i = 0
    while i < len(lines):
        ind, text = lines[i]
        buf = text
        j = i
        guard = 0
        while _paren_depth(buf) > 0 and j + 1 < len(lines) and guard < 100:
            j += 1
            guard += 1
            buf += " " + lines[j][1].strip()
        if _paren_depth(buf) > 0:
            raise PineError(f"סוגריים לא סגורים ליד: `{buf[:60]}`")
        out.append((ind, buf))
        i = j + 1
    return out
    out = []
    i = 0
    instr = None
    while i < len(line):
        c = line[i]
        if instr:
            out.append(c)
            if c == instr and (i == 0 or line[i - 1] != "\\"):
                instr = None
        elif c in "\"'":
            instr = c
            out.append(c)
        elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).rstrip()


def _find_assign_op(text: str):
    """Locate := or = at depth 0. Returns (pos, op) or (None, None)."""
    depth = 0
    instr = None
    i = 0
    while i < len(text):
        c = text[i]
        if instr:
            if c == instr and text[i - 1] != "\\":
                instr = None
        elif c in "\"'":
            instr = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0:
            if text.startswith(":=", i):
                return i, ":="
            if c == "=" and not text.startswith("==", i) and not text.startswith("=>", i) \
                    and text[i - 1:i] not in ("!", "<", ">", "="):
                return i, "="
        i += 1
    return None, None


def _pine_ternary(expr: str) -> str:
    """Rewrite Pine's `cond ? a : b` as Python's `a if cond else b`.

    Assignment-aware: `x = c ? a : b` transforms only the right-hand side.
    """
    pos, op = _find_assign_op(expr)
    if pos is not None:
        lhs, rhs = expr[:pos].strip(), expr[pos + len(op):]
        # := is normalized to = for parsing; real op is re-detected from source text
        return f"{lhs} = {_ternary_expr(rhs)}"
    return _ternary_expr(expr)


def _ternary_expr(expr: str) -> str:
    """Rewrite Pine's `cond ? a : b` as Python's `a if cond else b`, recursively.

    Handles nesting and ternaries inside call args: f(x ? 1 : 0, y).
    String literals are masked so `?`/`:` inside them are ignored.
    """
    chars = list(expr)
    instr = None
    i = 0
    while i < len(chars):
        c = chars[i]
        if instr:
            chars[i] = " "
            if c == instr and expr[i - 1] != "\\":
                instr = None
        elif c in "\"'":
            instr = c
            chars[i] = " "
        i += 1
    masked = "".join(chars)

    qpos = masked.find("?")
    if qpos == -1:
        return expr
    qdepth = 0
    for c in masked[:qpos]:
        if c in "([":
            qdepth += 1
        elif c in ")]":
            qdepth -= 1

    # walk back to the condition start
    depth = qdepth
    cstart = 0
    k = qpos - 1
    while k >= 0:
        c = masked[k]
        if c in ")]":
            depth += 1
        elif c in "([":
            depth -= 1
            if depth < qdepth:
                cstart = k + 1
                break
        elif depth == qdepth and c in ",:":
            cstart = k + 1
            break
        k -= 1

    # find the matching ':'
    depth = qdepth
    nested = 0
    cpos = -1
    k = qpos + 1
    while k < len(masked):
        c = masked[k]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth < qdepth:
                break
        elif c == "?" and depth >= qdepth:
            nested += 1
        elif c == ":" and depth >= qdepth:
            if nested == 0:
                cpos = k
                break
            nested -= 1
        k += 1
    if cpos == -1:
        raise PineError("תחביר `? :` לא תקין — חסר `:`")

    # find the end of b (',' or closing paren at qdepth)
    depth = qdepth
    bend = len(masked)
    k = cpos + 1
    while k < len(masked):
        c = masked[k]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth < qdepth:
                bend = k
                break
        elif c == "," and depth == qdepth:
            bend = k
            break
        k += 1

    before = expr[:cstart]
    cond = expr[cstart:qpos].strip()
    a = expr[qpos + 1:cpos].strip()
    b = expr[cpos + 1:bend].strip()
    after = expr[bend:]
    if not cond or not a or not b:
        raise PineError("תחביר `? :` לא תקין")
    new = (f"{before}({_ternary_expr(a)}) if ({_ternary_expr(cond)}) "
           f"else ({_ternary_expr(b)}){after}")
    return _ternary_expr(new)


class _Runner:

    def __init__(self, candles: List[Dict], symbol: str, period: str, interval: str):
        self.n = len(candles)
        n = self.n
        o = [c["open"] for c in candles]
        h = [c["high"] for c in candles]
        l = [c["low"] for c in candles]
        cl = [c["close"] for c in candles]
        times = [c["time"] for c in candles]
        vol = [c.get("volume") for c in candles]
        self.candles = candles
        self.times = times
        self.symbol = symbol
        self.ns: Dict[str, Any] = {
            "open": o, "high": h, "low": l, "close": cl, "volume": vol,
            "hlc3": [(a + b + d) / 3 for a, b, d in zip(h, l, cl)],
            "hl2": [(a + b) / 2 for a, b in zip(h, l)],
            "ohlc4": [(a + b + d + e) / 4 for a, b, d, e in zip(o, h, l, cl)],
            "bar_index": list(range(n)),
            "time": [t * 1000 for t in times],
            "dayofweek": _DOW([datetime.fromtimestamp(t, tz=timezone.utc).isoweekday() % 7 + 1 for t in times]),
            "na": None, "true": True, "false": False,
            "syminfo": _NS(ticker=symbol),
            "timeframe": _NS(period=interval),
            "barstate": _NS(
                isfirst=[i == 0 for i in range(n)],
                islast=[i == n - 1 for i in range(n)],
                isnew=[True] * n,
                isconfirmed=[True] * n,
            ),
            "strategy": _NS(long="long", short="short",
                           percent_of_equity="percent_of_equity", cash="cash",
                           position_size=[0] * n,
                           commission=_NS(percent="percent",
                                          cash_per_order="cash_per_order",
                                          cash_per_contract="cash_per_contract")),
            "color": _NS(**{k: v for k, v in COLORS.items()}),
            "location": _NS(abovebar="aboveBar", belowbar="belowBar",
                             top="aboveBar", bottom="belowBar"),
            "shape": _NS(triangleup="arrowUp", triangledown="arrowDown",
                         circle="circle", diamond="circle", square="circle",
                         labelup="arrowUp", labeldown="arrowDown"),
        }
        self.meta = {"kind": "indicator", "title": "Pine Script", "overlay": True}
        self.plots: List[Dict] = []
        self.shape_markers: List[Dict] = []
        self.hlines: List[Dict] = []
        self.alerts: List[Dict] = []   # {time, message}
        self.entries: List[Dict] = []   # {id, dir, when}
        self.closes: List[Dict] = []    # {id, when}
        self.exits: List[Dict] = []     # {id, from_entry, when, profit_t, loss_t, limit, stop}
        self.funcs: Dict[str, Tuple[List[str], str]] = {}
        self.inputs: Dict[str, Any] = {}
        self.notes: List[str] = []
        self._position_used = False
        self._tick_noted = False

    # ---------------- parsing ----------------

    def parse(self, code: str):
        m = re.search(r"//@version\s*=\s*(\d+)", code)
        ver = int(m.group(1)) if m else None
        self.meta["pine_version"] = ver
        if ver is not None and ver not in (5, 6):
            self.notes.append(
                f"זוהתה גרסת Pine v{ver} — רץ במצב תאימות (אותה תת-קבוצה נתמכת כמו v5/v6)"
            )
        lines = []
        for raw in code.splitlines():
            t = _strip_comment(raw)
            if t.strip():
                indent = len(t) - len(t.lstrip(" \t"))
                lines.append((indent, t.strip()))
        if not lines:
            raise PineError("הקוד ריק")
        base = min(i for i, _ in lines)
        lines = [(i - base, t) for i, t in lines]
        lines = _join_continuations(lines)
        stmts, pos = self._build(lines, 0, -1)
        if pos != len(lines):
            raise PineError("שגיאת הזחה בקוד")
        self._exec_block(stmts)

    def _build(self, lines, pos, parent_indent):
        stmts = []
        block_indent = None
        while pos < len(lines):
            ind, text = lines[pos]
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind != block_indent:
                raise PineError(f"שגיאת הזחה ליד: `{text[:40]}`")
            low = text.lower()
            if low == "if" or low.startswith("if "):
                cond = text[2:].strip()
                if not cond:
                    raise PineError("תנאי `if` ריק")
                pos += 1
                then, pos = self._build(lines, pos, ind)
                if not then:
                    raise PineError("בלוק `if` ריק")
                els = []
                if pos < len(lines) and lines[pos][0] == ind and lines[pos][1].lower() == "else":
                    pos += 1
                    els, pos = self._build(lines, pos, ind)
                stmts.append(("if", cond, then, els))
            elif low.startswith("for "):
                if _FOR_IN_RE.match(text):
                    raise PineError("לולאות `for x in מערך` אינן נתמכות (אין מערכים) — נסה צורה וקטורית עם ta.*")
                m = _FOR_RE.match(text)
                if not m:
                    raise PineError(
                        "תחביר `for` לא מובן — צורה נתמכת: `for i = 0 to 9` / `for i = 9 downto 0` / `for i = 0 to 9 by 2`"
                    )
                var, a_expr, direction, b_expr, by_expr = m.group(1), m.group(2), m.group(3).lower(), m.group(4), m.group(5)
                pos += 1
                body, pos = self._build(lines, pos, ind)
                if not body:
                    raise PineError("גוף לולאת `for` ריק")
                stmts.append(("for", var, a_expr.strip(), direction, b_expr.strip(),
                              by_expr.strip() if by_expr else None, body))
            elif low.startswith("while "):
                cond = text[5:].strip()
                if not cond:
                    raise PineError("תנאי `while` ריק")
                pos += 1
                body, pos = self._build(lines, pos, ind)
                if not body:
                    raise PineError("גוף לולאת `while` ריק")
                stmts.append(("while", cond, body))
            elif re.match(r"^(?:var\s+)?[A-Za-z_]\w*\s*:?=\s*switch\b", text, re.I):
                sm = _SWITCH_ASSIGN_RE.match(text)
                if not sm:
                    raise PineError("`switch` נתמך רק כביטוי בהשמה: `x = switch y`")
                lhs, op, disc = sm.group(1), sm.group(2), sm.group(3).strip()
                pos += 1
                cases = []
                while pos < len(lines) and lines[pos][0] > ind:
                    ctext = lines[pos][1]
                    if "=>" not in ctext:
                        raise PineError(f"שורת `switch` לא תקינה (חסר `=>`): `{ctext[:40]}`")
                    vpart, rpart = ctext.split("=>", 1)
                    vpart, rpart = vpart.strip(), rpart.strip()
                    if not rpart:
                        raise PineError("תוצאת `case` ריקה ב-`switch`")
                    vals = None if not vpart else [v.strip() for v in self._split_top(vpart)]
                    cases.append((vals, rpart))
                    pos += 1
                if not cases:
                    raise PineError("בלוק `switch` ריק")
                stmts.append(("switch", lhs, op, disc, cases))
            else:
                stmts.append(("stmt", text))
                pos += 1
        return stmts, pos

    # ---------------- execution ----------------

    def _exec_block(self, stmts):
        for s in stmts:
            if s[0] == "if":
                self._exec_if(s[1], s[2], s[3])
            elif s[0] == "for":
                self._exec_for(s[1], s[2], s[3], s[4], s[5], s[6])
            elif s[0] == "while":
                self._exec_while(s[1], s[2])
            elif s[0] == "switch":
                self._exec_switch(s[1], s[2], s[3], s[4])
            else:
                self._exec_line(s[1])

    def _eval_scalar(self, expr, what):
        v = self._eval_expr(expr)
        if _is_series(v):
            uniq = {x for x in v if x is not None}
            if len(uniq) != 1:
                raise PineError(f"{what}: נדרש ערך סקלרי (לא סדרה)")
            v = next(iter(uniq))
        if v is None or isinstance(v, bool):
            raise PineError(f"{what}: נדרש מספר")
        return v

    def _exec_for(self, var, a_expr, direction, b_expr, by_expr, body):
        a = self._eval_scalar(a_expr, "גבול לולאת for")
        b = self._eval_scalar(b_expr, "גבול לולאת for")
        step = self._eval_scalar(by_expr, "צעד לולאת for") if by_expr else 1
        if step == 0:
            raise PineError("צעד 0 בלולאת for")
        step = abs(step) if direction == "to" else -abs(step)
        vals = []
        v = a
        guard = 0
        if step > 0:
            while v <= b:
                vals.append(v); v += step; guard += 1
                if guard > 100000:
                    raise PineError("לולאת for חרגה מ-100,000 איטרציות")
        else:
            while v >= b:
                vals.append(v); v += step; guard += 1
                if guard > 100000:
                    raise PineError("לולאת for חרגה מ-100,000 איטרציות")
        if not vals:
            return
        missing = object()
        saved = self.ns.get(var, missing)
        try:
            for v in vals:
                self.ns[var] = v
                self._exec_block(body)
        finally:
            if saved is missing:
                self.ns.pop(var, None)
            else:
                self.ns[var] = saved

    def _exec_while(self, cond, body):
        guard = 0
        while True:
            c = self._eval_expr(cond)
            if _is_series(c):
                # תנאי סקלרי הופך לסדרה קבועה — מקבלים אם כל הערכים זהים
                uniq = {bool(_truthy(x)) for x in c}
                if len(uniq) != 1:
                    raise PineError("תנאי while חייב להיות סקלרי (true/false), לא סדרה משתנה")
                c = next(iter(uniq))
            if not _truthy(c):
                break
            self._exec_block(body)
            guard += 1
            if guard > 100000:
                raise PineError("לולאת while חרגה מ-100,000 איטרציות")

    def _exec_switch(self, lhs, op, disc_expr, cases):
        n = self.n
        ld = _as_series(self._eval_expr(disc_expr), n)
        result = [None] * n
        matched = [False] * n
        for val_exprs, rexpr in cases:
            rval = _as_series(self._eval_expr(rexpr), n)
            if val_exprs is None:
                mask = [not m for m in matched]
            else:
                mask = [False] * n
                for ve in val_exprs:
                    vv = _as_series(self._eval_expr(ve), n)
                    for i in range(n):
                        if (not matched[i] and ld[i] is not None and vv[i] is not None
                                and ld[i] == vv[i]):
                            mask[i] = True
            for i in range(n):
                if mask[i]:
                    result[i] = rval[i]
                    matched[i] = True
        if op == ":=" and lhs not in self.ns:
            raise PineError(f"`:=` דורש שהמשתנה `{lhs}` יוגדר קודם")
        self.ns[lhs] = result

    def _exec_if(self, cond, then, els):
        cond_s = _as_bool_series(self._eval_expr(cond), self.n)
        for s in then:
            self._exec_guarded(s, cond_s, invert=False)
        if els:
            inv = [not c for c in cond_s]
            for s in els:
                self._exec_guarded(s, inv, invert=False)

    def _exec_guarded(self, stmt, mask, invert):
        """Execute a statement from inside an if-block (masked semantics)."""
        if stmt[0] == "if":
            # nested if: combine masks
            inner = _as_bool_series(self._eval_expr(stmt[1]), self.n)
            combined = [m and i for m, i in zip(mask, inner)]
            for s in stmt[2]:
                self._exec_guarded(s, combined, False)
            if stmt[3]:
                inv = [m and not i for m, i in zip(mask, inner)]
                for s in stmt[3]:
                    self._exec_guarded(s, inv, False)
            return
        if stmt[0] in ("for", "while", "switch"):
            raise PineError(f"`{stmt[0]}` בתוך `if` אינו נתמך — הוצא אותו אל מחוץ ל-if")
        text = stmt[1]
        try:
            tree = ast.parse(_pine_ternary(text), mode="exec")
        except SyntaxError:
            raise PineError(f"שגיאת תחביר ליד: `{text[:60]}`")
        node = tree.body[0]
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id in ("alert", "alertcondition"):
                self._call_alert(fn.id, node.value, None, mask=mask)
                return
            self._eval_strategy_call_guarded(node.value, mask)
            return
        if isinstance(node, ast.Assign):
            self._exec_assign_guarded(node, text, mask)
            return
        raise PineError(
            "בתוך `if` נתמכים strategy.entry/close/exit, alert() והשמות (:=) — "
            f"ליד: `{text[:50]}`"
        )

    def _exec_line(self, text: str):
        low = text.lower()
        if low.startswith("import "):
            raise PineError("`import` אינו נתמך — הדבק ספריות כקוד רגיל")
        if low.startswith("library("):
            raise PineError("ספריות Pine אינן נתמכות")
        vm = re.match(r"var\s+", text, re.I)
        if vm:
            # var x = ... — אתחול חד-פעמי (במודל הווקטורי זה כמו השמה רגילה)
            text = text[vm.end():].strip()
            low = text.lower()
            if not text or low.startswith("var "):
                raise PineError("תחביר `var` לא תקין")
        m = _FUNCDEF_RE.match(text)
        if m:
            name, params_s, body = m.group(1), m.group(2), m.group(3)
            params = [p.strip().split("=")[0].strip() for p in params_s.split(",") if p.strip()]
            self.funcs[name] = (params, body.strip())
            return
        if low.startswith("indicator(") or low.startswith("strategy("):
            self._parse_header(text)
            return
        try:
            tree = ast.parse(_pine_ternary(text), mode="exec")
        except SyntaxError:
            raise PineError(f"שגיאת תחביר ליד: `{text[:60]}`")
        if len(tree.body) != 1:
            raise PineError(f"שגיאת תחביר ליד: `{text[:60]}`")
        node = tree.body[0]
        if isinstance(node, ast.Expr):
            self._eval_expr_node(node.value)
            return
        if isinstance(node, ast.Assign):
            self._exec_assign(node, text, guarded=None)
            return
        raise PineError(f"שורה לא מובנת: `{text[:60]}`")

    def _parse_header(self, text: str):
        kind = "strategy" if text.lower().startswith("strategy(") else "indicator"
        inner = text[text.index("(") + 1 : text.rindex(")")]
        title = None
        overlay = True
        for part in self._split_top(inner):
            kv = part.split("=", 1)
            if len(kv) == 2:
                k, v = kv[0].strip().lower(), kv[1].strip()
                if k == "title" and v[:1] in "\"'":
                    title = v[1:-1]
                elif k == "shorttitle" and title is None and v[:1] in "\"'":
                    title = v[1:-1]
                elif k == "overlay":
                    overlay = v.lower() == "true"
                elif k == "initial_capital":
                    try:
                        self.meta["initial_capital"] = float(v)
                    except ValueError:
                        pass
                elif k == "commission_value":
                    try:
                        self.meta["commission_value"] = float(v)
                    except ValueError:
                        pass
                elif k == "commission_type":
                    self.meta["commission_type"] = "percent" if "percent" in v.lower() else v.strip().strip("\"'")
            elif title is None and part.strip()[:1] in "\"'":
                title = part.strip()[1:-1]
        self.meta = {
            "kind": kind,
            "title": title or ("אסטרטגיה" if kind == "strategy" else "אינדיקטור"),
            "overlay": overlay,
            "pine_version": self.meta.get("pine_version"),
        }

    @staticmethod
    def _split_top(s: str) -> List[str]:
        parts, depth, cur, instr = [], 0, [], None
        for i, c in enumerate(s):
            if instr:
                cur.append(c)
                if c == instr and s[i - 1] != "\\":
                    instr = None
            elif c in "\"'":
                instr = c
                cur.append(c)
            elif c in "([":
                depth += 1
                cur.append(c)
            elif c in ")]":
                depth -= 1
                cur.append(c)
            elif c == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(c)
        parts.append("".join(cur))
        return parts

    # ---------------- assignment ----------------

    def _exec_assign(self, node: ast.Assign, text: str, guarded):
        pos, op = _find_assign_op(text)
        lhs = text[:pos].strip()
        rhs = text[pos + len(op):].strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", lhs):
            # tuple unpack: [a, b] = ...
            mt = re.fullmatch(r"\[\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*\]", lhs)
            if not mt or op != "=" or guarded:
                raise PineError(f"השמה לא חוקית: `{text[:60]}`")
            names = [x.strip() for x in mt.group(1).split(",")]
            val = self._eval_expr(rhs)
            if isinstance(val, tuple):
                vals = list(val)
            else:
                raise PineError("פירוק tuple דורש פונקציה שמחזירה כמה ערכים (למשל ta.macd)")
            if len(vals) != len(names):
                raise PineError("מספר המשתנים בפירוק לא תואם")
            for nm, v in zip(names, vals):
                self.ns[nm] = v
            return
        name = lhs
        if name in self.funcs or name in ("if", "for", "while"):
            raise PineError(f"שם שמור: `{name}`")
        if op == ":=" and name not in self.ns:
            raise PineError(f"`:=` דורש שהמשתנה `{name}` יוגדר קודם עם `=`")
        # השמה רקורסיבית: x := ...x[1]... — חישוב סדרתי נר-אחר-נר
        if op == ":=" and re.search(rf"\b{re.escape(name)}\s*\[\s*[1-9]\d*\s*\]", rhs):
            self._exec_assign_sequential(name, rhs, guarded)
            return
        val = self._eval_expr(rhs)
        if guarded is not None:
            old = self.ns.get(name, None)
            new = self._where(guarded, val, old)
            self.ns[name] = new
        else:
            old = self.ns.get(name, None)
            # שמירת סקלר: i := i + 1 נשאר מספר ולא הופך לסדרה קבועה
            if (op == ":=" and old is not None and not _is_series(old)
                    and _is_series(val)):
                uniq = {v for v in val if v is not None}
                if len(uniq) == 1:
                    u = next(iter(uniq))
                    if isinstance(u, (int, float)) and not isinstance(u, bool):
                        val = u
            self.ns[name] = val

    def _exec_assign_sequential(self, name, rhs, mask=None):
        """x := ביטוי שמתייחס ל-x[1] — מעריך נר אחר נר (עד 2000 נרות)."""
        n = self.n
        if n > 2000:
            raise PineError(
                "השמה רקורסיבית (x := ...x[1]...) נתמכת עד 2000 נרות — בחר טווח/אינטרוול קצר יותר"
            )
        try:
            tree = ast.parse(_pine_ternary(rhs), mode="eval").body
        except SyntaxError:
            raise PineError(f"שגיאת תחביר בביטוי: `{rhs[:60]}`")
        refs = {nd.id for nd in ast.walk(tree) if isinstance(nd, ast.Name)}
        refs.discard(name)
        init = self.ns.get(name)
        init_scalar = init if not _is_series(init) else None
        mask_s = _as_bool_series(mask, n) if mask is not None else [True] * n
        out = [None] * n
        for i in range(n):
            saved = {}
            for r in refs:
                if r in self.ns:
                    v = self.ns[r]
                    if _is_series(v) and len(v) > i + 1:
                        saved[r] = v
                        self.ns[r] = v[: i + 1]
            self.ns[name] = out[:i] + [init_scalar]
            old_n = self.n
            self.n = i + 1
            try:
                val = self._eval_expr_node(tree, None)
            finally:
                self.n = old_n
                for r, v in saved.items():
                    self.ns[r] = v
            new_v = val[i] if _is_series(val) else val
            if mask_s[i]:
                out[i] = new_v
            else:
                out[i] = out[i - 1] if i > 0 else init_scalar
        self.ns[name] = out

    def _exec_assign_guarded(self, node: ast.Assign, text: str, mask: List[bool]):
        self._exec_assign(node, text, guarded=mask)

    def _where(self, cond: List[bool], a: Any, b: Any) -> List:
        la, lb = _as_series(a, self.n), _as_series(b, self.n)
        return [x if c else y for c, x, y in zip(cond, la, lb)]

    def _eval_strategy_call_guarded(self, node: ast.Call, mask: List[bool]):
        self._eval_strategy_call(node, extra_when=mask)

    # ---------------- expression evaluation (AST whitelist) ----------------

    def _eval_expr(self, expr: str) -> Any:
        try:
            tree = ast.parse(_pine_ternary(expr), mode="eval")
        except SyntaxError:
            raise PineError(f"שגיאת תחביר בביטוי: `{expr[:60]}`")
        return self._eval_expr_node(tree.body)

    def _eval_expr_node(self, node: ast.AST, local: Optional[Dict[str, Any]] = None) -> Any:
        n = self.n
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            nm = node.id
            if local and nm in local:
                return local[nm]
            if nm in self.ns:
                return self.ns[nm]
            if nm in self.funcs:
                raise PineError(f"`{nm}` היא פונקציה — קרא לה עם סוגריים")
            raise PineError(f"משתנה לא מוכר: `{nm}`")
        if isinstance(node, ast.BinOp):
            a = self._eval_expr_node(node.left, local)
            b = self._eval_expr_node(node.right, local)
            return self._eval_binop(node.op, a, b)
        if isinstance(node, ast.UnaryOp):
            a = self._eval_expr_node(node.operand, local)
            if isinstance(node.op, ast.USub):
                la = _as_series(a, n)
                return [None if x is None else -x for x in la]
            if isinstance(node.op, ast.UAdd):
                return a
            if isinstance(node.op, ast.Not):
                return [not _truthy(x) for x in _as_series(a, n)]
            raise PineError("אופרטור לא נתמך")
        if isinstance(node, ast.BoolOp):
            vals = [self._eval_expr_node(v, local) for v in node.values]
            res = _as_bool_series(vals[0], n)
            for v in vals[1:]:
                vb = _as_bool_series(v, n)
                if isinstance(node.op, ast.And):
                    res = [x and y for x, y in zip(res, vb)]
                else:
                    res = [x or y for x, y in zip(res, vb)]
            return res
        if isinstance(node, ast.Compare):
            left = self._eval_expr_node(node.left, local)
            res = [True] * n
            for op, comp in zip(node.ops, node.comparators):
                right = self._eval_expr_node(comp, local)
                la, lb = _as_series(left, n), _as_series(right, n)
                cur = []
                for x, y in zip(la, lb):
                    if x is None or y is None:
                        cur.append(False)
                    elif isinstance(op, ast.Lt):
                        cur.append(x < y)
                    elif isinstance(op, ast.LtE):
                        cur.append(x <= y)
                    elif isinstance(op, ast.Gt):
                        cur.append(x > y)
                    elif isinstance(op, ast.GtE):
                        cur.append(x >= y)
                    elif isinstance(op, ast.Eq):
                        cur.append(x == y)
                    elif isinstance(op, ast.NotEq):
                        cur.append(x != y)
                    else:
                        raise PineError("אופרטור השוואה לא נתמך")
                res = [r and c for r, c in zip(res, cur)]
                left = right
            return res
        if isinstance(node, ast.IfExp):
            t = _as_bool_series(self._eval_expr_node(node.test, local), n)
            b = self._eval_expr_node(node.body, local)
            o = self._eval_expr_node(node.orelse, local)
            return self._where(t, b, o)
        if isinstance(node, ast.Call):
            return self._eval_call(node, local)
        if isinstance(node, ast.Attribute):
            return self._eval_attribute(node, local)
        if isinstance(node, ast.Subscript):
            return self._eval_subscript(node, local)
        if isinstance(node, (ast.Tuple, ast.List)):
            return [self._eval_expr_node(e, local) for e in node.elts]
        raise PineError("ביטוי לא נתמך בתת-הקבוצה הזו")

    def _eval_binop(self, op, a, b):
        n = self.n
        la, lb = _as_series(a, n), _as_series(b, n)
        if isinstance(op, ast.Add):
            if _contains_str(a) or _contains_str(b):
                return [None if x is None or y is None else str(x) + str(y)
                        for x, y in zip(la, lb)]
            f = lambda x, y: x + y
        elif isinstance(op, ast.Sub):
            f = lambda x, y: x - y
        elif isinstance(op, ast.Mult):
            f = lambda x, y: x * y
        elif isinstance(op, ast.Div):
            def f(x, y):
                return None if y == 0 else x / y
        elif isinstance(op, ast.Mod):
            def f(x, y):
                return None if y == 0 else x % y
        elif isinstance(op, ast.Pow):
            def f(x, y):
                try:
                    return x ** y
                except Exception:
                    return None
        else:
            raise PineError("אופרטור חשבוני לא נתמך")
        return [None if x is None or y is None else f(x, y) for x, y in zip(la, lb)]

    def _eval_attribute(self, node: ast.Attribute, local):
        val = self._eval_expr_node(node.value, local)
        attr = node.attr
        if isinstance(val, _NS):
            if attr in val.__dict__:
                if val is self.ns.get("strategy") and attr == "position_size":
                    self._position_used = True
                return val.__dict__[attr]
            raise PineError(f"`{attr}` לא מוכר")
        if isinstance(val, _DOW) and attr in (
                "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"):
            return {"sunday": 1, "monday": 2, "tuesday": 3, "wednesday": 4,
                    "thursday": 5, "friday": 6, "saturday": 7}[attr]
        raise PineError("גישה למאפיין לא נתמכת כאן")

    def _eval_subscript(self, node: ast.Subscript, local):
        v = self._eval_expr_node(node.value, local)
        sl = node.slice
        k = None
        if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
            k = sl.value
        elif isinstance(sl, ast.Name):
            # אינדקס דינמי ממשתנה לולאה: close[i] (גם כשהמשתנה הפך לסדרה קבועה)
            nm = sl.id
            val = local.get(nm) if local and nm in local else self.ns.get(nm, None)
            if _is_series(val):
                uniq = {v for v in val if v is not None}
                val = next(iter(uniq)) if len(uniq) == 1 else None
            try:
                ok = (val is not None and not isinstance(val, bool)
                      and not _is_series(val) and int(val) == val)
            except (TypeError, ValueError):
                ok = False
            if ok:
                k = int(val)
        if k is None:
            raise PineError("אינדקס היסטוריה חייב להיות מספר שלם (למשל close[1])")
        if k < 0:
            raise PineError("היסטוריה שלילית (x[-1]) אינה נתמכת")
        if not _is_series(v):
            raise PineError("אפשר לגשת להיסטוריה רק של סדרה")
        return [None] * min(k, self.n) + v[: self.n - k] if k else list(v)

    # ---------------- calls ----------------

    def _call_args(self, node: ast.Call, local):
        args = [self._eval_expr_node(a, local) for a in node.args]
        kwargs = {kw.arg: self._eval_expr_node(kw.value, local) for kw in node.keywords}
        return args, kwargs

    def _eval_call(self, node: ast.Call, local):
        fn = node.func
        # ta.* / math.* / strategy.* / color.* etc.
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            mod = fn.value.id
            name = fn.attr
            if mod == "ta":
                return self._call_ta(name, node, local)
            if mod == "math":
                return self._call_math(name, node, local)
            if mod == "strategy":
                return self._eval_strategy_call(node)
            if mod == "input":
                return self._call_input(name, node, local)
            if mod == "color":
                if name == "new":
                    args, _ = self._call_args(node, local)
                    return args[0] if args else "#000000"
                raise PineError(f"color.{name} לא נתמך")
            if mod == "str":
                if name == "tostring":
                    args, kw = self._call_args(node, local)
                    v = args[0] if args else None

                    def _tos(x):
                        if x is None:
                            return "na"
                        if isinstance(x, bool):
                            return "true" if x else "false"
                        if isinstance(x, float):
                            return f"{x:.2f}" if (x == 0 or abs(x) >= 0.01) else f"{x:.6f}"
                        return str(x)

                    if _is_series(v):
                        return [_tos(x) for x in v]
                    return _tos(v)
                raise PineError(f"str.{name} לא נתמך")
        if isinstance(fn, ast.Name):
            nm = fn.id
            if nm in ("plot", "plotshape", "plotchar", "bgcolor", "hline", "fill"):
                return self._call_draw(nm, node, local)
            if nm in ("alert", "alertcondition"):
                return self._call_alert(nm, node, local, mask=None)
            if nm in ("nz", "na", "fixnan"):
                return self._call_util(nm, node, local)
            if nm == "input":
                return self._call_input("value", node, local)
            if nm in self.funcs:
                return self._call_userfunc(nm, node, local)
            raise PineError(f"פונקציה לא מוכרת: `{nm}`")
        raise PineError("קריאה לא נתמכת")

    def _call_userfunc(self, nm, node, local):
        params, body = self.funcs[nm]
        args, kwargs = self._call_args(node, local)
        if len(args) + len(kwargs) > len(params):
            raise PineError(f"`{nm}` קיבלה יותר מדי ארגומנטים")
        child = dict(local or {})
        for p, a in zip(params, args):
            child[p] = a
        for p in params[len(args):]:
            if p in kwargs:
                child[p] = kwargs[p]
            else:
                raise PineError(f"חסר ארגומנט `{p}` בקריאה ל-`{nm}`")
        return self._eval_expr_node(ast.parse(_pine_ternary(body), mode="eval").body, child)

    def _as_int(self, v, what):
        if _is_series(v):
            v = next((x for x in v if x is not None), None)
        if v is None:
            raise PineError(f"ערך חסר עבור {what}")
        return int(v)

    def _as_float(self, v, what):
        if _is_series(v):
            v = next((x for x in v if x is not None), None)
        if v is None:
            raise PineError(f"ערך חסר עבור {what}")
        return float(v)

    def _call_ta(self, name, node, local):
        n = self.n
        args, kw = self._call_args(node, local)
        S = lambda v: _as_series(v, n)
        I = lambda v, w: self._as_int(v, w)
        if name == "ema":
            return p_ema(S(args[0]), I(args[1], "length"))
        if name == "sma":
            return p_sma(S(args[0]), I(args[1], "length"))
        if name == "rma":
            return p_rma(S(args[0]), I(args[1], "length"))
        if name == "wma":
            return p_wma(S(args[0]), I(args[1], "length"))
        if name == "rsi":
            return p_rsi(S(args[0]), I(args[1], "length"))
        if name == "macd":
            return p_macd(S(args[0]), I(args[1], "f"), I(args[2], "s"), I(args[3], "sig"))
        if name == "stdev":
            return p_stdev(S(args[0]), I(args[1], "length"))
        if name == "atr":
            return p_atr(S(args[0]), S(args[1]), S(args[2]), I(args[3], "length"))
        if name == "tr":
            return p_tr(S(args[0]), S(args[1]), S(args[2]))
        if name == "highest":
            return p_highest(S(args[0]), I(args[1], "length"))
        if name == "lowest":
            return p_lowest(S(args[0]), I(args[1], "length"))
        if name == "change":
            return p_change(S(args[0]), I(args[1], "length") if len(args) > 1 else 1)
        if name == "mom":
            return p_mom(S(args[0]), I(args[1], "length"))
        if name == "roc":
            return p_roc(S(args[0]), I(args[1], "length"))
        if name == "crossover":
            return p_crossover(S(args[0]), S(args[1]))
        if name == "crossunder":
            return p_crossunder(S(args[0]), S(args[1]))
        if name == "vwap":
            vol = S(kw.get("volume", self.ns["volume"]))
            return p_vwap(S(args[0]), vol, self.times)
        if name == "cci":
            return p_cci(S(args[0]), I(args[1], "length"))
        if name == "mfi":
            return p_mfi(S(args[0]), S(args[1]), S(args[2]), S(args[3]), I(args[4], "length"))
        if name == "wpr":
            return p_wpr(S(args[0]), S(args[1]), S(args[2]), I(args[3], "length"))
        if name == "stoch":
            k = I(args[3], "k") if len(args) > 3 else 14
            d = I(args[4], "d") if len(args) > 4 else 3
            smooth = I(args[5], "smooth") if len(args) > 5 else 3
            return p_stoch(S(args[0]), S(args[1]), S(args[2]), k, d, smooth)
        if name == "linreg":
            return p_linreg(S(args[0]), I(args[1], "length"))
        if name == "correlation":
            return p_correlation(S(args[0]), S(args[1]), I(args[2], "length"))
        if name == "variance":
            return p_variance(S(args[0]), I(args[1], "length"))
        if name == "cum":
            return p_cum(S(args[0]))
        if name == "barssince":
            return p_barssince(_as_bool_series(args[0], n))
        if name == "valuewhen":
            occ = I(args[2], "occurrence") if len(args) > 2 else 0
            return p_valuewhen(_as_bool_series(args[0], n), S(args[1]), occ)
        if name == "highestbars":
            return p_highestbars(S(args[0]), I(args[1], "length"))
        if name == "lowestbars":
            return p_lowestbars(S(args[0]), I(args[1], "length"))
        if name == "supertrend":
            factor = float(kw.get("factor", args[3] if len(args) > 3 else 3.0))
            atr_len = I(kw.get("atrLength", args[4] if len(args) > 4 else 10), "atrLength")
            return p_supertrend(S(args[0]), S(args[1]), S(args[2]), factor, atr_len)
        raise PineError(f"ta.{name} אינו נתמך (נתמכים: ema/sma/rma/wma/rsi/macd/stdev/variance/atr/tr/highest/lowest/highestbars/lowestbars/change/mom/roc/cum/crossover/crossunder/vwap/cci/mfi/wpr/stoch/linreg/correlation/barssince/valuewhen/supertrend)")

    def _call_math(self, name, node, local):
        n = self.n
        args, kw = self._call_args(node, local)
        if name == "abs":
            return _m1(abs)(args[0], n)
        if name == "max":
            res = _as_series(args[0], n)
            for a in args[1:]:
                la = _as_series(a, n)
                res = [None if x is None or y is None else max(x, y) for x, y in zip(res, la)]
            return res
        if name == "min":
            res = _as_series(args[0], n)
            for a in args[1:]:
                la = _as_series(a, n)
                res = [None if x is None or y is None else min(x, y) for x, y in zip(res, la)]
            return res
        if name == "sum":
            s, length = _as_series(args[0], n), self._as_int(args[1], "length")
            out = [None] * n
            for i in range(n):
                if _valid_window(s, i, length):
                    out[i] = sum(s[i - length + 1 : i + 1])
            return out
        if name == "avg":
            series = [_as_series(a, n) for a in args]
            return [None if any(x is None for x in vals) else sum(vals) / len(vals)
                    for vals in zip(*series)]
        if name == "floor":
            return _m1(math.floor)(args[0], n)
        if name == "ceil":
            return _m1(math.ceil)(args[0], n)
        if name == "round":
            prec = self._as_int(args[1], "precision") if len(args) > 1 else 0
            return _m1(lambda x: round(x, prec))(args[0], n)
        if name == "sqrt":
            return _m1(lambda x: math.sqrt(x) if x >= 0 else None)(args[0], n)
        if name == "pow":
            return _m2(lambda x, y: x ** y)(args[0], args[1], n)
        if name == "exp":
            return _m1(math.exp)(args[0], n)
        if name == "log":
            return _m1(lambda x: math.log(x) if x > 0 else None)(args[0], n)
        if name == "sign":
            return _m1(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))(args[0], n)
        raise PineError(f"math.{name} אינו נתמך")

    def _call_input(self, name, node, local):
        args, kw = self._call_args(node, local)
        if "defval" in kw:
            default = kw["defval"]
        elif args:
            default = args[0]
        else:
            raise PineError("input() דורש ערך ברירת מחדל")
        title = kw.get("title", args[1] if len(args) > 1 else name)
        key = str(title) if isinstance(title, str) else name
        self.inputs[key] = default if not _is_series(default) else "series"
        return default

    def _call_util(self, name, node, local):
        n = self.n
        args, kw = self._call_args(node, local)
        s = _as_series(args[0], n)
        if name == "nz":
            rep = kw.get("replacement", args[1] if len(args) > 1 else 0)
            lr = _as_series(rep, n)
            return [r if x is None else x for x, r in zip(s, lr)]
        if name == "na":
            return [x is None for x in s]
        if name == "fixnan":
            out = []
            last = None
            for x in s:
                if x is None:
                    out.append(last)
                else:
                    last = x
                    out.append(x)
            return out
        raise PineError("unreachable")

    def _call_draw(self, nm, node, local):
        n = self.n
        args, kw = self._call_args(node, local)
        series = _as_series(args[0], n) if args else [None] * n
        title = kw.get("title", args[1] if len(args) > 1 else None)
        color = kw.get("color", None)
        if nm == "plot":
            if len(self.plots) >= 20:
                raise PineError("יותר מדי plot() — מקסימום 20")
            vals = [None if x is None else (1.0 if x is True else (0.0 if x is False else float(x)))
                    for x in series]
            self.plots.append({
                "name": str(title) if title else f"Plot {len(self.plots)+1}",
                "color": str(color) if color else "#2962ff",
                "values": vals,
            })
            return None
        if nm in ("plotshape", "plotchar"):
            if len(self.shape_markers) > 5000:
                raise PineError("יותר מדי סמני plotshape")
            style = str(kw.get("style", "circle"))
            loc = str(kw.get("location", "abovebar"))
            text = str(kw.get("text", title or ""))
            shape_map = {"arrowUp": "arrowUp", "arrowDown": "arrowDown", "circle": "circle"}
            shape = shape_map.get(style, "circle")
            pos = "aboveBar" if loc == "aboveBar" else "belowBar"
            for i, x in enumerate(series):
                if _truthy(x):
                    self.shape_markers.append({
                        "time": self.times[i],
                        "position": pos,
                        "color": str(color) if color else "#ff9800",
                        "shape": shape,
                        "text": text,
                    })
            return None
        if nm == "hline":
            price = args[0] if args else None
            if _is_series(price):
                price = next((x for x in price if x is not None), None)
            if price is None:
                self.notes.append("`hline` עם מחיר חסר התעלם")
                return None
            if len(self.hlines) >= 20:
                raise PineError("יותר מדי hline() — מקסימום 20")
            self.hlines.append({
                "price": float(price),
                "color": str(color) if color else "#787b86",
                "title": str(title) if title else "",
            })
            return None
        # bgcolor / fill — ignored
        self.notes.append(f"`{nm}` התעלם (לא מוצג)")
        return None

    def _call_alert(self, nm, node, local, mask=None):
        n = self.n
        args, kw = self._call_args(node, local)
        if nm == "alertcondition":
            cond = _as_bool_series(args[0] if args else False, n)
            message = args[2] if len(args) > 2 else (args[1] if len(args) > 1 else "")
            fire = cond
        else:
            message = args[0] if args else ""
            freq = str(kw.get("freq", "once_per_bar"))
            if freq == "once_per_bar_close":
                fire = [i == n - 1 for i in range(n)]
            else:
                fire = [True] * n
        if mask is not None:
            m = _as_bool_series(mask, n)
            fire = [f and x for f, x in zip(fire, m)]
        msgs = _as_series(message, n) if _is_series(message) else [message] * n
        for i in range(n):
            if not fire[i]:
                continue
            msg = msgs[i]
            if msg is None:
                continue
            if len(self.alerts) >= 20000:
                self.notes.append("נרשמו 20000 התראות — עצרתי כאן")
                break
            self.alerts.append({"time": self.times[i], "message": str(msg)})
        return None

    def _eval_strategy_call(self, node: ast.Call, extra_when=None):
        name = node.func.attr
        args, kw = self._call_args(node, None)
        if name in ("entry", "order"):
            if not args:
                raise PineError("strategy.entry דורש מזהה (id)")
            sid = str(args[0])
            direction = str(args[1]).lower() if len(args) > 1 else "long"
            if direction not in ("long", "short"):
                raise PineError("כיוון חייב להיות strategy.long או strategy.short")
            for bad in ("qty", "limit", "stop"):
                if bad in kw and kw[bad] is not None:
                    raise PineError(
                        f"strategy.entry עם `{bad}` אינו נתמך — השתמש ב-when= בלבד"
                    )
            when = kw.get("when", [True] * self.n)
            if extra_when is not None:
                when = [a and b for a, b in zip(_as_bool_series(when, self.n), extra_when)]
            self.entries.append({"id": sid, "dir": direction, "when": _as_bool_series(when, self.n)})
            return None
        if name == "close":
            when = kw.get("when", [True] * self.n)
            if extra_when is not None:
                when = [a and b for a, b in zip(_as_bool_series(when, self.n), extra_when)]
            sid = str(args[0]) if args else "all"
            self.closes.append({"id": sid, "when": _as_bool_series(when, self.n)})
            return None
        if name == "exit":
            sid = str(args[0]) if args else "exit"
            from_entry = kw.get("from_entry", None)
            from_entry = "" if from_entry is None else str(from_entry)

            def _num(v):
                if v is None:
                    return None
                if _is_series(v):
                    return v
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            profit = _num(kw.get("profit"))
            loss = _num(kw.get("loss"))
            limit = _num(kw.get("limit"))
            stop = _num(kw.get("stop"))
            if (profit is not None or loss is not None) and not getattr(self, "_tick_noted", False):
                self._tick_noted = True
                self.notes.append("profit/loss בטיקים חושבו לפי טיק של $0.01 (מניות)")
            if kw.get("qty") is not None or kw.get("qty_percent") is not None:
                self.notes.append(f"strategy.exit('{sid}'): יציאה חלקית (qty) לא נתמכת — בוצעה יציאה מלאה")
            when = kw.get("when", None)
            if when is not None and extra_when is not None:
                when = [a and b for a, b in zip(_as_bool_series(when, self.n), extra_when)]
            elif extra_when is not None:
                when = extra_when
            self.exits.append({
                "id": sid,
                "from_entry": from_entry,
                "when": _as_bool_series(when, self.n) if when is not None else None,
                "profit": profit, "loss": loss, "limit": limit, "stop": stop,
            })
            return None
        if name == "cancel":
            self.notes.append("strategy.cancel התעלם")
            return None
        self.notes.append(f"strategy.{name} התעלם")
        return None


# =====================================================================
# Backtest from Pine signals — long/short, all-in, with commission
# =====================================================================

def backtest_pine(
    candles: List[Dict],
    entries: List[Dict],
    closes: List[Dict],
    exits: Optional[List[Dict]] = None,
    commission_pct: float = 0.1,
    initial_capital: float = 10000.0,
) -> Dict:
    n = len(candles)
    long_sig = [False] * n
    short_sig = [False] * n
    exit_sig = [False] * n
    for e in entries:
        dst = long_sig if e["dir"] == "long" else short_sig
        for i, v in enumerate(e["when"]):
            dst[i] = dst[i] or v
    for c in closes:
        for i, v in enumerate(c["when"]):
            exit_sig[i] = exit_sig[i] or v

    # strategy.exit levels: profit/loss בטיקים ($0.01 לטיק), limit/stop במחיר
    TICK = 0.01
    exit_specs = []
    for x in (exits or []):
        def _s(v):
            return None if v is None else _as_series(v, n)
        pd = _s(x.get("profit"))
        ld_ = _s(x.get("loss"))
        exit_specs.append({
            "from_entry": x.get("from_entry", ""),
            "when": x.get("when"),
            "profit_d": [None if t is None else t * TICK for t in pd] if pd else None,
            "loss_d": [None if t is None else t * TICK for t in ld_] if ld_ else None,
            "limit": _s(x.get("limit")),
            "stop": _s(x.get("stop")),
        })

    comm = commission_pct / 100.0
    cash = initial_capital
    shares = 0.0
    entry_price = 0.0
    entry_fee = 0.0
    entry_id = ""
    pos = 0  # 1 long, -1 short, 0 flat
    equity: List[float] = []
    pos_hist: List[int] = []
    trades: List[Dict] = []
    markers: List[Dict] = []
    closes_l = [c["close"] for c in candles]
    highs_l = [c["high"] for c in candles]
    lows_l = [c["low"] for c in candles]

    def equity_now(price):
        if pos == 1:
            return cash + shares * price
        if pos == -1:
            return cash - shares * price
        return cash

    def close_position(i, price, forced=False, reason="signal"):
        nonlocal cash, shares, pos, entry_price, entry_fee, entry_id
        exit_label = {"tp": "TP", "sl": "SL"}.get(reason, "Exit")
        if pos == 1:
            proceeds = shares * price * (1 - comm)
            pnl = proceeds - shares * entry_price - entry_fee
            cash = cash + proceeds
            trades.append({
                "side": "sell", "time": candles[i]["time"], "price": round(price, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round((price - entry_price) / entry_price * 100, 2) if entry_price else 0,
                **({"forced_close": True} if forced else {}),
                **({"exit_reason": reason} if reason != "signal" else {}),
            })
            markers.append({"time": candles[i]["time"], "position": "aboveBar",
                            "color": "#787b86", "shape": "circle", "text": exit_label})
        elif pos == -1:
            cost = shares * price * (1 + comm)
            pnl = shares * (entry_price - price) - entry_fee - shares * price * comm
            cash = cash - cost
            trades.append({
                "side": "cover", "time": candles[i]["time"], "price": round(price, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round((entry_price - price) / entry_price * 100, 2) if entry_price else 0,
                **({"forced_close": True} if forced else {}),
                **({"exit_reason": reason} if reason != "signal" else {}),
            })
            markers.append({"time": candles[i]["time"], "position": "belowBar",
                            "color": "#787b86", "shape": "circle", "text": exit_label})
        shares = 0.0
        pos = 0
        entry_id = ""

    def check_exits(i):
        """בדיקת strategy.exit לעמדה פתוחה. מחזיר True אם נסגרה."""
        ep = entry_price
        hi, lo = highs_l[i], lows_l[i]
        for xs in exit_specs:
            fe = xs["from_entry"]
            if fe and fe != entry_id:
                continue
            if pos == 1:
                st = xs["stop"]
                if st and st[i] is not None and lo is not None and lo <= st[i]:
                    close_position(i, st[i], reason="sl"); return True
                ld = xs["loss_d"]
                if ld and ld[i] is not None and lo is not None and lo <= ep - ld[i]:
                    close_position(i, ep - ld[i], reason="sl"); return True
                lm = xs["limit"]
                if lm and lm[i] is not None and hi is not None and hi >= lm[i]:
                    close_position(i, lm[i], reason="tp"); return True
                pd = xs["profit_d"]
                if pd and pd[i] is not None and hi is not None and hi >= ep + pd[i]:
                    close_position(i, ep + pd[i], reason="tp"); return True
            elif pos == -1:
                st = xs["stop"]
                if st and st[i] is not None and hi is not None and hi >= st[i]:
                    close_position(i, st[i], reason="sl"); return True
                ld = xs["loss_d"]
                if ld and ld[i] is not None and hi is not None and hi >= ep + ld[i]:
                    close_position(i, ep + ld[i], reason="sl"); return True
                lm = xs["limit"]
                if lm and lm[i] is not None and lo is not None and lo <= lm[i]:
                    close_position(i, lm[i], reason="tp"); return True
                pd = xs["profit_d"]
                if pd and pd[i] is not None and lo is not None and lo <= ep - pd[i]:
                    close_position(i, ep - pd[i], reason="tp"); return True
            if xs["when"] and xs["when"][i]:
                close_position(i, closes_l[i]); return True
        return False

    for i in range(n):
        price = closes_l[i]
        if exit_sig[i] and pos != 0:
            close_position(i, price)
        if pos != 0:
            check_exits(i)
        if long_sig[i] and pos <= 0:
            if pos == -1:
                close_position(i, price)
            fee = cash * comm
            shares = (cash - fee) / price if price else 0
            entry_price = price
            entry_fee = fee
            entry_id = next((e["id"] for e in entries if e["dir"] == "long" and e["when"][i]), "")
            cash = 0.0
            pos = 1
            trades.append({"side": "buy", "time": candles[i]["time"], "price": round(price, 2)})
            markers.append({"time": candles[i]["time"], "position": "belowBar",
                            "color": "#089981", "shape": "arrowUp", "text": "Long"})
        elif short_sig[i] and pos >= 0:
            if pos == 1:
                close_position(i, price)
            fee = cash * comm
            shares = (cash - fee) / price if price else 0
            entry_price = price
            entry_fee = fee
            entry_id = next((e["id"] for e in entries if e["dir"] == "short" and e["when"][i]), "")
            cash = cash + shares * price  # proceeds of the short sale
            pos = -1
            trades.append({"side": "short", "time": candles[i]["time"], "price": round(price, 2)})
            markers.append({"time": candles[i]["time"], "position": "aboveBar",
                            "color": "#f23645", "shape": "arrowDown", "text": "Short"})
        equity.append(round(equity_now(price), 2))
        pos_hist.append(pos)

    if pos != 0:
        close_position(n - 1, closes_l[-1], forced=True)
        equity[-1] = round(equity_now(closes_l[-1]), 2)

    final_equity = equity[-1]
    total_return = (final_equity - initial_capital) / initial_capital * 100
    buy_hold = (closes_l[-1] - closes_l[0]) / closes_l[0] * 100 if closes_l[0] else 0.0

    exits = [t for t in trades if t["side"] in ("sell", "cover")]
    wins = [t for t in exits if t.get("pnl", 0) > 0]
    losses = [t for t in exits if t.get("pnl", 0) <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else 999.0)
    win_rate = round(len(wins) / len(exits) * 100, 1) if exits else 0.0

    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak else 0.0
        max_dd = max(max_dd, dd)

    rets = [(equity[i] - equity[i - 1]) / equity[i - 1] for i in range(1, len(equity)) if equity[i - 1]]
    if len(rets) > 1 and sum(x * x for x in rets) > 0:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1))
        sharpe = round(mean / sd * math.sqrt(252), 2) if sd else 0.0
    else:
        sharpe = 0.0

    max_pts = 400
    step = max(1, len(equity) // max_pts)
    equity_ds = [{"time": candles[i]["time"], "value": equity[i]}
                 for i in range(0, len(equity), step)]

    return {
        "candles": n,
        "metrics": {
            "total_return_pct": round(total_return, 2),
            "buy_hold_pct": round(buy_hold, 2),
            "num_trades": len(exits),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe": sharpe,
            "final_equity": round(final_equity, 2),
        },
        "trades": trades[-60:],
        "markers": markers,
        "equity_curve": equity_ds,
        "position_series": pos_hist,
    }


# =====================================================================
# Public entry point
# =====================================================================

def run_pine(
    candles: List[Dict],
    code: str,
    symbol: str = "",
    period: str = "",
    interval: str = "",
    commission_pct: float = 0.1,
) -> Dict:
    if len(candles) < 10:
        raise PineError("אין מספיק נרות להרצה")
    runner = _Runner(candles, symbol, period, interval)
    runner.parse(code)
    if runner._position_used:
        # מעבר שני: strategy.position_size מחושב מהבקטסט של המעבר הראשון
        commission_0 = commission_pct
        if (runner.meta.get("commission_type") == "percent"
                and runner.meta.get("commission_value") is not None):
            commission_0 = runner.meta["commission_value"]
        bt0 = backtest_pine(candles, runner.entries, runner.closes, runner.exits,
                            commission_pct=commission_0,
                            initial_capital=runner.meta.get("initial_capital", 10000.0))
        # position_size בסקריפט = העמדה בתחילת הנר → מזיזים את הסדרה נר אחד קדימה
        ps0 = bt0["position_series"]
        ps_shifted = [0] + ps0[:-1]
        runner2 = _Runner(candles, symbol, period, interval)
        runner2.ns["strategy"].__dict__["position_size"] = ps_shifted
        runner2.parse(code)
        runner2._position_used = False  # לא מריצים מעבר שלישי
        runner = runner2

    result: Dict = {
        "title": runner.meta["title"],
        "kind": runner.meta["kind"],
        "overlay": runner.meta["overlay"],
        "pine_version": runner.meta.get("pine_version"),
        "plots": runner.plots,
        "shape_markers": runner.shape_markers,
        "hlines": runner.hlines,
        "alerts": runner.alerts,
        "notes": runner.notes,
        "inputs": {k: v for k, v in runner.inputs.items() if not isinstance(v, list)},
        "num_bars": runner.n,
        "markers": [],
        "metrics": None,
    }

    if runner.meta["kind"] == "strategy" or runner.entries or runner.closes or runner.exits:
        if not runner.entries:
            raise PineError(
                "זו אסטרטגיה אבל לא נמצא אף strategy.entry — "
                "הוסף למשל: strategy.entry(\"Long\", strategy.long, when=longCond)"
            )
        if (runner.meta.get("commission_type") == "percent"
                and runner.meta.get("commission_value") is not None):
            commission_pct = runner.meta["commission_value"]
        initial_capital = runner.meta.get("initial_capital", 10000.0)
        bt = backtest_pine(candles, runner.entries, runner.closes, runner.exits,
                            commission_pct=commission_pct,
                            initial_capital=initial_capital)
        result["markers"] = bt["markers"] + runner.shape_markers
        result["metrics"] = bt["metrics"]
        result["trades"] = bt["trades"]
        result["kind"] = "strategy"
    else:
        result["markers"] = runner.shape_markers
        if not runner.plots and not runner.shape_markers:
            raise PineError(
                "לא נמצא plot() או strategy.entry() בקוד — "
                "הוסף plot(mySeries) לאינדיקטור או strategy.entry לאסטרטגיה"
            )
    return result
