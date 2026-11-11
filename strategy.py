"""Trading bot loop: candlestick patterns at key levels.

Every cycle during market hours, for each symbol in config.json:

  1. Read config.json (symbols, level/trend windows, dollars per trade, mode).
  2. Fetch the last `lookback_days` daily bars for the symbol from yfinance.
  3. Not holding it: look for a BUY pattern (candles.find_entry).
     Holding it: look for a SELL (candles.find_exit) -- the stop or target
     being reached, or a bearish pattern.
  4. Place the order through brokers.execute_signal() -- a simulated fill in
     paper mode, a Robinhood market order in live mode.

Today's bar is ignored while the market is open: its candle keeps changing
until the close. Each signal is acted on once (state/signal_state.json
remembers the last one handled).
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


def completed_bars(bars, lookback_days, now=None):
    """Drop today's still-forming bar while the market is open, keep the last N days."""
    now = now or market_now()
    if len(bars) and bars.index[-1] == now.date() and market_is_open(now):
        bars = bars.iloc[:-1]
    return bars.tail(lookback_days)


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


def _round(value):
    return None if value is None or pd.isna(value) else round(float(value), 4)


def check_symbol(cfg, symbol, signal_state):
    """Check one symbol for a candlestick setup, trade it, and return its status row.

    Not holding the symbol -> look for a BUY (candles.find_entry).
    Holding it            -> look for a SELL (candles.find_exit), using the
                             stop and target saved when it was bought.
    """
    bars = fetch_daily_bars(symbol, cfg["lookback_days"])
    latest_price = float(bars["close"].iloc[-1])

    completed = completed_bars(bars, cfg["lookback_days"])
    frame = candles.add_context(completed, cfg["level_window"], cfg["trend_window"])
    needed = max(cfg["level_window"], cfg["trend_window"]) + 1
    if len(frame) < needed:
        raise RuntimeError(f"Only {len(frame)} days of data for {symbol}; need {needed}")

    last = frame.iloc[-1]
    bar_date = frame.index[-1].isoformat()
    plans = _load_json_dict(TRADE_PLANS_PATH)
    holding = symbol in brokers.load_positions()

    if holding:
        # The live price is checked against the stop and target too, so they
        # trigger during the day the way a broker's stop/limit order would.
        decision = candles.find_exit(frame, plans.get(symbol), latest_price)
    else:
        decision = candles.find_entry(frame, cfg["reward_risk"])
    signal = decision["action"] if decision else None

    print(f"{symbol} {bar_date}: close ${last.close:.2f} | support {last.support:.2f} | "
          f"resistance {last.resistance:.2f} | {'uptrend' if candles.uptrend(frame) else 'no uptrend'} | "
          f"{'holding' if holding else 'flat'} | signal: "
          f"{decision['action'] + ' (' + decision['reason'] + ')' if decision else 'none'}")

    last_handled = signal_state.get(symbol, {})
    if signal and last_handled.get("date") == bar_date and last_handled.get("action") == signal:
        print(f"{symbol}: {signal} on {bar_date} already handled.")
    elif signal:
        if signal == "BUY" and latest_price <= decision["stop"]:
            # Price opened below the pattern's stop: the setup already failed.
            result = {"status": "skipped",
                      "reason": f"price {latest_price:.2f} is already at/below the stop {decision['stop']:.2f}"}
        else:
            result = brokers.execute_signal(
                cfg, signal, symbol, latest_price, source="bot", pattern=decision["reason"],
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
    return {
        "as_of": bar_date,
        "close": round(float(last.close), 4),
        "latest_price": round(latest_price, 4),
        "uptrend": bool(candles.uptrend(frame)),
        "holding": symbol in brokers.load_positions(),
        "support": _round(last.support),
        "resistance": _round(last.resistance),
        "signal": signal,
        "pattern": decision["reason"] if decision else None,
        "stop": plan.get("stop") if plan else None,
        "target": plan.get("target") if plan else None,
        "last_signal": signal_state.get(symbol),
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
        "level_window": cfg["level_window"],
        "trend_window": cfg["trend_window"],
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
                  f"{', '.join(cfg['symbols'])} | candlestick patterns | "
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
