import json
import csv
import os
import sys
import datetime
import time
import urllib.request
import robinhood
import robin_stocks.robinhood as r
from zoneinfo import ZoneInfo


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "state")

POSITIONS_PATH = os.path.join(STATE_DIR, "positions.json")
COOLDOWNS_PATH = os.path.join(STATE_DIR, "cooldowns.json")
UNIVERSE_PATH = os.path.join(STATE_DIR, "universe.json")
STRATEGY_YAML_PATH = os.path.join(STATE_DIR, "strategy.yaml")
LEDGER_PATH = os.path.join(SCRIPT_DIR, "trade_ledger.csv")
ALERT_HISTORY_PATH = os.path.join(STATE_DIR, "alert_history.json")
PARAMS_PATH = os.path.join(SCRIPT_DIR, "strategy_params.json")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "heartbeat.json")

# Minutes to wait before re-entering a symbol after it was closed, so a
# stop-loss exit isn't immediately undone by the RSI scan in the same cycle.
REENTRY_COOLDOWN_MINUTES = 60

# Fixed dollar size for every entry.
ORDER_DOLLARS = 20.00
PAPER_MODE = True  # Set to False to enable live trading
VERBOSE = False
MARKET_TZ = ZoneInfo("America/New_York")
NO_NEW_ENTRIES_AFTER = datetime.time(15, 45)  # stop buying at 3:45 PM ET
MARKET_OPEN = datetime.time(9, 30)
MARKET_CLOSE = datetime.time(16, 0)
FLATTEN_AT = datetime.time(15, 55)            # close everything at 3:55 PM ET
FLATTEN_ENABLED = True


def load_params(file_path):
    with open(file_path, 'r') as f:
        return json.load(f)


def load_exit_rules():
    """Read stop-loss / take-profit thresholds (percent) from strategy.yaml.

    These live in state/strategy.yaml (stop_loss_pct was previously defined
    there but never read by the live path). Returns (stop_loss_pct,
    take_profit_pct); either may be None if not configured.
    """
    stop_loss_pct = None
    take_profit_pct = None
    try:
        import yaml
        with open(STRATEGY_YAML_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        if cfg.get("stop_loss_pct") is not None:
            stop_loss_pct = float(cfg["stop_loss_pct"])
        if cfg.get("take_profit_pct") is not None:
            take_profit_pct = float(cfg["take_profit_pct"])
    except Exception as e:
        print(f"Could not load exit rules from {STRATEGY_YAML_PATH}: {e}", file=sys.stderr)
    return stop_loss_pct, take_profit_pct


# ---------------------------------------------------------------------------
# Local open-position ledger
#
# build_holdings() reflects only SETTLED positions, so a fractional order
# placed on one cycle may not appear before the next cycle runs -- which is how
# the same ticker got bought repeatedly (DJT/SLV twice within 6 minutes). We
# record every open position locally the instant an order is placed, and treat
# that as the authoritative "do I already hold this?" check. It also stores the
# entry price/quantity needed for stop-loss / take-profit and realised PnL.
# ---------------------------------------------------------------------------


def load_positions():
    if not os.path.exists(POSITIONS_PATH):
        return {}
    try:
        with open(POSITIONS_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_positions(positions):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(POSITIONS_PATH, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"Failed to persist open positions: {e}", file=sys.stderr)


def record_open_position(symbol, entry_price, quantity, invested, timestamp=None):
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    positions = load_positions()
    positions[symbol] = {
        "entry_price": float(entry_price),
        "quantity": float(quantity),
        "invested": float(invested),
        "timestamp": timestamp,
    }
    save_positions(positions)


def remove_position(symbol):
    positions = load_positions()
    if symbol in positions:
        del positions[symbol]
        save_positions(positions)


def _load_cooldowns():
    if not os.path.exists(COOLDOWNS_PATH):
        return {}
    try:
        with open(COOLDOWNS_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def mark_exit_cooldown(symbol, timestamp=None):
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cooldowns = _load_cooldowns()
    cooldowns[symbol] = timestamp
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(COOLDOWNS_PATH, "w") as f:
            json.dump(cooldowns, f, indent=2)
    except Exception as e:
        print(f"Failed to persist re-entry cooldown for {symbol}: {e}", file=sys.stderr)


def in_reentry_cooldown(symbol, cooldown_minutes=REENTRY_COOLDOWN_MINUTES):
    last_exit = _load_cooldowns().get(symbol)
    if not last_exit:
        return False
    try:
        last_time = datetime.datetime.fromisoformat(last_exit)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - last_time).total_seconds() < cooldown_minutes * 60
    except Exception:
        return False


def have_open_position(symbol):
    """True if we hold this symbol either locally (just placed) or on Robinhood."""
    if symbol in load_positions():
        return True
    try:
        holdings = r.account.build_holdings()
        return symbol in holdings
    except Exception as e:
        # If we cannot confirm, err on the side of NOT double-buying.
        print(f"Could not verify holdings for {symbol}; assuming position exists: {e}",
              file=sys.stderr)
        return True

def calculate_rsi(prices, period=14):
    if prices is None or len(prices) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)

    # Wilder's seed: simple average of the first `period` price changes.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing across EVERY remaining change, including the most recent
    # bar. gains/losses have len(prices) - 1 elements, so the last valid index
    # is len(gains) - 1; iterate to len(gains) so nothing is dropped. The
    # avg_loss == 0 shortcut is applied only AFTER smoothing -- doing it on the
    # seed (as before) would return 100 and silently discard every bar after
    # the seed window whenever the seed happened to contain no down moves.
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))

def check_and_record_alert(symbol, action, price, rsi):
    history_path = ALERT_HISTORY_PATH
    os.makedirs(STATE_DIR, exist_ok=True)
    history = {}
    if os.path.exists(history_path):
        try:
            with open(history_path, "r") as f:
                history = json.load(f)
        except Exception:
            pass
            
    now = datetime.datetime.now(datetime.timezone.utc)
    key = f"{symbol}_{action}"
    last_alert = history.get(key)
    if last_alert:
        try:
            last_time = datetime.datetime.fromisoformat(last_alert["timestamp"])
            if (now - last_time).total_seconds() < 4 * 3600:
                return False
        except Exception:
            pass
            
    history[key] = {
        "timestamp": now.isoformat(),
        "price": price,
        "rsi": rsi
    }
    try:
        with open(history_path, "w") as f:
            json.dump(history, f)
    except Exception:
        pass
    return True

def _extract_filled_quantity(order_info, fallback_price):
    """Best-effort parse of filled share quantity from a Robinhood order dict."""
    if isinstance(order_info, dict):
        for key in ("quantity", "cumulative_quantity"):
            val = order_info.get(key)
            if val:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    # Fall back to the notional / price estimate for fractional orders.
    if fallback_price and fallback_price > 0:
        return ORDER_DOLLARS / fallback_price
    return 0.0


def log_trade(symbol, action, rsi, price, reason=None):
    file_exists = os.path.isfile(LEDGER_PATH)
    
    # If file exists but has the old schema (no 'rsi' header), we can overwrite/reset it
    has_rsi_header = False
    if file_exists:
        try:
            with open(LEDGER_PATH, 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header and 'rsi' in header:
                    has_rsi_header = True
        except Exception:
            pass

    # If it exists but does not have the new header, overwrite it (or start fresh)
    mode = 'a'
    if file_exists and not has_rsi_header:
        mode = 'w'
        file_exists = False
        
    # --- LIVE TRADING EXECUTION ---
    order_info = None
    if action == 'BUY':
        if have_open_position(symbol):
            print(f"Skipping {symbol} BUY: Position already open.")
            return
        if in_reentry_cooldown(symbol):
            print(f"Skipping {symbol} BUY: within re-entry cooldown.")
            return

        if PAPER_MODE:
            balance = load_paper_balance()
            if balance < ORDER_DOLLARS:
                print(f"📝 PAPER MODE: Insufficient balance (${balance:.2f}) to buy {symbol}. Skipping.")
                return
            print(f"📝 PAPER MODE: Simulating BUY for {symbol} of ${ORDER_DOLLARS:.2f}...")
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record_open_position(symbol, price, ORDER_DOLLARS / price, ORDER_DOLLARS, timestamp)
            save_paper_balance(balance - ORDER_DOLLARS)
        else:
            print(f"🚀 Placing LIVE buy order for {symbol} of ${ORDER_DOLLARS:.2f}...")
            try:
                order_info = r.orders.order_buy_fractional_by_price(symbol, ORDER_DOLLARS)
                print(f"Order response: {order_info}")
                timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                qty = _extract_filled_quantity(order_info, price)
                record_open_position(symbol, price, qty, ORDER_DOLLARS, timestamp)
            except Exception as e:
                print(f"Failed to execute live BUY order for {symbol}: {e}", file=sys.stderr)
                raise e
        
    elif action == 'SELL':
        if PAPER_MODE:
            print(f"📝 PAPER MODE: Simulating SELL for {symbol}...")
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            positions = load_positions()
            entry = positions.get(symbol)
            if entry:
                qty = float(entry["quantity"])
                proceeds = qty * price
                balance = load_paper_balance()
                save_paper_balance(balance + proceeds)
            remove_position(symbol)
            mark_exit_cooldown(symbol, timestamp)
        else:
            print(f"🚀 Checking active positions to sell entire holding of {symbol}...")
            try:
                holdings = r.account.build_holdings()
                positions = load_positions()
                if symbol in holdings:
                    qty = float(holdings[symbol]['quantity'])
                    if qty > 0:
                        print(f"Found {qty} shares of {symbol}. Placing LIVE market sell order...")
                        order_info = r.orders.order_sell_market(symbol, qty, timeInForce='gfd')
                        print(f"Order response: {order_info}")
                        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        remove_position(symbol)
                        mark_exit_cooldown(symbol, timestamp)
                    else:
                        print(f"No shares to sell for {symbol}. Clearing stale tracking.")
                        remove_position(symbol)
                        return
                else:
                    print(f"No settled position found for {symbol}; leaving tracking intact.")
                    return
            except Exception as e:
                print(f"Failed to execute live SELL order for {symbol}: {e}", file=sys.stderr)
                raise e

    ledger_type = 'SIMULATED' if PAPER_MODE else ('LIVE' if not reason else f'LIVE:{reason}')
    with open(LEDGER_PATH, mode, newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['timestamp', 'symbol', 'action', 'rsi', 'price', 'type'])
        timestamp = datetime.datetime.now().isoformat()
        writer.writerow([timestamp, symbol, action, f"{rsi:.2f}", price, ledger_type])
        
    # Trigger Discord Instant Webhook Alert Broadcast (deduplicated)
    if not check_and_record_alert(symbol, action, price, rsi):
        print(f"Skipping duplicate alert for {symbol} {action}")
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook_url:
        message = {
            "content": f"🚨 **MOMENTUM TRADE ALERT** 🚨\n**Action:** {action}\n**Asset:** {symbol}\n**Execution Price:** ${price:.2f}\n**Calculated RSI:** {rsi:.2f}\n**Mode:** LIVE ORDER EXECUTED"
        }
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(message).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
            )
            urllib.request.urlopen(req)
        except Exception as e:
            print(f"Failed to push alert out to Discord endpoint: {e}")

def get_trading_universe(params):
    """Return a STABLE tradeable universe.

    Trading Robinhood's live Top-100 popularity list means the tradeable set
    silently changes between cycles. To avoid that churn:

      1. If strategy_params.json defines a non-empty "universe" list, trade
         exactly that -- fully explicit, never changes on its own.
      2. Otherwise snapshot the Top-100 ONCE to state/universe.json and reuse
         that snapshot on every subsequent cycle. Delete the file (or set
         "refresh_universe": true in params) to deliberately re-snapshot.
    """
    configured = params.get("universe")
    if isinstance(configured, list) and configured:
        print(f"Universe: {len(configured)} explicitly configured tickers (fixed).")
        return [s.strip().upper() for s in configured if s and s.strip()]

    if params.get("refresh_universe") and os.path.exists(UNIVERSE_PATH):
        try:
            os.remove(UNIVERSE_PATH)
            print("refresh_universe set: cleared existing universe snapshot.")
        except Exception as e:
            print(f"Could not clear universe snapshot: {e}", file=sys.stderr)

    if os.path.exists(UNIVERSE_PATH):
        try:
            with open(UNIVERSE_PATH) as f:
                snapshot = json.load(f)
            if isinstance(snapshot, dict):
                tickers = snapshot.get("tickers", [])
            else:
                tickers = snapshot
            if tickers:
                print(f"Universe: {len(tickers)} tickers from pinned snapshot "
                      f"({UNIVERSE_PATH}).")
                return tickers
        except Exception as e:
            print(f"Could not read universe snapshot, re-snapshotting: {e}", file=sys.stderr)

    tickers = robinhood.get_top_100_tickers()
    if not tickers:
        raise RuntimeError("No tickers fetched from Robinhood API")

    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(UNIVERSE_PATH, "w") as f:
            json.dump({
                "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "top_100",
                "tickers": tickers,
            }, f, indent=2)
        print(f"Universe: snapshotted {len(tickers)} Top-100 tickers to {UNIVERSE_PATH}; "
              f"this set is now pinned for future cycles.")
    except Exception as e:
        print(f"Failed to persist universe snapshot: {e}", file=sys.stderr)
    return tickers


def check_exits(interval, stop_loss_pct, take_profit_pct):
    """Enforce exit discipline on every OPEN position.

    Runs independently of the RSI scan and the trading universe, so a position
    is protected by its stop-loss / take-profit even after the ticker rotates
    out of the scan set. Sells when the position's return versus its recorded
    entry price crosses -stop_loss_pct or +take_profit_pct.
    """
    positions = load_positions()
    if not positions:
        return
    if stop_loss_pct is None and take_profit_pct is None:
        return

    print(f"Checking exit rules on {len(positions)} open position(s) "
          f"(stop -{stop_loss_pct}% / take +{take_profit_pct}%)...")

    for symbol, pos in list(positions.items()):
        try:
            entry_price = float(pos.get("entry_price", 0.0))
            if entry_price <= 0:
                continue

            close_prices = robinhood.get_historical_close_prices(symbol, interval=interval, span='day')
            if not close_prices:
                continue
            current_price = close_prices[-1]
            change_pct = (current_price - entry_price) / entry_price * 100.0

            reason = None
            if stop_loss_pct is not None and change_pct <= -abs(stop_loss_pct):
                reason = "STOP_LOSS"
            elif take_profit_pct is not None and change_pct >= abs(take_profit_pct):
                reason = "TAKE_PROFIT"

            if reason:
                print(f"⛔ {reason} for {symbol}: entry ${entry_price:.2f} -> "
                      f"${current_price:.2f} ({change_pct:+.2f}%). Closing position.")
                # rsi is not the trigger here; pass the position's current
                # change so the ledger/alert still carries a number.
                exit_rsi = calculate_rsi(close_prices)
                log_trade(symbol, 'SELL', exit_rsi if exit_rsi is not None else 0.0, current_price, reason=reason)
        except Exception as e:
            err_msg = str(e)
            if "Login check failed" in err_msg or "Login failed" in err_msg or "Verification challenge requested" in err_msg:
                raise e
            print(f"Error checking exit for {symbol}: {e}")

def market_now():
    return datetime.datetime.now(MARKET_TZ)


def past_entry_cutoff():
    now = market_now()
    if now.weekday() > 4:
        return True
    return now.time() >= NO_NEW_ENTRIES_AFTER

def market_is_open():
    now = market_now()
    if now.weekday() > 4:  # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN <= now.time() < MARKET_CLOSE


def is_flatten_window():
    now = market_now()
    if now.weekday() > 4:
        return False
    return now.time() >= FLATTEN_AT


def flatten_all_positions(interval):
    """Close every open position — end-of-day discipline."""
    positions = load_positions()
    if not positions:
        return

    print(f"🔔 EOD FLATTEN: closing {len(positions)} position(s) before the bell.")

    for symbol in list(positions.keys()):
        try:
            close_prices = robinhood.get_historical_close_prices(symbol, interval=interval, span='day')
            if not close_prices:
                print(f"  No price for {symbol}; leaving position open.")
                continue

            current_price = close_prices[-1]
            rsi_val = calculate_rsi(close_prices)
            log_trade(symbol, 'SELL', rsi_val if rsi_val is not None else 0.0,
                      current_price, reason="EOD_FLATTEN")
        except Exception as e:
            print(f"  Failed to flatten {symbol}: {e}", file=sys.stderr)

def write_heartbeat(status="ok", detail=None):
    """Record that a scan cycle completed. /health reads this to tell
    a live bot apart from one whose Robinhood session has died."""
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {
        "status": status,
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if detail:
        payload["detail"] = detail
    try:
        with open(HEARTBEAT_PATH, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"Failed to write heartbeat: {e}", file=sys.stderr)

def run_strategy():
    print("Starting always-on execution loop...")
    while True:
        try:
            print(f"\nStarting strategy run at {datetime.datetime.now()}")
            
            # Verify Robinhood authentication
            login_status_str = robinhood._ensure_login()
            try:
                login_status = json.loads(login_status_str)
            except Exception as e:
                raise RuntimeError(f"Authentication response parsing failed: {e}")
                
            if login_status.get("status") == "error":
                raise RuntimeError(f"Authentication failed: {login_status.get('message')}")

            params = load_params(PARAMS_PATH)
            interval = params.get('time_interval', '5minute')
            oversold_threshold = params.get('rsi_oversold_threshold', 30.0)
            overbought_threshold = params.get('rsi_overbought_threshold', 70.0)
            stop_loss_pct, take_profit_pct = load_exit_rules()
            if FLATTEN_ENABLED and is_flatten_window():
                flatten_all_positions(interval)
                print("Market closing. Sleeping 5 minutes...")
                time.sleep(300)
                continue

            if FLATTEN_ENABLED and is_flatten_window():
                flatten_all_positions(interval)
                print("Market closing. Sleeping 5 minutes...")
                time.sleep(300)
                continue

            if not market_is_open():
                print(f"Market closed ({market_now().strftime('%I:%M %p %Z')}). Idling...")
                write_heartbeat("ok")
                time.sleep(900)
                continue

            # Exit discipline first: protect open positions before looking for
            # new entries, and independent of the (pinned) trading universe.
            check_exits(interval, stop_loss_pct, take_profit_pct)

            print("Initializing Day-Trading Scanner across active tickers...")
            symbols = get_trading_universe(params)
            if not symbols:
                raise RuntimeError("No tickers in trading universe")

            print(f"Successfully loaded {len(symbols)} tickers for tracking loop.")

            for symbol in symbols:
                try:
                    # Query the 5-minute tracking candles inside extended hours boundaries
                    close_prices = robinhood.get_historical_close_prices(symbol, interval=interval, span='day')
                    if not close_prices:
                        continue
                        
                    rsi_val = calculate_rsi(close_prices)
                    if rsi_val is None:
                        continue
                        
                    current_price = close_prices[-1]
                    if VERBOSE:
                        print(f"Ticker: {symbol:5} | Current Price: ${current_price:7.2f} | Current RSI: {rsi_val:.2f}")
                    
                    # Momentum trigger check
                    
                    # Momentum trigger check
                    if rsi_val <= oversold_threshold and not past_entry_cutoff():
                        print(f"🔥 MOMENTUM BUY SIGNAL TRIGGERED FOR {symbol} AT RSI {rsi_val:.2f}")
                        log_trade(symbol, 'BUY', rsi_val, current_price)
                    elif rsi_val >= overbought_threshold and have_open_position(symbol):
                        print(f"💥 MOMENTUM SELL SIGNAL TRIGGERED FOR {symbol} AT RSI {rsi_val:.2f}")
                        log_trade(symbol, 'SELL', rsi_val, current_price)
                        
                except Exception as e:
                    # If it's a login check / multi-factor verification block error, raise it to restart loop
                    err_msg = str(e)
                    if "Login check failed" in err_msg or "Login failed" in err_msg or "Verification challenge requested" in err_msg:
                        raise e
                    print(f"Error skipping iteration sequence for {symbol}: {e}")
            
            write_heartbeat("ok")

            print("Completed scanning tickers. Waiting 5 minutes (300 seconds)...")            

            time.sleep(300)
            
        except (RuntimeError, Exception) as e:
            print(f"\n[ERROR] Authentication or data fetch handshake failed: {e}", file=sys.stderr)
            write_heartbeat("error", str(e))
            try:
                robinhood._ensure_login._logged_in = False
            except Exception:
                pass
            print("Waiting 60 seconds before retrying execution sequence...", file=sys.stderr)
            time.sleep(60)

PAPER_BALANCE_FILE = os.path.join(STATE_DIR, "paper_balance.json")

def load_paper_balance():
    if not os.path.exists(PAPER_BALANCE_FILE):
        return 0.0
    with open(PAPER_BALANCE_FILE) as f:
        return json.load(f).get("balance", 0.0)

def save_paper_balance(amount):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PAPER_BALANCE_FILE, "w") as f:
        json.dump({"balance": round(amount, 2)}, f, indent=2)

if __name__ == "__main__":
    try:
        run_strategy()
    except KeyboardInterrupt:
        print("\nBot execution terminated by user. Exiting smoothly.")
        sys.exit(0)
