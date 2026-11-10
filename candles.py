"""Candlestick pattern strategy.

Built from Data Trader's video "The BEST Candlestick Pattern Guide You'll EVER
FIND". It uses his six patterns, his stop for each one, and his "advanced
strategy": only take a pattern where it lines up with a key level, and aim for
a profit target twice the size of the stop.

Everything here is a pure function of a DataFrame of daily bars with columns
open/high/low/close, oldest first, so it can be tested without a network or a
broker. strategy.py fetches the bars and places the orders.

How a day is read, step by step:

  1. add_context() measures each candle (body, wicks) and what it formed
     against: the average candle size, recent support (lowest low) and
     resistance (highest high), and the trend moving average.
  2. Each pattern function looks at the newest candles and returns the STOP
     price the video gives for that pattern, or None if the pattern isn't there.
     Every pattern takes a `side`: "bull" (buyers taking over) or "bear"
     (sellers taking over), so one function covers both versions.
  3. find_entry() takes a BUY when:
       - a REVERSAL pattern (three-bar reversal, engulfing, shrinking candles,
         pin bar) forms at support or on a pullback to the rising trend
         average, or
       - a CONTINUATION pattern (three-bar continuation, breakout candle) forms
         while price is above the trend average.
     Entry is the pattern's close, the target is reward_risk x the risk.
  4. find_exit() sells when price reaches the stop or the target, or a bearish
     reversal forms at resistance, or a bearish breakout candle forms.
"""

import pandas as pd

# "Large" candle = body bigger than the average body of the last few weeks.
LARGE_BODY = 1.0
# Breakout candle = body at least this many times the average.
BREAKOUT_BODY = 1.5
# "Nearly full body" = the body is at least this share of the whole candle.
FULL_BODY_SHARE = 0.6
# Pin bar = the long wick is at least 2x the body and most of the candle.
PIN_WICK_TO_BODY = 2.0
PIN_WICK_SHARE = 0.6
# Three-bar continuation: the middle candle is at most half the first one.
CONTINUATION_PULLBACK = 0.5
# Breakout needs at least this many small "consolidation" candles before it.
CONSOLIDATION_CANDLES = 3
# Shrinking candles needs at least this many candles getting smaller.
SHRINKING_RUN = 3
# "At a level" = within this many average candle ranges of it.
LEVEL_TOLERANCE = 1.0


# ---------------------------------------------------------------------------
# Step 1: measure the candles and their surroundings
# ---------------------------------------------------------------------------


def add_context(bars, level_window, trend_window):
    """Return a copy of `bars` with the columns the patterns need.

    The averages, support and resistance are shifted by one day, so a candle
    is always compared with the days BEFORE it.
    """
    f = bars[["open", "high", "low", "close"]].astype(float).copy()
    f["body"] = (f.close - f.open).abs()
    f["range"] = f.high - f.low
    f["upper_wick"] = f.high - f[["open", "close"]].max(axis=1)
    f["lower_wick"] = f[["open", "close"]].min(axis=1) - f.low
    f["green"] = f.close > f.open
    f["red"] = f.close < f.open

    f["avg_body"] = f.body.rolling(level_window).mean().shift(1)
    f["avg_range"] = f.range.rolling(level_window).mean().shift(1)
    f["support"] = f.low.rolling(level_window).min().shift(1)
    f["resistance"] = f.high.rolling(level_window).max().shift(1)
    f["trend_ma"] = f.close.rolling(trend_window).mean()
    return f


# ---------------------------------------------------------------------------
# Small helpers so each pattern reads like the video describes it
# ---------------------------------------------------------------------------


def _with(c, side):
    """Is candle `c` the color of `side`? (green for bull, red for bear)"""
    return bool(c.green) if side == "bull" else bool(c.red)


def _against(c, side):
    """Is candle `c` the opposite color?"""
    return bool(c.red) if side == "bull" else bool(c.green)


def _beyond(price, than, side):
    """Is `price` past `than` in the direction of `side`? (above for bull)"""
    return price > than if side == "bull" else price < than


def _stop_end(c, side):
    """The wick end a stop goes behind: the low for a buy, the high for a sell."""
    return c.low if side == "bull" else c.high


def _large(c):
    return c.body > LARGE_BODY * c.avg_body


def _full(c):
    return c.range > 0 and c.body >= FULL_BODY_SHARE * c.range


def _last(frame, n):
    return [frame.iloc[i] for i in range(-n, 0)] if len(frame) >= n else None


# ---------------------------------------------------------------------------
# Step 2: the six patterns. Each returns the stop price, or None.
# ---------------------------------------------------------------------------


def engulfing(frame, side):
    """#1 Engulfing: a small candle, then a bigger candle of the other color
    whose body fully covers the first body. Stop: the big candle's wick end."""
    candles = _last(frame, 2)
    if not candles:
        return None
    first, second = candles
    covers = (max(second.open, second.close) >= max(first.open, first.close)
              and min(second.open, second.close) <= min(first.open, first.close))
    if _against(first, side) and _with(second, side) and covers and second.body > first.body:
        return _stop_end(second, side)
    return None


def _pin_bar(c, side):
    wick = c.lower_wick if side == "bull" else c.upper_wick
    return (_with(c, side) and c.range > 0
            and wick >= PIN_WICK_TO_BODY * c.body
            and wick >= PIN_WICK_SHARE * c.range)


def confirmed_pin_bar(frame, side):
    """#2 Pin bar: a small body with a long wick on one side (green with a long
    lower wick for bull). The video waits for one more candle of the same color
    to confirm. Stop: the end of the pin bar's wick."""
    candles = _last(frame, 2)
    if not candles:
        return None
    pin, confirm = candles
    if _pin_bar(pin, side) and _with(confirm, side):
        return _stop_end(pin, side)
    return None


def three_bar_continuation(frame, side):
    """#3 Three-bar continuation: a large candle, a small opposite candle at
    most half its body, then another large candle closing past the second
    one's close. Stop: the second candle's wick end."""
    candles = _last(frame, 3)
    if not candles:
        return None
    c1, c2, c3 = candles
    if (_with(c1, side) and _large(c1)
            and _against(c2, side) and c2.body <= CONTINUATION_PULLBACK * c1.body
            and _with(c3, side) and _large(c3) and _beyond(c3.close, c2.close, side)):
        return _stop_end(c2, side)
    return None


def three_bar_reversal(frame, side):
    """#4 Three-bar reversal: a large, nearly full-bodied candle, a smaller
    candle of the same color, then a full-bodied candle of the other color at
    least as big as the first (the video's "strong" version). Stop: the second
    candle's wick end."""
    candles = _last(frame, 3)
    if not candles:
        return None
    c1, c2, c3 = candles
    if (_against(c1, side) and _large(c1) and _full(c1)
            and _against(c2, side) and c2.body < c1.body
            and _with(c3, side) and _full(c3) and c3.body >= c1.body):
        return _stop_end(c2, side)
    return None


def breakout_candle(frame, side):
    """#5 Breakout: at least three small "consolidation" candles (any color),
    then one large candle. Stop: the breakout candle's open."""
    candles = _last(frame, CONSOLIDATION_CANDLES + 1)
    if not candles:
        return None
    *quiet, breakout = candles
    if (all(c.body < breakout.avg_body for c in quiet)
            and _with(breakout, side) and breakout.body >= BREAKOUT_BODY * breakout.avg_body):
        return breakout.open
    return None


def shrinking_candles(frame, side):
    """#6 Shrinking candles: three candles of one color each smaller than the
    last (the move is running out of steam), then a large candle of the other
    color closing past the second shrinking candle. Stop: the previous
    candle's wick end."""
    candles = _last(frame, SHRINKING_RUN + 1)
    if not candles:
        return None
    *run, turn = candles
    bodies = [c.body for c in run]
    shrinking = (all(_against(c, side) for c in run)
                 and all(later < earlier for earlier, later in zip(bodies, bodies[1:])))
    past_second = _beyond(turn.close, run[1].high if side == "bull" else run[1].low, side)
    if shrinking and _with(turn, side) and _large(turn) and past_second:
        return _stop_end(run[-1], side)
    return None


REVERSAL_PATTERNS = [
    ("three-bar reversal", three_bar_reversal),
    ("engulfing", engulfing),
    ("shrinking candles", shrinking_candles),
    ("pin bar", confirmed_pin_bar),
]
CONTINUATION_PATTERNS = [
    ("three-bar continuation", three_bar_continuation),
    ("breakout candle", breakout_candle),
]


# ---------------------------------------------------------------------------
# Step 3: key levels (the video's "advanced strategy")
# ---------------------------------------------------------------------------


def at_support(frame):
    """Did any of the last 3 candles reach the recent support?"""
    cur = frame.iloc[-1]
    return frame.low.iloc[-3:].min() <= cur.support + LEVEL_TOLERANCE * cur.avg_range


def at_resistance(frame):
    cur = frame.iloc[-1]
    return frame.high.iloc[-3:].max() >= cur.resistance - LEVEL_TOLERANCE * cur.avg_range


def pullback_to_trend(frame):
    """Uptrend (close above the trend average), and price dipped to it: the
    average acts as a moving ("dynamic") support level."""
    cur = frame.iloc[-1]
    return (cur.close > cur.trend_ma
            and frame.low.iloc[-3:].min() <= cur.trend_ma + LEVEL_TOLERANCE * cur.avg_range)


def uptrend(frame):
    cur = frame.iloc[-1]
    return cur.close > cur.trend_ma


# ---------------------------------------------------------------------------
# Step 4: decisions
# ---------------------------------------------------------------------------


def _ready(frame):
    if len(frame) < SHRINKING_RUN + 1:
        return False
    needed = ["avg_body", "avg_range", "support", "resistance", "trend_ma"]
    return not frame.iloc[-1][needed].isna().any()


def find_entry(frame, reward_risk):
    """Return a BUY plan for the newest candle, or None.

    The plan is {"action", "pattern", "level", "entry", "stop", "target",
    "reason", "price"}.
    """
    if not _ready(frame):
        return None

    candidates = []
    if at_support(frame):
        candidates += [(name, fn, "support") for name, fn in REVERSAL_PATTERNS]
    elif pullback_to_trend(frame):
        candidates += [(name, fn, "trend average") for name, fn in REVERSAL_PATTERNS]
    if uptrend(frame):
        candidates += [(name, fn, "uptrend") for name, fn in CONTINUATION_PATTERNS]

    entry = float(frame.close.iloc[-1])
    for name, pattern, level in candidates:
        stop = pattern(frame, "bull")
        if stop is None or entry - stop <= 0:
            continue
        risk = entry - float(stop)
        return {
            "action": "BUY",
            "pattern": name,
            "level": level,
            "entry": round(entry, 4),
            "stop": round(float(stop), 4),
            "target": round(entry + reward_risk * risk, 4),
            "reason": f"bullish {name} at {level}" if level != "uptrend" else f"bullish {name} in uptrend",
            "price": entry,
        }
    return None


def find_exit(frame, plan=None, latest_price=None):
    """Return a SELL decision for a held position, or None.

    `plan` is the saved BUY plan (stop, target, and the date it was bought).
    The stop and target count as hit when:
      - a completed candle AFTER the buy day reached them, or
      - `latest_price` (the live quote during the day) has reached them.
    Without a plan (a position opened by a TradingView alert) only the bearish
    patterns can close it.
    """
    if not _ready(frame):
        return None
    cur = frame.iloc[-1]
    stop = plan.get("stop") if plan else None
    target = plan.get("target") if plan else None
    after_buy = plan and frame.index[-1].isoformat() > str(plan.get("date", ""))

    def sell(reason, price):
        return {"action": "SELL", "pattern": reason.split(":")[0], "reason": reason,
                "price": float(price)}

    if stop is not None:
        if latest_price is not None and latest_price <= stop:
            return sell(f"stop loss: price {latest_price:.2f} reached stop {stop:.2f}", latest_price)
        if after_buy and cur.low <= stop:
            return sell(f"stop loss: low {cur.low:.2f} reached stop {stop:.2f}", min(cur.open, stop))
    if target is not None:
        if latest_price is not None and latest_price >= target:
            return sell(f"take profit: price {latest_price:.2f} reached target {target:.2f}", latest_price)
        if after_buy and cur.high >= target:
            return sell(f"take profit: high {cur.high:.2f} reached target {target:.2f}", max(cur.open, target))

    if at_resistance(frame):
        for name, pattern in REVERSAL_PATTERNS:
            if pattern(frame, "bear") is not None:
                return sell(f"bearish {name} at resistance", cur.close)
    if breakout_candle(frame, "bear") is not None:
        return sell("bearish breakout candle", cur.close)
    return None


def backtest(bars, level_window=20, trend_window=50, reward_risk=2.0):
    """Replay the rules one day at a time and list the trades they'd make.

    Returns a list of {"date", "action", "price", "reason"} rows. Stops and
    targets fill at their own price (or the open, if price gapped past them).
    """
    frame = add_context(bars, level_window, trend_window)
    trades, plan = [], None
    for i in range(len(frame)):
        upto = frame.iloc[: i + 1]
        decision = find_exit(upto, plan) if plan else find_entry(upto, reward_risk)
        if decision:
            date = frame.index[i]
            trades.append({"date": date, "action": decision["action"],
                           "price": decision["price"], "reason": decision["reason"]})
            plan = dict(decision, date=date.isoformat()) if decision["action"] == "BUY" else None
    return trades
