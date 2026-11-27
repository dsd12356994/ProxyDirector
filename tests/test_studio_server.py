"""The Studio back end, exercised in process with FastAPI's test client.

The server's data folders are moved to a temporary project, credentials are hidden, Blender is
replaced by a fake renderer and no subprocess is started, so the suite cannot reach a provider.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

from tests.support import REPO, FakeRenderer, ledger_rows, sandbox_config

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import ui.server as server
from pipeline import blender_step, director, tripo
from pipeline.tools import REFUSED


class StudioCase(unittest.TestCase):
    verdicts: tuple = (True,)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        cfg = sandbox_config(self.root)
        self.runs = self.root / "data" / "runs"
        self.ledger = self.root / "data" / "ledger.jsonl"
        logs = self.root / "logs"
        logs.mkdir()
        tripo.seed_cache(cfg, self.ledger, "test seed")
        self.renderer = FakeRenderer(*self.verdicts)
        self.started: list[list[str]] = []
        self.swap(server, "CFG", cfg)
        self.swap(server, "RUNS_DIR", self.runs)
        self.swap(server, "LEDGER", self.ledger)
        self.swap(server, "UI_LOGS", logs)
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
        self.started.append([str(part) for part in cmd])

        class Running:
            @staticmethod
            def poll():
                return None
        return Running()

    def turn(self, sid, text, timeout=30.0, **mode):
        response = self.client.post(f"/api/chat/{sid}/send", json={"text": text, **mode})
        self.assertEqual(response.status_code, 200, response.text)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.client.get(f"/api/chat/{sid}/events").json()
            if not state["busy"]:
                return state["events"]
            time.sleep(0.05)
        self.fail("the chat turn did not finish")

    def new_chat(self, backend="mock"):
        response = self.client.post("/api/chat/new", json={"backend": backend})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class ReadOnlyRoutes(StudioCase):
    def test_health_and_static_shell(self):
        self.assertEqual(self.client.get("/api/health").json()["service"], "proxydirector")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("ProxyDirector", page.text)
        for asset in ("app.js", "workspace.js", "style.css"):
            self.assertEqual(self.client.get(f"/static/{asset}").status_code, 200)

    def test_overview_never_returns_secrets(self):
        overview = self.client.get("/api/overview").json()
        self.assertEqual(overview["backends"]["mock"], {"model": "scripted", "has_key": True})
        self.assertEqual(overview["backends"]["deepseek"]["has_key"], False)
        self.assertEqual(overview["budget"]["runs_total"], 0)
        self.assertEqual(overview["kling"]["credits_per_video"], 8.0)
        self.assertNotIn("api_key", json.dumps(overview).lower())

    def test_kling_balance_is_not_requested_without_a_key(self):
        self.assertEqual(self.client.get("/api/kling/packs").json(), {"packs": [], "remaining": None})

    def test_empty_history(self):
        self.assertEqual(self.client.get("/api/runs").json(), [])
        self.assertEqual(self.client.get("/api/chat/sessions").json(), [])

    def test_frozen_views(self):
        view = self.client.get("/api/views/view_1.png")
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.headers["content-type"], "image/png")
        self.assertEqual(self.client.get("/api/views/view_9.png").status_code, 400)
        self.assertNotEqual(self.client.get("/api/views/..%2Fconfig.yaml").status_code, 200)

    def test_user_guide_download(self):
        guide = self.client.get("/user-guide")
        self.assertEqual(guide.status_code, 200)
        self.assertIn("wordprocessingml", guide.headers["content-type"])


class MediaRoutes(StudioCase):
    def test_only_whitelisted_run_files_are_served(self):
        run = self.runs / "20260101_000000_mock_agent_s4207"
        (run / "render_v1" / "frames").mkdir(parents=True)
        (run / "render_v1" / "reference.mp4").write_bytes(b"clip")
        (run / "render_v1" / "worker_stdout.log").write_text("log", encoding="utf-8")
        (run / "seedance_prompt.txt").write_text("prompt", encoding="utf-8")
        base = f"/api/media/{run.name}"
        self.assertEqual(self.client.get(f"{base}/render_v1/reference.mp4").content, b"clip")
        for path in ("render_v1/worker_stdout.log", "seedance_prompt.txt", "render_v1/..%2F..%2F..%2Fledger.jsonl",
                     "render_v1/%2E%2E/seedance_prompt.txt", "commercial/state.json"):
            with self.subTest(path=path):
                self.assertIn(self.client.get(f"{base}/{path}").status_code, (400, 404))
        self.assertIn(self.client.get("/api/media/%2E%2E/render_v1/reference.mp4").status_code, (400, 404))
        self.assertEqual(self.client.get(f"{base}/commercial/video.mp4").status_code, 404)

    def test_run_detail_stays_inside_the_runs_folder(self):
        self.assertEqual(self.client.get("/api/runs/no-such-run").status_code, 404)
        self.assertEqual(self.client.get("/api/runs/%2E%2E").status_code, 404)


class FramingLab(StudioCase):
    def test_recorded_orbit_matches_the_readme(self):
        """README: 300 encoded frames and one closure pose; at 5% and 1.15x the smallest margin is at 3.60 s."""
        original = self.client.get("/api/framing-lab").json()
        self.assertEqual((original["pose_count"], original["video_frames"]), (301, 300))
        self.assertTrue(original["passed"])
        self.assertEqual(len(self.client.get("/api/framing-lab/bounds").json()), 301)
        pushed = self.client.get("/api/framing-lab", params={"zoom": 1.15, "margin": 0.05}).json()
        self.assertFalse(pushed["passed"])
        self.assertAlmostEqual(pushed["worst"]["time_s"], 3.60, places=2)

    def test_parameters_are_bounded(self):
        for params in ({"zoom": 2}, {"zoom": 0.5}, {"margin": 0.5}, {"frame": 999}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/framing-lab", params=params).status_code, 422)


class CaseReplay(StudioCase):
    def test_bundled_cases_and_batch_counts(self):
        """README: 11 archived clips, 3 baseline, 3 Astra, 3 DeepSeek, 2 GLM."""
        evidence = self.client.get("/api/evidence").json()
        self.assertEqual([case["id"] for case in evidence["cases"]], ["early-tripo", "compact", "baseline", "deepseek"])
        self.assertEqual(evidence["batch"]["total"], 11)
        self.assertEqual({c["label"]: c["count"] for c in evidence["batch"]["conditions"]},
                         {"Fixed baseline": 3, "Astra": 3, "DeepSeek": 3, "GLM": 2})
        for case in evidence["cases"]:
            for kind in ("reference", "output"):
                with self.subTest(case=case["id"], kind=kind):
                    self.assertIn(kind, case["media"])
        clip = self.client.get("/api/evidence/media/baseline/output")
        self.assertEqual(clip.status_code, 200)
        self.assertEqual(clip.headers["content-type"], "video/mp4")
        self.assertEqual(self.client.get("/api/evidence/media/nothing/output").status_code, 404)


class LaunchRun(StudioCase):
    def launch(self, **spec):
        return self.client.post("/api/runs", json={"backend": "mock", **spec})

    def test_rehearsal_run_uses_this_interpreter(self):
        """The launcher used to call .venv/bin/python, which does not exist on Windows."""
        response = self.launch()
        self.assertEqual(response.status_code, 200, response.text)
        command = self.started[-1]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(Path(command[0]).exists())
        self.assertIn("--dry-run", command)
        self.assertNotIn("--auto-approve", command)
        run_id = response.json()["run_id"]
        self.assertIn(run_id, command)
        self.assertTrue((self.runs / run_id).is_dir())
        self.assertTrue((self.root / "logs" / f"{run_id}.log").exists())

    def test_paid_run_needs_the_acknowledgement(self):
        self.assertEqual(self.launch(dry_run=False).status_code, 400)
        self.assertEqual(self.launch(dry_run=True, ack_paid=True).status_code, 400)
        self.assertEqual(self.started, [])
        self.assertEqual(self.launch(dry_run=False, ack_paid=True).status_code, 200)
        self.assertIn("--auto-approve", self.started[-1])
        self.assertNotIn("--dry-run", self.started[-1])

    def test_unknown_choices_are_rejected(self):
        self.assertEqual(self.client.post("/api/runs", json={"backend": "nope"}).status_code, 400)
        self.assertEqual(self.launch(condition="other").status_code, 400)
        self.assertEqual(self.started, [])

    def test_style_idea_is_passed_as_one_argument(self):
        self.launch(style="dark studio; rm -rf / && echo")
        command = self.started[-1]
        self.assertEqual(command[command.index("--style") + 1], "dark studio; rm -rf / && echo")


class DirectorChat(StudioCase):
    def test_preset_turn_renders_audits_and_freezes_a_request(self):
        chat = self.new_chat()
        events = self.turn(chat["sid"], "Standard orbit")
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds[:2], ["user", "mode"])
        self.assertEqual(kinds[-1], "done")
        calls = [event["tool"] for event in events if event["kind"] == "tool_call"]
        self.assertEqual(calls, ["get_run_context", "plan_orbit", "build_seedance_prompt", "submit_seedance"])
        self.assertTrue(all(event["ok"] for event in events if event["kind"] == "tool_result"))
        run_dir = self.runs / chat["run_id"]
        self.assertTrue((run_dir / "commercial" / "frozen_request.json").exists())

        listed = self.client.get("/api/runs").json()
        self.assertEqual([run["id"] for run in listed], [chat["run_id"]])
        self.assertEqual(listed[0]["status"], "dry_run")
        self.assertEqual(self.client.get(listed[0]["video"]).status_code, 200)
        detail = self.client.get(f"/api/runs/{chat['run_id']}").json()
        self.assertTrue(detail["prompt_text"].startswith("Create one continuous photorealistic product video"))
        self.assertEqual(detail["frozen_request"]["provider"], "kling")
        sessions = self.client.get("/api/chat/sessions").json()
        self.assertEqual(sessions[0]["title"], "Standard orbit")
        self.assertTrue(any(row["status"] == "chat_turn" for row in ledger_rows(self.ledger)))

    def test_follow_up_keeps_the_current_elevation(self):
        """README: a counterclockwise follow-up keeps the current elevation."""
        chat = self.new_chat()
        self.turn(chat["sid"], "35° elevation")
        self.turn(chat["sid"], "Counterclockwise orbit")
        first, second = (plan for _, plan in self.renderer.calls)
        self.assertEqual((first.elevation_degrees, first.direction), (35.0, "cw"))
        self.assertEqual((second.elevation_degrees, second.direction), (35.0, "ccw"))

    def test_free_text_with_the_preset_director_explains_the_presets(self):
        chat = self.new_chat()
        events = self.turn(chat["sid"], "make it dramatic")
        self.assertEqual([event["kind"] for event in events], ["user", "mode", "done"])
        self.assertIn("Standard orbit", events[-1]["output"])
        self.assertEqual(self.renderer.calls, [])

    def test_input_validation(self):
        chat = self.new_chat()
        send = f"/api/chat/{chat['sid']}/send"
        self.assertEqual(self.client.post(send, json={"text": "   "}).status_code, 400)
        self.assertEqual(self.client.post(send, json={"text": "x", "dry_run": False}).status_code, 400)
        self.assertEqual(self.client.post(send, json={"text": "x", "dry_run": True, "ack_paid": True}).status_code, 400)
        self.assertEqual(self.client.post("/api/chat/nope/send", json={"text": "x"}).status_code, 404)
        self.assertEqual(self.client.get("/api/chat/nope/events").status_code, 404)
        self.assertEqual(self.client.post("/api/chat/new", json={"backend": "nope"}).status_code, 400)

    def test_gate_refusal_is_shown_as_refused_and_the_turn_survives(self):
        steps = iter([ModelResponse(parts=[ToolCallPart(tool_name="build_seedance_prompt", args="{}")]),
                      ModelResponse(parts=[TextPart(content="I need a passing render first.")])])
        self.swap(director, "build_model", lambda backend_cfg: FunctionModel(lambda messages, info: next(steps)))
        chat = self.new_chat()
        events = self.turn(chat["sid"], "build the prompt now")
        results = [event for event in events if event["kind"] == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["output"].startswith(REFUSED))
        self.assertEqual(events[-1]["kind"], "done")
        self.assertNotIn("error", [event["kind"] for event in events])


class DirectorChatWithoutBlender(StudioCase):
    verdicts = (RuntimeError("Blender was not found. Install Blender or set PROXYDIRECTOR_BLENDER."),)

    def test_render_failure_is_reported_and_the_session_stays_usable(self):
        chat = self.new_chat()
        events = self.turn(chat["sid"], "Standard orbit")
        self.assertEqual(events[-1]["kind"], "error")
        self.assertIn("Blender was not found", events[-1]["message"])
        failed = [event for event in events if event["kind"] == "tool_result" and not event["ok"]]
        self.assertEqual([event["tool"] for event in failed], ["plan_orbit"])
        self.assertEqual(self.client.get("/api/runs").json()[0]["status"], "failed")
        turns = [row for row in ledger_rows(self.ledger) if row.get("stage") == "llm"]
        self.assertEqual([row["status"] for row in turns], ["chat_turn_failed"],
                         "model calls made before the failure still belong in the cost log")

        self.renderer.verdicts = [True]
        events = self.turn(chat["sid"], "Standard orbit")
        self.assertEqual(events[-1]["kind"], "done")


class StyleExpansion(StudioCase):
    def test_camera_wording_is_refused_before_any_model_call(self):
        response = self.client.post("/api/expand_prompt", json={"idea": "slow zoom on the plug"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("shot planner", response.json()["detail"])

    def test_idea_length_is_bounded(self):
        self.assertEqual(self.client.post("/api/expand_prompt", json={"idea": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/api/expand_prompt", json={"idea": "x" * 301}).status_code, 400)
        self.assertEqual(self.client.post("/api/expand_prompt", json={"idea": "x", "backend": "nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
