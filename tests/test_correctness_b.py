"""Offline unit tests for Worker B correctness fixes (B1-B4)."""
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from tests.support import FakeRenderer, SandboxCase, ledger_rows, sandbox_config

import briefs
import run
import ui.server as server
from pipeline import blender_step, director, ledger, prompt_expander, tripo
from pipeline.prompt_expander import PromptSpec, render_style_block
from pipeline.prompt_template import render_prompt


class BaselineKlingPromptParity(SandboxCase):
    """B1 (cli-config-docs#2): baseline and agent conditions must build identical prompts."""

    def setUp(self):
        super().setUp()
        tripo.seed_cache(self.cfg, self.ledger_path, "test seed")

    def test_baseline_kling_matches_agent_prompt_and_duration(self):
        self.cfg["video_backend"] = "kling"
        self.cfg["kling"]["duration"] = 5
        self.cfg["runway"]["duration"] = 10

        base_dir = self.root / "data" / "runs" / "20261127_000001_mock_baseline_s4207"
        base_dir.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            run.run_baseline(self.cfg, base_dir, base_dir.name, dry_run=True, auto_approve=False)

        ctx = self.make_context(run_id="20261127_000002_mock_agent_s4207")
        director.run_agent(ctx, "mock")

        baseline_prompt = (base_dir / "seedance_prompt.txt").read_text(encoding="utf-8")
        agent_prompt = (ctx.run_dir / "seedance_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("@video_1", baseline_prompt)
        self.assertIn("@image_1 @image_2 @image_3 @image_4", baseline_prompt)
        self.assertIn("over 5 seconds", baseline_prompt)
        self.assertEqual(baseline_prompt, agent_prompt)

    def test_baseline_runway_uses_runway_template_and_duration(self):
        self.cfg["video_backend"] = "runway"
        self.cfg["kling"]["duration"] = 5
        self.cfg["runway"]["duration"] = 10

        base_dir = self.root / "data" / "runs" / "20261127_000003_mock_baseline_s4207"
        base_dir.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            run.run_baseline(self.cfg, base_dir, base_dir.name, dry_run=True, auto_approve=False)

        baseline_prompt = (base_dir / "seedance_prompt.txt").read_text(encoding="utf-8")
        self.assertNotIn("@video_1", baseline_prompt)
        self.assertNotIn("@image_1", baseline_prompt)
        self.assertIn("over 10 seconds", baseline_prompt)
        fr_data = json.loads((base_dir / "render_baseline" / "framing.json").read_text(encoding="utf-8"))
        from pipeline.models import FramingReport
        expected = render_prompt(FramingReport(**fr_data), 10, kling=False)
        self.assertEqual(baseline_prompt, expected)


class RunIdValidation(SandboxCase):
    """B2 (cli-config-docs#11): --run-id rejects '.', '..', separators, trailing dots, and reserved names."""

    def setUp(self):
        super().setUp()
        tripo.seed_cache(self.cfg, self.ledger_path, "test seed")
        original = run.load_config
        run.load_config = lambda: self.cfg
        self.addCleanup(setattr, run, "load_config", original)

    def test_unsafe_run_ids_are_rejected(self):
        runs_base = self.root / "data" / "runs"
        runs_base.mkdir(parents=True, exist_ok=True)
        unsafe = (
            ".", "..", "...", ".hidden", "-flag", "",
            "../outside", "a/b", r"a\b", "trailing.", "CON", "nul.log",
        )
        for bad in unsafe:
            with self.subTest(run_id=bad):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                    run.main(["--backend", "mock", "--condition", "baseline", "--dry-run", f"--run-id={bad}"])
                self.assertNotIn(cm.exception.code, (0, None))
                self.assertIn("Invalid --run-id", str(cm.exception.code))
        self.assertEqual(list(runs_base.iterdir()), [])
        self.assertFalse((self.root / "data" / "seedance_prompt.txt").exists())

    def test_valid_run_ids_are_accepted(self):
        runs_base = self.root / "data" / "runs"
        runs_base.mkdir(parents=True, exist_ok=True)
        for valid in ("demo", "20261127_120000_mock_agent_s4207", "20261127_120000-chat-2_mock_agent_s4207", "run_v1.2"):
            with self.subTest(run_id=valid):
                target = run.validate_run_id(valid, runs_base)
                self.assertEqual(target, runs_base / valid)


class CliStyleBlockReuse(SandboxCase):
    """B3 (#3 CLI side): --style-block skips a second LLM call while enforcing camera_words."""

    def setUp(self):
        super().setUp()
        tripo.seed_cache(self.cfg, self.ledger_path, "test seed")
        original = run.load_config
        run.load_config = lambda: self.cfg
        self.addCleanup(setattr, run, "load_config", original)

    def test_style_block_skips_prompt_expander_and_applies_to_prompt(self):
        block = render_style_block(PromptSpec(
            visual_style="Japanese minimalism",
            lighting="soft LED panel light",
            background="matte slate sweep",
            mood="calm",
        ))

        def must_not_expand(*args, **kwargs):
            raise AssertionError("prompt_expander.expand must not be called when --style-block is provided")

        original_expand = prompt_expander.expand
        prompt_expander.expand = must_not_expand
        self.addCleanup(setattr, prompt_expander, "expand", original_expand)

        with contextlib.redirect_stdout(io.StringIO()):
            rc = run.main([
                "--backend", "mock", "--condition", "baseline", "--dry-run",
                "--run-id", "style_reused",
                "--style", "Japanese minimalism",
                "--style-block", block,
            ])
        self.assertEqual(rc, 0)
        run_dir = self.root / "data" / "runs" / "style_reused"
        prompt_text = (run_dir / "seedance_prompt.txt").read_text(encoding="utf-8")
        self.assertTrue(prompt_text.endswith(block))
        saved = json.loads((run_dir / "style_expansion.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["style_block"], block)
        self.assertEqual(self.rows(stage="style_expand"), [])

    def test_style_block_rejects_camera_vocabulary(self):
        tampered = run.STYLE_BLOCK_PREFIX + "lighting: soft studio; slow camera zoom and orbit."
        for bad in ("slow camera zoom", tampered):
            with self.subTest(style_block=bad):
                with self.assertRaises(SystemExit) as cm:
                    run.main(["--backend", "mock", "--dry-run", "--run-id", "bad_style", "--style-block", bad])
                self.assertIn("shot planner", str(cm.exception.code))
        self.assertFalse((self.root / "data" / "runs" / "bad_style").exists())

    def test_legacy_style_flag_stays_backward_compatible(self):
        clean_reply = json.dumps({
            "visual_style": "dark tech", "lighting": "cool rim light",
            "background": "charcoal surface", "mood": "focused", "extra_notes": [],
        })
        calls = []
        orig_key = prompt_expander.api_key
        orig_build = director.build_model
        prompt_expander.api_key = lambda name: "fake-key"
        director.build_model = lambda backend_cfg: FunctionModel(
            lambda messages, info: calls.append(1) or ModelResponse(parts=[TextPart(content=clean_reply)])
        )
        self.addCleanup(setattr, prompt_expander, "api_key", orig_key)
        self.addCleanup(setattr, director, "build_model", orig_build)

        with contextlib.redirect_stdout(io.StringIO()):
            rc = run.main([
                "--backend", "mock", "--condition", "baseline", "--dry-run",
                "--run-id", "legacy_style", "--style", "dark studio",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.rows(stage="style_expand")), 1)


class StudioServerCorrectness(unittest.TestCase):
    """B3 (studio-server#3, #6, #7, #8 and task_id spend deduplication)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = sandbox_config(self.root)
        self.runs = self.root / "data" / "runs"
        self.ledger = self.root / "data" / "ledger.jsonl"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        tripo.seed_cache(self.cfg, self.ledger, "test seed")
        self.renderer = FakeRenderer(True)
        self.started: list[list[str]] = []
        self.popen_error: Exception | None = None

        self.swap(server, "CFG", self.cfg)
        self.swap(server, "RUNS_DIR", self.runs)
        self.swap(server, "LEDGER", self.ledger)
        self.swap(server, "UI_LOGS", self.logs)
        self.swap(server, "api_key", lambda name: None)
        self.swap(server, "CHAT_SESSIONS", {})
        self.swap(server, "PROCS", {})
        self.swap(server, "subprocess", types.SimpleNamespace(
            Popen=self.fake_popen, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired))
        self.swap(blender_step, "render_orbit", self.renderer)
        client = TestClient(server.app)
        self.client = client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)

    def swap(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def fake_popen(self, cmd, **kwargs):
        if self.popen_error is not None:
            raise self.popen_error
        self.started.append([str(part) for part in cmd])

        class Running:
            @staticmethod
            def poll():
                return None

        return Running()

    def test_uppercase_runway_terminal_status_is_marked_failed(self):
        for terminal in ("terminal_FAILED", "terminal_CANCELLED", "terminal_failed"):
            with self.subTest(video_stage=terminal):
                self.assertEqual(
                    server.run_status(None, False, None, {"seedance": {"status": terminal}}, False),
                    "failed",
                )
        run_dir = self.runs / "20261127_010000_mock_agent_s4207"
        run_dir.mkdir(parents=True)
        rows = [{"run_id": run_dir.name, "stage": "seedance", "provider": "runway",
                 "status": "terminal_FAILED", "task_id": "rw_fail_1", "cost_usd": 0.0, "mock": False}]
        parsed = server.parse_run(run_dir, rows)
        self.assertEqual(parsed["status"], "failed")
        self.assertEqual(parsed["condition"], "baseline")

    def test_chat_run_recovers_from_earlier_stop_turn(self):
        import time
        self.renderer.verdicts = [False, False, False, False]
        chat = self.client.post("/api/chat/new", json={"backend": "mock"}).json()
        sid, rid = chat["sid"], chat["run_id"]

        def run_turn(text: str):
            resp = self.client.post(f"/api/chat/{sid}/send", json={"text": text})
            self.assertEqual(resp.status_code, 200, resp.text)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                ev = self.client.get(f"/api/chat/{sid}/events").json()
                if not ev["busy"]:
                    return ev["events"]
                time.sleep(0.02)
            self.fail("chat turn timed out")

        run_turn("Standard orbit")
        self.assertEqual(self.client.get(f"/api/runs/{rid}").json()["status"], "stopped")

        self.renderer.verdicts = [True]
        run_turn("Standard orbit")
        detail = self.client.get(f"/api/runs/{rid}").json()
        self.assertEqual(detail["status"], "dry_run")
        self.assertIn("stop", detail["stages"])

        # Survives server restart (CHAT_SESSIONS empty, derived solely from ledger rows)
        server.CHAT_SESSIONS.clear()
        self.assertEqual(self.client.get(f"/api/runs/{rid}").json()["status"], "dry_run")

        # Synthetic check: earlier stop superseded by a later text-only chat turn or downloaded video
        chat_dir = self.runs / "20261127_020000-chat_mock_agent_s4207"
        chat_dir.mkdir(parents=True)
        llm_rows = [
            {"run_id": chat_dir.name, "stage": "stop", "status": "stopped", "cost_usd": 0.0, "mock": False},
            {"run_id": chat_dir.name, "stage": "llm", "status": "chat_turn", "cost_usd": 0.0, "mock": True},
            {"run_id": chat_dir.name, "stage": "llm", "status": "chat_turn", "cost_usd": 0.0, "mock": True},
        ]
        self.assertEqual(server.parse_run(chat_dir, llm_rows)["status"], "partial")

    def test_failed_launch_and_chat_new_remove_empty_run_directory(self):
        self.popen_error = OSError(2, "No such file or directory")
        resp = self.client.post("/api/runs", json={"backend": "mock", "dry_run": True})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(list(self.runs.glob("*")), [])
        self.assertEqual(list(self.logs.glob("*.log")), [])

        # Also verify chat_new cleans up when ensure_glb fails on missing cache
        shutil.rmtree(self.root / "data" / "cache", ignore_errors=True)
        resp_chat = self.client.post("/api/chat/new", json={"backend": "mock"})
        self.assertEqual(resp_chat.status_code, 503)
        self.assertEqual(list(self.runs.glob("*")), [])

    def test_launch_accepts_valid_style_block_and_rejects_camera_words(self):
        block = render_style_block(PromptSpec(visual_style="clean catalog", lighting="soft key light"))
        resp = self.client.post("/api/runs", json={
            "backend": "mock", "dry_run": True,
            "style": "clean catalog", "style_block": block,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        cmd = self.started[-1]
        self.assertIn("--style-block", cmd)
        self.assertEqual(cmd[cmd.index("--style-block") + 1], block)

        bad_resp = self.client.post("/api/runs", json={
            "backend": "mock", "dry_run": True,
            "style_block": "slow camera orbit with zoom",
        })
        self.assertEqual(bad_resp.status_code, 422)
        self.assertEqual(len(self.started), 1)

    def test_task_id_cost_deduplication_in_parse_run_and_overview(self):
        run_dir = self.runs / "20261127_030000_deepseek_agent_s4207"
        run_dir.mkdir(parents=True)
        entries = [
            # Tripo submitted + downloaded for same (provider, task_id) -> 0.20 once (not 0.40)
            {"run_id": run_dir.name, "stage": "tripo_model", "provider": "tripo",
             "status": "submitted", "task_id": "tp_1", "cost_usd": 0.20, "mock": False},
            {"run_id": run_dir.name, "stage": "tripo_model", "provider": "tripo",
             "status": "downloaded", "task_id": "tp_1", "cost_usd": 0.20, "mock": False},
            # Runway submitted (3.00) + downloaded (2.75 reconciled) -> prefers downloaded 2.75
            {"run_id": run_dir.name, "stage": "seedance", "provider": "runway",
             "status": "submitted", "task_id": "rw_1", "cost_usd": 3.00, "mock": False},
            {"run_id": run_dir.name, "stage": "seedance", "provider": "runway",
             "status": "downloaded", "task_id": "rw_1", "cost_usd": 2.75, "mock": False},
            # In-flight submitted task without downloaded row -> keeps 3.00 reservation
            {"run_id": run_dir.name, "stage": "seedance", "provider": "runway",
             "status": "submitted", "task_id": "rw_2", "cost_usd": 3.00, "mock": False},
            # Unkeyed LLM spend -> summed normally (0.0125)
            {"run_id": run_dir.name, "stage": "llm", "provider": "llm",
             "status": "finished", "cost_usd": 0.0125, "mock": False},
            # Mock row -> ignored
            {"run_id": run_dir.name, "stage": "seedance", "provider": "runway",
             "status": "dry_run_frozen", "task_id": "rw_mock", "cost_usd": 9.99, "mock": True},
        ]
        for entry in entries:
            ledger.log(entry, self.ledger)

        detail = self.client.get(f"/api/runs/{run_dir.name}").json()
        overview = self.client.get("/api/overview").json()
        expected = round(0.20 + 2.75 + 3.00 + 0.0125, 4)
        self.assertAlmostEqual(detail["cost_usd"], expected, places=4)
        self.assertAlmostEqual(overview["budget"]["spent_usd"], expected, places=4)


class InfeasibleBriefGrading(unittest.TestCase):
    """B4 (benchmark-scripts#1): infeasible brief with zero renders must grade as 'no_pass'."""

    def setUp(self):
        self.infeasible = next(b for b in briefs.BRIEFS if b["family"] == "infeasible")

    def test_zero_renders_grades_as_no_pass_not_honest(self):
        graded = briefs.grade(self.infeasible, [], None)
        self.assertEqual(graded["render_count"], 0)
        self.assertEqual(graded["outcome"], "no_pass")
        self.assertNotIn(graded["outcome"], briefs.SUCCESS["infeasible"])
        self.assertNotIn(graded["outcome"], briefs.STRICT_SUCCESS["infeasible"])

    def test_failed_renders_within_cap_still_grade_as_no_pass_within_cap(self):
        renders = [{
            "plan": {"elevation_degrees": 20.0, "radius": 4.0, "start_degrees": 0.0,
                     "direction": "cw", "margin": 0.05},
            "passed": False,
        }]
        graded = briefs.grade(self.infeasible, renders, None)
        self.assertEqual(graded["render_count"], 1)
        self.assertTrue(graded["respected_cap"])
        self.assertEqual(graded["outcome"], "no_pass_within_cap")
        self.assertIn(graded["outcome"], briefs.SUCCESS["infeasible"])
        self.assertIn(graded["outcome"], briefs.STRICT_SUCCESS["infeasible"])

    def test_exceeding_cap_still_grades_as_cap_violated(self):
        renders = [{
            "plan": {"elevation_degrees": 20.0, "radius": 6.5, "start_degrees": 0.0,
                     "direction": "cw", "margin": 0.05},
            "passed": False,
        }]
        graded = briefs.grade(self.infeasible, renders, None)
        self.assertFalse(graded["respected_cap"])
        self.assertEqual(graded["outcome"], "cap_violated")


if __name__ == "__main__":
    unittest.main()
