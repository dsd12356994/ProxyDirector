"""The video prompt: the frozen template must stay byte-identical to the one the experiments used."""
import hashlib
import unittest

from tests.support import FakeRenderer, plan

from pipeline.prompt_template import TEMPLATE, render_prompt, to_kling

# SHA-256 of TEMPLATE at commit ce34d05, the commit that produced the November batch and the
# shot-brief benchmark. Changing the template breaks comparability with those results: do it only
# on purpose, and record the new hash together with the reason.
FROZEN_TEMPLATE_SHA256 = "ae66b74c10c93f84402ce34609eb93718f156988d736e6741fd69dc9c3e134d0"
FROZEN_KLING_TEMPLATE_SHA256 = "4ad306fb8de91a3221226e307d763fb2605f343852c8ccfc1e2563b9d413a1f1"


def report(tmp, **changes):
    return FakeRenderer(True)({}, tmp, tmp / "ledger.jsonl", "run", None, plan(**changes), "v1")


class FrozenTemplate(unittest.TestCase):
    def test_template_text_is_unchanged(self):
        self.assertEqual(hashlib.sha256(TEMPLATE.encode("utf-8")).hexdigest(), FROZEN_TEMPLATE_SHA256)

    def test_kling_wording_is_unchanged(self):
        self.assertEqual(hashlib.sha256(to_kling(TEMPLATE).encode("utf-8")).hexdigest(),
                         FROZEN_KLING_TEMPLATE_SHA256)


class RenderPrompt(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_slots_are_filled_from_the_audited_render(self):
        text = render_prompt(report(self.tmp, elevation_degrees=35.0), duration=10)
        self.assertNotIn("{", text)
        self.assertIn("about 35 degrees", text)
        self.assertIn("over 10 seconds", text)
        self.assertIn("(front, then left, then back, then right)", text)
        self.assertIn("approximately 5% minimum edge clearance", text)
        self.assertTrue(text.endswith("Silent video."))

    def test_empty_emphasis_and_style_leave_the_template_alone(self):
        framing = report(self.tmp)
        plain = render_prompt(framing, duration=10)
        self.assertEqual(render_prompt(framing, duration=10, emphasis_notes="   ", style_block="  "), plain)
        expected = TEMPLATE.format(margin_percent=5, duration=10, elevation_degrees=50.0,
                                   side_sequence="front, then left, then back, then right", emphasis="")
        self.assertEqual(plain, expected)

    def test_emphasis_sentence_goes_after_the_appearance_paragraph(self):
        framing = report(self.tmp)
        text = render_prompt(framing, duration=10, emphasis_notes="Keep the scroll wheel ridges crisp.")
        self.assertIn("added objects. Keep the scroll wheel ridges crisp.\n\nThe mouse and every part", text)

    def test_style_block_is_appended_and_kling_ids_are_used(self):
        framing = report(self.tmp)
        text = render_prompt(framing, duration=10, kling=True, style_block="Visual style notes: dark studio.")
        self.assertTrue(text.endswith("Silent video.\n\nVisual style notes: dark studio."))
        self.assertIn("@video_1", text)
        self.assertIn("@image_4", text)


if __name__ == "__main__":
    unittest.main()
