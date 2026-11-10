"""Candlestick pattern strategy.

Built from the patterns in "The BEST Candlestick Pattern Guide You'll EVER
FIND" (Data Trader): engulfing candles, pin bars, breakout candles and
shrinking candles, traded only where they matter -- at a support/resistance
level or a pullback to the trend -- which is the video's "advanced strategy":
a pattern on its own is noise, a pattern at a level is a trade.

Everything here is a pure function of a DataFrame of daily bars with columns
open/high/low/close, oldest first, so it can be tested without a network or
a broker. strategy.py fetches the bars and places the orders.

How a day is read, step by step:

  1. add_context() measures each candle (body, wicks) and the backdrop it
     formed against: the average candle size, the recent support (lowest low)
     and resistance (highest high), and the trend moving average.
  2. find_entry() looks for a BUY on the newest completed candle:
       - a bullish REVERSAL pattern (engulfing, hammer pin bar, or shrinking
         red candles followed by a green one) at support or on a pullback to
         the rising trend average, or
       - a bullish BREAKOUT candle closing above resistance in an uptrend.
     A BUY comes with a stop (the pattern's low) and a target
     (reward_risk times the risk above the entry).
  3. find_exit() decides when to SELL a position: the stop is hit, the target
     is hit, a bearish reversal pattern forms at resistance, or a bearish
     breakout candle closes below support.
"""

import pandas as pd

# A pin bar's long wick must be at least this many times its body, and take up
# at least this share of the whole candle.
PIN_WICK_TO_BODY = 2.0
PIN_WICK_SHARE = 0.6
# A breakout candle's body must be this many times the average body.
BREAKOUT_BODY_MULTIPLE = 1.5
# How many shrinking candles in a row signal that a move is running out of steam.
SHRINKING_RUN = 3
# "At a level" means within this many average candle ranges of it.
LEVEL_TOLERANCE = 1.0


# ---------------------------------------------------------------------------
# Step 1: measure the candles and their surroundings
# ---------------------------------------------------------------------------


def add_context(bars, level_window, trend_window):
    """Return a copy of `bars` with the columns the patterns need.

    The averages, support and resistance are shifted by one day, so a candle
    is always compared with the days BEFORE it -- a breakout candle can't be
    measured against a resistance level it created itself.
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
# Step 2: the patterns. Each takes candles (rows of the frame) and answers
# True/False, so each one can be read and tested on its own.
# ---------------------------------------------------------------------------


def bullish_engulfing(prev, cur):
    """A red candle swallowed by a bigger green one: buyers overpowered sellers."""
    return (bool(prev.red) and bool(cur.green)
            and cur.open <= prev.close and cur.close >= prev.open
            and cur.body > prev.body)


def bearish_engulfing(prev, cur):
    """A green candle swallowed by a bigger red one: sellers took over."""
    return (bool(prev.green) and bool(cur.red)
            and cur.open >= prev.close and cur.close <= prev.open
            and cur.body > prev.body)


def bullish_pin_bar(cur):
    """Hammer: a long lower wick. Price was pushed down and rejected."""
    return (cur.range > 0
            and cur.lower_wick >= PIN_WICK_TO_BODY * cur.body
            and cur.lower_wick >= PIN_WICK_SHARE * cur.range)


def bearish_pin_bar(cur):
    """Shooting star: a long upper wick. Price was pushed up and rejected."""
    return (cur.range > 0
            and cur.upper_wick >= PIN_WICK_TO_BODY * cur.body
            and cur.upper_wick >= PIN_WICK_SHARE * cur.range)


def bullish_breakout(cur):
    """A big green candle closing above resistance: buyers broke the level."""
    return (bool(cur.green)
            and cur.close > cur.resistance
            and cur.body >= BREAKOUT_BODY_MULTIPLE * cur.avg_body)


def bearish_breakout(cur):
    """A big red candle closing below support: sellers broke the level."""
    return (bool(cur.red)
            and cur.close < cur.support
            and cur.body >= BREAKOUT_BODY_MULTIPLE * cur.avg_body)


def _shrinking(candles, color):
    """True when every candle is `color` and each body is smaller than the last."""
    bodies = list(candles.body)
    return (all(candles[color])
            and all(later < earlier for earlier, later in zip(bodies, bodies[1:])))


def bullish_shrinking_reversal(frame):
    """Red candles getting smaller (selling is drying up), then a green candle
    that closes above the previous candle's high (buyers step in)."""
    if len(frame) < SHRINKING_RUN + 1:
        return False
    run, cur = frame.iloc[-SHRINKING_RUN - 1:-1], frame.iloc[-1]
    return _shrinking(run, "red") and bool(cur.green) and cur.close > run.high.iloc[-1]


def bearish_shrinking_reversal(frame):
    """Green candles getting smaller (buying is drying up), then a red candle
    that closes below the previous candle's low (sellers step in)."""
    if len(frame) < SHRINKING_RUN + 1:
        return False
    run, cur = frame.iloc[-SHRINKING_RUN - 1:-1], frame.iloc[-1]
    return _shrinking(run, "green") and bool(cur.red) and cur.close < run.low.iloc[-1]


# ---------------------------------------------------------------------------
# Step 3: where the pattern formed
# ---------------------------------------------------------------------------


def at_support(cur):
    return cur.low <= cur.support + LEVEL_TOLERANCE * cur.avg_range


def at_resistance(cur):
    return cur.high >= cur.resistance - LEVEL_TOLERANCE * cur.avg_range


def pullback_to_trend(cur):
    """In an uptrend (close above the trend average), the candle dipped to it."""
    return cur.close > cur.trend_ma and cur.low <= cur.trend_ma + LEVEL_TOLERANCE * cur.avg_range


# ---------------------------------------------------------------------------
# Step 4: decisions
# ---------------------------------------------------------------------------


def _ready(frame, rows):
    if len(frame) < rows:
        return False
    needed = ["avg_body", "avg_range", "support", "resistance", "trend_ma"]
    return not frame.iloc[-1][needed].isna().any()


def find_entry(frame, reward_risk):
    """Return a BUY plan for the newest candle, or None.

    The plan is {"action", "pattern", "level", "entry", "stop", "target", "reason"}.
    """
    if not _ready(frame, SHRINKING_RUN + 1):
        return None
    prev, cur = frame.iloc[-2], frame.iloc[-1]

    # Where is this candle? Reversal patterns only count at one of these.
    if at_support(cur):
        level = "support"
    elif pullback_to_trend(cur):
        level = "trend average"
    else:
        level = None

    pattern, stop = None, None
    if level and bullish_engulfing(prev, cur):
        pattern, stop = "bullish engulfing", min(prev.low, cur.low)
    elif level and bullish_pin_bar(cur):
        pattern, stop = "hammer pin bar", cur.low
    elif level and bullish_shrinking_reversal(frame):
        pattern, stop = "shrinking candles reversal", frame.low.iloc[-SHRINKING_RUN - 1:].min()
    elif bullish_breakout(cur) and cur.close > cur.trend_ma:
        pattern, level, stop = "breakout candle", "resistance", cur.low

    if pattern is None:
        return None
    entry = float(cur.close)
    risk = entry - float(stop)
    if risk <= 0:
        return None
    return {
        "action": "BUY",
        "pattern": pattern,
        "level": level,
        "entry": round(entry, 4),
        "stop": round(float(stop), 4),
        "target": round(entry + reward_risk * risk, 4),
        "reason": f"{pattern} at {level}",
    }


def find_exit(frame, plan=None):
    """Return a SELL decision for a held position, or None.

    `plan` is the stored BUY plan (stop/target). Without one -- e.g. a position
    opened by a TradingView alert -- only the bearish patterns can close it.
    """
    if not _ready(frame, SHRINKING_RUN + 1):
        return None
    prev, cur = frame.iloc[-2], frame.iloc[-1]

    if plan and plan.get("stop") is not None and cur.close <= plan["stop"]:
        reason = f"stop loss: closed at {cur.close:.2f}, stop {plan['stop']:.2f}"
    elif plan and plan.get("target") is not None and cur.close >= plan["target"]:
        reason = f"take profit: closed at {cur.close:.2f}, target {plan['target']:.2f}"
    elif at_resistance(cur) and bearish_engulfing(prev, cur):
        reason = "bearish engulfing at resistance"
    elif at_resistance(cur) and bearish_pin_bar(cur):
        reason = "shooting star pin bar at resistance"
    elif at_resistance(cur) and bearish_shrinking_reversal(frame):
        reason = "shrinking candles reversal at resistance"
    elif bearish_breakout(cur):
        reason = "breakdown candle below support"
    else:
        return None
    return {"action": "SELL", "pattern": reason.split(":")[0], "reason": reason}


def backtest(bars, level_window=20, trend_window=50, reward_risk=2.0):
    """Replay the rules one day at a time and list the trades they'd make.

    Useful to see how often the strategy fires on a symbol before trading it.
    Returns a list of {"date", "action", "price", "reason"} rows.
    """
    frame = add_context(bars, level_window, trend_window)
    trades, plan = [], None
    for i in range(len(frame)):
        upto = frame.iloc[: i + 1]
        decision = find_exit(upto, plan) if plan else find_entry(upto, reward_risk)
        if decision:
            trades.append({"date": frame.index[i], "action": decision["action"],
                           "price": float(upto.close.iloc[-1]), "reason": decision["reason"]})
            plan = decision if decision["action"] == "BUY" else None
    return trades
