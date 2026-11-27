"""The benchmark's table builder must not replace the committed tables with a smaller set."""
import contextlib
import csv
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

import aggregate

COMMITTED = REPO / "data" / "experiments" / "shot_briefs"


class AggregateGuard(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        for name in ("results.csv", "summary.md", "ground_truth.json", "scripted_baseline.json"):
            shutil.copy2(COMMITTED / name, self.out / name)
        original = aggregate.OUT
        aggregate.OUT = self.out
        self.addCleanup(setattr, aggregate, "OUT", original)

    def tables(self):
        return {name: (self.out / name).read_bytes() for name in ("results.csv", "summary.md")}

    def rows(self):
        with (self.out / "results.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def run_aggregate(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return aggregate.main(list(argv))

    def test_missing_trial_records_do_not_overwrite_the_tables(self):
        """A fresh clone: the per-trial records are kept out of Git, the tables are committed."""
        self.assertEqual(len(self.rows()), 56)
        before = self.tables()
        with self.assertRaises(SystemExit) as stop:
            self.run_aggregate()
        self.assertNotIn(stop.exception.code, (0, None))
        self.assertIn("trials.jsonl", str(stop.exception.code))
        self.assertEqual(self.tables(), before)

    def test_fewer_trials_than_the_existing_table_are_refused(self):
        scripted = json.loads((self.out / "scripted_baseline.json").read_text(encoding="utf-8"))
        partial = [dict(scripted[0], backend="deepseek", model="deepseek-flash", rep=1)]
        (self.out / "trials.jsonl").write_text("\n".join(json.dumps(t) for t in partial) + "\n", encoding="utf-8")
        before = self.tables()
        with self.assertRaises(SystemExit) as stop:
            self.run_aggregate()
        self.assertIn("56", str(stop.exception.code))
        self.assertEqual(self.tables(), before)

    def test_explicit_override_writes_the_smaller_tables(self):
        self.assertEqual(self.run_aggregate("--allow-fewer-trials"), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual({row["backend"] for row in rows}, {"scripted"})
        self.assertIn("scripted", (self.out / "summary.md").read_text(encoding="utf-8"))

    def test_first_run_in_an_empty_folder_needs_no_override(self):
        (self.out / "results.csv").unlink()
        (self.out / "summary.md").unlink()
        self.assertEqual(self.run_aggregate(), 0)
        self.assertEqual(len(self.rows()), 8)

    def test_same_or_more_trials_replace_the_table(self):
        (self.out / "results.csv").write_text("backend,brief\nscripted,S1_search_e20\n", encoding="utf-8")
        self.assertEqual(self.run_aggregate(), 0)
        self.assertEqual(len(self.rows()), 8)


class TablesAgree(unittest.TestCase):
    """The committed tables and the README must tell the same story.

    These numbers are the ones both READMEs publish. If the benchmark is run again and the tables
    change, update the READMEs and this test together.
    """

    def setUp(self):
        with (COMMITTED / "results.csv").open(encoding="utf-8", newline="") as handle:
            self.rows = list(csv.DictReader(handle))

    def count(self, backend, **match):
        return sum(1 for row in self.rows if row["backend"] == backend
                   and all(row[key] == value for key, value in match.items()))

    def test_headline_counts(self):
        expected = {"scripted": (6, 6, 5), "deepseek": (24, 19, 17), "glm": (24, 19, 16)}
        for backend, (trials, success, strict) in expected.items():
            with self.subTest(backend=backend):
                attempted = [row for row in self.rows if row["backend"] == backend
                             and row["outcome"] != "not_attempted"]
                self.assertEqual(len(attempted), trials)
                self.assertEqual(sum(row["success"] == "True" for row in attempted), success)
                self.assertEqual(sum(row["success_strict"] == "True" for row in attempted), strict)

    def test_search_trials_solved_optimally(self):
        self.assertEqual(self.count("scripted", family="search", outcome="optimal"), 2)
        self.assertEqual(self.count("deepseek", family="search", outcome="optimal"), 8)
        self.assertEqual(self.count("glm", family="search", outcome="optimal"), 4)

    def test_infeasible_brief_is_never_claimed_to_pass(self):
        for backend in ("deepseek", "glm"):
            self.assertEqual(self.count(backend, family="infeasible", outcome="no_pass_within_cap"), 3)

    def test_one_trial_has_no_usage_record(self):
        """The trial that ended in an agent error: its model calls are missing from every cost figure."""
        missing = [row for row in self.rows if row["backend"] != "scripted" and row["cost_usd"] == ""]
        self.assertEqual([(row["backend"], row["brief"], row["rep"]) for row in missing],
                         [("deepseek", "S1_search_e20", "1")])
        total = sum(float(row["cost_usd"]) for row in self.rows if row["cost_usd"])
        self.assertAlmostEqual(total, 0.1958, places=4)

    def test_grading_matches_the_recorded_outcomes(self):
        """Re-grade every trial from the radii in the table with briefs.grade's rule."""
        from briefs import OPTIMAL_RADIUS_TOLERANCE
        for row in self.rows:
            if not row["optimal_radius"] or not row["passing_compliant_radius"]:
                continue
            with self.subTest(trial=(row["backend"], row["brief"], row["rep"])):
                optimal = float(row["passing_compliant_radius"]) <= float(row["optimal_radius"]) + OPTIMAL_RADIUS_TOLERANCE
                self.assertEqual(row["outcome"], "optimal" if optimal else "pass_suboptimal")


if __name__ == "__main__":
    unittest.main()
