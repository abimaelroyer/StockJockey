"""Moving average crossover bot.

Every cycle during market hours:

  1. Read config.json (symbol, short/long MA periods, trade size, mode).
  2. Fetch the last `lookback_days` daily bars for the symbol from yfinance.
  3. Compute the short and long simple moving averages.
  4. Compare the two most recent COMPLETED days. A crossover is when the short
     MA goes from at/below the long MA to above it (BUY), or from at/above to
     below it (SELL).
  5. On a crossover, place the order through brokers.execute_signal() -- a
     simulated fill in paper mode, a Robinhood market order in live mode.

Today's bar is ignored while the market is open: its close keeps moving, and a
signal from a half-finished day can flip back before the bell. Each crossover
is acted on once (state/signal_state.json remembers the last one handled).
"""

import datetime
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import brokers
import robinhood
from config import load_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "heartbeat.json")
STATUS_PATH = os.path.join(STATE_DIR, "status.json")
SIGNAL_STATE_PATH = os.path.join(STATE_DIR, "signal_state.json")

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = datetime.time(9, 30)
MARKET_CLOSE = datetime.time(16, 0)
CLOSED_MARKET_SLEEP_SECONDS = 900


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def market_now():
    return datetime.datetime.now(MARKET_TZ)


def market_is_open(now=None):
    now = now or market_now()
    if now.weekday() > 4:  # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() < MARKET_CLOSE


def fetch_daily_closes(symbol, lookback_days):
    """Daily closes for `symbol`, oldest first, indexed by trading date.

    `lookback_days` counts trading days; ~1.6 calendar days per trading day
    covers weekends and holidays, then the series is trimmed by the caller.
    """
    start = datetime.date.today() - datetime.timedelta(days=int(lookback_days * 1.6) + 10)
    history = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d", auto_adjust=False)
    if history is None or history.empty or "Close" not in history:
        raise RuntimeError(f"yfinance returned no daily data for {symbol}")
    closes = history["Close"].dropna()
    closes.index = [ts.date() for ts in closes.index]
    return closes


def completed_closes(closes, lookback_days, now=None):
    """Drop today's still-forming bar while the market is open, keep the last N days."""
    now = now or market_now()
    if len(closes) and closes.index[-1] == now.date() and market_is_open(now):
        closes = closes.iloc[:-1]
    return closes.tail(lookback_days)


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


def compute_moving_averages(closes, short_window, long_window):
    return pd.DataFrame({
        "close": closes,
        "short_ma": closes.rolling(window=short_window).mean(),
        "long_ma": closes.rolling(window=long_window).mean(),
    })


def detect_crossover(frame):
    """Return "BUY", "SELL" or None by comparing the last two rows of `frame`."""
    if len(frame) < 2:
        return None
    prev, cur = frame.iloc[-2], frame.iloc[-1]
    if pd.isna([prev.short_ma, prev.long_ma, cur.short_ma, cur.long_ma]).any():
        return None
    if prev.short_ma <= prev.long_ma and cur.short_ma > cur.long_ma:
        return "BUY"
    if prev.short_ma >= prev.long_ma and cur.short_ma < cur.long_ma:
        return "SELL"
    return None


# ---------------------------------------------------------------------------
# State files read by the API / trader panel
# ---------------------------------------------------------------------------


def _write_json(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to write {path}: {e}", file=sys.stderr)


def _load_signal_state():
    try:
        with open(SIGNAL_STATE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_heartbeat(status="ok", detail=None):
    """Record that a cycle completed. /health reads this to tell a live bot
    apart from one that has stalled or keeps failing."""
    payload = {"status": status, "time": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if detail:
        payload["detail"] = detail
    _write_json(HEARTBEAT_PATH, payload)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


def run_cycle(cfg):
    symbol = cfg["symbol"]
    closes = fetch_daily_closes(symbol, cfg["lookback_days"])
    latest_price = float(closes.iloc[-1])

    frame = compute_moving_averages(
        completed_closes(closes, cfg["lookback_days"]),
        cfg["short_window"],
        cfg["long_window"],
    )
    if len(frame) < cfg["long_window"] + 1:
        raise RuntimeError(
            f"Only {len(frame)} days of data for {symbol}; need {cfg['long_window'] + 1}"
        )

    last = frame.iloc[-1]
    bar_date = frame.index[-1].isoformat()
    signal = detect_crossover(frame)

    print(f"{symbol} {bar_date}: close ${last.close:.2f} | "
          f"{cfg['short_window']}-day MA {last.short_ma:.2f} | "
          f"{cfg['long_window']}-day MA {last.long_ma:.2f} | signal: {signal or 'none'}")

    signal_state = _load_signal_state()
    last_handled = signal_state.get(symbol, {})
    result = None
    if signal and last_handled.get("date") == bar_date and last_handled.get("action") == signal:
        print(f"{signal} crossover on {bar_date} already handled.")
    elif signal:
        print(f"{signal} crossover: {cfg['short_window']}-day MA crossed "
              f"{'above' if signal == 'BUY' else 'below'} the {cfg['long_window']}-day MA on {bar_date}.")
        result = brokers.execute_signal(
            cfg, signal, symbol, latest_price, source="bot",
            short_ma=float(last.short_ma), long_ma=float(last.long_ma),
        )
        if result["status"] == "skipped":
            print(f"Order skipped: {result['reason']}")
        # Broker errors raise before this point, so a failed order is retried
        # next cycle; filled and deliberately-skipped signals are not.
        signal_state[symbol] = {"date": bar_date, "action": signal, "result": result["status"]}
        _write_json(SIGNAL_STATE_PATH, signal_state)

    _write_json(STATUS_PATH, {
        "symbol": symbol,
        "mode": cfg["mode"],
        "short_window": cfg["short_window"],
        "long_window": cfg["long_window"],
        "as_of": bar_date,
        "close": round(float(last.close), 4),
        "latest_price": round(latest_price, 4),
        "short_ma": round(float(last.short_ma), 4),
        "long_ma": round(float(last.long_ma), 4),
        "trend": "above" if last.short_ma > last.long_ma else "below",
        "signal": signal,
        "last_crossover": signal_state.get(symbol),
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    return signal, result


def run_strategy():
    print("Starting moving average crossover loop...")
    while True:
        try:
            cfg = load_config()
            print(f"\nCycle at {market_now().strftime('%Y-%m-%d %I:%M %p %Z')} | "
                  f"{cfg['symbol']} {cfg['short_window']}/{cfg['long_window']} SMA | "
                  f"{cfg['trade_quantity']} share(s) | {cfg['mode'].upper()} mode")

            if not market_is_open():
                print("Market closed. Idling...")
                write_heartbeat("ok")
                time.sleep(CLOSED_MARKET_SLEEP_SECONDS)
                continue

            run_cycle(cfg)
            write_heartbeat("ok")
            time.sleep(cfg["check_interval_minutes"] * 60)

        except Exception as e:
            print(f"\n[ERROR] Cycle failed: {e}", file=sys.stderr)
            write_heartbeat("error", str(e))
            if "Login" in str(e):
                # Force a fresh Robinhood login on the next live order.
                robinhood._ensure_login._logged_in = False
            print("Retrying in 60 seconds...", file=sys.stderr)
            time.sleep(60)


if __name__ == "__main__":
    try:
        run_strategy()
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
        sys.exit(0)
