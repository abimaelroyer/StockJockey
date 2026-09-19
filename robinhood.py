"""Secure wrapper for robin_stocks Robinhood trading toolkit.

Reads credentials from the following environment variables:
  - ROBINHOOD_USER
  - ROBINHOOD_PASS

Usage:
  python robinhood.py login
  python robinhood.py quote SPY
  python robinhood.py buy SPY 1
  python robinhood.py sell SPY 1
  python robinhood.py watchlist
  python robinhood.py positions

This script never logs credentials, auth tokens, or 2FA codes.
"""

import json
import os
import sys
from typing import Any, Dict


def _load_env_file():
    """Load variables from .env file into os.environ if .env exists."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key and val:
                            os.environ[key] = val
        except Exception:
            pass


_load_env_file()

try:
    import robin_stocks.robinhood as r
except ImportError:
    print(
        json.dumps(
            {
                "status": "error",
                "message": "robin_stocks is not installed. Run: pip install robin_stocks",
            }
        )
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Credential loader
# ---------------------------------------------------------------------------


def _load_credentials() -> Dict[str, str]:
    missing = []
    creds: Dict[str, str] = {}

    for key in ("ROBINHOOD_USER", "ROBINHOOD_PASS"):
        value = os.environ.get(key, "").strip()
        if not value:
            missing.append(key)
        else:
            creds[key] = value

    if missing:
        raise EnvironmentError(
            f"Required environment variable(s) not set: {', '.join(missing)}"
        )

    return creds


def cmd_balance(_args: list[str]) -> str:
    """Return total portfolio equity from Robinhood."""
    login_status = _ensure_login()
    if json.loads(login_status).get("status") == "error":
        return login_status

    try:
        profile = r.profiles.load_portfolio_profile()
        equity = profile.get("equity") or profile.get("extended_hours_equity") or "0"
        withdrawable = profile.get("withdrawable_amount", "0")
        return _encode_success({
            "equity": float(equity),
            "withdrawable": float(withdrawable)
        })
    except Exception as exc:
        return _encode_failure(f"Balance fetch failed: {exc}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_success(data: Any) -> str:
    return json.dumps({"status": "ok", "data": data})


def _encode_failure(message: str) -> str:
    # Guard against leaking secrets in error paths
    try:
        creds = _load_credentials()
        unsafe_tokens = (creds.get("ROBINHOOD_USER", ""), creds.get("ROBINHOOD_PASS", ""))
    except EnvironmentError:
        unsafe_tokens = ("", "")

    lowered = message.lower()
    for token in unsafe_tokens:
        if token and token.lower() in lowered:
            message = "[REDACTED: potential credential detected in error message]"
            break

    return json.dumps({"status": "error", "message": message})


# ---------------------------------------------------------------------------
# Robinstocks thin wrappers
# ---------------------------------------------------------------------------

_login_sentinel = "__LOGGED_IN__"


def init_session():
    user = os.environ.get("ROBINHOOD_USER")
    password = os.environ.get("ROBINHOOD_PASS")
    
    if not user or not password:
        raise ValueError("ROBINHOOD_USER or ROBINHOOD_PASS environment variables are not set.")

    # 1. Check if an active, valid session is already cached locally
    try:
        current_profile = r.profiles.load_account_profile()
        if current_profile and 'account_number' in current_profile:
            print("✅ Session active! Already securely authenticated to Robinhood.")
            return
    except Exception:
        print("🔄 No cached session found or token expired. Initializing fresh login handshake...")

    # 2. Fresh Native Login
    # By running this directly, robin_stocks will trigger the device approval push notification
    r.login(username=user, password=password, store_session=True)
    print("✅ Successfully logged in and cached new session token!")


def _ensure_login() -> str:
    """Log in with env credentials and return a JSON status string."""
    if getattr(_ensure_login, "_logged_in", False):
        return _encode_success({"message": "Already logged in."})

    try:
        init_session()
    except Exception as exc:
        return _encode_failure(f"Login failed: {type(exc).__name__}: {exc}")

    _ensure_login._logged_in = True  # type: ignore[attr-defined]
    return _encode_success({"message": "Login successful."})


def _logout() -> str:
    try:
        r.authentication.logout()
    except Exception:
        pass
    _ensure_login._logged_in = False  # type: ignore[attr-defined]
    return _encode_success({"message": "Logged out."})


def cmd_login(_args: list[str]) -> str:
    return _ensure_login()


def cmd_logout(_args: list[str]) -> str:
    return _logout()


def cmd_quote(args: list[str]) -> str:
    if not args:
        return _encode_failure("Usage: quote <SYMBOL>")
    symbol = args[0].upper().strip()
    if not symbol.isalpha() or not (1 <= len(symbol) <= 6):
        return _encode_failure("Invalid ticker symbol.")

    try:
        data = r.stocks.get_quotes([symbol])
    except Exception as exc:
        return _encode_failure(f"Quote lookup failed: {exc}")

    if not data or not isinstance(data, dict):
        return _encode_failure(f"No data returned for {symbol}.")

    # Surface only the essentials; avoid dumping internal metadata.
    summary = {
        "symbol": symbol,
        "ask_price": data.get("ask_price"),
        "bid_price": data.get("bid_price"),
        "last_trade_price": data.get("last_trade_price"),
        "previous_close": data.get("previous_close"),
    }
    return _encode_success(summary)


def _execute_trade(side: str, symbol: str, quantity: Any) -> str:
    login_status = _ensure_login()
    if json.loads(login_status).get("status") == "error":
        return login_status

    if side not in ("buy", "sell"):
        return _encode_failure("Side must be 'buy' or 'sell'.")

    if not symbol or not isinstance(symbol, str) or not symbol.isalpha():
        return _encode_failure("Invalid symbol.")

    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return _encode_failure("Quantity must be an integer.")

    if quantity < 1:
        return _encode_failure("Quantity must be at least 1.")

    try:
        result = r.orders.order_buy_market(symbol, quantity) if side == "buy" else r.orders.order_sell_market(symbol, quantity)
        # Narrow what we return so we don't surface internal tokens.
        if isinstance(result, dict):
            result.pop("oauth_token", None)
            result.pop("refresh_token", None)
            result.pop("access_token", None)
    except Exception as exc:
        return _encode_failure(f"Order failed: {type(exc).__name__}: {exc}")

    return _encode_success(
        {
            "side": side,
            "symbol": symbol.upper(),
            "quantity": quantity,
            "order": result,
        }
    )


def cmd_buy(args: list[str]) -> str:
    if len(args) < 2:
        return _encode_failure("Usage: buy <SYMBOL> <QUANTITY>")
    symbol, qty = args[0], args[1]
    return _execute_trade("buy", symbol, qty)


def cmd_sell(args: list[str]) -> str:
    if len(args) < 2:
        return _encode_failure("Usage: sell <SYMBOL> <QUANTITY>")
    symbol, qty = args[0], args[1]
    return _execute_trade("sell", symbol, qty)


def cmd_watchlist(_args: list[str]) -> str:
    """Show symbols on the default watchlist."""
    login_status = _ensure_login()
    if json.loads(login_status).get("status") == "error":
        return login_status

    try:
        items = r.account.get_watchlist_by_name("Default") or {}
        watchlist = items.get("results", [])
    except Exception as exc:
        return _encode_failure(f"Watchlist fetch failed: {exc}")

    symbols = [item.get("symbol") for item in watchlist if item.get("symbol")]
    return _encode_success({"symbols": symbols})


def cmd_positions(_args: list[str]) -> str:
    """Return open positions."""
    login_status = _ensure_login()
    if json.loads(login_status).get("status") == "error":
        return login_status

    try:
        positions = r.account.build_holdings()
    except Exception as exc:
        return _encode_failure(f"Positions fetch failed: {exc}")

    return _encode_success({"positions": positions})


def get_top_100_tickers() -> list[str]:
    """Retrieve the top 100 most popular symbols from Robinhood."""
    login_status_str = _ensure_login()
    try:
        login_status = json.loads(login_status_str)
    except Exception as e:
        raise RuntimeError(f"Login response JSON parsing failed: {e}")
    if login_status.get("status") == "error":
        raise RuntimeError(f"Login check failed: {login_status.get('message')}")
    try:
        tickers = r.get_top_100(info='symbol')
        if not tickers:
            return []
        return [ticker for ticker in tickers if ticker]
    except Exception as e:
        print(f"Error fetching top 100: {e}", file=sys.stderr)
        raise e


def get_historical_close_prices(symbol: str, interval: str = '5minute', span: str = 'day') -> list[float]:
    """Fetch historical close prices for a given symbol, including extended-hours."""
    login_status_str = _ensure_login()
    try:
        login_status = json.loads(login_status_str)
    except Exception as e:
        raise RuntimeError(f"Login response JSON parsing failed: {e}")
    if login_status.get("status") == "error":
        raise RuntimeError(f"Login check failed: {login_status.get('message')}")
    try:
        historical_data = r.stocks.get_stock_historicals(symbol, interval=interval, span=span, bounds='extended')
        if not historical_data:
            return []
        return [float(candle['close_price']) for candle in historical_data if candle.get('close_price') is not None]
    except Exception as e:
        print(f"API Quote lookup failed for {symbol}: {e}", file=sys.stderr)
        raise e


def cmd_top_100(_args: list[str]) -> str:
    tickers = get_top_100_tickers()
    return _encode_success(tickers)


def cmd_historical(args: list[str]) -> str:
    if not args:
        return _encode_failure("Usage: historical <SYMBOL> [interval] [span]")
    symbol = args[0].upper().strip()
    interval = args[1] if len(args) > 1 else '5minute'
    span = args[2] if len(args) > 2 else 'day'
    prices = get_historical_close_prices(symbol, interval=interval, span=span)
    return _encode_success(prices)


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "quote": cmd_quote,
    "buy": cmd_buy,
    "sell": cmd_sell,
    "watchlist": cmd_watchlist,
    "positions": cmd_positions,
    "top_100": cmd_top_100,
    "historical": cmd_historical,
    "balance": cmd_balance,
    "help": None,
}


def usage() -> str:
    return json.dumps(
        {
            "status": "ok",
            "commands": sorted(COMMANDS.keys()),
            "env_required": ["ROBINHOOD_USER", "ROBINHOOD_PASS"],
            "note": "All secrets are read from environment variables only.",
        }
    )


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(usage())
        return 0

    command = args[0].lower()
    rest = args[1:]

    if command == "help":
        print(usage())
        return 0

    handler = COMMANDS.get(command)
    if handler is None:
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": f"Unknown command: {command}. Run 'help' for available commands.",
                }
            )
        )
        return 1

    print(handler(rest))

    # Optional logout hook: uncomment if you want auth cleanup after each run.
    # cmd_logout([])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
