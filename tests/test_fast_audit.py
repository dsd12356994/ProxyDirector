"""Offline unit tests for `fast_audit.py`, Part A geometry sweep, harness safeguards, preflight, and scripted arms."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from tests.support import REPO

import briefs
import fast_audit
import harness
import part_a_sweep
import preflight
import run_scripted_baseline
from make_fixed_lens_worker import FIXED_LENS_MM
from pipeline import blender_step, director
from pipeline.models import ShotPlan

GLB = REPO / "assets" / "seed_tripo" / "model.glb"
AUDIT_LOOP_DIR = REPO / "data" / "experiments" / "audit_loop"


class FastAuditDirectMeshChecks(unittest.TestCase):
    """Direct geometric checks against `assets/seed_tripo/model.glb` (zero archive dependency)."""

    def test_fitted_lens_on_baseline_plan_reproduces_fixed_lens_constant(self):
        baseline_plan = {
            "elevation_degrees": 50.0,
            "radius": 6.0,
            "start_degrees": 0.0,
            "direction": "cw",
            "margin": 0.05,
        }
        report = fast_audit.audit_plan(GLB, baseline_plan, fixed_lens_mm=0.0)
        self.assertTrue(report["passed"])
        self.assertAlmostEqual(report["lens_mm"], 68.17216, places=5)
        self.assertAlmostEqual(report["lens_mm"], FIXED_LENS_MM, places=5)

    def test_convex_hull_pruning_matches_all_49530_vertices_exactly(self):
        sample_plans = (
            {"elevation_degrees": 20.0, "radius": 6.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05},
            {"elevation_degrees": 70.0, "radius": 4.0, "start_degrees": 90.0, "direction": "ccw", "margin": 0.05},
            {"elevation_degrees": 50.0, "radius": 7.0, "start_degrees": 180.0, "direction": "cw", "margin": 0.10},
            {"elevation_degrees": 85.0, "radius": 6.0, "start_degrees": 90.0, "direction": "ccw", "margin": 0.03},
        )
        for p in sample_plans:
            with self.subTest(plan=p):
                full = fast_audit.audit_plan(GLB, p, use_all_vertices=True)
                pruned = fast_audit.audit_plan(GLB, p, use_all_vertices=False)
                self.assertEqual(full["vertices"], 49530)
                self.assertEqual(pruned["vertices"], 49530)
                self.assertEqual(full["passed"], pruned["passed"])
                self.assertEqual(full["lens_mm"], pruned["lens_mm"])
                self.assertEqual(full["shift"], pruned["shift"])
                self.assertEqual(full["violations"], pruned["violations"])
                for a, b in zip(full["min_margins_lbrt"], pruned["min_margins_lbrt"]):
                    self.assertAlmostEqual(a, b, places=12)

    def test_monotonicity_in_radius_across_elevations(self):
        radii = [round(3.0 + 0.5 * i, 1) for i in range(19)]
        for elev in (10.0, 20.0, 35.0, 50.0, 70.0, 85.0):
            seen_pass = False
            prev_margin = -1e9
            for r in radii:
                rep = fast_audit.audit_plan(
                    GLB,
                    {"elevation_degrees": elev, "radius": r, "start_degrees": 0.0, "direction": "cw", "margin": 0.05},
                )
                min_m = min(rep["min_margins_lbrt"])
                self.assertGreaterEqual(min_m + 1e-9, prev_margin, f"Non-monotone margin at {elev}° r={r}")
                prev_margin = min_m
                if rep["passed"]:
                    seen_pass = True
                else:
                    self.assertFalse(seen_pass, f"Pass->fail reversal at {elev}° r={r}")

    def test_side_sequence_all_8_cardinal_combinations(self):
        expected = {
            (0, "cw"): ["front", "left", "back", "right"],
            (90, "cw"): ["right", "front", "left", "back"],
            (180, "cw"): ["back", "right", "front", "left"],
            (270, "cw"): ["left", "back", "right", "front"],
            (0, "ccw"): ["front", "right", "back", "left"],
            (90, "ccw"): ["right", "back", "left", "front"],
            (180, "ccw"): ["back", "left", "front", "right"],
            (270, "ccw"): ["left", "front", "right", "back"],
        }
        self.assertEqual(len(expected), 8)
        for (start, direction), seq in expected.items():
            with self.subTest(start=start, direction=direction):
                self.assertEqual(fast_audit.side_sequence(start, direction), seq)

    def test_part_a_sweep_artifacts_on_disk(self):
        geom = json.loads((AUDIT_LOOP_DIR / "geometry_map.json").read_text(encoding="utf-8"))
        summary = geom["summary"]
        self.assertEqual(summary["fitted_lens_passed"], 304)
        self.assertEqual(summary["fitted_lens_total"], 304)
        self.assertEqual(summary["fixed_lens_passed"], 223)
        self.assertEqual(summary["fixed_lens_total"], 304)
        self.assertEqual(summary["r6_passing_elevation_count"], 8)
        self.assertEqual(summary["r6_tightest_elevation_count"], 2)
        self.assertTrue(summary["monotone_in_radius_all_16_elevations"])

        svg = (AUDIT_LOOP_DIR / "min_radius_by_elevation.svg").read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("223/304", svg)
        self.assertIn("r = 6.0", svg)


class HarnessAndBriefProtocolTests(unittest.TestCase):
    """Verify 2x2 harness rules, neutral context, message capture, pre-audit retry, and leakage detection."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proxydirector_bench_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_render_note_and_limit_messages_for_1_vs_6_audits(self):
        note1 = briefs.render_note(1)
        note6 = briefs.render_note(6)
        self.assertIn("You have exactly one local render; your first plan is final.", note1)
        self.assertIn("You have at most 6 local renders.", note6)

        orig_file = blender_step.__file__
        orig_render = blender_step.render_orbit

        d1 = self.tmp / "one_audit"
        d1.mkdir()
        _, tools1, lim1 = harness.prepare(d1, max_renders=1)
        self.assertEqual(lim1, 1)
        self.assertEqual(blender_step.__file__, orig_file)
        self.assertIs(blender_step.render_orbit, orig_render)

        ctx_json = json.loads(tools1["get_run_context"]())
        self.assertNotIn("baseline_plan", ctx_json)
        self.assertNotIn("about 5%", ctx_json["shot_requirements"])
        self.assertIn("Follow the user's shot brief", ctx_json["shot_requirements"])

        first = tools1["plan_orbit"](ShotPlan(elevation_degrees=20.0, radius=6.0))
        self.assertIn("Framing failed", first)
        second = tools1["plan_orbit"](ShotPlan(elevation_degrees=20.0, radius=6.5))
        self.assertEqual(
            second,
            "Render limit reached (1/1). Do not call plan_orbit again; finish using your audited results or report if no plan passed.",
        )
        self.assertNotIn("adjust the plan and render again", second)

    def test_grade_radius_gap_and_infeasible_compliant_requirement(self):
        s2 = briefs.BRIEF_BY_ID["S2_search_e70"]
        wide = briefs.grade(
            s2,
            [{"plan": {"elevation_degrees": 70.0, "radius": 6.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}],
            4.0,
        )
        self.assertEqual(wide["outcome"], "pass_suboptimal")
        self.assertAlmostEqual(wide["radius_gap"], 2.0)
        self.assertFalse(wide["goal_success"])

        tight = briefs.grade(
            s2,
            [{"plan": {"elevation_degrees": 70.0, "radius": 4.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}],
            4.0,
        )
        self.assertEqual(tight["outcome"], "optimal")
        self.assertAlmostEqual(tight["radius_gap"], 0.0)
        self.assertTrue(tight["goal_success"])

        i1 = briefs.BRIEF_BY_ID["I1_infeasible_e20_r4"]
        # Non-compliant elevation (75 deg instead of 17..23) must NOT score no_pass_within_cap
        noncompliant = briefs.grade(
            i1,
            [{"plan": {"elevation_degrees": 75.0, "radius": 4.0, "start_degrees": 0.0, "direction": "cw", "margin": 0.05}, "passed": True}],
            None,
        )
        self.assertEqual(noncompliant["outcome"], "no_pass")
        self.assertFalse(noncompliant["goal_success"])

    def test_capture_messages_and_single_pre_audit_retry_accounting(self):
        calls = {"n": 0}

        def flaky_then_ok(messages, info):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP 429 rate limited before first audit")
            has_tool_ret = any(part.part_kind == "tool-return" for m in messages for part in m.parts)
            if not has_tool_ret:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="plan_orbit", args=json.dumps({"plan": {
                        "elevation_degrees": 85.0, "radius": 6.0, "start_degrees": 90.0, "direction": "ccw", "margin": 0.03
                    }}))],
                    model_name="deepseek-chat",
                    provider_response_id="resp-call-1",
                )
            return ModelResponse(
                parts=[TextPart(content="Final plan: 85 deg, radius 6.0.")],
                model_name="deepseek-chat",
                provider_response_id="resp-final-2",
            )

        orig_build = director.build_model
        director.build_model = lambda cfg: FunctionModel(flaky_then_ok)
        self.addCleanup(setattr, director, "build_model", orig_build)

        run_dir = self.tmp / "c2_retry"
        run_dir.mkdir()
        c2 = briefs.BRIEF_BY_ID["C2_fixed_e85_ccw_90"]
        rec = harness.run_llm_trial("deepseek", c2, run_dir, arm="llm_one", max_renders=1, retry_sleep_s=0.0)
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["attempts"], 2)
        self.assertEqual(rec["provider_model"], "function:flaky_then_ok:")
        self.assertEqual(rec["provider_response_ids"], ["resp-call-1", "resp-final-2"])
        self.assertTrue((self.tmp / "c2_retry_attempt1_failed" / "failed_attempt.json").is_file())
        self.assertTrue((run_dir / "messages.json").is_file())
        self.assertIsNotNone(rec["cost_usd"])

    def test_emphasis_leakage_does_not_false_positive_on_frozen_template_words(self):
        run_dir = self.tmp / "leak_check"
        run_dir.mkdir()
        _, tools, _ = harness.prepare(run_dir, max_renders=6)
        tools["plan_orbit"](ShotPlan(elevation_degrees=50.0, radius=6.0, start_degrees=0.0, direction="cw", margin=0.05))
        # "continuous" and "360°" and "camera" appear in the frozen template itself, while `orbit`/`camera` trigger stripping!
        ret = tools["build_seedance_prompt"](emphasis_notes="continuous 360° camera orbit")
        self.assertIn("The emphasis note was left out", ret)
        transcript = [
            {"call": "build_seedance_prompt", "args": {"emphasis_notes": "continuous 360° camera orbit"}},
            {"return": ret[:400], "tool": "build_seedance_prompt"},
        ]
        attempted, leaked = harness._audit_emphasis_leakage(run_dir, transcript)
        self.assertEqual(attempted, 1)
        self.assertEqual(leaked, 0)

    def test_repo_commit_resolution_works_without_git_in_path(self):
        sha = harness.resolve_repo_commit(REPO)
        self.assertGreaterEqual(len(sha), 7)
        self.assertNotEqual(sha, "unknown")


class ScriptedArmsAndPreflightTests(unittest.TestCase):
    """Verify fair regex-only `script_one` (1/6) and `script_loop` (4/6) arms, plus offline `preflight.py`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proxydirector_script_pf_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_scripted_arms_forbid_spec_access_and_score_1_of_6_vs_4_of_6(self):
        class SpecForbiddenBrief(dict):
            def __getitem__(self, key):
                if key == "spec" and self.forbid_spec:
                    raise AssertionError("run_scripted_baseline accessed brief['spec'] during planning!")
                return super().__getitem__(key)

            def get(self, key, default=None):
                if key == "spec" and self.forbid_spec:
                    raise AssertionError("run_scripted_baseline accessed brief.get('spec') during planning!")
                return super().get(key, default)

        orig_collect = run_scripted_baseline.collect_and_grade

        def unlocked_collect(b, rdir, res, gt):
            b.forbid_spec = False
            return orig_collect(b, rdir, res, gt)

        run_scripted_baseline.collect_and_grade = unlocked_collect
        self.addCleanup(setattr, run_scripted_baseline, "collect_and_grade", orig_collect)

        gt = json.loads((AUDIT_LOOP_DIR / "ground_truth.json").read_text(encoding="utf-8"))["ground_truth"]

        one_res = {}
        loop_res = {}
        for bid in briefs.MAIN_BRIEF_IDS:
            raw = briefs.BRIEF_BY_ID[bid]
            w1 = SpecForbiddenBrief(raw)
            w1.forbid_spec = True
            one_res[bid] = run_scripted_baseline.run_brief(w1, self.tmp, gt, arm="script_one")

            w2 = SpecForbiddenBrief(raw)
            w2.forbid_spec = True
            loop_res[bid] = run_scripted_baseline.run_brief(w2, self.tmp, gt, arm="script_loop")

        self.assertEqual(sum(r["goal_success"] for r in one_res.values()), 1)
        self.assertEqual(one_res["S1_search_e20"]["outcome"], "no_pass")
        self.assertEqual(one_res["S2_search_e70"]["outcome"], "pass_suboptimal")
        self.assertEqual(one_res["S2_search_e70"]["passing_compliant_radius"], 6.0)
        self.assertEqual(one_res["S3_search_e50_m10"]["outcome"], "no_pass")
        self.assertEqual(one_res["L1_lang_low_tight"]["outcome"], "not_attempted")
        self.assertEqual(one_res["L2_lang_top_back"]["outcome"], "not_attempted")
        self.assertEqual(one_res["I1_infeasible_e20_r4"]["outcome"], "no_pass_within_cap")

        self.assertEqual(sum(r["goal_success"] for r in loop_res.values()), 4)
        self.assertEqual((loop_res["S1_search_e20"]["outcome"], loop_res["S1_search_e20"]["passing_compliant_radius"]), ("optimal", 6.5))
        self.assertEqual((loop_res["S2_search_e70"]["outcome"], loop_res["S2_search_e70"]["passing_compliant_radius"]), ("optimal", 4.0))
        self.assertEqual((loop_res["S3_search_e50_m10"]["outcome"], loop_res["S3_search_e50_m10"]["passing_compliant_radius"]), ("optimal", 7.0))
        self.assertEqual(loop_res["L1_lang_low_tight"]["outcome"], "not_attempted")
        self.assertEqual(loop_res["L2_lang_top_back"]["outcome"], "not_attempted")
        self.assertEqual(loop_res["I1_infeasible_e20_r4"]["outcome"], "no_pass_within_cap")

    def test_preflight_glm_endpoint_failover_and_c2_gate_offline(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api.z.ai" in url:
                return httpx.Response(401, json={"error": "invalid international key"})
            if "open.bigmodel.cn" in url:
                return httpx.Response(200, json={"data": [{"id": "embedding-3"}, {"id": "glm-4.5-flash"}]})
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(mock_client.close)

        shutil.copy2(AUDIT_LOOP_DIR / "ground_truth.json", self.tmp / "ground_truth.json")

        def c2_model(messages, info):
            has_ret = any(p.part_kind == "tool-return" for m in messages for p in m.parts)
            if not has_ret:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="plan_orbit", args=json.dumps({"plan": {
                        "elevation_degrees": 85.0, "radius": 6.0, "start_degrees": 90.0, "direction": "ccw", "margin": 0.03
                    }}))],
                    model_name="glm-4.5-flash",
                    provider_response_id="glm-pf-1",
                )
            return ModelResponse(parts=[TextPart(content="Passed at r=6.")], model_name="glm-4.5-flash", provider_response_id="glm-pf-2")

        orig_key = preflight.api_key
        orig_build = director.build_model
        preflight.api_key = lambda name: "fake-glm-key"
        director.build_model = lambda cfg: FunctionModel(c2_model)
        self.addCleanup(setattr, preflight, "api_key", orig_key)
        self.addCleanup(setattr, director, "build_model", orig_build)

        rec = preflight.run_preflight_for_backend("glm", self.tmp, http_client=mock_client)
        self.assertTrue(rec["preflight_passed"])
        self.assertEqual(rec["base_url"], "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(rec["resolved_model"], "glm-4.5-flash")
        self.assertEqual(rec["render_count"], 1)
        self.assertFalse((self.tmp / "trials.jsonl").exists(), "Preflight must never write to trials.jsonl")


if __name__ == "__main__":
    unittest.main()
