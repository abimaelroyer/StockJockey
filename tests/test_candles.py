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


# A quiet base: 30 small alternating candles between 99.2 and 100.8, so
# support is ~99.2, resistance ~100.8, the average body is 0.5 and the trend
# average ~100.25.
BASE = [(100.0, 100.8, 99.2, 100.5) if i % 2 else (100.5, 100.8, 99.2, 100.0) for i in range(30)]


def context(extra, level_window=20, trend_window=20):
    return candles.add_context(bars(BASE + extra), level_window, trend_window)


class PatternTests(unittest.TestCase):
    """Each pattern returns the video's stop price, or None when it isn't there."""

    def test_engulfing(self):
        f = context([(100.6, 100.7, 100.0, 100.1), (99.9, 101.4, 99.5, 101.3)])
        self.assertEqual(candles.engulfing(f, "bull"), 99.5)  # low of the big candle
        self.assertIsNone(candles.engulfing(f, "bear"))

    def test_engulfing_needs_the_whole_body_covered(self):
        f = context([(100.6, 100.7, 100.0, 100.1), (100.2, 100.9, 100.1, 100.8)])
        self.assertIsNone(candles.engulfing(f, "bull"))

    def test_bearish_engulfing_stop_is_the_high(self):
        f = context([(100.1, 100.7, 100.0, 100.6), (100.8, 101.0, 99.5, 99.7)])
        self.assertEqual(candles.engulfing(f, "bear"), 101.0)

    def test_pin_bar_needs_a_confirming_candle(self):
        pin = (100.0, 100.3, 97.0, 100.2)  # green, long lower wick
        self.assertIsNone(candles.confirmed_pin_bar(context([pin]), "bull"))
        f = context([pin, (100.2, 100.9, 100.1, 100.8)])
        self.assertEqual(candles.confirmed_pin_bar(f, "bull"), 97.0)
        red_after = context([pin, (100.2, 100.3, 99.6, 99.7)])
        self.assertIsNone(candles.confirmed_pin_bar(red_after, "bull"))

    def test_three_bar_continuation(self):
        f = context([(100.0, 101.6, 99.9, 101.5), (101.5, 101.6, 100.9, 101.1),
                     (101.1, 102.8, 101.0, 102.7)])
        self.assertEqual(candles.three_bar_continuation(f, "bull"), 100.9)

    def test_continuation_pullback_too_deep(self):
        f = context([(100.0, 101.6, 99.9, 101.5), (101.5, 101.6, 100.2, 100.3),
                     (100.3, 102.0, 100.2, 101.9)])
        self.assertIsNone(candles.three_bar_continuation(f, "bull"))

    def test_three_bar_reversal(self):
        f = context([(101.0, 101.1, 99.4, 99.5), (99.5, 99.6, 98.8, 99.0),
                     (99.0, 100.7, 98.9, 100.6)])
        self.assertEqual(candles.three_bar_reversal(f, "bull"), 98.8)

    def test_weak_third_candle_is_not_a_reversal(self):
        f = context([(101.0, 101.1, 99.4, 99.5), (99.5, 99.6, 98.8, 99.0),
                     (99.0, 99.9, 98.9, 99.8)])
        self.assertIsNone(candles.three_bar_reversal(f, "bull"))

    def test_breakout_candle_stop_is_its_open(self):
        quiet = [(100.0, 100.3, 99.9, 100.2), (100.2, 100.4, 100.0, 100.1), (100.1, 100.3, 99.9, 100.2)]
        f = context(quiet + [(100.2, 101.6, 100.1, 101.5)])
        self.assertEqual(candles.breakout_candle(f, "bull"), 100.2)
        busy = context([(100.0, 101.3, 99.9, 101.2)] + quiet[1:] + [(100.2, 101.6, 100.1, 101.5)])
        self.assertIsNone(candles.breakout_candle(busy, "bull"))

    def test_shrinking_candles(self):
        f = context([(101.5, 101.6, 100.0, 100.1), (100.1, 100.2, 99.3, 99.4),
                     (99.4, 99.5, 99.0, 99.1), (99.1, 100.4, 99.0, 100.3)])
        self.assertEqual(candles.shrinking_candles(f, "bull"), 99.0)

    def test_growing_candles_are_not_shrinking(self):
        f = context([(100.5, 100.6, 100.2, 100.3), (100.3, 100.4, 99.7, 99.8),
                     (99.8, 99.9, 98.9, 99.0), (99.0, 100.4, 98.9, 100.3)])
        self.assertIsNone(candles.shrinking_candles(f, "bull"))


class EntryExitTests(unittest.TestCase):
    REVERSAL_AT_SUPPORT = [(101.0, 101.1, 99.4, 99.5), (99.5, 99.6, 98.8, 99.0),
                           (99.0, 100.7, 98.9, 100.6)]

    def test_reversal_at_support_is_a_buy_with_stop_and_2r_target(self):
        plan = candles.find_entry(context(self.REVERSAL_AT_SUPPORT), reward_risk=2.0)
        self.assertEqual((plan["action"], plan["pattern"], plan["level"]),
                         ("BUY", "three-bar reversal", "support"))
        self.assertEqual((plan["entry"], plan["stop"]), (100.6, 98.8))
        self.assertAlmostEqual(plan["target"], 100.6 + 2 * (100.6 - 98.8))

    def test_reversal_away_from_any_level_is_ignored(self):
        climb = [(100 + 2 * i, 102.5 + 2 * i, 99.8 + 2 * i, 102 + 2 * i) for i in range(12)]
        top = 124
        reversal = [(top + 1.0, top + 1.1, top - 1.6, top - 1.5), (top - 1.5, top - 1.4, top - 2.2, top - 2.0),
                    (top - 2.0, top + 1.2, top - 2.1, top + 1.1)]
        f = candles.add_context(bars(BASE + climb + reversal), 5, 40)
        self.assertIsNotNone(candles.three_bar_reversal(f, "bull"))
        self.assertFalse(candles.at_support(f) or candles.pullback_to_trend(f))
        plan = candles.find_entry(f, 2.0)
        self.assertTrue(plan is None or plan["level"] == "uptrend")

    def test_quiet_day_is_no_signal(self):
        self.assertIsNone(candles.find_entry(context([(100.2, 100.6, 99.9, 100.4)]), 2.0))

    def test_stop_and_target_from_the_live_price(self):
        f = context(self.REVERSAL_AT_SUPPORT)
        plan = {"stop": 98.8, "target": 104.2, "date": f.index[-1].isoformat()}
        self.assertIsNone(candles.find_exit(f, plan, latest_price=100.0))
        self.assertTrue(candles.find_exit(f, plan, latest_price=98.7)["reason"].startswith("stop loss"))
        self.assertTrue(candles.find_exit(f, plan, latest_price=104.3)["reason"].startswith("take profit"))

    def test_buy_day_candle_does_not_trigger_its_own_stop(self):
        f = context(self.REVERSAL_AT_SUPPORT)
        plan = {"stop": 98.9, "target": 104.2, "date": f.index[-1].isoformat()}  # low 98.9 == stop
        self.assertIsNone(candles.find_exit(f, plan))

    def test_later_candle_reaching_the_stop_sells_at_the_stop(self):
        f = context(self.REVERSAL_AT_SUPPORT + [(100.5, 100.6, 98.0, 98.2)])
        plan = {"stop": 98.8, "target": 104.2, "date": f.index[-2].isoformat()}
        decision = candles.find_exit(f, plan)
        self.assertTrue(decision["reason"].startswith("stop loss"))
        self.assertEqual(decision["price"], 98.8)

    def test_bearish_pattern_at_resistance_exits_without_a_plan(self):
        f = context([(100.1, 100.7, 100.0, 100.6), (100.8, 101.0, 99.5, 99.7)])
        self.assertEqual(candles.find_exit(f)["reason"], "bearish engulfing at resistance")

    def test_not_enough_history(self):
        f = candles.add_context(bars(BASE[:5]), 20, 50)
        self.assertIsNone(candles.find_entry(f, 2.0))
        self.assertIsNone(candles.find_exit(f, {"stop": 1, "target": 2}))

    def test_backtest_pairs_buys_and_sells(self):
        data = bars(BASE + self.REVERSAL_AT_SUPPORT + [(100.5, 100.6, 98.0, 98.2)])
        trades = candles.backtest(data, 20, 20)
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
        reversal = [(101.0, 101.1, 99.4, 99.5), (99.5, 99.6, 98.8, 99.0), (99.0, 100.7, 98.9, 100.6)]
        buy_day = bars(BASE + reversal)
        rows, execute = self.run_with(buy_day, {"status": "filled", "trade": {}})
        self.assertEqual(rows["HAM"]["signal"], "BUY")
        self.assertEqual(execute.call_args.args[1:3], ("BUY", "HAM"))
        self.assertEqual(execute.call_args.kwargs["note"], "bullish three-bar reversal at support")

        # Same bar again: not traded twice.
        _, execute = self.run_with(buy_day, {"status": "filled", "trade": {}})
        execute.assert_not_called()

        # Next day trades below the stop while we hold it: stop loss.
        self.positions["HAM"] = {"quantity": 1, "mode": "paper"}
        crash = bars(BASE + reversal + [(100.5, 100.6, 98.0, 98.2)])
        rows, execute = self.run_with(crash, {"status": "filled", "trade": {}})
        self.assertEqual(execute.call_args.args[1:3], ("SELL", "HAM"))
        self.assertTrue(rows["HAM"]["pattern"].startswith("stop loss"))


    def test_buy_skipped_when_price_already_below_stop(self):
        reversal = [(101.0, 101.1, 99.4, 99.5), (99.5, 99.6, 98.8, 99.0), (99.0, 100.7, 98.9, 100.6)]
        data = bars(BASE + reversal + [(98.0, 98.2, 97.5, 97.8)])  # today's live bar gapped down
        with mock.patch.object(strategy, "market_is_open", return_value=True), \
             mock.patch.object(strategy, "market_now",
                               return_value=datetime.datetime(2026, 10, 26, 11, 0, tzinfo=strategy.MARKET_TZ)):
            data.index = list(data.index[:-1]) + [datetime.date(2026, 10, 26)]
            rows, execute = self.run_with(data, {"status": "filled", "trade": {}})
        execute.assert_not_called()
        self.assertEqual(rows["HAM"]["last_crossover"]["result"], "skipped")


if __name__ == "__main__":
    unittest.main()
