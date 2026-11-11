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


class CompletedBarsTests(unittest.TestCase):
    def test_drops_todays_bar_while_market_open(self):
        closes = series([1, 2, 3], end=datetime.date(2026, 10, 27))
        now = datetime.datetime(2026, 10, 27, 11, 0, tzinfo=ET)
        self.assertEqual(list(strategy.completed_bars(closes, 100, now)), [1, 2])

    def test_keeps_todays_bar_after_close(self):
        closes = series([1, 2, 3], end=datetime.date(2026, 10, 27))
        now = datetime.datetime(2026, 10, 27, 16, 30, tzinfo=ET)
        self.assertEqual(list(strategy.completed_bars(closes, 100, now)), [1, 2, 3])

    def test_trims_to_lookback(self):
        closes = series(range(150))
        now = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=ET)
        self.assertEqual(len(strategy.completed_bars(closes, 100, now)), 100)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config.validate({})
        self.assertEqual((cfg["symbols"], cfg["level_window"], cfg["trend_window"], cfg["trade_dollars"],
                          cfg["reward_risk"]), (["AAPL"], 20, 50, 100.0, 2.0))
        with self.assertRaises(ValueError):
            config.validate({"reward_risk": 0})

    def test_old_moving_average_config_migrates(self):
        cfg = config.validate({"strategy": "sma_crossover", "short_window": 15, "long_window": 40})
        self.assertEqual((cfg["level_window"], cfg["trend_window"]), (15, 40))
        for gone in ("strategy", "short_window", "long_window"):
            self.assertNotIn(gone, cfg)

    def test_symbols_are_cleaned(self):
        cfg = config.validate({"symbols": ["aapl", " msft ", "AAPL", ""]})
        self.assertEqual(cfg["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(config.validate({"symbols": "spy, qqq"})["symbols"], ["SPY", "QQQ"])

    def test_old_single_symbol_config_migrates(self):
        cfg = config.validate({"symbol": "msft", "trade_quantity": 1})
        self.assertEqual(cfg["symbols"], ["MSFT"])
        self.assertNotIn("symbol", cfg)
        self.assertNotIn("trade_quantity", cfg)

    def test_falls_back_to_example_until_config_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            example = os.path.join(tmp, "config.example.json")
            local = os.path.join(tmp, "config.json")
            with open(example, "w") as f:
                f.write('{"symbols": ["SPY"], "trade_dollars": 25}')
            with mock.patch.object(config, "CONFIG_PATH", local), \
                 mock.patch.object(config, "EXAMPLE_CONFIG_PATH", example):
                self.assertEqual(config.load_config()["symbols"], ["SPY"])
                config.update_config({"trade_dollars": 30})
                self.assertTrue(os.path.exists(local))
                self.assertEqual(config.load_config()["trade_dollars"], 30.0)
                with open(example) as f:
                    self.assertIn("25", f.read())  # template untouched

    def test_rejects_bad_values(self):
        for bad in ({"level_window": 1},
                    {"trend_window": 120, "lookback_days": 100},
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
        buy = brokers.execute_signal(self.cfg, "BUY", "AAPL", 250.0, "bot", pattern="bullish engulfing at support")
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


def bars(rows, end=datetime.date(2026, 10, 23)):
    days = pd.bdate_range(end=end, periods=len(rows))
    return pd.DataFrame(rows, index=[d.date() for d in days],
                        columns=["open", "high", "low", "close"], dtype=float)


QUIET = [(100.0, 100.8, 99.2, 100.5) if i % 2 else (100.5, 100.8, 99.2, 100.0) for i in range(30)]
# Large red, smaller red, big green at support: a bullish three-bar reversal.
REVERSAL = [(101.0, 101.1, 99.4, 99.5), (99.5, 99.6, 98.8, 99.0), (99.0, 100.7, 98.9, 100.6)]


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, rel in [("STATE_DIR", ""), ("STATUS_PATH", "status.json"),
                          ("SIGNAL_STATE_PATH", "signal_state.json"),
                          ("TRADE_PLANS_PATH", "trade_plans.json")]:
            patcher = mock.patch.object(strategy, name, os.path.join(tmp.name, rel))
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in [(strategy.brokers, "load_positions"), (strategy, "market_is_open")]:
            patcher = mock.patch.object(target, value, return_value={} if value == "load_positions" else False)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = config.validate({"symbols": ["REV", "FLAT", "BAD"], "level_window": 20,
                                    "trend_window": 20, "lookback_days": 40})

    def fake_fetch(self, symbol, lookback_days):
        if symbol == "BAD":
            raise RuntimeError("no data")
        return bars(QUIET + (REVERSAL if symbol == "REV" else QUIET[:3]))

    def test_each_symbol_checked_and_failures_isolated(self):
        with mock.patch.object(strategy, "fetch_daily_bars", self.fake_fetch), \
             mock.patch.object(strategy.brokers, "execute_signal",
                               return_value={"status": "filled", "trade": {}}) as execute:
            rows = strategy.run_cycle(self.cfg)
        self.assertEqual(rows["REV"]["signal"], "BUY")
        self.assertIsNone(rows["FLAT"]["signal"])
        self.assertIn("error", rows["BAD"])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[1:3], ("BUY", "REV"))

        # The same signal is not traded twice.
        with mock.patch.object(strategy, "fetch_daily_bars", self.fake_fetch), \
             mock.patch.object(strategy.brokers, "execute_signal") as execute:
            strategy.run_cycle(self.cfg)
        execute.assert_not_called()

    def test_all_symbols_failing_raises(self):
        cfg = config.validate({"symbols": ["BAD"]})
        with mock.patch.object(strategy, "fetch_daily_bars", self.fake_fetch):
            with self.assertRaises(RuntimeError):
                strategy.run_cycle(cfg)


class LedgerMigrationTests(unittest.TestCase):
    def test_moving_average_ledger_keeps_its_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trade_ledger.csv")
            with open(path, "w") as f:
                f.write("timestamp,symbol,action,quantity,price,short_ma,long_ma,mode,source\n"
                        "2026-10-01T15:00:00+00:00,AAPL,BUY,0.1,250.0,251.0,249.0,paper,bot\n")
            with mock.patch.object(brokers, "LEDGER_PATH", path), \
                 mock.patch.object(brokers, "ARCHIVE_DIR", os.path.join(tmp, "archive")):
                brokers._ensure_ledger_schema()
                rows = brokers.read_ledger()
            self.assertEqual(list(rows[0]), brokers.LEDGER_FIELDS)
            self.assertEqual((rows[0]["symbol"], rows[0]["price"], rows[0]["pattern"]), ("AAPL", "250.0", ""))
            self.assertFalse(os.path.exists(os.path.join(tmp, "archive")))


if __name__ == "__main__":
    unittest.main()
