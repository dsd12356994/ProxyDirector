"""The agent loop end to end with the offline preset director (no LLM, no Blender)."""
import json
import unittest

from tests.support import SandboxCase

from pipeline import director
from pipeline.tools import REFUSED, make_tools  # noqa: F401


class PresetDirector(SandboxCase):
    def test_standard_run_freezes_a_request(self):
        ctx = self.make_context()
        output, usage = director.run_agent(ctx, "mock")
        self.assertIn("No video service was called", output)
        self.assertEqual([tag for tag, _ in self.renderer.calls], ["v1"])
        self.assertEqual(self.renderer.calls[0][1].model_dump(), self.cfg["pipeline"]["baseline_plan"])
        frozen = json.loads((ctx.run_dir / "commercial" / "frozen_request.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["provider"], "kling")
        self.assertEqual([row["status"] for row in self.rows(stage="seedance")], ["dry_run_frozen"])
        llm = self.rows(stage="llm")
        self.assertEqual(len(llm), 1)
        self.assertEqual(llm[0]["status"], "finished")
        self.assertTrue(llm[0]["mock"])
        self.assertGreaterEqual(llm[0]["requests"], 4)


class PresetDirectorFailingAudit(SandboxCase):
    verdicts = (False,)

    def test_stops_at_the_render_limit(self):
        ctx = self.make_context()
        output, _ = director.run_agent(ctx, "mock")
        self.assertEqual(len(self.renderer.calls), self.cfg["pipeline"]["max_orbit_iterations"])
        self.assertIn("still fails", output)
        self.assertEqual(len(self.rows(stage="stop")), 1)
        self.assertFalse((ctx.run_dir / "seedance_prompt.txt").exists())
        self.assertEqual(self.rows(stage="seedance"), [])


class PresetDirectorRecovers(SandboxCase):
    verdicts = (False, True)

    def test_retry_with_a_wider_plan_then_freeze(self):
        ctx = self.make_context()
        output, _ = director.run_agent(ctx, "mock")
        self.assertEqual(len(self.renderer.calls), 2)
        self.assertEqual(self.renderer.calls[1][1].radius, 8.0)
        self.assertIn("No video service was called", output)


class UsageIsLoggedWhenTheRunBreaks(SandboxCase):
    verdicts = (RuntimeError("Blender render failed (exit code 1)."),)

    def test_model_calls_made_before_a_crash_reach_the_ledger(self):
        ctx = self.make_context()
        with self.assertRaises(RuntimeError):
            director.run_agent(ctx, "mock")
        llm = self.rows(stage="llm")
        self.assertEqual(len(llm), 1, "the model calls made before the crash were paid for")
        self.assertEqual(llm[0]["status"], "failed")
        self.assertGreaterEqual(llm[0]["requests"], 2)


class PresetDirectorHandlesRefusals(SandboxCase):
    def test_a_refused_gate_ends_in_stop_with_the_reason(self):
        """Rule 4 of the system prompt: if the gate refuses, call stop with the reason."""
        ctx = self.make_context(chat_session=True)
        (ctx.run_dir / "commercial").mkdir()
        (ctx.run_dir / "commercial" / "state.json").write_text("{}", encoding="utf-8")
        output, _ = director.run_agent(ctx, "mock")
        self.assertEqual(self.renderer.calls, [])
        stops = self.rows(stage="stop")
        self.assertEqual(len(stops), 1)
        self.assertIn("paid submission", stops[0]["reason"])
        self.assertIn("paid submission", output)


class SystemPrompt(unittest.TestCase):
    def test_system_prompt_is_the_one_the_experiments_used(self):
        """The benchmark protocol records the exact frozen pipeline.director.SYSTEM_PROMPT."""
        import hashlib
        self.assertEqual(hashlib.sha256(director.SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
                         SYSTEM_PROMPT_SHA256)


# SHA-256 of director.SYSTEM_PROMPT (guards prompt stability across benchmark runs).
SYSTEM_PROMPT_SHA256 = "5262642a7448136e9417e47131525482bed0414957a47ccb6f418a4a8cf26cb9"


if __name__ == "__main__":
    unittest.main()
