"""Run with:  python -m unittest discover tests"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import candles  # noqa: E402
import config  # noqa: E402
import strategy  # noqa: E402


def bars(candles_ohlc, end=datetime.date(2026, 10, 23)):
    """Build a bar DataFrame from (open, high, low, close) tuples, oldest first."""
    days = pd.bdate_range(end=end, periods=len(candles_ohlc))
    return pd.DataFrame(candles_ohlc, index=[d.date() for d in days],
                        columns=["open", "high", "low", "close"], dtype=float)


def rows(*ohlc):
    """Measured candles (body, wicks...) without the rolling context."""
    return candles.add_context(bars(list(ohlc)), 1, 1)


# A quiet base: 30 small alternating candles between 99 and 101, so support is
# ~99, resistance ~101, the average body is 0.5 and the trend average ~100.
BASE = [(100.0, 100.8, 99.2, 100.5) if i % 2 else (100.5, 100.8, 99.2, 100.0) for i in range(30)]


def context(extra):
    return candles.add_context(bars(BASE + extra), 20, 20)


class PatternTests(unittest.TestCase):
    def test_bullish_engulfing(self):
        f = rows((101, 101.2, 99.8, 100), (99.8, 101.6, 99.6, 101.5))
        self.assertTrue(candles.bullish_engulfing(f.iloc[0], f.iloc[1]))
        self.assertFalse(candles.bearish_engulfing(f.iloc[0], f.iloc[1]))

    def test_engulfing_needs_the_whole_body_covered(self):
        f = rows((101, 101.2, 99.8, 100), (100.2, 100.9, 100.1, 100.8))
        self.assertFalse(candles.bullish_engulfing(f.iloc[0], f.iloc[1]))

    def test_bearish_engulfing(self):
        f = rows((100, 101.2, 99.8, 101), (101.2, 101.4, 99.5, 99.7))
        self.assertTrue(candles.bearish_engulfing(f.iloc[0], f.iloc[1]))

    def test_hammer_and_shooting_star(self):
        hammer = rows((100, 100.3, 97, 100.2)).iloc[0]
        star = rows((100, 103, 99.8, 99.9)).iloc[0]
        self.assertTrue(candles.bullish_pin_bar(hammer))
        self.assertFalse(candles.bearish_pin_bar(hammer))
        self.assertTrue(candles.bearish_pin_bar(star))
        self.assertFalse(candles.bullish_pin_bar(star))

    def test_big_body_is_not_a_pin_bar(self):
        self.assertFalse(candles.bullish_pin_bar(rows((100, 103, 99, 102.8)).iloc[0]))

    def test_shrinking_red_candles_then_green(self):
        f = rows((104, 104.1, 101, 101.2), (101.2, 101.3, 99.6, 99.8),
                 (99.8, 99.9, 99.0, 99.2), (99.2, 100.5, 99.1, 100.4))
        self.assertTrue(candles.bullish_shrinking_reversal(f))

    def test_growing_red_candles_are_not_shrinking(self):
        f = rows((101, 101.1, 100.4, 100.5), (100.5, 100.6, 99.4, 99.5),
                 (99.5, 99.6, 97.4, 97.5), (97.5, 100, 97.4, 99.9))
        self.assertFalse(candles.bullish_shrinking_reversal(f))

    def test_breakout_candle(self):
        f = context([(100.6, 103.2, 100.5, 103.0)])
        self.assertTrue(candles.bullish_breakout(f.iloc[-1]))
        small = context([(100.9, 101.3, 100.8, 101.2)])  # above resistance but tiny body
        self.assertFalse(candles.bullish_breakout(small.iloc[-1]))


class EntryExitTests(unittest.TestCase):
    def test_hammer_at_support_is_a_buy_with_stop_and_target(self):
        f = context([(99.5, 99.7, 97.0, 99.6)])
        plan = candles.find_entry(f, reward_risk=2.0)
        self.assertEqual((plan["action"], plan["pattern"], plan["level"]),
                         ("BUY", "hammer pin bar", "support"))
        self.assertEqual(plan["stop"], 97.0)
        self.assertAlmostEqual(plan["target"], 99.6 + 2 * (99.6 - 97.0))

    def test_pattern_away_from_any_level_is_ignored(self):
        # Climb well above the base, then a hammer in mid-air.
        climb = [(100 + 2 * i, 102.5 + 2 * i, 99.8 + 2 * i, 102 + 2 * i) for i in range(12)]
        f = candles.add_context(bars(BASE + climb + [(120, 120.2, 117, 120.1)]), 5, 40)
        self.assertTrue(candles.bullish_pin_bar(f.iloc[-1]))
        self.assertIsNone(candles.find_entry(f, 2.0))

    def test_breakout_in_uptrend_is_a_buy(self):
        plan = candles.find_entry(context([(100.6, 103.2, 100.5, 103.0)]), 2.0)
        self.assertEqual((plan["action"], plan["pattern"]), ("BUY", "breakout candle"))

    def test_quiet_day_is_no_signal(self):
        self.assertIsNone(candles.find_entry(context([(100.2, 100.6, 99.9, 100.4)]), 2.0))

    def test_stop_and_target_exits(self):
        plan = {"stop": 99.0, "target": 103.0}
        self.assertTrue(candles.find_exit(context([(100, 100.2, 98.5, 98.8)]), plan)["reason"]
                        .startswith("stop loss"))
        self.assertTrue(candles.find_exit(context([(100.6, 103.5, 100.5, 103.2)]), plan)["reason"]
                        .startswith("take profit"))
        self.assertIsNone(candles.find_exit(context([(100.2, 100.6, 99.9, 100.4)]), plan))

    def test_shooting_star_at_resistance_exits_without_a_plan(self):
        decision = candles.find_exit(context([(100.6, 103, 100.4, 100.5)]))
        self.assertEqual(decision["reason"], "shooting star pin bar at resistance")

    def test_not_enough_history(self):
        f = candles.add_context(bars(BASE[:5]), 20, 50)
        self.assertIsNone(candles.find_entry(f, 2.0))
        self.assertIsNone(candles.find_exit(f, {"stop": 1, "target": 2}))

    def test_backtest_pairs_buys_and_sells(self):
        trades = candles.backtest(bars(BASE + [(99.5, 99.7, 97.0, 99.6), (99.6, 100, 96, 96.5)]), 20, 20)
        self.assertEqual([t["action"] for t in trades], ["BUY", "SELL"])
        self.assertTrue(trades[1]["reason"].startswith("stop loss"))


class CandlestickCycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, rel in [("STATE_DIR", ""), ("STATUS_PATH", "status.json"),
                          ("SIGNAL_STATE_PATH", "signal_state.json"),
                          ("TRADE_PLANS_PATH", "trade_plans.json")]:
            patcher = mock.patch.object(strategy, name, os.path.join(tmp.name, rel))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.positions = {}
        patcher = mock.patch.object(strategy.brokers, "load_positions", lambda: self.positions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = config.validate({"symbols": ["HAM"], "short_window": 20, "long_window": 21,
                                    "lookback_days": 40})
        self.market_closed = mock.patch.object(strategy, "market_is_open", return_value=False)
        self.market_closed.start()
        self.addCleanup(self.market_closed.stop)

    def run_with(self, data, execute_result):
        with mock.patch.object(strategy, "fetch_daily_bars", lambda s, n: data), \
             mock.patch.object(strategy.brokers, "execute_signal", return_value=execute_result) as execute:
            rows = strategy.run_cycle(self.cfg)
        return rows, execute

    def test_buy_saves_plan_then_stop_sells(self):
        hammer = bars(BASE + [(99.5, 99.7, 97.0, 99.6)])
        rows, execute = self.run_with(hammer, {"status": "filled", "trade": {}})
        self.assertEqual(rows["HAM"]["signal"], "BUY")
        self.assertEqual(execute.call_args.args[1:3], ("BUY", "HAM"))
        self.assertEqual(execute.call_args.kwargs["note"], "hammer pin bar at support")

        # Same bar again: not traded twice.
        _, execute = self.run_with(hammer, {"status": "filled", "trade": {}})
        execute.assert_not_called()

        # Next day closes below the hammer's low while we hold it: stop loss.
        self.positions["HAM"] = {"quantity": 1, "mode": "paper"}
        crash = bars(BASE + [(99.5, 99.7, 97.0, 99.6), (99.6, 100, 96, 96.5)])
        rows, execute = self.run_with(crash, {"status": "filled", "trade": {}})
        self.assertEqual(execute.call_args.args[1:3], ("SELL", "HAM"))
        self.assertTrue(rows["HAM"]["pattern"].startswith("stop loss"))


if __name__ == "__main__":
    unittest.main()
