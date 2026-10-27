"""Bot settings, read from config.json.

config.json holds what the bot trades (symbols, moving-average periods, dollars
per trade, paper/live mode). Credentials never go here -- they live in .env
(see .env.example).
"""

import json
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

DEFAULTS = {
    "symbols": ["AAPL"],
    "short_window": 20,
    "long_window": 50,
    "trade_dollars": 100.0,
    "lookback_days": 100,
    "mode": "paper",
    "check_interval_minutes": 15,
    "paper_starting_balance": 1000.0,
}

MODES = ("paper", "live")
MAX_SYMBOLS = 25
# Robinhood's smallest fractional (dollar-based) order.
MIN_TRADE_DOLLARS = 1.0

# Fields the trader panel is allowed to change. "mode" is deliberately left
# out: switching to live (real-money) trading requires editing config.json by
# hand and restarting the bot.
EDITABLE_FIELDS = (
    "symbols",
    "short_window",
    "long_window",
    "trade_dollars",
    "lookback_days",
    "check_interval_minutes",
)

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _as_int(cfg, key, minimum):
    try:
        value = int(cfg[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a whole number")
    if value < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    return value


def _clean_symbols(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError("symbols must be a list of stock symbols")
    symbols = []
    for raw in value:
        symbol = str(raw).strip().upper()
        if not symbol:
            continue
        if not _SYMBOL_RE.match(symbol):
            raise ValueError(f"Invalid stock symbol: {raw!r}")
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError("symbols must list at least one stock")
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"symbols can list at most {MAX_SYMBOLS} stocks")
    return symbols


def validate(raw):
    """Return a normalised copy of the config, or raise ValueError."""
    raw = dict(raw)
    # Older configs traded a single "symbol" in whole shares.
    if "symbols" not in raw and "symbol" in raw:
        raw["symbols"] = [raw["symbol"]]
    raw.pop("symbol", None)
    raw.pop("trade_quantity", None)
    cfg = {**DEFAULTS, **raw}

    cfg["symbols"] = _clean_symbols(cfg["symbols"])

    cfg["short_window"] = _as_int(cfg, "short_window", 1)
    cfg["long_window"] = _as_int(cfg, "long_window", 2)
    if cfg["short_window"] >= cfg["long_window"]:
        raise ValueError("short_window must be smaller than long_window")

    # A crossover compares the two most recent days, so both days need a
    # fully-formed long moving average: that takes long_window + 1 bars.
    cfg["lookback_days"] = _as_int(cfg, "lookback_days", 2)
    if cfg["lookback_days"] < cfg["long_window"] + 1:
        raise ValueError(
            f"lookback_days must be at least long_window + 1 ({cfg['long_window'] + 1})"
        )

    try:
        cfg["trade_dollars"] = round(float(cfg["trade_dollars"]), 2)
    except (TypeError, ValueError):
        raise ValueError("trade_dollars must be a number")
    if cfg["trade_dollars"] < MIN_TRADE_DOLLARS:
        raise ValueError(f"trade_dollars must be at least ${MIN_TRADE_DOLLARS:.2f}")

    cfg["check_interval_minutes"] = _as_int(cfg, "check_interval_minutes", 1)

    mode = str(cfg["mode"]).strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cfg["mode"] = mode

    try:
        cfg["paper_starting_balance"] = float(cfg["paper_starting_balance"])
    except (TypeError, ValueError):
        raise ValueError("paper_starting_balance must be a number")

    return cfg


def _read_raw():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_config():
    return validate(_read_raw())


def update_config(updates):
    """Apply panel-editable fields, validate, and persist. Returns the new config."""
    raw = _read_raw()
    for key in EDITABLE_FIELDS:
        if key in updates:
            raw[key] = updates[key]
    cfg = validate(raw)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return cfg
