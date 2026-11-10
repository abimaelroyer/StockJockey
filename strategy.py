"""Trading bot loop.

config.json's "strategy" picks the rules:

  candlestick    -- candlestick patterns at key levels (see candles.py). The
                    default.
  sma_crossover  -- the original moving average crossover, described below.

Both read the same completed daily bars and trade through
brokers.execute_signal().

SMA crossover: every cycle during market hours, for each symbol in config.json:

  1. Read config.json (symbols, short/long MA periods, dollars per trade, mode).
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
import candles
import robinhood
from config import load_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "heartbeat.json")
STATUS_PATH = os.path.join(STATE_DIR, "status.json")
SIGNAL_STATE_PATH = os.path.join(STATE_DIR, "signal_state.json")
# Stop and target for each candlestick position, saved when it is bought.
TRADE_PLANS_PATH = os.path.join(STATE_DIR, "trade_plans.json")

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


def fetch_daily_bars(symbol, lookback_days):
    """Daily open/high/low/close bars for `symbol`, oldest first, indexed by
    trading date.

    `lookback_days` counts trading days; ~1.6 calendar days per trading day
    covers weekends and holidays, then the frame is trimmed by the caller.
    """
    start = datetime.date.today() - datetime.timedelta(days=int(lookback_days * 1.6) + 10)
    try:
        history = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d", auto_adjust=False)
    except Exception as e:
        raise RuntimeError(f"No price data for {symbol} from yfinance (check the symbol): {e}")
    columns = ["Open", "High", "Low", "Close"]
    if history is None or history.empty or not set(columns) <= set(history.columns):
        raise RuntimeError(f"No price data for {symbol} from yfinance (check the symbol)")
    bars = history[columns].dropna()
    bars.columns = ["open", "high", "low", "close"]
    bars.index = [ts.date() for ts in bars.index]
    return bars


def fetch_daily_closes(symbol, lookback_days):
    """Daily closes for `symbol`, oldest first, indexed by trading date."""
    return fetch_daily_bars(symbol, lookback_days)["close"]


def completed_closes(closes, lookback_days, now=None):
    """Drop today's still-forming bar while the market is open, keep the last N days.

    Works on a Series of closes or a DataFrame of bars alike."""
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


def _load_json_dict(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_signal_state():
    return _load_json_dict(SIGNAL_STATE_PATH)


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


def check_symbol(cfg, symbol, signal_state):
    """Check one symbol with the configured strategy and return its status row."""
    if cfg["strategy"] == "candlestick":
        return check_symbol_candles(cfg, symbol, signal_state)
    return check_symbol_crossover(cfg, symbol, signal_state)


def check_symbol_crossover(cfg, symbol, signal_state):
    """Check one symbol for a crossover, trade it, and return its status row."""
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

    last_handled = signal_state.get(symbol, {})
    if signal and last_handled.get("date") == bar_date and last_handled.get("action") == signal:
        print(f"{symbol}: {signal} crossover on {bar_date} already handled.")
    elif signal:
        print(f"{symbol}: {signal} crossover: {cfg['short_window']}-day MA crossed "
              f"{'above' if signal == 'BUY' else 'below'} the {cfg['long_window']}-day MA on {bar_date}.")
        result = brokers.execute_signal(
            cfg, signal, symbol, latest_price, source="bot",
            short_ma=float(last.short_ma), long_ma=float(last.long_ma),
        )
        if result["status"] == "skipped":
            print(f"{symbol}: order skipped: {result['reason']}")
        # Broker errors raise before this point, so a failed order is retried
        # next cycle; filled and deliberately-skipped signals are not. Saved
        # per symbol so a crash later in the cycle can't repeat this order.
        signal_state[symbol] = {"date": bar_date, "action": signal, "result": result["status"]}
        _write_json(SIGNAL_STATE_PATH, signal_state)

    return {
        "as_of": bar_date,
        "close": round(float(last.close), 4),
        "latest_price": round(latest_price, 4),
        "short_ma": round(float(last.short_ma), 4),
        "long_ma": round(float(last.long_ma), 4),
        "trend": "above" if last.short_ma > last.long_ma else "below",
        "signal": signal,
        "last_crossover": signal_state.get(symbol),
    }


def _round(value):
    return None if value is None or pd.isna(value) else round(float(value), 4)


def check_symbol_candles(cfg, symbol, signal_state):
    """Check one symbol for a candlestick setup, trade it, and return its status row.

    Not holding the symbol -> look for a BUY (candles.find_entry).
    Holding it            -> look for a SELL (candles.find_exit), using the
                             stop and target saved when it was bought.
    """
    bars = fetch_daily_bars(symbol, cfg["lookback_days"])
    latest_price = float(bars["close"].iloc[-1])

    completed = completed_closes(bars, cfg["lookback_days"])
    # short_window sets how far back support/resistance and "average candle"
    # look; long_window is the trend average.
    frame = candles.add_context(completed, cfg["short_window"], cfg["long_window"])
    if len(frame) < cfg["long_window"] + 1:
        raise RuntimeError(
            f"Only {len(frame)} days of data for {symbol}; need {cfg['long_window'] + 1}"
        )

    last = frame.iloc[-1]
    bar_date = frame.index[-1].isoformat()
    plans = _load_json_dict(TRADE_PLANS_PATH)
    holding = symbol in brokers.load_positions()

    if holding:
        decision = candles.find_exit(frame, plans.get(symbol))
    else:
        decision = candles.find_entry(frame, cfg["reward_risk"])
    signal = decision["action"] if decision else None

    print(f"{symbol} {bar_date}: close ${last.close:.2f} | support {last.support:.2f} | "
          f"resistance {last.resistance:.2f} | {cfg['long_window']}-day MA {last.trend_ma:.2f} | "
          f"{'holding' if holding else 'flat'} | signal: "
          f"{decision['action'] + ' (' + decision['reason'] + ')' if decision else 'none'}")

    last_handled = signal_state.get(symbol, {})
    if signal and last_handled.get("date") == bar_date and last_handled.get("action") == signal:
        print(f"{symbol}: {signal} on {bar_date} already handled.")
    elif signal:
        result = brokers.execute_signal(
            cfg, signal, symbol, latest_price, source="bot",
            short_ma=_round(frame.close.rolling(cfg["short_window"]).mean().iloc[-1]),
            long_ma=_round(last.trend_ma), note=decision["reason"],
        )
        if result["status"] == "skipped":
            print(f"{symbol}: order skipped: {result['reason']}")
        if signal == "BUY" and result["status"] == "filled":
            plans[symbol] = {k: decision[k] for k in ("pattern", "level", "entry", "stop", "target")}
            plans[symbol]["date"] = bar_date
        elif signal == "SELL":
            plans.pop(symbol, None)
        _write_json(TRADE_PLANS_PATH, plans)
        signal_state[symbol] = {"date": bar_date, "action": signal, "result": result["status"],
                                "pattern": decision["reason"]}
        _write_json(SIGNAL_STATE_PATH, signal_state)

    plan = plans.get(symbol) if symbol in brokers.load_positions() else None
    short_ma = frame.close.rolling(cfg["short_window"]).mean().iloc[-1]
    return {
        "as_of": bar_date,
        "close": round(float(last.close), 4),
        "latest_price": round(latest_price, 4),
        "short_ma": _round(short_ma),
        "long_ma": _round(last.trend_ma),
        "trend": "above" if last.close > last.trend_ma else "below",
        "support": _round(last.support),
        "resistance": _round(last.resistance),
        "signal": signal,
        "pattern": decision["reason"] if decision else None,
        "stop": plan.get("stop") if plan else None,
        "target": plan.get("target") if plan else None,
        "last_crossover": signal_state.get(symbol),
    }


def _is_login_error(exc):
    return "Login" in str(exc) or "Verification challenge" in str(exc)


def run_cycle(cfg):
    """Check every configured symbol. One symbol's data problem doesn't stop
    the others; a Robinhood login failure does, since every order would fail."""
    signal_state = _load_signal_state()
    rows = {}
    for symbol in cfg["symbols"]:
        try:
            rows[symbol] = check_symbol(cfg, symbol, signal_state)
        except Exception as e:
            if _is_login_error(e):
                raise
            print(f"{symbol}: check failed: {e}", file=sys.stderr)
            rows[symbol] = {"error": str(e)}

    _write_json(STATUS_PATH, {
        "mode": cfg["mode"],
        "strategy": cfg["strategy"],
        "short_window": cfg["short_window"],
        "long_window": cfg["long_window"],
        "trade_dollars": cfg["trade_dollars"],
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "symbols": rows,
    })
    failed = [s for s, row in rows.items() if "error" in row]
    if failed and len(failed) == len(rows):
        raise RuntimeError(f"Every symbol failed this cycle (first error: {rows[failed[0]]['error']})")
    return rows


def run_strategy():
    print("Starting trading loop...")
    while True:
        try:
            cfg = load_config()
            print(f"\nCycle at {market_now().strftime('%Y-%m-%d %I:%M %p %Z')} | "
                  f"{', '.join(cfg['symbols'])} | {cfg['strategy']} "
                  f"({cfg['short_window']}/{cfg['long_window']}) | "
                  f"${cfg['trade_dollars']:.2f} per trade | {cfg['mode'].upper()} mode")

            if not market_is_open():
                print("Market closed. Idling...")
                write_heartbeat("ok")
                time.sleep(CLOSED_MARKET_SLEEP_SECONDS)
                continue

            rows = run_cycle(cfg)
            failed = [s for s, row in rows.items() if "error" in row]
            write_heartbeat("ok", f"check failed for {', '.join(failed)}" if failed else None)
            time.sleep(cfg["check_interval_minutes"] * 60)

        except Exception as e:
            print(f"\n[ERROR] Cycle failed: {e}", file=sys.stderr)
            write_heartbeat("error", str(e))
            if _is_login_error(e):
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
