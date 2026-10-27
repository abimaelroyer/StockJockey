"""Run with:  python -m unittest discover tests"""

import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brokers  # noqa: E402
import config  # noqa: E402
import strategy  # noqa: E402

ET = strategy.MARKET_TZ


def series(values, end=datetime.date(2026, 10, 23)):
    days = pd.bdate_range(end=end, periods=len(values))
    return pd.Series(values, index=[d.date() for d in days], dtype=float)


class DetectCrossoverTests(unittest.TestCase):
    def frame(self, values, short=2, long=4):
        return strategy.compute_moving_averages(series(values), short, long)

    def test_golden_cross_is_buy(self):
        # short MA goes from below the long MA to above it on the last day
        self.assertEqual(strategy.detect_crossover(self.frame([10, 10, 10, 9, 9, 13])), "BUY")

    def test_death_cross_is_sell(self):
        self.assertEqual(strategy.detect_crossover(self.frame([10, 10, 10, 11, 11, 7])), "SELL")

    def test_no_cross_while_trend_continues(self):
        self.assertIsNone(strategy.detect_crossover(self.frame([1, 2, 3, 4, 5, 6, 7])))
        self.assertIsNone(strategy.detect_crossover(self.frame([7, 6, 5, 4, 3, 2, 1])))

    def test_cross_must_be_on_last_two_days(self):
        # crossed up a day earlier, still above today -> no new signal
        self.assertIsNone(strategy.detect_crossover(self.frame([10, 10, 10, 9, 9, 13, 14])))

    def test_touch_then_move_counts_as_cross(self):
        # equal yesterday, above today
        self.assertEqual(strategy.detect_crossover(self.frame([10, 10, 10, 10, 10, 12])), "BUY")

    def test_not_enough_data(self):
        self.assertIsNone(strategy.detect_crossover(self.frame([10, 11, 12, 13])))

    def test_20_50_on_realistic_series(self):
        # 70 days falling then 30 days rising: one golden cross somewhere in the rise
        values = [200 - i for i in range(70)] + [130 + 3 * i for i in range(30)]
        frame = strategy.compute_moving_averages(series(values), 20, 50)
        signals = [
            strategy.detect_crossover(frame.iloc[: i + 1]) for i in range(len(frame))
        ]
        self.assertEqual([s for s in signals if s], ["BUY"])


class CompletedClosesTests(unittest.TestCase):
    def test_drops_todays_bar_while_market_open(self):
        closes = series([1, 2, 3], end=datetime.date(2026, 10, 27))
        now = datetime.datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        self.assertEqual(list(strategy.completed_closes(closes, 100, now)), [1, 2])

    def test_keeps_todays_bar_after_close(self):
        closes = series([1, 2, 3], end=datetime.date(2026, 10, 27))
        now = datetime.datetime(2026, 10, 27, 16, 30, tzinfo=ET)
        self.assertEqual(list(strategy.completed_closes(closes, 100, now)), [1, 2, 3])

    def test_trims_to_lookback(self):
        closes = series(range(150))
        now = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=ET)
        self.assertEqual(len(strategy.completed_closes(closes, 100, now)), 100)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config.validate({})
        self.assertEqual((cfg["symbols"], cfg["short_window"], cfg["long_window"], cfg["trade_dollars"]),
                         (["AAPL"], 20, 50, 100.0))

    def test_symbols_are_cleaned(self):
        cfg = config.validate({"symbols": ["aapl", " msft ", "AAPL", ""]})
        self.assertEqual(cfg["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(config.validate({"symbols": "spy, qqq"})["symbols"], ["SPY", "QQQ"])

    def test_old_single_symbol_config_migrates(self):
        cfg = config.validate({"symbol": "msft", "trade_quantity": 1})
        self.assertEqual(cfg["symbols"], ["MSFT"])
        self.assertNotIn("symbol", cfg)
        self.assertNotIn("trade_quantity", cfg)

    def test_rejects_bad_values(self):
        for bad in ({"short_window": 50, "long_window": 20},
                    {"long_window": 120, "lookback_days": 100},
                    {"mode": "yolo"},
                    {"symbols": []},
                    {"symbols": ["AAPL", "NOT A TICKER"]},
                    {"trade_dollars": 0.5}):
            with self.assertRaises(ValueError, msg=bad):
                config.validate(bad)


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, rel in [
            ("POSITIONS_PATH", "state/positions.json"),
            ("PAPER_BALANCE_PATH", "state/paper_balance.json"),
            ("LEDGER_PATH", "trade_ledger.csv"),
            ("ARCHIVE_DIR", "archive"),
        ]:
            patcher = mock.patch.object(brokers, name, os.path.join(tmp.name, rel))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = config.validate({"paper_starting_balance": 1000})

    def test_buy_then_sell_round_trip(self):
        buy = brokers.execute_signal(self.cfg, "BUY", "AAPL", 250.0, "bot", 251.0, 249.0)
        self.assertEqual(buy["status"], "filled")
        self.assertEqual(brokers.load_positions()["AAPL"]["quantity"], 0.4)  # $100 / $250
        self.assertAlmostEqual(brokers.load_paper_balance(1000), 900.0)

        again = brokers.execute_signal(self.cfg, "BUY", "AAPL", 255.0, "tradingview")
        self.assertEqual(again["status"], "skipped")

        sell = brokers.execute_signal(self.cfg, "SELL", "AAPL", 260.0, "bot")
        self.assertEqual(sell["status"], "filled")
        self.assertAlmostEqual(brokers.load_paper_balance(1000), 1004.0)
        self.assertEqual(brokers.load_positions(), {})
        self.assertEqual([r["action"] for r in brokers.read_ledger()], ["BUY", "SELL"])

    def test_fractional_quantity_rounds_down(self):
        brokers.execute_signal(self.cfg, "BUY", "AAPL", 347.0, "bot")
        qty = brokers.load_positions()["AAPL"]["quantity"]
        self.assertEqual(qty, 0.288184)
        self.assertLessEqual(qty * 347.0, 100.0)

    def test_one_balance_covers_several_symbols(self):
        for sym, px in (("AAPL", 347.0), ("MSFT", 532.0), ("SPY", 771.0)):
            self.assertEqual(brokers.execute_signal(self.cfg, "BUY", sym, px, "bot")["status"], "filled")
        self.assertEqual(sorted(brokers.load_positions()), ["AAPL", "MSFT", "SPY"])
        # three $100 buys, each rounded down to whole millionths of a share
        self.assertAlmostEqual(brokers.load_paper_balance(1000), 700.0, delta=0.01)

    def test_sell_without_position_is_skipped(self):
        self.assertEqual(brokers.execute_signal(self.cfg, "SELL", "AAPL", 260.0, "bot")["status"], "skipped")

    def test_insufficient_paper_balance_is_skipped(self):
        brokers.save_paper_balance(60)
        result = brokers.execute_signal(self.cfg, "BUY", "AAPL", 250.0, "bot")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(brokers.read_ledger(), [])

    def test_live_position_not_sold_in_paper_mode(self):
        brokers.save_positions({"AAPL": {"quantity": 1, "entry_price": 1, "invested": 1,
                                         "timestamp": "", "mode": "live"}})
        result = brokers.execute_signal(self.cfg, "SELL", "AAPL", 260.0, "bot")
        self.assertEqual(result["status"], "skipped")
        self.assertIn("AAPL", brokers.load_positions())


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, rel in [("STATE_DIR", ""), ("STATUS_PATH", "status.json"),
                          ("SIGNAL_STATE_PATH", "signal_state.json")]:
            patcher = mock.patch.object(strategy, name, os.path.join(tmp.name, rel))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = config.validate({"symbols": ["UP", "FLAT", "BAD"], "short_window": 2,
                                    "long_window": 4, "lookback_days": 10})

    def fake_fetch(self, symbol, lookback_days):
        if symbol == "BAD":
            raise RuntimeError("no data")
        values = [10, 10, 10, 9, 9, 13] if symbol == "UP" else [5, 5, 5, 5, 5, 5]
        return series(values)

    def test_each_symbol_checked_and_failures_isolated(self):
        with mock.patch.object(strategy, "fetch_daily_closes", self.fake_fetch),              mock.patch.object(strategy.brokers, "execute_signal",
                               return_value={"status": "filled", "trade": {}}) as execute:
            rows = strategy.run_cycle(self.cfg)
        self.assertEqual(rows["UP"]["signal"], "BUY")
        self.assertIsNone(rows["FLAT"]["signal"])
        self.assertIn("error", rows["BAD"])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[1:3], ("BUY", "UP"))

        # The same crossover is not traded twice.
        with mock.patch.object(strategy, "fetch_daily_closes", self.fake_fetch),              mock.patch.object(strategy.brokers, "execute_signal") as execute:
            strategy.run_cycle(self.cfg)
        execute.assert_not_called()

    def test_all_symbols_failing_raises(self):
        cfg = config.validate({"symbols": ["BAD"], "short_window": 2, "long_window": 4, "lookback_days": 10})
        with mock.patch.object(strategy, "fetch_daily_closes", self.fake_fetch):
            with self.assertRaises(RuntimeError):
                strategy.run_cycle(cfg)


if __name__ == "__main__":
    unittest.main()
