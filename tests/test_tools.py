"""The director's tools: the free render loop, the prompt builder and the paid gate."""
import builtins
import json
import unittest
from pathlib import Path

from tests.support import SandboxCase, plan

from pipeline import kling, runway
from pipeline.tools import REFUSED, make_tools


class GateRefusals(SandboxCase):
    """A refusal is an answer the director can act on. It must never end the agent run."""

    def test_building_or_submitting_before_any_render_is_refused(self):
        tools = make_tools(self.make_context())
        self.assertTrue(tools["build_seedance_prompt"]().startswith(REFUSED))
        self.assertTrue(tools["submit_seedance"]("480p", True).startswith(REFUSED))

    def test_submit_needs_a_prompt_and_an_explicit_confirmation(self):
        ctx = self.make_context()
        tools = make_tools(ctx)
        self.assertTrue(tools["plan_orbit"](plan()).startswith("Framing passed."))
        answer = tools["submit_seedance"]("480p", True)
        self.assertTrue(answer.startswith(REFUSED))
        self.assertIn("prompt", answer)
        tools["build_seedance_prompt"]()
        answer = tools["submit_seedance"]("480p", False)
        self.assertTrue(answer.startswith(REFUSED))
        self.assertIn("confirmation", answer.lower())
        self.assertFalse((ctx.run_dir / "commercial").exists())

    def test_failed_audit_is_refused_with_a_reason(self):
        self.renderer.verdicts = [False]
        tools = make_tools(self.make_context())
        self.assertTrue(tools["plan_orbit"](plan(radius=3.0)).startswith("Framing failed."))
        answer = tools["build_seedance_prompt"]()
        self.assertTrue(answer.startswith(REFUSED))
        self.assertIn("audit", answer)


class PassingRenderStaysReachable(SandboxCase):
    """The benchmark's S1 trial: the best plan was found, a tighter one failed, and the run died."""

    verdicts = (True, False)

    def test_earlier_passing_render_can_be_selected(self):
        ctx = self.make_context()
        tools = make_tools(ctx)
        tools["plan_orbit"](plan(elevation_degrees=20.0, radius=6.5))
        tools["plan_orbit"](plan(elevation_degrees=20.0, radius=6.0))

        answer = tools["build_seedance_prompt"]()
        self.assertTrue(answer.startswith(REFUSED), "the latest render failed, so nothing is built by default")
        self.assertIn("v1", answer, "the refusal names the render that passed")

        answer = tools["build_seedance_prompt"](use_render="v1")
        self.assertFalse(answer.startswith(REFUSED), answer)
        prompt = (ctx.run_dir / "seedance_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("about 20 degrees", prompt)

        answer = tools["submit_seedance"]("480p", True)
        self.assertTrue(answer.startswith("[dry-run]"), answer)
        frozen = json.loads((ctx.run_dir / "commercial" / "frozen_request.json").read_text(encoding="utf-8"))
        chosen = ctx.run_dir / "render_v1" / "reference.mp4"
        self.assertEqual(frozen["reference_video_sha256"], kling.digest(chosen),
                         "the request must carry the reference clip of the render the prompt describes")

    def test_render_names_are_accepted_in_the_forms_the_tools_print(self):
        tools = make_tools(self.make_context())
        tools["plan_orbit"](plan())
        tools["plan_orbit"](plan(radius=5.0))
        for name in ("v1", "render_v1", "1", " V1 "):
            with self.subTest(name=name):
                self.assertFalse(tools["build_seedance_prompt"](use_render=name).startswith(REFUSED))

    def test_failed_or_unknown_renders_cannot_be_selected(self):
        tools = make_tools(self.make_context())
        tools["plan_orbit"](plan())
        tools["plan_orbit"](plan(radius=5.0))
        for name in ("v2", "v9", "baseline", "../v1"):
            with self.subTest(name=name):
                self.assertTrue(tools["build_seedance_prompt"](use_render=name).startswith(REFUSED))

    def test_render_limit_still_leaves_the_passing_render_usable(self):
        ctx = self.make_context()
        tools = make_tools(ctx)
        limit = self.cfg["pipeline"]["max_orbit_iterations"]
        for attempt in range(limit):
            tools["plan_orbit"](plan(radius=6.5 - 0.5 * attempt))
        self.assertTrue(tools["plan_orbit"](plan(radius=7.0)).startswith("Render limit reached."))
        self.assertEqual(len(self.renderer.calls), limit, "no render beyond the limit")
        self.assertFalse(tools["build_seedance_prompt"](use_render="v1").startswith(REFUSED))


class PromptFollowsTheRender(SandboxCase):
    verdicts = (True, True)

    def test_a_new_render_invalidates_the_built_prompt(self):
        tools = make_tools(self.make_context())
        tools["plan_orbit"](plan())
        tools["build_seedance_prompt"]()
        tools["plan_orbit"](plan(direction="ccw"))
        answer = tools["submit_seedance"]("480p", True)
        self.assertTrue(answer.startswith(REFUSED))
        self.assertIn("prompt", answer)

    def test_prompt_step_is_logged_with_its_render(self):
        tools = make_tools(self.make_context())
        tools["plan_orbit"](plan())
        tools["build_seedance_prompt"]()
        row = self.rows(stage="prompt")[-1]
        self.assertEqual(row["status"], "built")
        self.assertEqual(row.get("render"), "v1")

    def test_runway_backend_freezes_a_request_too(self):
        self.cfg["video_backend"] = "runway"
        ctx = self.make_context()
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        tools["build_seedance_prompt"]()
        self.assertIn("Supported quality", tools["submit_seedance"]("4k", True))
        self.assertTrue(tools["submit_seedance"]("480p", True).startswith("[dry-run]"))
        frozen = json.loads((ctx.run_dir / "commercial" / "frozen_request.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen["reference_video_sha256"], runway.digest(ctx.run_dir / "render_v1" / "reference.mp4"))


class EmphasisChannel(SandboxCase):
    """The template is frozen. The one free-text slot must not carry camera instructions."""

    def build(self, note):
        ctx = self.make_context()
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        answer = tools["build_seedance_prompt"](emphasis_notes=note)
        return answer, (ctx.run_dir / "seedance_prompt.txt").read_text(encoding="utf-8")

    def test_object_detail_is_kept(self):
        answer, prompt = self.build("Keep the scroll wheel ridges crisp.")
        self.assertFalse(answer.startswith(REFUSED))
        self.assertIn("Keep the scroll wheel ridges crisp.", prompt)
        self.assertNotIn("emphasis_dropped", self.rows(stage="prompt")[-1])

    def test_camera_or_framing_wording_is_left_out_and_reported(self):
        for note in ("Orbit slowly and keep a wide margin around the cable.",
                     "Zoom in on the USB plug at the end.",
                     "Hold the camera at a lower angle.",
                     "保持镜头环绕速度均匀"):
            with self.subTest(note=note):
                answer, prompt = self.build(note)
                self.assertFalse(answer.startswith(REFUSED), "the prompt is still built, without the note")
                self.assertNotIn(note, prompt)
                self.assertIn("left out", answer)
                self.assertTrue(self.rows(stage="prompt")[-1].get("emphasis_dropped"))

    def test_prompt_without_the_note_equals_the_plain_prompt(self):
        _, with_bad_note = self.build("Zoom in on the USB plug.")
        _, plain = self.build("")
        self.assertEqual(with_bad_note, plain)

    def test_overlong_or_multi_line_note_is_left_out(self):
        answer, prompt = self.build("Keep the wheel crisp.\nKeep the plug bright.")
        self.assertNotIn("Keep the plug bright.", prompt)
        self.assertIn("left out", answer)
        answer, prompt = self.build("Keep the wheel crisp. " * 12)
        self.assertNotIn("Keep the wheel crisp.", prompt)
        self.assertIn("left out", answer)


class HumanGate(SandboxCase):
    def test_declined_submission_is_returned_and_not_asked_again(self):
        ctx = self.make_context(dry_run=False, auto_approve=False)
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        tools["build_seedance_prompt"]()

        asked = []
        original_input = builtins.input
        builtins.input = lambda prompt="": asked.append(prompt) or "no"
        self.addCleanup(setattr, builtins, "input", original_input)

        def must_not_run(*args, **kwargs):
            raise AssertionError("a declined submission must not reach the video provider")
        for module in (kling, runway):
            for name in ("build_request", "submit"):
                original = getattr(module, name)
                setattr(module, name, must_not_run)
                self.addCleanup(setattr, module, name, original)

        first = tools["submit_seedance"]("480p", True)
        self.assertTrue(first.startswith(REFUSED))
        self.assertIn("declined", first)
        second = tools["submit_seedance"]("480p", True)
        self.assertTrue(second.startswith(REFUSED))
        self.assertEqual(len(asked), 1, "the person at the gate is asked once")
        self.assertEqual(len(self.rows(status="gate_declined_by_human")), 1)


class ChatSession(SandboxCase):
    verdicts = (True, True, True)

    def test_previous_request_is_archived_with_its_render(self):
        ctx = self.make_context(chat_session=True)
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        tools["build_seedance_prompt"]()
        tools["submit_seedance"]("480p", True)
        self.assertTrue((ctx.run_dir / "commercial" / "frozen_request.json").exists())

        ctx.turn_start_iteration = ctx.iterations          # a new chat turn
        tools["plan_orbit"](plan(direction="ccw"))
        self.assertFalse((ctx.run_dir / "seedance_prompt.txt").exists())
        self.assertFalse((ctx.run_dir / "commercial").exists())
        self.assertTrue((ctx.run_dir / "render_v1" / "seedance_prompt.txt").exists())
        self.assertTrue((ctx.run_dir / "render_v1" / "commercial" / "frozen_request.json").exists())

    def test_reusing_an_older_render_twice_does_not_collide(self):
        ctx = self.make_context(chat_session=True)
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        for _ in range(2):
            tools["build_seedance_prompt"](use_render="v1")
            tools["submit_seedance"]("480p", True)
            ctx.turn_start_iteration = ctx.iterations
            self.assertTrue(tools["plan_orbit"](plan(radius=7.0)).startswith("Framing passed."))
        self.assertTrue((ctx.run_dir / "render_v1" / "commercial" / "frozen_request.json").exists())

    def test_paid_record_blocks_further_edits_with_a_refusal(self):
        ctx = self.make_context(chat_session=True)
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        (ctx.run_dir / "commercial").mkdir()
        (ctx.run_dir / "commercial" / "kling_state.json").write_text("{}", encoding="utf-8")
        for call in (lambda: tools["plan_orbit"](plan(radius=7.0)),
                     lambda: tools["build_seedance_prompt"](),
                     lambda: tools["submit_seedance"]("480p", True)):
            answer = call()
            self.assertTrue(answer.startswith(REFUSED))
            self.assertIn("paid submission", answer)
        self.assertEqual(len(self.renderer.calls), 1)


class RenderFailure(SandboxCase):
    verdicts = (True, RuntimeError("Blender render failed (exit code 1)."))

    def test_a_broken_render_raises_and_keeps_the_earlier_result_usable(self):
        ctx = self.make_context()
        tools = make_tools(ctx)
        tools["plan_orbit"](plan())
        with self.assertRaises(RuntimeError):
            tools["plan_orbit"](plan(radius=7.0))
        self.assertTrue(tools["build_seedance_prompt"]().startswith(REFUSED))
        self.assertFalse(tools["build_seedance_prompt"](use_render="v1").startswith(REFUSED))


class ToolSurface(SandboxCase):
    def test_tool_names_and_signatures_seen_by_the_model(self):
        """Pydantic AI builds each tool schema from the signature, so a wrapper must not hide it."""
        import inspect
        tools = make_tools(self.make_context())
        self.assertEqual(list(tools), ["get_run_context", "plan_orbit", "latest_framing_status",
                                       "build_seedance_prompt", "submit_seedance", "stop"])
        self.assertEqual(list(inspect.signature(tools["plan_orbit"]).parameters), ["plan"])
        self.assertEqual(list(inspect.signature(tools["build_seedance_prompt"]).parameters),
                         ["emphasis_notes", "use_render"])
        self.assertEqual(list(inspect.signature(tools["submit_seedance"]).parameters), ["quality", "confirm"])
        for tool in tools.values():
            self.assertTrue(tool.__doc__ and tool.__doc__.strip())

    def test_run_context_reports_the_current_plan(self):
        tools = make_tools(self.make_context())
        context = json.loads(tools["get_run_context"]())
        self.assertIsNone(context["current_plan"])
        self.assertEqual(context["baseline_plan"]["radius"], 6.0)
        tools["plan_orbit"](plan(radius=7.5))
        self.assertEqual(json.loads(tools["get_run_context"]())["current_plan"]["radius"], 7.5)

    def test_stop_is_logged(self):
        tools = make_tools(self.make_context())
        self.assertIn("No plan passes.", tools["stop"]("No plan passes."))
        self.assertEqual(self.rows(stage="stop")[-1]["reason"], "No plan passes.")


if __name__ == "__main__":
    unittest.main()
