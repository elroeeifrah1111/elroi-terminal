"""
strategy_engine.py — Pine-Script-style strategy framework with backtesting & optimization.

No third-party dependencies (pure Python) so it stays free-tier friendly.

Concepts (mirroring TradingView Pine Script):
  - `ta.*`        : technical indicators (ta.ema, ta.rsi, ta.macd, ...)
  - Strategy      : registered class with param specs + generate() producing
                    per-bar signals (1 = enter long, -1 = exit, 0 = flat),
                    like strategy.entry()/strategy.close() in Pine.
  - run_backtest  : walks candles, simulates long-only trades, returns metrics,
                    trades and chart markers.
  - optimize_strategy : grid-search over param ranges, ranked by chosen metric,
                    per symbol/timeframe — like TradingView's Strategy Tester
                    "Optimization" but automated.
"""

import itertools
import math
from typing import Dict, List, Optional, Tuple


# =====================================================================
# ta.* — technical indicators (Pine-style), pure Python
# =====================================================================
class ta:
    @staticmethod
    def ema(values: List[float], length: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if length < 1 or len(values) < length:
            return out
        k = 2.0 / (length + 1)
        prev = sum(values[:length]) / length  # seed with SMA (like Pine)
        out[length - 1] = prev
        for i in range(length, len(values)):
            prev = values[i] * k + prev * (1 - k)
            out[i] = prev
        return out

    @staticmethod
    def sma(values: List[float], length: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if length < 1 or len(values) < length:
            return out
        window = sum(values[:length])
        out[length - 1] = window / length
        for i in range(length, len(values)):
            window += values[i] - values[i - length]
            out[i] = window / length
        return out

    @staticmethod
    def stdev(values: List[float], length: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if length < 1 or len(values) < length:
            return out
        for i in range(length - 1, len(values)):
            w = values[i - length + 1 : i + 1]
            mean = sum(w) / length
            var = sum((x - mean) ** 2 for x in w) / length
            out[i] = math.sqrt(var)
        return out

    @staticmethod
    def rsi(values: List[float], length: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if length < 1 or len(values) < length + 1:
            return out
        gains, losses = [], []
        for i in range(1, len(values)):
            d = values[i] - values[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_g = sum(gains[:length]) / length
        avg_l = sum(losses[:length]) / length
        out[length] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        for i in range(length + 1, len(values)):
            d = values[i] - values[i - 1]
            avg_g = (avg_g * (length - 1) + max(d, 0.0)) / length
            avg_l = (avg_l * (length - 1) + max(-d, 0.0)) / length
            out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        return out

    @staticmethod
    def macd(
        values: List[float], fast: int, slow: int, signal: int
    ) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
        ef, es = ta.ema(values, fast), ta.ema(values, slow)
        macd_line: List[Optional[float]] = [None] * len(values)
        for i in range(len(values)):
            if ef[i] is not None and es[i] is not None:
                macd_line[i] = ef[i] - es[i]
        valid = [x for x in macd_line if x is not None]
        sig_raw = ta.ema(valid, signal)
        signal_line: List[Optional[float]] = [None] * len(values)
        first = next((i for i, x in enumerate(macd_line) if x is not None), None)
        if first is not None:
            for j, v in enumerate(sig_raw):
                if v is not None:
                    signal_line[first + j] = v
        hist: List[Optional[float]] = [None] * len(values)
        for i in range(len(values)):
            if macd_line[i] is not None and signal_line[i] is not None:
                hist[i] = macd_line[i] - signal_line[i]
        return macd_line, signal_line, hist

    @staticmethod
    def atr(
        highs: List[float], lows: List[float], closes: List[float], length: int
    ) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(closes)
        if length < 1 or len(closes) < length + 1:
            return out
        trs = []
        for i in range(1, len(closes)):
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
        prev = sum(trs[:length]) / length
        out[length] = prev
        for i in range(length + 1, len(closes)):
            prev = (prev * (length - 1) + trs[i - 1]) / length
            out[i] = prev
        return out

    @staticmethod
    def bbands(
        values: List[float], length: int, mult: float
    ) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
        basis = ta.sma(values, length)
        dev = ta.stdev(values, length)
        upper: List[Optional[float]] = [None] * len(values)
        lower: List[Optional[float]] = [None] * len(values)
        for i in range(len(values)):
            if basis[i] is not None and dev[i] is not None:
                upper[i] = basis[i] + mult * dev[i]
                lower[i] = basis[i] - mult * dev[i]
        return basis, upper, lower

    @staticmethod
    def crossover(a: List[Optional[float]], b: List[Optional[float]]) -> List[bool]:
        """True where a crosses OVER b (Pine's ta.crossover)."""
        out = [False] * len(a)
        for i in range(1, len(a)):
            if a[i - 1] is None or b[i - 1] is None or a[i] is None or b[i] is None:
                continue
            out[i] = a[i - 1] <= b[i - 1] and a[i] > b[i]
        return out

    @staticmethod
    def crossunder(a: List[Optional[float]], b: List[Optional[float]]) -> List[bool]:
        out = [False] * len(a)
        for i in range(1, len(a)):
            if a[i - 1] is None or b[i - 1] is None or a[i] is None or b[i] is None:
                continue
            out[i] = a[i - 1] >= b[i - 1] and a[i] < b[i]
        return out

    @staticmethod
    def crossed_over_value(series: List[Optional[float]], value: float) -> List[bool]:
        return ta.crossover(series, [value] * len(series))

    @staticmethod
    def crossed_under_value(series: List[Optional[float]], value: float) -> List[bool]:
        return ta.crossunder(series, [value] * len(series))


# =====================================================================
# Strategy base — Pine-style: params + generate() -> per-bar signals
# =====================================================================
class Strategy:
    id = "base"
    name_he = "בסיס"
    description_he = ""
    # param_specs: name -> {default, min, max, step, label_he}
    param_specs: Dict[str, Dict] = {}

    def defaults(self) -> Dict[str, float]:
        return {k: v["default"] for k, v in self.param_specs.items()}

    def min_bars(self, params: Dict[str, float]) -> int:
        return 60

    def generate(
        self,
        closes: List[float],
        highs: List[float],
        lows: List[float],
        params: Dict[str, float],
    ) -> List[int]:
        """Return per-bar signal: 1 = enter long, -1 = exit, 0 = hold/flat."""
        raise NotImplementedError


class EmaCrossStrategy(Strategy):
    id = "ema_cross"
    name_he = "חציית ממוצעים (EMA)"
    description_he = "קנייה כשממוצע מהיר חוצה מעל איטי, מכירה בחצייה מטה"
    param_specs = {
        "fast": {"default": 12, "min": 2, "max": 60, "step": 1, "label_he": "מהיר"},
        "slow": {"default": 26, "min": 5, "max": 200, "step": 1, "label_he": "איטי"},
    }

    def min_bars(self, params):
        return int(params["slow"]) + 10

    def generate(self, closes, highs, lows, params):
        fast, slow = int(params["fast"]), int(params["slow"])
        if fast >= slow:
            raise ValueError("הממוצע המהיר חייב להיות קצר מהאיטי")
        ef, es = ta.ema(closes, fast), ta.ema(closes, slow)
        up, down = ta.crossover(ef, es), ta.crossunder(ef, es)
        return [1 if u else (-1 if d else 0) for u, d in zip(up, down)]


class RsiReversalStrategy(Strategy):
    id = "rsi_reversal"
    name_he = "RSI קיצון (Mean Reversion)"
    description_he = "קנייה כשה-RSI נופל מתחת לסף מכירת-יתר, מכירה מעל קניית-יתר"
    param_specs = {
        "length": {"default": 14, "min": 2, "max": 50, "step": 1, "label_he": "תקופה"},
        "oversold": {"default": 30, "min": 5, "max": 40, "step": 1, "label_he": "מכירת יתר"},
        "overbought": {"default": 70, "min": 60, "max": 95, "step": 1, "label_he": "קניית יתר"},
    }

    def min_bars(self, params):
        return int(params["length"]) + 10

    def generate(self, closes, highs, lows, params):
        length = int(params["length"])
        os_, ob = float(params["oversold"]), float(params["overbought"])
        if os_ >= ob:
            raise ValueError("סף מכירת-יתר חייב להיות נמוך מסף קניית-יתר")
        r = ta.rsi(closes, length)
        up = ta.crossed_under_value(r, os_)   # נכנס למכירת יתר -> קנייה
        down = ta.crossed_over_value(r, ob)   # נכנס לקניית יתר -> מכירה
        return [1 if u else (-1 if d else 0) for u, d in zip(up, down)]


class MacdStrategy(Strategy):
    id = "macd_trend"
    name_he = "מגמת MACD"
    description_he = "קנייה כשקו ה-MACD חוצה מעל קו האיתות, מכירה בחצייה מטה"
    param_specs = {
        "fast": {"default": 12, "min": 2, "max": 40, "step": 1, "label_he": "מהיר"},
        "slow": {"default": 26, "min": 5, "max": 100, "step": 1, "label_he": "איטי"},
        "signal": {"default": 9, "min": 2, "max": 40, "step": 1, "label_he": "איתות"},
    }

    def min_bars(self, params):
        return int(params["slow"]) + int(params["signal"]) + 10

    def generate(self, closes, highs, lows, params):
        fast, slow, sig = int(params["fast"]), int(params["slow"]), int(params["signal"])
        if fast >= slow:
            raise ValueError("הממוצע המהיר חייב להיות קצר מהאיטי")
        macd_line, signal_line, _ = ta.macd(closes, fast, slow, sig)
        up, down = ta.crossover(macd_line, signal_line), ta.crossunder(macd_line, signal_line)
        return [1 if u else (-1 if d else 0) for u, d in zip(up, down)]


class BollingerBreakoutStrategy(Strategy):
    id = "bb_breakout"
    name_he = "פריצת בולינג'ר"
    description_he = "קנייה בפריצה מעל הרצועה העליונה, מכירה בחזרה מתחת לקו האמצע"
    param_specs = {
        "length": {"default": 20, "min": 5, "max": 100, "step": 1, "label_he": "תקופה"},
        "mult": {"default": 2.0, "min": 1.0, "max": 3.5, "step": 0.5, "label_he": "מכפיל"},
    }

    def min_bars(self, params):
        return int(params["length"]) + 10

    def generate(self, closes, highs, lows, params):
        length, mult = int(params["length"]), float(params["mult"])
        basis, upper, _ = ta.bbands(closes, length, mult)
        up = ta.crossover(closes, upper)
        down = ta.crossunder(closes, basis)
        return [1 if u else (-1 if d else 0) for u, d in zip(up, down)]


STRATEGIES: Dict[str, Strategy] = {
    s.id: s
    for s in (
        EmaCrossStrategy(),
        RsiReversalStrategy(),
        MacdStrategy(),
        BollingerBreakoutStrategy(),
    )
}


def list_strategies() -> List[Dict]:
    return [
        {
            "id": s.id,
            "name_he": s.name_he,
            "description_he": s.description_he,
            "params": s.param_specs,
        }
        for s in STRATEGIES.values()
    ]


# =====================================================================
# Backtest engine — long-only, all-in, with commission
# =====================================================================
def _frange(start: float, stop: float, step: float) -> List[float]:
    vals, v = [], start
    while v <= stop + 1e-9:
        vals.append(round(v, 6))
        v += step
    return vals


def run_backtest(
    candles: List[Dict],
    strategy_id: str,
    params: Optional[Dict[str, float]] = None,
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
) -> Dict:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"אסטרטגיה לא מוכרת: {strategy_id}")
    strat = STRATEGIES[strategy_id]
    merged = strat.defaults()
    if params:
        for k, v in params.items():
            if k in merged:
                merged[k] = float(v)

    n = len(candles)
    need = strat.min_bars(merged)
    if n < need:
        raise ValueError(f"אין מספיק נרות ({n}); נדרשים לפחות {need} לאסטרטגיה זו")

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    signals = strat.generate(closes, highs, lows, merged)
    if len(signals) != n:
        raise ValueError("שגיאת אסטרטגיה: מספר הסיגנלים לא תואם למספר הנרות")

    return backtest_from_signals(
        candles, signals,
        initial_capital=initial_capital,
        commission_pct=commission_pct,
        strategy_id=strategy_id,
        params=merged,
    )


def backtest_from_signals(
    candles: List[Dict],
    signals: List[int],
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
    strategy_id: str = "custom",
    params: Optional[Dict] = None,
) -> Dict:
    """Run the long-only trade simulation on a precomputed signal list.

    signals: 1 = enter long, -1 = exit to flat, 0 = hold.
    Returns the same metrics/trades/markers/equity_curve structure as
    run_backtest — used for user-supplied Python strategies.
    """
    n = len(candles)
    if len(signals) != n:
        raise ValueError("שגיאת אסטרטגיה: מספר הסיגנלים לא תואם למספר הנרות")
    merged = dict(params or {})
    closes = [c["close"] for c in candles]
    comm = commission_pct / 100.0
    cash, shares, entry_price = initial_capital, 0.0, 0.0
    equity: List[float] = []
    trades: List[Dict] = []
    markers: List[Dict] = []

    for i, c in enumerate(candles):
        price = c["close"]
        sig = signals[i]
        if sig == 1 and shares == 0:
            fee = cash * comm
            shares = (cash - fee) / price
            entry_price = price
            cash = 0.0
            trades.append({"side": "buy", "time": c["time"], "price": round(price, 2)})
            markers.append(
                {"time": c["time"], "position": "belowBar", "color": "#089981",
                 "shape": "arrowUp", "text": "Buy"}
            )
        elif sig == -1 and shares > 0:
            proceeds = shares * price * (1 - comm)
            cost_basis = shares * entry_price
            pnl = proceeds - cost_basis
            pnl_pct = (price - entry_price) / entry_price * 100
            cash = proceeds
            trades.append(
                {"side": "sell", "time": c["time"], "price": round(price, 2),
                 "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2)}
            )
            markers.append(
                {"time": c["time"], "position": "aboveBar", "color": "#f23645",
                 "shape": "arrowDown", "text": "Sell"}
            )
            shares = 0.0
        equity.append(round(cash + shares * price, 2))

    # close any open position at the last close
    if shares > 0:
        price = closes[-1]
        proceeds = shares * price * (1 - comm)
        cost_basis = shares * entry_price
        pnl = proceeds - cost_basis
        cash = proceeds
        trades.append(
            {"side": "sell", "time": candles[-1]["time"], "price": round(price, 2),
             "pnl": round(pnl, 2),
             "pnl_pct": round((price - entry_price) / entry_price * 100, 2),
             "forced_close": True}
        )
        equity[-1] = round(cash, 2)
        shares = 0.0

    final_equity = equity[-1]
    total_return = (final_equity - initial_capital) / initial_capital * 100
    buy_hold = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0

    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    losses = [t for t in sells if t.get("pnl", 0) <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else 999.0)
    win_rate = round(len(wins) / len(sells) * 100, 1) if sells else 0.0

    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak else 0.0
        max_dd = max(max_dd, dd)

    # Sharpe (approx, from equity curve returns)
    rets = [(equity[i] - equity[i - 1]) / equity[i - 1] for i in range(1, len(equity)) if equity[i - 1]]
    if len(rets) > 1 and sum(x * x for x in rets) > 0:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1))
        sharpe = round(mean / sd * math.sqrt(252), 2) if sd else 0.0
    else:
        sharpe = 0.0

    # downsample equity for transport
    max_pts = 400
    step = max(1, len(equity) // max_pts)
    equity_ds = [
        {"time": candles[i]["time"], "value": equity[i]} for i in range(0, len(equity), step)
    ]

    return {
        "strategy_id": strategy_id,
        "params": merged,
        "candles": n,
        "metrics": {
            "total_return_pct": round(total_return, 2),
            "buy_hold_pct": round(buy_hold, 2),
            "num_trades": len(sells),
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe": sharpe,
            "final_equity": round(final_equity, 2),
        },
        "trades": trades[-60:],          # last 60 trade events
        "markers": markers,
        "equity_curve": equity_ds,
    }


# =====================================================================
# Optimizer — grid search over param ranges, ranked per timeframe
# =====================================================================
OPTIMIZE_METRICS = {
    "total_return": ("total_return_pct", True),
    "profit_factor": ("profit_factor", True),
    "sharpe": ("sharpe", True),
    "win_rate": ("win_rate_pct", True),
}

MAX_OPTIMIZE_COMBOS = 500


def _auto_search_grid(param_specs: Dict[str, Dict], center: Dict[str, float]):
    """Focused grid around `center` values (radius in steps), clamped to spec bounds."""
    names, grids = [], []
    for name, spec in param_specs.items():
        step = spec["step"]
        c = center.get(name, spec["default"])
        is_int = float(step).is_integer()
        radius = 3 if is_int else 2
        lo = max(spec["min"], c - radius * step)
        hi = min(spec["max"], c + radius * step)
        vals = _frange(lo, hi, step)
        if c < lo or c > hi:
            vals = sorted(set(vals + [round(c, 6)]))
        names.append(name)
        grids.append(vals)
    return names, grids


def optimize_strategy(
    candles: List[Dict],
    strategy_id: str,
    metric: str = "profit_factor",
    params: Optional[Dict[str, float]] = None,
    ranges: Optional[Dict[str, Tuple[float, float, float]]] = None,
    max_combos: int = MAX_OPTIMIZE_COMBOS,
    min_trades: int = 3,
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
) -> Dict:
    """Grid-search best params for this symbol/timeframe.

    Without explicit `ranges`, searches a focused grid around `params`
    (or strategy defaults) — full-space search explodes combinatorially.
    """
    if strategy_id not in STRATEGIES:
        raise ValueError(f"אסטרטגיה לא מוכרת: {strategy_id}")
    if metric not in OPTIMIZE_METRICS:
        raise ValueError(f"מדד לא מוכר: {metric}")
    strat = STRATEGIES[strategy_id]
    metric_key, higher_better = OPTIMIZE_METRICS[metric]

    center = strat.defaults()
    if params:
        for k, v in params.items():
            if k in center:
                center[k] = float(v)

    names, grids = [], []
    if ranges:
        for name, spec in strat.param_specs.items():
            lo, hi, step = ranges.get(name, (spec["min"], spec["max"], spec["step"]))
            names.append(name)
            grids.append(_frange(lo, hi, step))
    else:
        names, grids = _auto_search_grid(strat.param_specs, center)

    combos = list(itertools.product(*grids))
    if len(combos) > max_combos:
        raise ValueError(
            f"יותר מדי שילובים ({len(combos)}); צמצם טווחים (מקסימום {max_combos})"
        )

    scored = []
    for combo in combos:
        params = dict(zip(names, combo))
        try:
            res = run_backtest(
                candles, strategy_id, params,
                initial_capital=initial_capital, commission_pct=commission_pct,
            )
        except ValueError:
            continue  # invalid combo (e.g. fast >= slow, not enough bars)
        m = res["metrics"]
        if m["num_trades"] < min_trades:
            continue
        val = m[metric_key]
        if val is None:
            continue
        scored.append({"params": params, "metrics": m})

    scored.sort(key=lambda r: r["metrics"][metric_key], reverse=higher_better)
    return {
        "strategy_id": strategy_id,
        "metric": metric,
        "combos_tested": len(combos),
        "combos_valid": len(scored),
        "top": scored[:10],
    }
