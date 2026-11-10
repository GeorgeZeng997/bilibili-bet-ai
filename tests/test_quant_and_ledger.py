import csv
import datetime
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from auth import parse_cookie_string
from bili_client import BiliClient
import cli
from ledger import BetLedger
from quant import (
    apply_drift_haircut,
    compute_odds_drift_retention,
    evaluate_prediction_options,
    fractional_kelly_fixed_odds,
    optimize_plaza_bet_with_calc,
    parse_hours_to_close,
    parse_pool_coins,
)


class TestQuantEngine(unittest.TestCase):
    def test_parse_pool_coins_units_and_malformed(self) -> None:
        self.assertEqual(
            parse_pool_coins("\u786c\u5e01\u5956\u6c60\uff1a6139.5\u4e07\u300011\u670815\u65e5 00:00\u5c01\u76d8"),
            61395000.0,
        )
        self.assertEqual(parse_pool_coins("1.2\u4ebf"), 120000000.0)
        self.assertEqual(parse_pool_coins("12,345"), 12345.0)
        self.assertEqual(parse_pool_coins("\u5956\u6c60\uff1a."), 0.0)
        self.assertEqual(parse_pool_coins(""), 0.0)

    def test_parse_hours_to_close_multiple_formats(self) -> None:
        ref = datetime.datetime(2026, 11, 10, 12, 0, 0).timestamp()
        self.assertAlmostEqual(
            parse_hours_to_close(
                "\u786c\u5e01\u5956\u6c60\uff1a99.0\u4e07\u300011\u670811\u65e5 00:00\u5c01\u76d8",
                now_ts=ref,
            )
            or 0.0,
            12.0,
            places=2,
        )
        self.assertAlmostEqual(
            parse_hours_to_close("2026-11-10 18:30:00", now_ts=ref) or 0.0,
            6.5,
            places=2,
        )
        target_epoch = str(int(ref + 3600 * 5))
        self.assertAlmostEqual(
            parse_hours_to_close(target_epoch, now_ts=ref) or 0.0,
            5.0,
            places=2,
        )
        self.assertEqual(parse_hours_to_close(str(int(ref - 3600)), now_ts=ref), 0.0)
        self.assertIsNone(parse_hours_to_close("invalid date"))

    def test_drift_retention_and_haircut(self) -> None:
        ret_deep_late, _ = compute_odds_drift_retention(pool_coins=2_000_000, hours_to_close=4.0)
        ret_shallow_early, _ = compute_odds_drift_retention(pool_coins=2_000, hours_to_close=96.0)
        self.assertGreater(ret_deep_late, 0.95)
        self.assertEqual(ret_shallow_early, 0.35)
        self.assertAlmostEqual(apply_drift_haircut(2.0, 0.5), 1.5)
        # Preserve <= 1.0x trap odds untouched
        self.assertEqual(apply_drift_haircut(0.92, 0.5), 0.92)

    def test_fractional_kelly_edge_cases(self) -> None:
        # Positive EV case
        stake = fractional_kelly_fixed_odds(
            true_prob=0.65,
            decimal_odds=2.0,
            bankroll=1000.0,
            kelly_fraction=0.25,
            max_bet_ratio=0.10,
            max_single_bet=200,
        )
        # raw_f = 0.30, 0.25 * 0.30 = 0.075 -> 75 coins
        self.assertEqual(stake, 75)

        # Negative EV returns 0
        self.assertEqual(fractional_kelly_fixed_odds(0.40, 2.0, 1000.0), 0)

        # Certainty p=1.0 should size up to cap rather than returning 0
        self.assertEqual(
            fractional_kelly_fixed_odds(1.0, 1.5, 1000.0, kelly_fraction=0.25, max_bet_ratio=0.10, max_single_bet=80),
            80,
        )

        # Zero kelly_fraction, NaN, out-of-range p_true, or <=1.02 odds return 0
        self.assertEqual(fractional_kelly_fixed_odds(0.80, 2.0, 1000.0, kelly_fraction=0.0), 0)
        self.assertEqual(fractional_kelly_fixed_odds(math.nan, 2.0, 1000.0), 0)
        self.assertEqual(fractional_kelly_fixed_odds(1.2, 2.0, 1000.0), 0)
        self.assertEqual(fractional_kelly_fixed_odds(0.95, 1.01, 1000.0), 0)

    def test_optimize_and_evaluate_options(self) -> None:
        # Simulate parimutuel pool with 200 coins on opt 1 and 200 coins on opt 2
        def mock_calc(_pid: int, opt_idx: int, coins: int) -> float:
            if opt_idx == 1:
                return coins * ((400.0 + coins) / (200.0 + coins))
            return coins * 0.95  # Trap option (<1.0x due to time weight)

        best_c, best_pred, best_odds = optimize_plaza_bet_with_calc(
            calc_fn=mock_calc,
            predict_id=101,
            opt_idx=1,
            true_prob=0.70,
            bankroll=500.0,
            kelly_fraction=0.25,
            max_single_bet=100,
            drift_retention=0.95,
        )
        self.assertGreater(best_c, 0)
        self.assertGreater(best_pred, best_c)
        self.assertGreater(best_odds, 1.2)

        analyses = evaluate_prediction_options(
            predict_id=101,
            options=[
                {"opt_idx": 1, "opt_desc": "Team A", "percent": 50.0},
                {"opt_idx": 2, "opt_desc": "Team B", "percent": 50.0},
            ],
            true_probs={1: 0.70, 2: 0.30},
            calc_fn=mock_calc,
            bankroll=500.0,
            pool_coins=500_000,
            hours_to_close=4.0,
        )
        self.assertEqual(analyses[0].tier, "STRONG")
        self.assertGreater(analyses[0].recommended_coins, 0)
        self.assertEqual(analyses[1].tier, "TRAP")
        self.assertEqual(analyses[1].recommended_coins, 0)

        # Far from close (>24h) should trigger SNIPE_LATER with 0 recommended coins
        early = evaluate_prediction_options(
            predict_id=101,
            options=[{"opt_idx": 1, "opt_desc": "Team A", "percent": 50.0}],
            true_probs={1: 0.70},
            calc_fn=mock_calc,
            bankroll=500.0,
            pool_coins=500_000,
            hours_to_close=48.0,
        )
        self.assertEqual(early[0].tier, "SNIPE_LATER")
        self.assertEqual(early[0].recommended_coins, 0)


class TestAuthAndLedger(unittest.TestCase):
    def test_cookie_parsing_and_atomic_save(self) -> None:
        parsed = parse_cookie_string("Cookie: SESSDATA=abc123;\r\n bili_jct=csrf456; empty=")
        self.assertEqual(parsed["SESSDATA"], "abc123")
        self.assertEqual(parsed["bili_jct"], "csrf456")

        with tempfile.TemporaryDirectory() as tmp:
            cpath = Path(tmp) / "cookies.json"
            client = BiliClient(cookie_path=cpath)
            client.save_cookies({"SESSDATA": "s1\r\n", "bili_jct": "j1"})
            self.assertTrue(client.is_logged_in)
            self.assertNotIn("\r", client._cookie_header())
            self.assertNotIn("\n", client._cookie_header())

    def test_ledger_proportional_settlement_no_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.db"
            ledger = BetLedger(db_path=db_path)
            ledger.record_bet("plaza", 888, "Match 888", 1, "Win", coins=10, effective_odds=1.8)
            ledger.record_bet("plaza", 888, "Match 888", 1, "Win", coins=30, effective_odds=1.7)
            ledger.record_bet("esports", 999, "Esports 999", 2, "Lose", coins=10, effective_odds=2.0)

            summary_before = ledger.get_summary()
            self.assertEqual(summary_before["pending"], 3)
            self.assertEqual(summary_before["pending_coins"], 50)

            # Settle predict_id=888 with +20.0 total net profit across the two plaza bets (10 + 30 coins)
            updated = ledger.update_settlement(888, "WON", 20.0, source="plaza")
            self.assertEqual(updated, 2)

            # Re-running settlement sync is idempotent and never double-counts PnL
            ledger.update_settlement(888, "WON", 20.0, source="plaza")
            summary_after = ledger.get_summary()
            self.assertEqual(summary_after["won"], 2)
            self.assertEqual(summary_after["pending"], 1)
            self.assertEqual(summary_after["pending_coins"], 10)
            self.assertAlmostEqual(summary_after["realized_pnl"], 20.0, places=2)

    def test_analytics_risk_guardrails_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger = BetLedger(db_path=tmp_path / "ledger.db")

            # Bet 1: WON +30 on 20 coins (p_true=0.80 -> Brier (0.8-1)^2 = 0.04)
            ledger.record_bet("plaza", 101, "M1", 1, "Yes", coins=20, effective_odds=2.5, true_prob=0.80)
            ledger.update_settlement(101, "WON", 30.0, source="plaza")

            # Bet 2: LOST -15 on 15 coins (p_true=0.60 -> Brier (0.6-0)^2 = 0.36)
            ledger.record_bet("plaza", 102, "M2", 2, "No", coins=15, effective_odds=2.0, true_prob=0.60)
            ledger.update_settlement(102, "LOST", -15.0, source="plaza")

            # Bet 3: PENDING 25 coins
            ledger.record_bet("plaza", 103, "M3", 1, "Yes", coins=25, effective_odds=1.9, true_prob=0.70)

            metrics = ledger.get_analytics()
            self.assertEqual(metrics["total_bets"], 3)
            self.assertEqual(metrics["settled_bets"], 2)
            self.assertEqual(metrics["won"], 1)
            self.assertEqual(metrics["lost"], 1)
            self.assertEqual(metrics["pending_coins"], 25)
            self.assertAlmostEqual(metrics["realized_pnl"], 15.0, places=2)
            self.assertAlmostEqual(metrics["roi_pct"], round(15.0 / 35.0 * 100.0, 2), places=2)
            self.assertAlmostEqual(metrics["profit_factor"], 2.0, places=2)
            self.assertAlmostEqual(metrics["max_drawdown_coins"], 15.0, places=2)
            self.assertAlmostEqual(metrics["current_drawdown_coins"], 15.0, places=2)
            self.assertAlmostEqual(metrics["brier_score"], 0.20, places=4)

            # Duplicate pending check
            dup = ledger.check_risk_guardrails(
                proposed_coins=10,
                bankroll=200.0,
                source="plaza",
                predict_id=103,
                prevent_duplicate_pending=True,
            )
            self.assertFalse(dup["allowed"])

            # Exposure cap check: bankroll=75, pending=25 -> total_equity=100; proposing 20 -> 45% > 35%
            exp = ledger.check_risk_guardrails(
                proposed_coins=20,
                bankroll=75.0,
                max_exposure_pct=0.35,
            )
            self.assertFalse(exp["allowed"])

            # Drawdown circuit breaker check: current_drawdown=15 >= limit 10
            dd = ledger.check_risk_guardrails(
                proposed_coins=5,
                bankroll=500.0,
                max_drawdown_coins=10.0,
            )
            self.assertFalse(dd["allowed"])

            # Allowed bet within limits
            ok = ledger.check_risk_guardrails(
                proposed_coins=10,
                bankroll=500.0,
                max_exposure_pct=0.35,
                max_drawdown_coins=25.0,
                source="plaza",
                predict_id=104,
                prevent_duplicate_pending=True,
            )
            self.assertTrue(ok["allowed"])

            # Export to CSV and JSON
            csv_file = tmp_path / "export.csv"
            json_file = tmp_path / "export.json"
            self.assertEqual(ledger.export_records(csv_file), 3)
            self.assertEqual(ledger.export_records(json_file), 3)
            with csv_file.open("r", encoding="utf-8") as f:
                rows_csv = list(csv.DictReader(f))
            rows_json = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertEqual(len(rows_csv), 3)
            self.assertEqual(len(rows_json), 3)

    def test_cli_dry_run_bet_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.db"
            cookie_path = Path(tmp) / "cookies.json"
            fake_client = BiliClient(cookie_path=cookie_path)
            fake_client.calc_plaza_award = lambda _pid, _opt, coins: float(coins) * 2.40  # type: ignore[assignment]
            fake_ledger = BetLedger(db_path=db_path)

            parser = cli.build_parser()
            args = parser.parse_args(
                [
                    "bet",
                    "--source",
                    "plaza",
                    "--id",
                    "555",
                    "--opt",
                    "2",
                    "--coins",
                    "20",
                    "--prob",
                    "0.60",
                    "--bankroll",
                    "200",
                    "--dry-run",
                ]
            )
            buf = io.StringIO()
            with (
                mock.patch.object(cli, "BiliClient", return_value=fake_client),
                mock.patch.object(cli, "BetLedger", return_value=fake_ledger),
                redirect_stdout(buf),
            ):
                args.func(args)
            out = buf.getvalue()
            self.assertIn("[DRY-RUN]", out)
            self.assertIn("2.40x", out)
            self.assertEqual(fake_ledger.get_summary()["total_bets"], 0)


if __name__ == "__main__":
    unittest.main()
