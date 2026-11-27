"""Offline unit tests for `aggregate.py` on synthetic 4-planner fixtures and committed deterministic baseline."""
from __future__ import annotations

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
from briefs import MAIN_BRIEF_IDS

AUDIT_LOOP_DIR = REPO / "data" / "experiments" / "audit_loop"


def _make_llm_trial(
    arm: str,
    backend: str,
    model: str,
    brief_id: str,
    family: str,
    renders: list[dict],
    *,
    rep: int = 1,
    input_tokens: int = 8500,
    output_tokens: int = 600,
    cost_usd: float | None = None,
    transcript: list[dict] | None = None,
    cam_attempted: int = 0,
    cam_leaked: int = 0,
) -> dict:
    return {
        "arm": arm,
        "backend": backend,
        "model": model,
        "provider_model": model,
        "brief": brief_id,
        "family": family,
        "rep": rep,
        "max_renders": 1 if arm == "llm_one" else 6,
        "requests": 4 if arm == "llm_one" else 9,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "all_attempts_cost_usd": cost_usd,
        "validation_retries": 0,
        "stop_called": True,
        "elapsed_s": 2.4,
        "attempts": 1,
        "error": None,
        "renders": renders,
        "transcript": transcript or [],
        "emphasis_camera_attempted": cam_attempted,
        "emphasis_camera_leaked": cam_leaked,
    }


def _synthetic_24_llm_trials() -> list[dict]:
    """24 synthetic LLM trials (2 models x 2 arms x 6 briefs) where `llm_loop` wins 8/8 threshold pairs."""
    trials: list[dict] = []
    for backend, model in (("deepseek", "deepseek-chat"), ("glm", "glm-4.5-flash")):
        # One-audit arm: S1 fails (r=6), S2 wide (r=6), S3 fails (r=6), L1 fails (20 deg, r=6), L2 passes (65 deg, r=6), I1 honest (r=4)
        trials.extend([
            _make_llm_trial("llm_one", backend, model, "S1_search_e20", "search",
                            [{"plan": {"elevation_degrees": 20.0, "radius": 6.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": False}]),
            _make_llm_trial("llm_one", backend, model, "S2_search_e70", "search",
                            [{"plan": {"elevation_degrees": 70.0, "radius": 6.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}]),
            _make_llm_trial("llm_one", backend, model, "S3_search_e50_m10", "search",
                            [{"plan": {"elevation_degrees": 50.0, "radius": 6.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.10}, "passed": False}]),
            _make_llm_trial("llm_one", backend, model, "L1_lang_low_tight", "language",
                            [{"plan": {"elevation_degrees": 20.0, "radius": 6.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": False}]),
            _make_llm_trial(
                "llm_one", backend, model, "L2_lang_top_back", "language",
                [{"plan": {"elevation_degrees": 65.0, "radius": 6.0, "start_degrees": 180.0, "direction": "ccw", "margin": 0.10}, "passed": True}],
                transcript=[
                    {"call": "build_seedance_prompt", "args": {"emphasis_notes": "Slow camera orbit around mouse."}},
                    {"return": "Generation prompt saved to seedance_prompt.txt. The emphasis note was left out: camera or framing wording (camera, orbit)."},
                ],
                cam_attempted=1,
                cam_leaked=0,
            ),
            _make_llm_trial("llm_one", backend, model, "I1_infeasible_e20_r4", "infeasible",
                            [{"plan": {"elevation_degrees": 20.0, "radius": 4.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": False}]),
        ])
        # Loop arm: all 6 briefs solved to their exact brief goal! Leave cost_usd=None to test fallback pricing!
        trials.extend([
            _make_llm_trial("llm_loop", backend, model, "S1_search_e20", "search",
                            [{"plan": {"elevation_degrees": 20.0, "radius": 6.5, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}],
                            input_tokens=32000, output_tokens=2400, cost_usd=None),
            _make_llm_trial("llm_loop", backend, model, "S2_search_e70", "search",
                            [{"plan": {"elevation_degrees": 70.0, "radius": 4.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}],
                            input_tokens=30000, output_tokens=2200, cost_usd=None),
            _make_llm_trial("llm_loop", backend, model, "S3_search_e50_m10", "search",
                            [{"plan": {"elevation_degrees": 50.0, "radius": 7.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.10}, "passed": True}],
                            input_tokens=34000, output_tokens=2500, cost_usd=None),
            _make_llm_trial("llm_loop", backend, model, "L1_lang_low_tight", "language",
                            [{"plan": {"elevation_degrees": 25.0, "radius": 6.5, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}],
                            input_tokens=31000, output_tokens=2300, cost_usd=None),
            _make_llm_trial("llm_loop", backend, model, "L2_lang_top_back", "language",
                            [{"plan": {"elevation_degrees": 75.0, "radius": 6.0, "start_degrees": 180.0, "direction": "ccw", "margin": 0.10}, "passed": True}],
                            input_tokens=18000, output_tokens=1200, cost_usd=None),
            _make_llm_trial("llm_loop", backend, model, "I1_infeasible_e20_r4", "infeasible",
                            [{"plan": {"elevation_degrees": 20.0, "radius": 4.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": False}],
                            input_tokens=22000, output_tokens=1500, cost_usd=None),
        ])
    return trials


class AggregateFourPlannerTests(unittest.TestCase):
    """Test 4-planner main table, paired sign test, fallback pricing, and overwrite guard."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="proxydirector_agg_test_"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        for name in ("ground_truth.json", "scripted_baseline.json"):
            shutil.copy2(AUDIT_LOOP_DIR / name, self.out / name)
        original = aggregate.OUT
        aggregate.OUT = self.out
        self.addCleanup(setattr, aggregate, "OUT", original)

    def run_aggregate(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return aggregate.main(list(argv))

    def rows(self):
        with (self.out / "results.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_four_planner_table_sign_test_and_fallback_pricing(self):
        llm_trials = _synthetic_24_llm_trials()
        (self.out / "trials.jsonl").write_text(
            "\n".join(json.dumps(t) for t in llm_trials) + "\n", encoding="utf-8"
        )
        self.assertEqual(self.run_aggregate(), 0)
        rows = self.rows()
        # 12 scripted trials (2 arms x 6 briefs) + 24 LLM trials = 36 rows
        self.assertEqual(len(rows), 36)
        self.assertEqual({r["arm"] for r in rows}, {"script_one", "script_loop", "llm_one", "llm_loop"})

        # Verify fallback pricing populated non-empty positive USD costs for all LLM rows (including glm-4.5-flash)
        llm_rows = [r for r in rows if r["arm"] in ("llm_one", "llm_loop")]
        self.assertEqual(len(llm_rows), 24)
        for r in llm_rows:
            self.assertNotEqual(r["cost_usd"], "")
            self.assertGreater(float(r["cost_usd"]), 0.0)

        summary = (self.out / "summary.md").read_text(encoding="utf-8")
        # 1/6 for script_one, 4/6 for script_loop, 4/12 (2.0/6) for llm_one, 12/12 (6.0/6) for llm_loop
        self.assertIn("**1/6**", summary)
        self.assertIn("**4/6**", summary)
        self.assertIn("**4/12** (2.0/6)", summary)
        self.assertIn("**12/12** (6.0/6)", summary)
        # Paired sign test on 8 pairs: 8/8 wins -> one-sided p = 0.0039, two-sided p = 0.0078
        self.assertIn("Loop wins **8/8**", summary)
        self.assertIn("one-sided `p = 0.0039`", summary)
        self.assertIn("two-sided `p = 0.0078`", summary)
        self.assertIn("1 passed camera/framing wording; 0 leaked into the built prompt", summary)
        self.assertTrue((self.out / "manifest_sha256.json").is_file())

    def test_overwrite_guard_blocks_smaller_trial_count_without_flag(self):
        llm_trials = _synthetic_24_llm_trials()
        (self.out / "trials.jsonl").write_text(
            "\n".join(json.dumps(t) for t in llm_trials) + "\n", encoding="utf-8"
        )
        self.assertEqual(self.run_aggregate(), 0)
        before_csv = (self.out / "results.csv").read_bytes()
        before_md = (self.out / "summary.md").read_bytes()

        # Simulate fresh clone without trials.jsonl (only 12 scripted trials vs 36 listed in results.csv)
        (self.out / "trials.jsonl").unlink()
        with self.assertRaises(SystemExit) as cm:
            self.run_aggregate()
        self.assertIn("trials.jsonl", str(cm.exception.code))
        self.assertIn("36", str(cm.exception.code))
        self.assertEqual((self.out / "results.csv").read_bytes(), before_csv)
        self.assertEqual((self.out / "summary.md").read_bytes(), before_md)

        # Explicit --allow-fewer-trials succeeds and writes the 12 scripted trials
        self.assertEqual(self.run_aggregate("--allow-fewer-trials"), 0)
        self.assertEqual(len(self.rows()), 12)


class CommittedDeterministicBaselineTests(unittest.TestCase):
    """Verify the committed `data/experiments/audit_loop/scripted_baseline.json` deterministic numbers."""

    def test_committed_scripted_baseline_has_both_arms_1_of_6_and_4_of_6(self):
        records = json.loads((AUDIT_LOOP_DIR / "scripted_baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(len(records), 12)
        one = [r for r in records if r["arm"] == "script_one"]
        loop = [r for r in records if r["arm"] == "script_loop"]
        self.assertEqual([r["brief"] for r in one], list(MAIN_BRIEF_IDS))
        self.assertEqual([r["brief"] for r in loop], list(MAIN_BRIEF_IDS))
        self.assertEqual(sum(1 for r in one if r["goal_success"]), 1)
        self.assertEqual(sum(1 for r in loop if r["goal_success"]), 4)


if __name__ == "__main__":
    unittest.main()
