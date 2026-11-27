"""The style channel: an idea is expanded into appearance notes; every model call is put on record."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import ledger_rows, sandbox_config

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from pipeline import director, prompt_expander

CLEAN = {"visual_style": "Japanese minimalism", "lighting": "soft LED panel light", "background": "pale sweep",
         "mood": "calm", "extra_notes": ["matte shell"]}
WITH_CAMERA_WORDS = dict(CLEAN, extra_notes=["slow orbit with a zoom at the end"])


class StyleExpansion(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = sandbox_config(self.root)
        self.ledger = self.root / "data" / "ledger.jsonl"
        self.swap(prompt_expander, "api_key", lambda name: "test-key")

    def swap(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def answers(self, *replies):
        """A scripted style model: one reply per call, no network."""
        queue = [json.dumps(reply) if isinstance(reply, dict) else reply for reply in replies]
        self.calls = 0

        def model(messages, info):
            self.calls += 1
            return ModelResponse(parts=[TextPart(content=queue[min(self.calls, len(queue)) - 1])])
        self.swap(director, "build_model", lambda backend_cfg: FunctionModel(model))

    def test_clean_answer_is_accepted_at_once(self):
        self.answers(CLEAN)
        spec, block = prompt_expander.expand("Japanese minimalism", self.cfg, "deepseek", "run-1", self.ledger)
        self.assertEqual(spec.visual_style, "Japanese minimalism")
        self.assertIn("lighting: soft LED panel light", block)
        self.assertIn("never change camera behavior", block)
        row = ledger_rows(self.ledger)[-1]
        self.assertEqual((row["stage"], row["status"], row["attempts"], row["requests"]), ("style_expand", "ok", 1, 1))

    def test_rejected_answer_is_retried_and_all_calls_are_on_record(self):
        self.answers(WITH_CAMERA_WORDS, "```json\n" + json.dumps(CLEAN) + "\n```")
        spec, _ = prompt_expander.expand("clean studio look", self.cfg, "deepseek", "run-2", self.ledger)
        self.assertEqual(spec.extra_notes, ["matte shell"])
        row = ledger_rows(self.ledger)[-1]
        self.assertEqual((row["status"], row["attempts"], row["requests"]), ("ok", 2, 2))
        self.assertGreater(row["input_tokens"], 0)

    def test_three_bad_answers_end_in_an_error_that_is_still_logged(self):
        self.answers(WITH_CAMERA_WORDS, "not json at all", WITH_CAMERA_WORDS)
        with self.assertRaises(ValueError):
            prompt_expander.expand("clean studio look", self.cfg, "deepseek", "run-3", self.ledger)
        self.assertEqual(self.calls, 3)
        row = ledger_rows(self.ledger)[-1]
        self.assertEqual((row["status"], row["attempts"], row["requests"]), ("rejected", 3, 3))

    def test_missing_key_is_reported_before_any_call(self):
        self.swap(prompt_expander, "api_key", lambda name: None)
        self.answers(CLEAN)
        with self.assertRaises(RuntimeError):
            prompt_expander.expand("clean studio look", self.cfg, "deepseek", "run-4", self.ledger)
        self.assertEqual(self.calls, 0)
        self.assertEqual(ledger_rows(self.ledger), [])

    def test_preset_director_falls_back_to_the_configured_style_model(self):
        self.answers(CLEAN)
        prompt_expander.expand("clean studio look", self.cfg, "mock", "run-5", self.ledger)
        self.assertEqual(ledger_rows(self.ledger)[-1]["backend"], self.cfg["prompt_expand"]["backend"])


if __name__ == "__main__":
    unittest.main()
