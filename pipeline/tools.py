"""LLM 的工具层。上排（本地免费）与下排（付费）之间隔着闸门：submit_seedance 必须过
取景检查 + 人工确认（或显式 auto_approve）才放行。所有调用都写账本。
成片层可插拔：cfg.video_backend = runway | kling，工具签名不变、内部路由。

A gate refusal is returned to the director as the tool's answer (it starts with REFUSED), so the
director can render again, pick another render or stop. Every audited render of the run stays
selectable, so a plan that passed is not lost when a later attempt fails.
"""
import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import blender_step, kling, ledger, runway, tripo
from .camera_words import camera_terms, framing_terms
from .models import FramingReport, ShotPlan
from .prompt_template import render_prompt

REFUSED = "Refused: "                 # how a tool answer starts when the gate said no
MAX_EMPHASIS_CHARS = 200              # "one short sentence" (director.SYSTEM_PROMPT, rule 3)


class GateRefused(RuntimeError):
    """Raised inside a tool when the gate says no. make_tools turns it into the tool's answer."""


@dataclass
class ToolContext:
    cfg: dict
    run_dir: Path
    run_id: str
    dry_run: bool
    auto_approve: bool
    budget_note: str = ""
    glb: Path = field(default=None)
    framing: FramingReport = field(default=None)          # the latest render, passed or not
    prompt_path: Path = field(default=None)
    iterations: int = 0
    style_block: str = ""      # 提示词扩写产物（只附加到冻结模板末尾，非镜头维度）
    chat_session: bool = False
    turn_start_iteration: int = 0
    renders: dict = field(default_factory=dict)           # "v1" -> FramingReport, every render of this run
    prompt_framing: FramingReport = field(default=None)   # the render the current prompt was built from
    gate_declined: bool = False                           # the person at the terminal said no

    @property
    def ledger_path(self) -> Path:
        return Path(self.cfg["_root"]) / "data" / "ledger.jsonl"

    def paid_record_exists(self) -> bool:
        commercial = self.run_dir / "commercial"
        return any((commercial / name).is_file() for name in ("kling_state.json", "state.json"))

    def check_editable(self) -> None:
        if self.chat_session and self.paid_record_exists():
            raise GateRefused("This run already has a paid submission. Start a new session to change the shot.")


def _answers_refusals(tool):
    """Give a gate refusal back to the director as the tool's answer instead of ending the agent run."""
    @functools.wraps(tool)                     # Pydantic AI builds the tool schema from the signature
    def answered(*args, **kwargs):
        try:
            return tool(*args, **kwargs)
        except GateRefused as refusal:
            return f"{REFUSED}{refusal}"
    return answered


def _render_tag(name: str) -> str:
    """'v2', 'render_v2', '2' or 'render_v2/reference.mp4' -> 'v2'. Anything else -> ''."""
    first = re.split(r"[\\/]", name.strip().lower())[0]
    match = re.fullmatch(r"(?:render_)?v?(\d+)", first)
    return f"v{int(match[1])}" if match else ""


def _describe(plan: ShotPlan) -> str:
    return (f"elevation {plan.elevation_degrees:g}, radius {plan.radius:g}, {plan.direction}, "
            f"start {plan.start_degrees:g}, margin {plan.margin:g}")


def _clean_emphasis(text: str) -> tuple[str, list[str]]:
    """The emphasis sentence that may enter the prompt, and why it was left out if it was.

    The template is frozen and the reference clip fixes the camera, so the one free-text slot may
    only describe the object. A note with camera or framing wording, a line break, or more than
    MAX_EMPHASIS_CHARS characters is left out; the prompt is then exactly the plain template.
    """
    note = (text or "").strip()
    if not note:
        return "", []
    reasons = camera_terms(note) + framing_terms(note)
    if reasons:
        return "", [f"camera or framing wording ({', '.join(reasons)})"]
    if "\n" in note or "\r" in note or len(note) > MAX_EMPHASIS_CHARS:
        return "", ["longer than one short sentence"]
    return note, []


def _archive_request(ctx: ToolContext) -> None:
    """Chat sessions: keep a frozen rehearsal request with the render it was built from, so the run
    folder shows only the current shot's prompt and request. Nothing is ever overwritten."""
    owner = ctx.prompt_framing or ctx.framing
    if owner is None or not owner.video_path:
        return
    home = Path(owner.video_path).parent
    for name in ("seedance_prompt.txt", "commercial"):
        artifact = ctx.run_dir / name
        if not artifact.exists():
            continue
        target, attempt = home / name, 1
        while target.exists():                           # an earlier request built from the same render
            attempt += 1
            target = home / f"{artifact.stem}.{attempt}{artifact.suffix}"
        artifact.rename(target)


def make_tools(ctx: ToolContext):
    """返回 名称->函数 映射。工具直接注册进 Pydantic AI Agent。"""

    def passing_renders() -> str:
        passed = [f"{tag} ({_describe(report.plan)})" for tag, report in ctx.renders.items() if report.passed]
        if not passed:
            return " No render of this run has passed the audit yet: adjust the plan and render again."
        return (" Renders that passed: " + "; ".join(passed) + ". Call build_seedance_prompt with use_render "
                "set to one of them, or render another plan.")

    def select_render(use_render: str) -> tuple[str, FramingReport]:
        if not use_render.strip():
            if ctx.framing is None:
                raise GateRefused("Render a reference and pass the framing audit before building a prompt."
                                  + (passing_renders() if ctx.renders else ""))
            tag = next((t for t, report in ctx.renders.items() if report is ctx.framing), "")
            if not ctx.framing.passed:
                raise GateRefused(f"The latest render ({tag}) failed the framing audit, so no prompt was built."
                                  + passing_renders())
            return tag, ctx.framing
        tag = _render_tag(use_render)
        if tag not in ctx.renders:
            raise GateRefused(f"There is no render named {use_render.strip()!r} in this run." + passing_renders())
        if not ctx.renders[tag].passed:
            raise GateRefused(f"Render {tag} failed the framing audit." + passing_renders())
        return tag, ctx.renders[tag]

    def human_gate(provider: str, quote: str) -> None:
        """Ask the person at the terminal before a paid submission. Dry runs and prior approval skip it."""
        if ctx.dry_run or ctx.auto_approve:
            return
        if ctx.gate_declined:
            raise GateRefused("The user declined the paid submission. Call stop with that reason.")
        try:
            answer = input(f"\n[Paid submission] {quote} Type yes to submit: ").strip().lower()
        except EOFError:                                 # no terminal to confirm at: never assume yes
            answer = ""
        if answer != "yes":
            ctx.gate_declined = True
            ledger.log({"run_id": ctx.run_id, "stage": "seedance", "provider": provider,
                        "status": "gate_declined_by_human", "cost_usd": 0.0, "mock": False},
                       ctx.ledger_path)
            raise GateRefused("The user declined the paid submission.")

    def get_run_context() -> str:
        """Read the shot requirements, available budget, current plan and submission mode."""
        spent = ledger.spent_usd(ctx.ledger_path)
        return json.dumps({
            "object": "Black wired mouse with two main buttons, scroll wheel, top button, two side buttons, one cable and one USB-A plug.",
            "shot_requirements": "One continuous 10-second, 360° orbit showing all four sides in sequence. "
                                 "Keep every component in frame with about 5% edge clearance, without cuts or reversals.",
            "budget_usd": ctx.cfg["budget"]["max_usd"], "spent_usd_all_runs": spent,
            "dry_run": ctx.dry_run, "auto_approve": ctx.auto_approve,
            "video_backend": ctx.cfg.get("video_backend", "runway"),
            "max_orbit_iterations": ctx.cfg["pipeline"]["max_orbit_iterations"],
            "baseline_plan": ctx.cfg["pipeline"]["baseline_plan"],
            "current_plan": ctx.framing.plan.model_dump() if ctx.framing else None,
            "glb_frozen_sha256": tripo.digest(ctx.glb) if ctx.glb else None,
            "note": "Local renders and framing checks are free. A live video submission requires a passing framing check and approval.",
        }, ensure_ascii=False)

    def plan_orbit(plan: ShotPlan) -> str:
        """Render the orbit locally and audit its framing at every pose. Adjust the plan if the audit fails."""
        ctx.check_editable()
        if ctx.iterations - ctx.turn_start_iteration >= ctx.cfg["pipeline"]["max_orbit_iterations"]:
            return ("Render limit reached. Build the prompt from a render that passed, or stop this turn."
                    + passing_renders())
        if ctx.chat_session:
            _archive_request(ctx)
        ctx.prompt_path = None                           # a new render invalidates the built prompt
        ctx.prompt_framing = None
        ctx.framing = None
        ctx.iterations += 1
        tag = f"v{ctx.iterations}"
        fr = blender_step.render_orbit(ctx.cfg, ctx.run_dir, ctx.ledger_path,
                                       ctx.run_id, ctx.glb, plan, tag)
        ctx.framing = fr
        ctx.renders[tag] = fr
        return fr.summary() + f" Reference video: {Path(fr.video_path).relative_to(ctx.run_dir).as_posix()}"

    def latest_framing_status() -> str:
        """Read the latest framing audit, including any pose that misses the requested clearance."""
        if ctx.framing is None:
            return "No reference has been rendered yet. Run plan_orbit first."
        report = ctx.framing.model_dump()
        if ctx.framing.video_path:
            report["video_path"] = Path(ctx.framing.video_path).relative_to(ctx.run_dir).as_posix()
        return json.dumps(report, ensure_ascii=False)

    def build_seedance_prompt(emphasis_notes: str = "", use_render: str = "") -> str:
        """Build the video prompt from an audited render and the fixed template.

        Args:
            emphasis_notes: Empty, or one short sentence about the object's appearance. Camera and framing wording is left out, because the reference video fixes the camera.
            use_render: Empty to use the latest render. To build from an earlier render that passed the audit, give its name, for example "v1".
        """
        ctx.check_editable()
        tag, report = select_render(use_render)
        note, left_out = _clean_emphasis(emphasis_notes)
        backend = ctx.cfg.get("video_backend", "runway")
        if backend == "kling":
            text = render_prompt(report, ctx.cfg["kling"]["duration"], note,
                                 kling=True, style_block=ctx.style_block)
        else:
            text = render_prompt(report, ctx.cfg["runway"]["duration"], note,
                                 style_block=ctx.style_block)
        ctx.prompt_path = ctx.run_dir / "seedance_prompt.txt"
        ctx.prompt_path.write_text(text, encoding="utf-8")
        ctx.prompt_framing = report
        entry = {"run_id": ctx.run_id, "stage": "prompt", "provider": "local",
                 "status": "built", "cost_usd": 0.0, "mock": False, "render": tag}
        if left_out:
            entry["emphasis_dropped"] = left_out
        ledger.log(entry, ctx.ledger_path)
        answer = f"Generation prompt saved to {ctx.prompt_path.name}."
        if report is not ctx.framing:
            answer += f" It was built from render {tag} ({_describe(report.plan)})."
        if left_out:
            answer += (f" The emphasis note was left out: {'; '.join(left_out)}. The reference video fixes "
                       "the camera, and the note may only describe the object.")
        return f"{answer}\n{text}"

    def submit_seedance(quality: str, confirm: bool) -> str:
        """Submit a video request after the framing check and prompt build, with explicit confirmation.
        Runway requests use the configured quality and rate for a 10-second video.
        Kling uses the configured model and resolution and charges credits."""
        ctx.check_editable()
        if not any(report.passed for report in ctx.renders.values()):
            raise GateRefused("Submission requires a passing framing audit.")
        if ctx.prompt_framing is None or ctx.prompt_path is None or not ctx.prompt_path.exists():
            raise GateRefused("Build the generation prompt before submitting.")
        if not confirm:
            raise GateRefused("Explicit confirmation is required to submit a video request.")
        reference = Path(ctx.prompt_framing.video_path)   # the clip of the render the prompt describes
        views_dir = Path(ctx.cfg["_root"]) / ctx.cfg["pipeline"]["views_dir"]
        view_files = {n: views_dir / n for n in tripo.SLOT_OF_VIEW}
        prompt_text = ctx.prompt_path.read_text(encoding="utf-8")
        backend = ctx.cfg.get("video_backend", "runway")

        if backend == "kling":
            est = kling.estimate_credits(ctx.cfg,
                                         ctx.cfg["pipeline"]["frames"] / ctx.cfg["pipeline"]["fps"],
                                         ctx.cfg["kling"]["duration"])
            human_gate("kling", f"Kling {ctx.cfg['kling']['model']} {ctx.cfg['kling']['resolution']} "
                                f"{ctx.cfg['kling']['duration']}s Estimated {est} credits.")
            body, record = kling.build_request(ctx.cfg, ctx.run_id, reference,
                                               view_files, prompt_text, dry_run=ctx.dry_run)
            video = kling.submit(ctx.cfg, ctx.run_dir, ctx.ledger_path, ctx.run_id,
                                 body, record, ctx.dry_run)
            return ("[dry-run] Request saved to commercial/frozen_request.json."
                    if ctx.dry_run else f"Video saved to {video.relative_to(ctx.run_dir).as_posix()}.")

        if quality not in ctx.cfg["runway"]["qualities"]:
            return f"Supported quality settings: {list(ctx.cfg['runway']['qualities'])}"
        q = ctx.cfg["runway"]["qualities"][quality]
        human_gate("runway", f"{quality} estimated at {q['credits']} credits (about USD {q['usd']:.2f}).")
        body, record = runway.build_request(ctx.cfg, reference, view_files, prompt_text, quality)
        video = runway.submit(ctx.cfg, ctx.run_dir, ctx.ledger_path, ctx.run_id,
                              body, record, ctx.dry_run)
        return ("[dry-run] Request saved to commercial/frozen_request.json."
                if ctx.dry_run else f"Video saved to {video.relative_to(ctx.run_dir).as_posix()}.")

    def stop(reason: str) -> str:
        """Stop the current run and record the reason."""
        ledger.log({"run_id": ctx.run_id, "stage": "stop", "provider": "local",
                    "status": "stopped", "reason": reason, "cost_usd": 0.0,
                    "mock": False}, ctx.ledger_path)
        return f"Run stopped. {reason}"

    tools = {"get_run_context": get_run_context, "plan_orbit": plan_orbit,
             "latest_framing_status": latest_framing_status,
             "build_seedance_prompt": build_seedance_prompt,
             "submit_seedance": submit_seedance, "stop": stop}
    return {name: _answers_refusals(tool) for name, tool in tools.items()}
