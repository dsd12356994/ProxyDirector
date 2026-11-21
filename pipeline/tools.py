"""LLM 的工具层。上排（本地免费）与下排（付费）之间隔着闸门：submit_seedance 必须过
取景检查 + 人工确认（或显式 auto_approve）才放行。所有调用都写账本。
成片层可插拔：cfg.video_backend = runway | kling，工具签名不变、内部路由。"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import blender_step, kling, ledger, runway, tripo
from .models import FramingReport, ShotPlan
from .prompt_template import render_prompt


class GateRefused(RuntimeError):
    pass


@dataclass
class ToolContext:
    cfg: dict
    run_dir: Path
    run_id: str
    dry_run: bool
    auto_approve: bool
    budget_note: str = ""
    glb: Path = field(default=None)
    framing: FramingReport = field(default=None)
    prompt_path: Path = field(default=None)
    iterations: int = 0
    style_block: str = ""      # 提示词扩写产物（只附加到冻结模板末尾，非镜头维度）
    chat_session: bool = False
    turn_start_iteration: int = 0

    @property
    def ledger_path(self) -> Path:
        return Path(self.cfg["_root"]) / "data" / "ledger.jsonl"

    def paid_record_exists(self) -> bool:
        commercial = self.run_dir / "commercial"
        return any((commercial / name).is_file() for name in ("kling_state.json", "state.json"))

    def check_editable(self) -> None:
        if self.chat_session and self.paid_record_exists():
            raise GateRefused("This run already has a paid submission. Start a new session to change the shot.")


def make_tools(ctx: ToolContext):
    """返回 (工具函数们, 名称->函数 映射)。工具直接注册进 Pydantic AI Agent。"""

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
            return "Render limit reached. Use the latest result or stop this turn."
        if ctx.chat_session and ctx.framing is not None:
            # 已冻结的演练请求随上一版镜头保留。根目录只呈现当前镜头的提示词和请求。
            previous = Path(ctx.framing.video_path).parent
            for name in ("seedance_prompt.txt", "commercial"):
                artifact = ctx.run_dir / name
                if artifact.exists():
                    artifact.rename(previous / name)
        ctx.prompt_path = None
        ctx.framing = None
        ctx.iterations += 1
        tag = f"v{ctx.iterations}"
        fr = blender_step.render_orbit(ctx.cfg, ctx.run_dir, ctx.ledger_path,
                                       ctx.run_id, ctx.glb, plan, tag)
        ctx.framing = fr
        return fr.summary() + f" Reference video: {Path(fr.video_path).relative_to(ctx.run_dir)}"

    def latest_framing_status() -> str:
        """Read the latest framing audit, including any pose that misses the requested clearance."""
        if ctx.framing is None:
            return "No reference has been rendered yet. Run plan_orbit first."
        report = ctx.framing.model_dump()
        if ctx.framing.video_path:
            report["video_path"] = str(Path(ctx.framing.video_path).relative_to(ctx.run_dir))
        return json.dumps(report, ensure_ascii=False)

    def build_seedance_prompt(emphasis_notes: str = "") -> str:
        """Build the video prompt from an audited shot and the fixed template. Add up to two short emphasis notes."""
        ctx.check_editable()
        if ctx.framing is None or not ctx.framing.passed:
            raise GateRefused("Render a reference and pass the framing audit before building a prompt.")
        backend = ctx.cfg.get("video_backend", "runway")
        if backend == "kling":
            text = render_prompt(ctx.framing, ctx.cfg["kling"]["duration"], emphasis_notes,
                                 kling=True, style_block=ctx.style_block)
        else:
            text = render_prompt(ctx.framing, ctx.cfg["runway"]["duration"], emphasis_notes,
                                 style_block=ctx.style_block)
        ctx.prompt_path = ctx.run_dir / "seedance_prompt.txt"
        ctx.prompt_path.write_text(text, encoding="utf-8")
        ledger.log({"run_id": ctx.run_id, "stage": "prompt", "provider": "local",
                    "status": "built", "cost_usd": 0.0, "mock": False}, ctx.ledger_path)
        return f"Generation prompt saved to {ctx.prompt_path.name}.\n{text}"

    def submit_seedance(quality: str, confirm: bool) -> str:
        """Submit a video request after the framing check and prompt build, with explicit confirmation.
        Runway requests use the configured quality and rate for a 10-second video.
        Kling uses the configured model and resolution and charges credits."""
        ctx.check_editable()
        if ctx.framing is None or not ctx.framing.passed:
            raise GateRefused("Submission requires a passing framing audit.")
        if ctx.prompt_path is None or not ctx.prompt_path.exists():
            raise GateRefused("Build the generation prompt before submitting.")
        if not confirm:
            raise GateRefused("Explicit confirmation is required to submit a video request.")
        views_dir = Path(ctx.cfg["_root"]) / ctx.cfg["pipeline"]["views_dir"]
        view_files = {n: views_dir / n for n in tripo.SLOT_OF_VIEW}
        prompt_text = ctx.prompt_path.read_text(encoding="utf-8")
        backend = ctx.cfg.get("video_backend", "runway")

        if backend == "kling":
            est = kling.estimate_credits(ctx.cfg,
                                         ctx.cfg["pipeline"]["frames"] / ctx.cfg["pipeline"]["fps"],
                                         ctx.cfg["kling"]["duration"])
            if not ctx.dry_run and not ctx.auto_approve:
                answer = input(f"\n[Paid submission] Kling {ctx.cfg['kling']['model']} "
                               f"{ctx.cfg['kling']['resolution']} {ctx.cfg['kling']['duration']}s "
                               f"Estimated {est} credits. Type yes to submit: ").strip().lower()
                if answer != "yes":
                    ledger.log({"run_id": ctx.run_id, "stage": "seedance", "provider": "kling",
                                "status": "gate_declined_by_human", "cost_usd": 0.0, "mock": False},
                               ctx.ledger_path)
                    raise GateRefused("The user declined the paid submission.")
            body, record = kling.build_request(ctx.cfg, ctx.run_id, Path(ctx.framing.video_path),
                                               view_files, prompt_text, dry_run=ctx.dry_run)
            video = kling.submit(ctx.cfg, ctx.run_dir, ctx.ledger_path, ctx.run_id,
                                 body, record, ctx.dry_run)
            return ("[dry-run] Request saved to commercial/frozen_request.json."
                    if ctx.dry_run else f"Video saved to {video.relative_to(ctx.run_dir)}.")

        if quality not in ctx.cfg["runway"]["qualities"]:
            return f"Supported quality settings: {list(ctx.cfg['runway']['qualities'])}"
        if not ctx.dry_run and not ctx.auto_approve:
            q = ctx.cfg["runway"]["qualities"][quality]
            answer = input(f"\n[Paid submission] {quality} estimated at {q['credits']} credits (about USD {q['usd']:.2f}). "
                           f"Type yes to submit: ").strip().lower()
            if answer != "yes":
                ledger.log({"run_id": ctx.run_id, "stage": "seedance", "provider": "runway",
                            "status": "gate_declined_by_human", "cost_usd": 0.0, "mock": False},
                           ctx.ledger_path)
                raise GateRefused("The user declined the paid submission.")
        body, record = runway.build_request(ctx.cfg, Path(ctx.framing.video_path),
                                            view_files, prompt_text, quality)
        video = runway.submit(ctx.cfg, ctx.run_dir, ctx.ledger_path, ctx.run_id,
                              body, record, ctx.dry_run)
        return ("[dry-run] Request saved to commercial/frozen_request.json."
                if ctx.dry_run else f"Video saved to {video.relative_to(ctx.run_dir)}.")

    def stop(reason: str) -> str:
        """Stop the current run and record the reason."""
        ledger.log({"run_id": ctx.run_id, "stage": "stop", "provider": "local",
                    "status": "stopped", "reason": reason, "cost_usd": 0.0,
                    "mock": False}, ctx.ledger_path)
        return f"Run stopped. {reason}"

    return {"get_run_context": get_run_context, "plan_orbit": plan_orbit,
            "latest_framing_status": latest_framing_status,
            "build_seedance_prompt": build_seedance_prompt,
            "submit_seedance": submit_seedance, "stop": stop}
