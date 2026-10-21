from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analysis


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.df = analysis.load_data()
        cls.validation = analysis.validate_data(cls.df)

    def test_dataset_shape_and_identity(self) -> None:
        self.assertEqual(self.validation["row_count"], 90189)
        self.assertEqual(self.validation["unique_users"], 90189)
        self.assertEqual(self.validation["duplicate_userids"], 0)
        self.assertEqual(set(self.validation["group_counts"]), {"gate_30", "gate_40"})

    def test_group_counts_reconcile(self) -> None:
        counts = self.validation["group_counts"]
        self.assertEqual(sum(counts.values()), self.validation["row_count"])

    def test_retention_interval_contains_estimate(self) -> None:
        lower, upper = analysis.wilson_interval(100, 1000)
        self.assertLess(lower, 0.1)
        self.assertGreater(upper, 0.1)
        self.assertGreaterEqual(lower, 0)
        self.assertLessEqual(upper, 1)

    def test_proportion_test_is_symmetric(self) -> None:
        forward = analysis.compare_proportions(100, 1000, 120, 1000)
        reverse = analysis.compare_proportions(120, 1000, 100, 1000)
        self.assertTrue(math.isclose(forward["p_value"], reverse["p_value"]))
        self.assertTrue(
            math.isclose(
                forward["difference"],
                -reverse["difference"],
            )
        )

    def test_round_statistics_cover_all_users(self) -> None:
        rounds = analysis.calculate_round_stats(self.df)
        self.assertEqual(
            rounds["groups"]["gate_30"]["users"]
            + rounds["groups"]["gate_40"]["users"],
            len(self.df),
        )
        self.assertGreaterEqual(rounds["p99_threshold"], 0)

    def test_sql_group_metrics_match_pandas(self) -> None:
        analysis.BUILD_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=analysis.BUILD_DIR
        ) as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            analysis.create_database(self.df, db_path)
            sql_results = analysis.run_sql_queries(
                db_path, analysis.SQL_PATH
            )

        observed = sql_results["group_metrics"].set_index("version")
        for group in ("gate_30", "gate_40"):
            expected = self.df[self.df["version"] == group]
            self.assertEqual(
                int(observed.loc[group, "users"]),
                len(expected),
            )
            self.assertTrue(
                math.isclose(
                    observed.loc[group, "d1_retention_pct"],
                    expected["retention_1"].mean() * 100,
                    abs_tol=0.01,
                )
            )
            self.assertTrue(
                math.isclose(
                    observed.loc[group, "d7_retention_pct"],
                    expected["retention_7"].mean() * 100,
                    abs_tol=0.01,
                )
            )


if __name__ == "__main__":
    unittest.main()

