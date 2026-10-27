import os
import json
import hmac
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()

import brokers
import robinhood
from config import load_config, update_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
MARKET_TZ = ZoneInfo("America/New_York")

app = Flask(__name__)
CORS(app)
API_KEY = os.environ.get("PANEL_API_KEY")

# TradingView can't send custom headers, so its webhook authenticates with a
# shared secret in the JSON body instead of X-API-Key.
PUBLIC_PATHS = ("/health", "/webhook/tradingview")


@app.before_request
def check_auth():
    if request.method == "OPTIONS" or request.path in PUBLIC_PATHS:
        return
    if not API_KEY or request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401


def _read_state(name, default):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


@app.route("/health")
def health():
    try:
        beat = _read_state("heartbeat.json", None)
        if beat is None:
            return jsonify({"status": "no_heartbeat"}), 503
        last = datetime.fromisoformat(beat["time"])
        age_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
        # The loop sleeps up to 15 min while the market is closed, or
        # check_interval_minutes while it is open.
        stale_after = max(20, load_config()["check_interval_minutes"] * 2 + 5)
        return jsonify({
            "status": "stale" if age_minutes > stale_after else beat.get("status", "ok"),
            "last_cycle": beat["time"],
            "minutes_since": round(age_minutes, 1),
            "detail": beat.get("detail"),
        })
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/status")
def status():
    """Latest moving-average snapshot written by the strategy loop."""
    return jsonify(_read_state("status.json", {}))


@app.route("/positions")
def positions():
    return jsonify(brokers.load_positions())


@app.route("/balance")
def balance():
    return jsonify({"balance": brokers.load_paper_balance(load_config()["paper_starting_balance"])})


@app.route("/robinhood-balance")
def robinhood_balance():
    # Only report when the bot already holds a session -- never start a login
    # (and a 2FA prompt) from a panel refresh.
    if not robinhood.is_logged_in():
        return jsonify({"equity": None, "detail": "Robinhood not connected"})
    result = json.loads(robinhood.cmd_balance([]))
    if result.get("status") == "error":
        return jsonify({"equity": None, "detail": result.get("message")})
    return jsonify(result["data"])


@app.route("/trades")
def trades():
    return jsonify(brokers.read_ledger())


@app.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        try:
            return jsonify(update_config(request.get_json(silent=True) or {}))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    return jsonify(load_config())


def _period_start(period):
    now = datetime.now(MARKET_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "1d": midnight,
        "1w": now - timedelta(days=7),
        "1m": now - timedelta(days=30),
        "1y": now - timedelta(days=365),
        "ytd": midnight.replace(month=1, day=1),
    }.get(period)


def _parse_ts(ts):
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@app.route("/pnl")
def pnl():
    """Realised P&L from the ledger, pairing each SELL with its BUY (FIFO)."""
    period = request.args.get("period", "all")
    start = _period_start(period)
    today = datetime.now(MARKET_TZ).date()

    rows = brokers.read_ledger()
    open_lots = defaultdict(deque)
    stats = {m: {"pnl": 0.0, "wins": 0, "losses": 0} for m in ("paper", "live")}
    trades_in_period = 0
    today_counts = {"paper": 0, "live": 0}

    for row in rows:
        ts = _parse_ts(row["timestamp"])
        mode = row["mode"] if row["mode"] in stats else "paper"
        qty = float(row["quantity"])
        price = float(row["price"])
        in_period = start is None or ts >= start
        trades_in_period += in_period
        if ts.astimezone(MARKET_TZ).date() == today:
            today_counts[mode] += 1

        lots = open_lots[(row["symbol"], mode)]
        if row["action"] == "BUY":
            lots.append([qty, price])
            continue

        realised = 0.0
        remaining = qty
        while remaining > 0 and lots:
            lot = lots[0]
            used = min(remaining, lot[0])
            realised += used * (price - lot[1])
            lot[0] -= used
            remaining -= used
            if lot[0] <= 0:
                lots.popleft()
        if in_period:
            stats[mode]["pnl"] += realised
            stats[mode]["wins" if realised > 0 else "losses"] += 1

    for s in stats.values():
        closed = s["wins"] + s["losses"]
        s["pnl"] = round(s["pnl"], 2)
        s["win_rate"] = round(100 * s["wins"] / closed, 1) if closed else 0.0

    return jsonify({
        "period": period,
        "trades_in_period": trades_in_period,
        "paper_today": today_counts["paper"],
        "live_today": today_counts["live"],
        "modes": stats,
    })


@app.route("/webhook/tradingview", methods=["POST"])
def tradingview_webhook():
    """Execute a TradingView alert through the configured broker.

    Alert message (JSON):
      {"secret": "<TRADINGVIEW_WEBHOOK_SECRET>", "symbol": "{{ticker}}",
       "action": "{{strategy.order.action}}", "price": {{close}}}
    """
    secret = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "")
    payload = request.get_json(force=True, silent=True) or {}
    if not secret or not hmac.compare_digest(str(payload.get("secret", "")), secret):
        return jsonify({"error": "unauthorized"}), 401

    cfg = load_config()
    action = str(payload.get("action", "")).strip().upper()
    if action not in ("BUY", "SELL"):
        return jsonify({"error": "action must be buy or sell"}), 400

    # TradingView may send "NASDAQ:AAPL"; only configured symbols are traded.
    symbol = str(payload.get("symbol") or "").split(":")[-1].strip().upper()
    if not symbol and len(cfg["symbols"]) == 1:
        symbol = cfg["symbols"][0]
    if symbol not in cfg["symbols"]:
        return jsonify({"error": f"{symbol or 'symbol'} is not in the bot's symbols "
                                 f"({', '.join(cfg['symbols'])})"}), 400

    try:
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        price = None
    if not price or price <= 0:
        price = float(yf.Ticker(symbol).fast_info["last_price"])

    try:
        result = brokers.execute_signal(cfg, action, symbol, price, source="tradingview")
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
