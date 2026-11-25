"""Standalone Base Breakout scanner for GitHub Actions.

Runs the exact v3 detector the terminal server seeds and schedules
(scanner.PY_BASEBO_SCHEDULED_CODE - byte-identical to the scheduled
code), over a ticker universe, outside the Render service:

  * candles come from scanner.batch_load_candles - the server's own
    bulk path (yf.download, auto_adjust=True, OHLC rounded to 2dp);
  * the detector runs through scanner.run_scan - the repo's own scan
    engine (hardened forkserver children), in the same chunk sizes the
    server's _run_scan_job uses (500 daily/weekly/monthly, 150 intraday);
  * findings are written to findings/<interval>.json (or
    findings/<interval>-chunk<i>.json when chunked) and POSTed to the
    terminal's ingest endpoint (POST /api/scans/ingest), which stores
    them as the scan's last_results and Telegram-notifies only the
    findings that are new versus the previously stored set.

Why: the Render free-tier service sleeps when idle and wipes its local
scans.db on restarts, so in-server scheduled passes kept dying. GitHub
Actions cron is the durable free scheduler; the terminal stays a dumb
store + notifier.

Usage:
    python actions_scan.py --interval 1mo --period 20y
    python actions_scan.py --interval 15m --period 1mo \
        --chunk-index 2 --chunks 4
    python actions_scan.py --interval 1mo --period 20y \
        --symbols ESEA,GRBK,TGB,NGL --no-ingest     # local test

The ingest secret comes from the INGEST_SECRET env var (never a CLI
value in workflow logs beyond the header); --no-ingest skips the POST.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from io import StringIO
from typing import Dict, List

import pandas as pd
import requests

import scanner

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("actions_scan")

DEFAULT_INGEST_URL = "https://elroi-terminal.onrender.com"
_TICKER_LIST_UA = {"User-Agent": "Mozilla/5.0 (compatible; ElroiTerminal/1.0)"}

# Same sub-chunk sizes as scan_api._run_scan_job on the server.
CHUNK_DAILY = 500
CHUNK_INTRADAY = 150


# ----------------------------------------------------------------------------
# Universe - same sources and cleaning as scan_api._fetch_ticker_list
# ----------------------------------------------------------------------------
def _clean_ticker_list(raw) -> List[str]:
    if isinstance(raw, str):
        raw = raw.split()
    out, seen = [], set()
    for t in raw:
        for tok in str(t or "").split():
            s = tok.strip().upper().replace(".", "-")
            if not s or s in seen:
                continue
            if not re.fullmatch(r"[A-Z0-9\-]{1,12}", s):
                continue
            seen.add(s)
            out.append(s)
    return out


def fetch_universe(preset: str) -> List[str]:
    if preset == "all_us":
        r = requests.get(
            "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/"
            "main/all/all_tickers.txt",
            headers=_TICKER_LIST_UA, timeout=60)
        r.raise_for_status()
        return _clean_ticker_list(
            [ln.strip() for ln in r.text.splitlines() if ln.strip()])
    if preset == "sp500":
        html = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=_TICKER_LIST_UA, timeout=60).text
        tables = pd.read_html(StringIO(html))
        return _clean_ticker_list(tables[0]["Symbol"].astype(str).tolist())
    if preset == "nasdaq100":
        html = requests.get(
            "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
            headers=_TICKER_LIST_UA, timeout=60).text
        tables = pd.read_html(StringIO(html))
        return _clean_ticker_list(tables[0]["Ticker"].astype(str).tolist())
    raise SystemExit(f"unknown universe preset: {preset}")


# ----------------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------------
def shape(r: Dict) -> Dict:
    """The public per-symbol result shape (mirrors scan_api's shaping)."""
    return {**{k: r.get(k) for k in ("symbol", "price", "change_pct",
                                     "signal", "score", "note", "error")},
            "details": r.get("details")}


def run_pass(symbols: List[str], interval: str, period: str,
             code: str) -> Dict:
    """Chunked pass over this job's symbol slice. Returns findings
    (signal results only, shaped) plus scanned/missing counts."""
    intraday = interval not in ("1d", "1wk", "1mo")
    size = CHUNK_INTRADAY if intraday else CHUNK_DAILY
    findings: List[Dict] = []
    scanned = 0
    missing = 0
    total_chunks = max(1, (len(symbols) + size - 1) // size)
    for idx, i in enumerate(range(0, len(symbols), size)):
        chunk = symbols[i:i + size]
        t0 = time.time()
        candles = scanner.batch_load_candles(chunk, period, interval)
        missing += len(chunk) - len(candles)
        scanned += len(candles)
        if candles:
            results = scanner.run_scan(code, candles, language="python",
                                       interval=interval)
            findings.extend(shape(r) for r in results if r.get("signal"))
        logger.info("sub-chunk %d/%d: %d symbols, %d with data, "
                    "%d findings so far, %.1fs",
                    idx + 1, total_chunks, len(chunk), len(candles),
                    len(findings), time.time() - t0)
    findings.sort(key=lambda r: (-(r.get("score") or 0), r["symbol"]))
    return {"findings": findings, "scanned": scanned, "missing": missing}


# ----------------------------------------------------------------------------
# Ingest POST
# ----------------------------------------------------------------------------
def post_ingest(base_url: str, secret: str, payload: Dict,
                attempts: int = 4) -> Dict:
    """POST findings to the terminal. Retries with backoff - the first
    hit may pay the Render cold start (~a minute) before answering."""
    url = base_url.rstrip("/") + "/api/scans/ingest"
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(url, json=payload,
                              headers={"X-Ingest-Secret": secret},
                              timeout=300)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001 - retried below
            last_err = str(e)
        logger.warning("ingest attempt %d/%d failed: %s",
                       attempt, attempts, last_err)
        if attempt < attempts:
            time.sleep(20 * attempt)
    raise SystemExit(f"ingest POST failed after {attempts} attempts: "
                     f"{last_err}")


# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Base Breakout Actions scan")
    ap.add_argument("--interval", required=True,
                    help="1d | 5m | 15m | 30m | 1h | 1wk | 1mo")
    ap.add_argument("--period", required=True,
                    help="yfinance period, e.g. 1y | 5d | 1mo | 10y | 20y")
    ap.add_argument("--universe", default="all_us",
                    help="preset: all_us | sp500 | nasdaq100")
    ap.add_argument("--symbols", default="",
                    help="explicit comma list - overrides --universe "
                         "(small tests)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the universe to its first N symbols (tests)")
    ap.add_argument("--chunk-index", type=int, default=0)
    ap.add_argument("--chunks", type=int, default=1,
                    help="total matrix chunks; this job takes a stride "
                         "slice symbols[index::chunks]")
    ap.add_argument("--out-dir", default="findings")
    ap.add_argument("--ingest-url", default=os.getenv(
        "TERMINAL_URL", DEFAULT_INGEST_URL))
    ap.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", ""),
                    help="workflow run id; chunks of one run are merged "
                         "server-side under this id")
    ap.add_argument("--no-ingest", action="store_true",
                    help="write the findings JSON only, skip the POST")
    args = ap.parse_args()

    if args.chunks < 1 or not (0 <= args.chunk_index < args.chunks):
        raise SystemExit("--chunk-index must satisfy 0 <= index < chunks")

    code = scanner.PY_BASEBO_SCHEDULED_CODE
    if "RETEST_TOL" not in code or "_buyers_rising" not in code:
        raise SystemExit("detector code failed the v3 sanity check")

    if args.symbols.strip():
        symbols = _clean_ticker_list(args.symbols.replace(",", " ").split())
        universe_name = "custom"
    else:
        symbols = fetch_universe(args.universe)
        universe_name = args.universe
    if args.limit:
        symbols = symbols[:args.limit]
    total_universe = len(symbols)
    if args.chunks > 1:
        symbols = symbols[args.chunk_index::args.chunks]
    if not symbols:
        raise SystemExit("empty symbol slice - nothing to scan")
    if not args.run_id:
        args.run_id = f"local-{int(time.time())}"

    logger.info("interval=%s period=%s universe=%s (%d symbols; this job: "
                "%d, chunk %d/%d) run_id=%s",
                args.interval, args.period, universe_name, total_universe,
                len(symbols), args.chunk_index, args.chunks, args.run_id)

    t0 = time.time()
    out = run_pass(symbols, args.interval, args.period, code)
    elapsed = round(time.time() - t0, 1)

    payload = {
        "interval": args.interval,
        "period": args.period,
        "universe": universe_name,
        "run_id": args.run_id,
        "chunk_index": args.chunk_index,
        "chunks": args.chunks,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scanned": out["scanned"],
        "missing": out["missing"],
        "elapsed_seconds": elapsed,
        "findings": out["findings"],
    }

    os.makedirs(args.out_dir, exist_ok=True)
    if args.chunks > 1:
        fname = f"{args.interval}-chunk{args.chunk_index}.json"
    else:
        fname = f"{args.interval}.json"
    fpath = os.path.join(args.out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    logger.info("wrote %s: %d findings, %d scanned, %d missing, %.1fs",
                fpath, len(out["findings"]), out["scanned"],
                out["missing"], elapsed)

    if args.no_ingest:
        logger.info("--no-ingest: skipping the ingest POST")
        return 0
    secret = os.getenv("INGEST_SECRET", "").strip()
    if not secret:
        raise SystemExit("INGEST_SECRET env var is not set - cannot POST "
                         "(use --no-ingest for a local dry run)")
    resp = post_ingest(args.ingest_url, secret, payload)
    logger.info("ingest response: %s", resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
