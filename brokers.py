"""Order execution for the crossover bot.

Two brokers sit behind one interface, chosen by config.json's "mode":

  paper -- simulated fills against a local cash balance (state/paper_balance.json).
           No money moves. Robinhood has no paper-trading API, so this is how
           paper trading works for both the bot and TradingView alerts.
  live  -- real fractional market orders on Robinhood via robin_stocks.

Every buy spends cfg["trade_dollars"] (fractional shares), so one paper or
Robinhood balance can cover several symbols.

Both the strategy loop and the TradingView webhook call execute_signal(), which
is serialised by a lock so the two can never double-buy the same symbol.
"""

import csv
import datetime
import json
import math
import os
import threading
import urllib.request

import robinhood

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
ARCHIVE_DIR = os.path.join(SCRIPT_DIR, "archive")
POSITIONS_PATH = os.path.join(STATE_DIR, "positions.json")
PAPER_BALANCE_PATH = os.path.join(STATE_DIR, "paper_balance.json")
LEDGER_PATH = os.path.join(SCRIPT_DIR, "trade_ledger.csv")

LEDGER_FIELDS = [
    "timestamp", "symbol", "action", "quantity", "price",
    "short_ma", "long_ma", "mode", "source",
]

_lock = threading.Lock()


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Bot-owned positions
#
# The bot only ever sells shares it bought itself. Positions are recorded here
# the moment an order is placed, so shares you hold in Robinhood outside the
# bot are never touched by a SELL signal.
# ---------------------------------------------------------------------------


def load_positions():
    data = _read_json(POSITIONS_PATH, {})
    return data if isinstance(data, dict) else {}


def save_positions(positions):
    _write_json(POSITIONS_PATH, positions)


def load_paper_balance(starting_balance):
    data = _read_json(PAPER_BALANCE_PATH, None)
    if not isinstance(data, dict) or "balance" not in data:
        save_paper_balance(starting_balance)
        return float(starting_balance)
    return float(data["balance"])


def save_paper_balance(amount):
    _write_json(PAPER_BALANCE_PATH, {"balance": round(amount, 2)})


# ---------------------------------------------------------------------------
# Brokers
# ---------------------------------------------------------------------------


class SkipTrade(Exception):
    """The order was not placed for an expected reason (not an error)."""


class PositionGone(SkipTrade):
    """The shares behind a bot position are no longer in the account."""


def _floor_shares(quantity):
    """Round a share count down to Robinhood's 6-decimal fractional precision."""
    return math.floor(quantity * 1_000_000) / 1_000_000


class PaperBroker:
    mode = "paper"

    def __init__(self, cfg):
        self.starting_balance = cfg["paper_starting_balance"]

    def buy(self, symbol, dollars, price):
        balance = load_paper_balance(self.starting_balance)
        if balance < dollars:
            raise SkipTrade(f"paper balance ${balance:.2f} is below the ${dollars:.2f} trade size")
        quantity = _floor_shares(dollars / price)
        save_paper_balance(balance - quantity * price)
        return quantity, price

    def sell(self, symbol, quantity, price):
        balance = load_paper_balance(self.starting_balance)
        save_paper_balance(balance + quantity * price)
        return quantity, price


class RobinhoodBroker:
    """Dollar-sized fractional market orders. Fill prices are recorded as the
    quote at order time; Robinhood fills fractional orders moments later."""

    mode = "live"

    def __init__(self, cfg):
        status = json.loads(robinhood._ensure_login())
        if status.get("status") == "error":
            raise RuntimeError(f"Login check failed: {status.get('message')}")

    def _order(self, side, symbol, amount):
        result = json.loads(robinhood._execute_fractional_trade(side, symbol, amount))
        if result.get("status") == "error":
            raise RuntimeError(f"Robinhood {side} failed: {result.get('message')}")
        order = (result.get("data") or {}).get("order") or {}
        if isinstance(order, dict) and order.get("detail"):
            # robin_stocks returns Robinhood's rejection as a dict, not an exception
            raise RuntimeError(f"Robinhood rejected the {side}: {order['detail']}")
        print(f"Robinhood order response: {order}")
        return order

    def buy(self, symbol, dollars, price):
        order = self._order("buy", symbol, dollars)
        try:
            quantity = float(order.get("quantity"))
        except (TypeError, ValueError):
            quantity = _floor_shares(dollars / price)
        return quantity, price

    def sell(self, symbol, quantity, price):
        quantity = _floor_shares(min(quantity, robinhood.get_held_quantity(symbol)))
        if quantity <= 0:
            raise PositionGone(f"Robinhood shows no shares of {symbol}; "
                               f"clearing the bot's position record")
        self._order("sell", symbol, quantity)
        return quantity, price


def get_broker(cfg):
    return RobinhoodBroker(cfg) if cfg["mode"] == "live" else PaperBroker(cfg)


# ---------------------------------------------------------------------------
# Ledger + alerts
# ---------------------------------------------------------------------------


def _ensure_ledger_schema():
    """Move aside a ledger written by an older version of the bot."""
    if not os.path.exists(LEDGER_PATH):
        return
    with open(LEDGER_PATH, newline="") as f:
        header = next(csv.reader(f), None)
    if header != LEDGER_FIELDS:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        os.replace(LEDGER_PATH, os.path.join(ARCHIVE_DIR, f"trade_ledger.{stamp}.bak.csv"))


def log_trade(row):
    _ensure_ledger_schema()
    file_exists = os.path.exists(LEDGER_PATH)
    with open(LEDGER_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def read_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _notify_discord(row):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    mode = "LIVE ORDER" if row["mode"] == "live" else "PAPER TRADE"
    lines = [
        "📈 **MA CROSSOVER TRADE** 📉",
        f"**Action:** {row['action']} {row['quantity']} × {row['symbol']}",
        f"**Price:** ${float(row['price']):.2f}",
    ]
    if row["short_ma"] and row["long_ma"]:
        lines.append(f"**Short MA / Long MA:** {row['short_ma']} / {row['long_ma']}")
    lines.append(f"**Mode:** {mode} (via {row['source']})")
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps({"content": "\n".join(lines)}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Failed to push alert to Discord: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def execute_signal(cfg, action, symbol, price, source, short_ma=None, long_ma=None):
    """Place a BUY or SELL for `symbol` on the broker selected by cfg["mode"].

    Returns {"status": "filled", "trade": {...}} or {"status": "skipped",
    "reason": "..."}. Raises on broker/network errors so callers can retry.
    """
    action = action.upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"Unknown action {action!r}")

    with _lock:
        positions = load_positions()
        held = positions.get(symbol)

        if action == "BUY" and held:
            return {"status": "skipped", "reason": f"already holding {symbol}"}
        if action == "SELL" and not held:
            return {"status": "skipped", "reason": f"no bot position in {symbol} to sell"}
        if action == "SELL" and held.get("mode") != cfg["mode"]:
            return {
                "status": "skipped",
                "reason": f"{symbol} position was opened in {held.get('mode')} mode, "
                          f"bot is now in {cfg['mode']} mode",
            }

        broker = get_broker(cfg)
        try:
            if action == "BUY":
                qty, fill = broker.buy(symbol, cfg["trade_dollars"], price)
                positions[symbol] = {
                    "entry_price": fill,
                    "quantity": qty,
                    "invested": round(qty * fill, 2),
                    "timestamp": _now_iso(),
                    "mode": broker.mode,
                }
            else:
                qty, fill = broker.sell(symbol, float(held["quantity"]), price)
                del positions[symbol]
        except PositionGone as e:
            del positions[symbol]
            save_positions(positions)
            return {"status": "skipped", "reason": str(e)}
        except SkipTrade as e:
            return {"status": "skipped", "reason": str(e)}

        save_positions(positions)
        row = {
            "timestamp": _now_iso(),
            "symbol": symbol,
            "action": action,
            "quantity": round(qty, 6),
            "price": round(fill, 4),
            "short_ma": "" if short_ma is None else round(short_ma, 4),
            "long_ma": "" if long_ma is None else round(long_ma, 4),
            "mode": broker.mode,
            "source": source,
        }
        log_trade(row)

    print(f"[{broker.mode.upper()}] {action} "
          f"{qty:g} {symbol} @ ${fill:.2f} (via {source})")
    _notify_discord(row)
    return {"status": "filled", "trade": row}

