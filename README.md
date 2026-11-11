# StockJockey

A candlestick pattern trading bot. It watches a list of stocks on daily
candles and trades the patterns from Data Trader's video, only where they
form at a key price level.

Price history (the last `lookback_days` daily bars) comes from **yfinance**.
Orders go to **paper** (simulated) or **Robinhood** (live), and the
**trader panel** shows each symbol's levels, signal and stop/target next to a
**TradingView** chart.

### Candlestick strategy

Built from Data Trader's video *The BEST Candlestick Pattern Guide You'll
EVER FIND*: his six patterns, his stop for each one, and his "advanced
strategy" of only trading a pattern at a key level, with a take-profit twice
the size of the stop. The rules live in `candles.py`.

| # | Pattern (bullish version) | Stop |
|---|---|---|
| 1 | **Engulfing**: a red candle, then a bigger green candle whose body fully covers it | low of the green candle |
| 2 | **Pin bar**: green, small body, long lower wick, confirmed by the next candle closing green | low of the pin bar's wick |
| 3 | **Three-bar continuation**: large green, small red (at most half its body), large green closing above the red's close | low of the middle candle |
| 4 | **Three-bar reversal**: large full-bodied red, smaller red, full-bodied green at least as big as the first | low of the middle candle |
| 5 | **Breakout candle**: 3+ small consolidation candles, then a green candle 1.5× the average body | breakout candle's open |
| 6 | **Shrinking candles**: 3 red candles each smaller than the last, then a large green candle closing above the second one | low of the previous candle |

**When it buys** (when not already holding):
- A **reversal** pattern (1, 2, 4, 6) where one of the last 3 candles reached
  **support** (the lowest low of the last `level_window` days) or dipped to the
  rising **`trend_window`-day average** (a moving "dynamic" key level).
- A **continuation** pattern (3, 5) while price is above that average.

Entry is the pattern's closing price. The target is entry + `reward_risk` ×
(entry − stop), 2× by default as in the video.

**When it sells**: the live price, or a later day's high/low, reaches the
stop or the target; or a bearish reversal pattern forms at **resistance**
(the highest high of the last `level_window` days); or a bearish breakout
candle forms. "At a level" means within one average candle range of it.
Stops and targets are saved in `state/trade_plans.json`. Patterns are read
from completed daily bars only, and each signal is acted on once.

On two years of AAPL daily bars (Nov 2024 to Nov 2026) the rules made 25 round
trips, 12 winners, about +17% summed across trades, while simply holding
AAPL gained about 52%. Across all 10 default tickers it won 38% of 169
trades for about +0.3% per trade, with nearly all of the profit in early
2025, so it has not shown a reliable edge; try it in paper mode first.
`candles.backtest(bars)` replays the rules on any OHLC DataFrame.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your credentials
cd trader-panel && npm install
```

### `config.json`: what to trade

`config.json` is per-machine and git-ignored, as is everything in `state/`
(positions, paper balance, heartbeat) and `trade_ledger.csv`, so `git pull`
never overwrites a server's settings or trades. Until `config.json` exists the
bot reads `config.example.json`; the first save from the panel creates it, or
copy the template yourself (`cp config.example.json config.json`).

```json
{
  "symbols": ["AAPL"],
  "level_window": 20,
  "trend_window": 50,
  "trade_dollars": 100.0,
  "lookback_days": 100,
  "reward_risk": 2.0,
  "mode": "paper",
  "check_interval_minutes": 15,
  "paper_starting_balance": 1000.0
}
```

- `level_window`: how many days back support, resistance and the "average
  candle" look.
- `trend_window`: the days in the trend average the patterns use as an
  uptrend check and a dynamic key level.
- `reward_risk`: profit target as a multiple of the risk to the
  stop (2.0 = aim for twice what you risk).
- `symbols`: every stock the bot watches (up to 25). Each one gets the same
  rules and is tracked separately, so the bot can hold several at
  once. A symbol yfinance doesn't recognise shows an error in the panel
  without stopping the others.
- `trade_dollars`: how much each BUY spends, as fractional shares (minimum
  $1). One balance covers every symbol: with $1,000 and $100 per trade the bot
  can hold up to 10 positions; further BUYs are skipped until a SELL frees cash.
- `mode`: `"paper"` simulates fills against a local cash balance
  (`state/paper_balance.json`). `"live"` sends real fractional market orders to
  Robinhood (regular market hours only).
  You can only change this by editing the file and restarting the bot. The
  panel can't flip it.
- The panel's **Strategy Settings** form edits every other field.

### `.env`: credentials

| Key | Needed for |
|---|---|
| `ROBINHOOD_USER`, `ROBINHOOD_PASS` | live mode (optional in paper mode) |
| `ROBINHOOD_MFA_SECRET` | optional: authenticator-app secret, so the bot answers 2FA itself |
| `PANEL_API_KEY` | trader panel (must match `VITE_PANEL_KEY` in `trader-panel/.env`) |
| `TRADINGVIEW_WEBHOOK_SECRET` | TradingView alerts (optional) |
| `DISCORD_WEBHOOK_URL` | trade notifications (optional) |

## Run

```bash
python run_both.py            # strategy loop + API on :5000
cd trader-panel && npm run dev
```

### Hosted panel (DigitalOcean droplet)

The droplet serves the panel at **https://159-223-160-191.sslip.io** behind a
username/password. nginx proxies `/api/*` to the API, which listens on
127.0.0.1 only, and adds the API key itself, so the key isn't in the
browser bundle. `deploy/setup_droplet.sh` sets all of this up (re-runnable):

```bash
ssh root@159.223.160.191 'bash -s' -- 159.223.160.191 < deploy/setup_droplet.sh
```

- Add or change a login on the droplet: `htpasswd /etc/nginx/stockjockey.htpasswd <username>`
- Publish panel changes from this machine: `cd trader-panel && npm run deploy`
- Restart the bot on the droplet: `systemctl restart stockjockey-strategy stockjockey-api`

Because the API is no longer public, `npm run dev` only works against a
local API (`python api.py`), not the droplet.

The loop checks every `check_interval_minutes` while the market is open and
idles when it's closed. It acts only on **completed** daily bars. Today's
still-moving bar is ignored until the close, so a half-finished day can't
trigger a trade that reverses by 4 PM. Each signal is acted on once.

The bot sells only shares it bought itself (tracked in `state/positions.json`).
Other AAPL shares you hold in Robinhood are never touched.

## TradingView

TradingView doesn't let outside programs place orders in its own Paper
Trading account, and Robinhood has no paper-trading API. So paper trading is
simulated by this bot, and TradingView is used two ways:

1. **Chart:** the panel embeds TradingView's daily candle chart.
2. **Alerts (optional):** any TradingView strategy alert can place trades
   through the bot. Create the alert with:
   - **Webhook URL:** `https://159-223-160-191.sslip.io/api/webhook/tradingview`
     (needs a paid TradingView plan; this route skips the panel login and
     checks the secret instead)
   - **Message:**
     ```json
     {"secret": "<TRADINGVIEW_WEBHOOK_SECRET>", "symbol": "{{ticker}}", "action": "{{strategy.order.action}}", "price": {{close}}}
     ```
   Create one alert per symbol. The alert is executed by whichever broker
   `mode` selects. Only symbols in `symbols` are accepted, and a BUY while already holding (or a SELL
   with nothing held) is skipped. That makes it safe to run alerts alongside
   the bot's own loop.

## API

All endpoints except `/health` and `/webhook/tradingview` need the
`X-API-Key` header.

`GET /health` · `GET /status` (latest per-symbol levels and signals) · `GET|POST /config` ·
`GET /positions` · `GET /balance` · `GET /robinhood-balance` · `GET /trades` ·
`GET /pnl?period=1d|1w|1m|1y|ytd|all` · `POST /webhook/tradingview`

## Tests

```bash
python -m unittest discover tests
```

The old RSI strategy's trade history is kept in `archive/trade_ledger_rsi.csv`.
