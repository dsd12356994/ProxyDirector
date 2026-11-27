"""The vocabulary filter that keeps camera wording out of the appearance channels."""
import unittest

from tests.support import REPO  # noqa: F401  (puts the repository on sys.path)

from pipeline.camera_words import camera_terms, framing_terms


class CameraTerms(unittest.TestCase):
    def test_camera_and_movement_words_are_found(self):
        cases = {
            "Slow orbit around the product": "orbit",
            "orbiting": "orbit",
            "Zoom in on the scroll wheel": "zoom",
            "a wide-angle lens look": "lens",
            "the camera drifts": "camera",
            "Pan left across the cable": "pan",
            "panning": "pan",
            "dolly shot": "dolly",
            "a close-up of the USB plug": "close-up",
            "closeup": "close-up",
            "rotating slowly": "rotate",
            "one full rotation": "rotate",
            "spinning turntable": "spin",
            "a tracking shot": "tracking shot",
            "aerial view": "aerial",
            "top-down": "top-down",
            "handheld feel": "handheld",
            "fisheye": "fisheye",
            "narrow field of view": "field of view",
            "FOV 20": "fov",
            "镜头缓慢推近": "镜头",
            "环绕一圈": "环绕",
        }
        for text, label in cases.items():
            with self.subTest(text=text):
                self.assertIn(label, camera_terms(text))

    def test_harmless_words_are_not_mistaken_for_camera_words(self):
        """Substring matching used to reject all of these (pan in Japanese, spin in spine, ...)."""
        for text in ("Japanese minimalism", "an expansive white backdrop", "soft LED panel light",
                     "a company showroom", "the spine of a book as a prop", "Spanish tiles",
                     "matte black with a champagne accent", "cranberry red accent", "a spindle-shaped highlight",
                     "a zoomorphic pattern", "dark studio with cool rim lighting",
                     "premium, calm, product-catalog mood", "深色科技感，冷色灯光"):
            with self.subTest(text=text):
                self.assertEqual(camera_terms(text), [])

    def test_matching_ignores_case(self):
        self.assertIn("orbit", camera_terms("ORBIT faster"))
        self.assertIn("camera", camera_terms("Camera"))


class FramingTerms(unittest.TestCase):
    def test_framing_words_are_found(self):
        cases = {
            "keep a wider margin": "margin",
            "fill the frame": "frame",
            "tighter framing": "frame",
            "reframe on the plug": "frame",
            "more edge clearance": "clearance",
            "a lower angle": "angle",
            "raise the elevation": "elevation",
            "crop the cable": "crop",
            "tilt down": "tilt",
            "取景更紧": "取景",
        }
        for text, label in cases.items():
            with self.subTest(text=text):
                self.assertIn(label, framing_terms(text))

    def test_object_descriptions_pass(self):
        for text in ("Keep the tangled cable loops distinct.", "The scroll wheel ridges stay crisp.",
                     "Preserve the glossy side buttons and the matte shell.", "One USB-A plug, metal contacts visible.",
                     "A rectangular plug housing", "framework-free"):
            with self.subTest(text=text):
                self.assertEqual(framing_terms(text), [])
                self.assertEqual(camera_terms(text), [])


class StyleChannel(unittest.TestCase):
    """prompt_expander keeps using the filter for the idea and for the model's answer."""

    def test_spec_violations(self):
        from pipeline.prompt_expander import PromptSpec, _violations
        clean = PromptSpec(visual_style="Japanese minimalism", lighting="soft LED panel light",
                           background="expansive white sweep", mood="calm", extra_notes=["matte finish"])
        self.assertEqual(_violations(clean), [])
        dirty = PromptSpec(visual_style="cinematic", lighting="rim light", extra_notes=["slow orbit, zoom at the end"])
        self.assertEqual(sorted(_violations(dirty)), ["orbit", "zoom"])

    def test_idea_with_camera_wording_is_refused_before_any_model_call(self):
        from pipeline import prompt_expander
        from pipeline.config import load_config
        with self.assertRaises(ValueError):
            prompt_expander.expand("zoom in slowly", load_config(), "deepseek")


if __name__ == "__main__":
    unittest.main()
