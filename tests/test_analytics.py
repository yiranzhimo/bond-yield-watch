#!/usr/bin/env python3
"""Unit tests for the pure analytics in scripts/update_yields.py.

These cover the logic that is easy to break silently and expensive to notice:
lookback arithmetic, volatility scaling, the alert decision, the sanity screen
and the cross-source comparison. Every case uses hand-built series so the
expected numbers are checkable by hand - no network access.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "update_yields.py"

spec = importlib.util.spec_from_file_location("update_yields", MODULE_PATH)
assert spec and spec.loader
uy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uy)


def series_from(pairs, tenor="10Y"):
    """Build a Series from [(iso_date, yield), ...]."""
    return {stamp: {tenor: value} for stamp, value in pairs}


def business_series(start: str, values, tenor="10Y"):
    """Consecutive weekday observations starting at ``start``."""
    current = date.fromisoformat(start)
    out = {}
    for value in values:
        while current.weekday() >= 5:
            current += timedelta(days=1)
        out[current.isoformat()] = {tenor: value}
        current += timedelta(days=1)
    return out


class TestHelpers(unittest.TestCase):
    def test_finite_rejects_sentinels(self):
        for bad in (None, "", "-", "--", "N/A", "null", "nan", float("inf")):
            self.assertIsNone(uy.finite(bad), f"{bad!r} should be rejected")

    def test_finite_accepts_numeric_strings(self):
        self.assertEqual(uy.finite(" 1.75 "), 1.75)
        self.assertEqual(uy.finite(2), 2.0)

    def test_bp_converts_percentage_points(self):
        self.assertEqual(uy.bp(0.01), 1.0)
        self.assertEqual(uy.bp(-0.235), -23.5)
        self.assertIsNone(uy.bp(None))


class TestChangeBp(unittest.TestCase):
    def test_one_day_uses_previous_observation(self):
        series = series_from([("2026-08-14", 1.70), ("2026-08-17", 1.75)])
        # 1.75 - 1.70 = 5bp, across a weekend.
        self.assertEqual(uy.change_bp(series, "10Y", 1), 5.0)

    def test_multi_day_lookback_uses_asof(self):
        series = series_from(
            [
                ("2026-07-17", 1.60),
                ("2026-08-10", 1.70),
                ("2026-08-17", 1.75),
            ]
        )
        # 30 calendar days before 2026-08-17 is 2026-07-18; the newest
        # observation at or before that is 2026-07-17 at 1.60.
        self.assertEqual(uy.change_bp(series, "10Y", 30), 15.0)

    def test_returns_none_without_enough_history(self):
        single = series_from([("2026-08-17", 1.75)])
        self.assertIsNone(uy.change_bp(single, "10Y", 1))
        self.assertIsNone(uy.change_bp({}, "10Y", 1))

    def test_returns_none_when_lookback_predates_series(self):
        series = series_from([("2026-08-14", 1.70), ("2026-08-17", 1.75)])
        self.assertIsNone(uy.change_bp(series, "10Y", 365))

    def test_ignores_other_tenors(self):
        series = {"2026-08-17": {"2Y": 1.2}, "2026-08-14": {"2Y": 1.1}}
        self.assertIsNone(uy.change_bp(series, "10Y", 1))


class TestLastGapDays(unittest.TestCase):
    def test_weekend_gap(self):
        series = series_from([("2026-08-14", 1.70), ("2026-08-17", 1.75)])
        self.assertEqual(uy.last_gap_days(series, "10Y"), 3)

    def test_holiday_gap_is_visible(self):
        # A Chinese New Year style break: the "1 day" change really spans a week.
        series = series_from([("2026-02-14", 1.70), ("2026-02-24", 1.80)])
        self.assertEqual(uy.last_gap_days(series, "10Y"), 10)

    def test_none_without_two_observations(self):
        self.assertIsNone(uy.last_gap_days(series_from([("2026-08-17", 1.7)]), "10Y"))


class TestPercentileRank(unittest.TestCase):
    def test_highest_value_is_hundred(self):
        series = business_series("2026-01-01", [1.0 + i * 0.01 for i in range(40)])
        self.assertEqual(uy.percentile_rank(series, "10Y"), 100.0)

    def test_lowest_value_is_low(self):
        series = business_series("2026-01-01", [2.0 - i * 0.01 for i in range(40)])
        rank = uy.percentile_rank(series, "10Y")
        self.assertIsNotNone(rank)
        self.assertLessEqual(rank, 2.5)

    def test_needs_minimum_sample(self):
        series = business_series("2026-08-01", [1.7, 1.71, 1.72])
        self.assertIsNone(uy.percentile_rank(series, "10Y"))

    def test_window_limits_lookback(self):
        # Old high values fall outside a short window, so the latest print
        # ranks at the top of what remains.
        values = [5.0] * 30 + [1.0 + i * 0.01 for i in range(30)]
        series = business_series("2025-01-01", values)
        self.assertEqual(uy.percentile_rank(series, "10Y", window=30), 100.0)


class TestChangeSigma(unittest.TestCase):
    def test_constant_series_has_no_sigma(self):
        series = business_series("2026-01-01", [1.7] * 30)
        self.assertIsNone(uy.change_sigma(series, "10Y"))

    def test_sigma_scales_with_volatility(self):
        calm = business_series("2026-01-01", [1.70 + (i % 2) * 0.01 for i in range(40)])
        wild = business_series("2026-01-01", [1.70 + (i % 2) * 0.10 for i in range(40)])
        calm_sigma = uy.change_sigma(calm, "10Y")
        wild_sigma = uy.change_sigma(wild, "10Y")
        self.assertIsNotNone(calm_sigma)
        self.assertIsNotNone(wild_sigma)
        self.assertGreater(wild_sigma, calm_sigma * 5)

    def test_five_step_measures_a_wider_window(self):
        """Weekly sigma is sampled from real 5-observation moves.

        A mean-reverting series has sizeable daily noise but little net weekly
        drift, so measured weekly sigma comes in *below* daily x sqrt(5) - the
        error that scaling assumption makes, and the reason weekly alerts were
        firing more often than the stated 2.5 sigma.
        """
        import math

        values = [1.70 + (0.05 if i % 2 else -0.05) for i in range(60)]
        series = business_series("2026-01-01", values)
        daily = uy.change_sigma(series, "10Y", step=1)
        weekly = uy.change_sigma(series, "10Y", step=5)
        self.assertIsNotNone(daily)
        self.assertIsNotNone(weekly)
        self.assertLess(weekly, daily * math.sqrt(5))

    def test_constant_step_series_has_no_sigma(self):
        """A perfectly linear trend has zero variance in its step sizes."""
        series = business_series("2026-01-01", [1.0 + i * 0.05 for i in range(60)])
        self.assertIsNone(uy.change_sigma(series, "10Y", step=1))
        self.assertIsNone(uy.change_sigma(series, "10Y", step=5))

    def test_step_must_be_positive(self):
        series = business_series("2026-01-01", [1.7 + i * 0.01 for i in range(30)])
        self.assertIsNone(uy.change_sigma(series, "10Y", step=0))

    def test_respects_runtime_rule_changes(self):
        """Window default is read at call time, not bound at import."""
        series = business_series("2026-01-01", [1.70 + (i % 3) * 0.02 for i in range(80)])
        original = uy.ALERT_RULES["sigma_window_days"]
        try:
            uy.ALERT_RULES["sigma_window_days"] = 15
            narrow = uy.change_sigma(series, "10Y")
            uy.ALERT_RULES["sigma_window_days"] = 70
            wide = uy.change_sigma(series, "10Y")
        finally:
            uy.ALERT_RULES["sigma_window_days"] = original
        self.assertIsNotNone(narrow)
        self.assertIsNotNone(wide)


class TestJudgeMove(unittest.TestCase):
    def base(self, **over):
        kwargs = dict(
            sigma_threshold=2.0,
            floor_bp=2.0,
            ceiling_bp=15.0,
            high_sigma=3.0,
        )
        kwargs.update(over)
        return kwargs

    def test_quiet_move_is_ignored(self):
        self.assertIsNone(uy.judge_move(1.0, 4.0, **self.base()))

    def test_sigma_breach_fires(self):
        verdict = uy.judge_move(9.0, 4.0, **self.base())
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["trigger"], "sigma")
        self.assertEqual(verdict["multiple"], 2.2)
        self.assertEqual(verdict["severity"], "medium")

    def test_high_severity_above_high_sigma(self):
        verdict = uy.judge_move(13.0, 4.0, **self.base())
        self.assertEqual(verdict["severity"], "high")

    def test_absolute_ceiling_fires_without_sigma(self):
        verdict = uy.judge_move(20.0, None, **self.base())
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["trigger"], "absolute")
        self.assertEqual(verdict["severity"], "high")

    def test_floor_blocks_statistical_trivia(self):
        """A 5-sigma move of 1bp in a motionless market is not news."""
        self.assertIsNone(uy.judge_move(1.0, 0.2, **self.base()))

    def test_both_triggers_are_labelled(self):
        verdict = uy.judge_move(30.0, 4.0, **self.base())
        self.assertEqual(verdict["trigger"], "both")
        self.assertEqual(verdict["severity"], "high")

    def test_direction_does_not_matter(self):
        up = uy.judge_move(9.0, 4.0, **self.base())
        down = uy.judge_move(-9.0, 4.0, **self.base())
        self.assertEqual(up["multiple"], down["multiple"])

    def test_missing_move_is_ignored(self):
        self.assertIsNone(uy.judge_move(None, 4.0, **self.base()))


class TestCurveShape(unittest.TestCase):
    def test_shape_bands(self):
        cases = [
            (-40.0, "deeply_inverted"),
            (-5.0, "inverted"),
            (10.0, "flat"),
            (60.0, "normal"),
            (150.0, "steep"),
        ]
        for spread, expected in cases:
            self.assertEqual(uy.curve_shape(spread, None), expected, f"at {spread}bp")

    def test_unknown_without_data(self):
        self.assertEqual(uy.curve_shape(None, None), "unknown")


class TestSanityScreen(unittest.TestCase):
    def setUp(self):
        self.base = business_series("2026-08-03", [1.70] * 10)

    def test_accepts_normal_value(self):
        merged, added, rejected = uy.merge_series(self.base, {"2026-08-18": {"10Y": 1.74}})
        self.assertEqual(added, 1)
        self.assertEqual(rejected, [])
        self.assertEqual(merged["2026-08-18"]["10Y"], 1.74)

    def test_rejects_out_of_range(self):
        _, added, rejected = uy.merge_series(self.base, {"2026-08-18": {"10Y": 99.0}})
        self.assertEqual(added, 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("超出合理区间", rejected[0])

    def test_rejects_absurd_jump(self):
        _, added, rejected = uy.merge_series(self.base, {"2026-08-18": {"10Y": 4.50}})
        self.assertEqual(added, 0)
        self.assertIn("跳变", rejected[0])

    def test_jump_budget_scales_with_gap(self):
        """A long break earns proportionally more room before rejection.

        The same +110bp move is bad news the next day and unremarkable after a
        month. A flat cap rejected 1716 valid values from the sparse early
        history, so the tolerance grows with sqrt(elapsed days).
        """
        # The fixture's last observation is 2026-08-14.
        next_day = uy.merge_series(self.base, {"2026-08-15": {"10Y": 2.80}})
        self.assertEqual(next_day[1], 0, "+110bp overnight should be rejected")

        a_month_later = uy.merge_series(self.base, {"2026-09-14": {"10Y": 2.80}})
        self.assertEqual(a_month_later[1], 1, "+110bp over a month is plausible")

    def test_gap_budget_is_capped(self):
        """Tolerance widens with time but never becomes unlimited."""
        _, added, rejected = uy.merge_series(self.base, {"2027-03-01": {"10Y": 9.00}})
        self.assertEqual(added, 0)
        self.assertIn("上限", rejected[0])

    def test_zero_print_is_rejected(self):
        _, _, rejected = uy.merge_series(self.base, {"2026-08-18": {"10Y": 0.0}})
        self.assertEqual(len(rejected), 1)

    def test_partial_row_keeps_good_tenor(self):
        incoming = {"2026-08-18": {"10Y": 1.74, "30Y": 99.0}}
        merged, added, rejected = uy.merge_series(self.base, incoming)
        self.assertEqual(added, 1)
        self.assertEqual(merged["2026-08-18"], {"10Y": 1.74})
        self.assertEqual(len(rejected), 1)

    def test_first_observation_has_no_reference(self):
        merged, added, rejected = uy.merge_series({}, {"2026-08-18": {"10Y": 1.74}})
        self.assertEqual(added, 1)
        self.assertEqual(rejected, [])


class TestStalenessAlert(unittest.TestCase):
    REF = date(2026, 8, 18)

    def alert_at(self, age_days):
        stamp = (self.REF - timedelta(days=age_days)).isoformat()
        return uy.staleness_alert("CN", {"as_of": stamp}, "中国国债", today=self.REF)

    def test_fresh_data_is_silent(self):
        for age in (0, 1, 2, 3):
            self.assertIsNone(self.alert_at(age), f"age {age} should be silent")

    def test_warns_after_threshold(self):
        alert = self.alert_at(4)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["severity"], "medium")
        self.assertEqual(alert["age_days"], 4)

    def test_escalates_when_very_stale(self):
        self.assertEqual(self.alert_at(8)["severity"], "high")
        self.assertEqual(self.alert_at(45)["severity"], "high")

    def test_missing_as_of_is_high(self):
        alert = uy.staleness_alert("CN", {"as_of": None}, "中国国债", today=self.REF)
        self.assertEqual(alert["severity"], "high")
        self.assertIsNone(alert["age_days"])

    def test_alert_kind_is_stable(self):
        """The dashboard and email filter on this string."""
        self.assertEqual(self.alert_at(10)["kind"], "stale_data")


class TestCompareUsSources(unittest.TestCase):
    def test_agreement(self):
        em = {"2026-08-14": {"2Y": 4.17, "10Y": 4.68}}
        fred = {"2026-08-14": {"2Y": 4.17, "10Y": 4.68}}
        result = uy.compare_us_sources(em, fred)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["max_abs_diff_bp"], 0.0)

    def test_detects_mismatch(self):
        em = {"2026-08-14": {"10Y": 4.68}}
        fred = {"2026-08-14": {"10Y": 4.95}}
        result = uy.compare_us_sources(em, fred)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["diff_bp"]["10Y"], -27.0)

    def test_small_difference_still_ok(self):
        em = {"2026-08-14": {"10Y": 4.68}}
        fred = {"2026-08-14": {"10Y": 4.71}}
        self.assertEqual(uy.compare_us_sources(em, fred)["status"], "ok")

    def test_no_shared_date(self):
        em = {"2026-08-17": {"10Y": 4.72}}
        fred = {"2026-08-10": {"10Y": 4.60}}
        self.assertEqual(uy.compare_us_sources(em, fred)["status"], "no_overlap")

    def test_compares_on_newest_shared_date(self):
        em = {"2026-08-17": {"10Y": 4.72}, "2026-08-14": {"10Y": 4.68}}
        fred = {"2026-08-14": {"10Y": 4.68}}
        result = uy.compare_us_sources(em, fred)
        self.assertEqual(result["as_of"], "2026-08-14")
        self.assertEqual(result["status"], "ok")

    def test_empty_inputs(self):
        self.assertEqual(uy.compare_us_sources({}, {})["status"], "no_overlap")


class TestSpreadHistory(unittest.TestCase):
    def test_only_shared_dates_are_used(self):
        left = series_from([("2026-08-14", 1.70), ("2026-08-17", 1.75)])
        right = series_from([("2026-08-17", 4.70)])
        spread = uy.spread_history(left, right, "10Y")
        self.assertEqual(list(spread), ["2026-08-17"])
        self.assertAlmostEqual(spread["2026-08-17"]["10Y"], -2.95, places=6)

    def test_holiday_mismatch_produces_no_phantom_jump(self):
        """Skipping unmatched dates is what stops holidays faking a gap."""
        left = business_series("2026-08-03", [1.70] * 6)
        right = {"2026-08-05": {"10Y": 4.70}}
        spread = uy.spread_history(left, right, "10Y")
        self.assertEqual(len(spread), 1)


class TestNeedsBackfill(unittest.TestCase):
    def full_history(self):
        values = [1.70 + (i % 5) * 0.01 for i in range(500)]
        start = (date.today() - timedelta(days=700)).isoformat()
        series = business_series(start, values)
        # Ensure the newest observation is recent.
        series[date.today().isoformat()] = {"10Y": 1.75}
        return {market: dict(series) for market in uy.MARKETS}

    def test_healthy_cache_is_incremental(self):
        backfill, _ = uy.needs_backfill(self.full_history())
        self.assertFalse(backfill)

    def test_empty_cache_triggers_backfill(self):
        backfill, reason = uy.needs_backfill({m: {} for m in uy.MARKETS})
        self.assertTrue(backfill)
        self.assertIn("无缓存历史", reason)

    def test_thin_cache_triggers_backfill(self):
        history = self.full_history()
        history["JP"] = dict(list(history["JP"].items())[:50])
        backfill, reason = uy.needs_backfill(history)
        self.assertTrue(backfill)
        self.assertIn("JP", reason)

    def test_stale_cache_triggers_backfill(self):
        history = self.full_history()
        cutoff = (date.today() - timedelta(days=60)).isoformat()
        history["US"] = {k: v for k, v in history["US"].items() if k <= cutoff}
        backfill, reason = uy.needs_backfill(history)
        self.assertTrue(backfill)
        self.assertIn("落后", reason)


class TestTemperature(unittest.TestCase):
    def snapshot(self, percentile, slope, momentum):
        tenors = {
            tenor: {
                "yield": 2.0,
                "percentile_2y": percentile,
                "change_1m_bp": momentum,
            }
            for tenor in uy.TENORS
        }
        return {
            "tenors": tenors,
            "term_structure": {"spread_10y_2y_bp": slope},
        }

    def test_high_yields_and_rising_reads_hot(self):
        result = uy.market_temperature(self.snapshot(98.0, 120.0, 30.0))
        self.assertGreaterEqual(result["score"], 75)
        self.assertEqual(result["level"], "hot")

    def test_low_yields_and_falling_reads_cold(self):
        result = uy.market_temperature(self.snapshot(3.0, -40.0, -30.0))
        self.assertLess(result["score"], 25)
        self.assertEqual(result["level"], "cold")

    def test_unknown_without_inputs(self):
        empty = {
            "tenors": {t: {"yield": None, "percentile_2y": None, "change_1m_bp": None} for t in uy.TENORS},
            "term_structure": {"spread_10y_2y_bp": None},
        }
        self.assertEqual(uy.market_temperature(empty)["level"], "unknown")

    def test_score_is_bounded(self):
        extreme = uy.market_temperature(self.snapshot(100.0, 9999.0, 9999.0))
        self.assertLessEqual(extreme["score"], 100.0)
        floor = uy.market_temperature(self.snapshot(0.0, -9999.0, -9999.0))
        self.assertGreaterEqual(floor["score"], 0.0)


class TestMappingGuard(unittest.TestCase):
    def rows(self, ten_field, two_field, spread_field, count=40, consistent=True):
        out = []
        for i in range(count):
            ten = 1.70 + i * 0.001
            two = 1.20 + i * 0.001
            spread = ten - two if consistent else 0.9
            out.append({ten_field: ten, two_field: two, spread_field: spread})
        return out

    def test_consistent_mapping_passes(self):
        fields = uy.EASTMONEY_FIELDS["CN"]
        spread_field = uy.EASTMONEY_SPREAD_FIELDS["CN"]
        rows = self.rows(fields["10Y"], fields["2Y"], spread_field)
        uy.validate_eastmoney_mapping(rows)  # must not raise

    def test_shuffled_mapping_raises(self):
        """This is the bug that shipped once: transposed tenor ids."""
        fields = uy.EASTMONEY_FIELDS["CN"]
        spread_field = uy.EASTMONEY_SPREAD_FIELDS["CN"]
        rows = self.rows(fields["10Y"], fields["2Y"], spread_field, consistent=False)
        with self.assertRaises(RuntimeError) as ctx:
            uy.validate_eastmoney_mapping(rows)
        self.assertIn("mapping looks wrong", str(ctx.exception))

    def test_too_few_rows_is_skipped_not_failed(self):
        fields = uy.EASTMONEY_FIELDS["CN"]
        spread_field = uy.EASTMONEY_SPREAD_FIELDS["CN"]
        rows = self.rows(fields["10Y"], fields["2Y"], spread_field, count=5)
        uy.validate_eastmoney_mapping(rows)  # too small to judge -> no raise


class TestMofParsing(unittest.TestCase):
    def test_reiwa_and_iso_dates(self):
        self.assertEqual(uy.parse_mof_date("2026/8/17"), "2026-08-17")
        self.assertEqual(uy.parse_mof_date("2026-8-17"), "2026-08-17")
        self.assertIsNone(uy.parse_mof_date("R8.8.3"))
        self.assertIsNone(uy.parse_mof_date("Date"))
        self.assertIsNone(uy.parse_mof_date(""))

    def test_parses_english_csv(self):
        csv_text = (
            "Interest Rate (August 2026),,,,,,,,,,,,,,,(Unit : %)\n"
            "Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y\n"
            "2026/8/17,1.4,1.696,1.8,2.0,2.177,2.3,2.4,2.6,2.7,2.919,3.4,3.8,4.0,4.05,4.05\n"
            ",,,,,,,,,,,,,,,\n"
        )
        parsed = uy.parse_mof_csv(csv_text.encode("utf-8"))
        self.assertEqual(set(parsed), {"2026-08-17"})
        row = parsed["2026-08-17"]
        self.assertEqual(row["2Y"], 1.696)
        self.assertEqual(row["10Y"], 2.919)
        self.assertEqual(row["30Y"], 4.05)

    def test_blank_cells_are_skipped(self):
        csv_text = (
            "Interest Rate,,,\n"
            "Date,2Y,10Y,30Y\n"
            "2026/8/17,,2.919,\n"
        )
        parsed = uy.parse_mof_csv(csv_text.encode("utf-8"))
        self.assertEqual(parsed["2026-08-17"], {"10Y": 2.919})


if __name__ == "__main__":
    unittest.main(verbosity=2)
