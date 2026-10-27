# StockJockey

A moving average crossover bot. It watches a list of stocks (AAPL by
default), and when a stock's 20-day simple moving average crosses its 50-day
between two consecutive days it places an order for that stock:

| Crossover | Meaning | Order |
|---|---|---|
| 20-day goes from at/below to **above** the 50-day | golden cross | BUY `trade_dollars` worth (fractional shares) |
| 20-day goes from at/above to **below** the 50-day | death cross | SELL the shares the bot bought |

Price history (the last `lookback_days` daily bars) comes from **yfinance**.
Orders go to **paper** (simulated) or **Robinhood** (live), and the
**trader panel** shows a **TradingView** chart with both moving averages.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your credentials
cd trader-panel && npm install
```

### `config.json`: what to trade

```json
{
  "symbols": ["AAPL"],
  "short_window": 20,
  "long_window": 50,
  "trade_dollars": 100.0,
  "lookback_days": 100,
  "mode": "paper",
  "check_interval_minutes": 15,
  "paper_starting_balance": 1000.0
}
```

- `symbols`: every stock the bot watches (up to 25). Each one gets the same
  crossover rules and is tracked separately, so the bot can hold several at
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
trigger a trade that reverses by 4 PM. Each crossover is acted on once.

The bot sells only shares it bought itself (tracked in `state/positions.json`).
Other AAPL shares you hold in Robinhood are never touched.

## TradingView

TradingView doesn't let outside programs place orders in its own Paper
Trading account, and Robinhood has no paper-trading API. So paper trading is
simulated by this bot, and TradingView is used two ways:

1. **Chart:** the panel embeds TradingView's chart with the same two SMAs.
2. **Alerts (optional):** `tradingview/sma_crossover.pine` is the same
   strategy in Pine Script. Add it to a daily chart to see and backtest the
   crossovers in TradingView's Strategy Tester. To have TradingView trigger
   trades, create an alert on the strategy with:
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

`GET /health` · `GET /status` (latest MA snapshot) · `GET|POST /config` ·
`GET /positions` · `GET /balance` · `GET /robinhood-balance` · `GET /trades` ·
`GET /pnl?period=1d|1w|1m|1y|ytd|all` · `POST /webhook/tradingview`

## Tests

```bash
python -m unittest discover tests
```

The old RSI strategy's trade history is kept in `archive/trade_ledger_rsi.csv`.
