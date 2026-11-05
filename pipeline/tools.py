"""LLM 的工具层。上排（本地免费）与下排（付费）之间隔着闸门：submit_seedance 必须过
取景检查 + 人工确认（或显式 auto_approve）才放行。所有调用都写账本。"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import blender_step, ledger, runway, tripo
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

    @property
    def ledger_path(self) -> Path:
        return Path(self.cfg["_root"]) / "data" / "ledger.jsonl"


def make_tools(ctx: ToolContext):
    """返回 (工具函数们, 名称->函数 映射)。工具直接注册进 Pydantic AI Agent。"""

    def get_run_context() -> str:
        """获取任务背景：镜头要求、预算与已花费、当前模式。先调这个。"""
        spent = ledger.spent_usd(ctx.ledger_path)
        return json.dumps({
            "object": "有线鼠标（黑）：本体、两主键、滚轮、顶键、两侧键、单根连续线缆、单 USB-A 接头",
            "shot_requirements": "10 秒完整 360° 连续环绕，四侧依次呈现，全部部件每帧入画，"
                                 "构图紧凑（安全边距约 5%），无切镜无反转",
            "budget_usd": ctx.cfg["budget"]["max_usd"], "spent_usd_all_runs": spent,
            "dry_run": ctx.dry_run, "auto_approve": ctx.auto_approve,
            "max_orbit_iterations": ctx.cfg["pipeline"]["max_orbit_iterations"],
            "glb_frozen_sha256": tripo.digest(ctx.glb) if ctx.glb else None,
            "note": "plan_orbit 与取景检查免费，可反复调；submit_seedance 花钱，需先通过取景检查",
        }, ensure_ascii=False)

    def plan_orbit(plan: ShotPlan) -> str:
        """[免费] 按相机方案渲染 10 秒环绕参考视频并做逐帧取景审计。返回审计结果；未通过就调参重试。"""
        if ctx.iterations >= ctx.cfg["pipeline"]["max_orbit_iterations"]:
            return "已达本地渲染迭代上限；请基于最后一次结果决策，或调用 stop。"
        ctx.iterations += 1
        tag = f"v{ctx.iterations}"
        fr = blender_step.render_orbit(ctx.cfg, ctx.run_dir, ctx.ledger_path,
                                       ctx.run_id, ctx.glb, plan, tag)
        ctx.framing = fr
        return fr.summary() + f" 参考视频：{fr.video_path}"

    def latest_framing_status() -> str:
        """[免费] 查看最近一次取景审计的完整数据（含未通过的逐帧越界明细）。"""
        if ctx.framing is None:
            return "还没有渲染过；先调用 plan_orbit。"
        return json.dumps(ctx.framing.model_dump(), ensure_ascii=False)

    def build_seedance_prompt(emphasis_notes: str = "") -> str:
        """[免费] 用已通过的取景结果按冻结模板生成 Seedance 提示词。emphasis_notes 可加 1-2 句补充要求。"""
        if ctx.framing is None or not ctx.framing.passed:
            raise GateRefused("提示词要基于取景通过的渲染结果生成；先 plan_orbit 并通过检查")
        text = render_prompt(ctx.framing, ctx.cfg["runway"]["duration"], emphasis_notes)
        ctx.prompt_path = ctx.run_dir / "seedance_prompt.txt"
        ctx.prompt_path.write_text(text, encoding="utf-8")
        ledger.log({"run_id": ctx.run_id, "stage": "prompt", "provider": "local",
                    "status": "built", "cost_usd": 0.0, "mock": False}, ctx.ledger_path)
        return f"提示词已生成（{ctx.prompt_path}）：\n{text}"

    def submit_seedance(quality: str, confirm: bool) -> str:
        """[付费闸门] 提交 Seedance 生成。前置条件：取景通过 + 提示词已生成 + confirm=true。
        价格：480p=$3.00 / 720p=$4.50（10 秒）。"""
        if quality not in ctx.cfg["runway"]["qualities"]:
            return f"quality 只支持 {list(ctx.cfg['runway']['qualities'])}"
        if ctx.framing is None or not ctx.framing.passed:
            raise GateRefused("取景检查未通过，禁止提交")
        if ctx.prompt_path is None or not ctx.prompt_path.exists():
            raise GateRefused("提示词未生成，禁止提交")
        if not confirm:
            raise GateRefused("confirm=false：付费提交必须显式确认")
        if not ctx.dry_run and not ctx.auto_approve:
            q = ctx.cfg["runway"]["qualities"][quality]
            answer = input(f"\n[付费闸门] {quality} 预计 {q['credits']} 积分 ≈ ${q['usd']:.2f}，"
                           f"输入 yes 提交 / 其他放弃： ").strip().lower()
            if answer != "yes":
                ledger.log({"run_id": ctx.run_id, "stage": "seedance", "provider": "runway",
                            "status": "gate_declined_by_human", "cost_usd": 0.0, "mock": False},
                           ctx.ledger_path)
                raise GateRefused("人工闸门未放行")
        views_dir = Path(ctx.cfg["_root"]) / ctx.cfg["pipeline"]["views_dir"]
        view_files = {n: views_dir / n for n in tripo.SLOT_OF_VIEW}
        body, record = runway.build_request(ctx.cfg, Path(ctx.framing.video_path),
                                            view_files, ctx.prompt_path.read_text(encoding="utf-8"),
                                            quality)
        video = runway.submit(ctx.cfg, ctx.run_dir, ctx.ledger_path, ctx.run_id,
                              body, record, ctx.dry_run)
        return ("[dry-run] 请求已冻结未触网：" if ctx.dry_run else "生成完成：") + str(video)

    def stop(reason: str) -> str:
        """停止本次运行并给出原因。"""
        ledger.log({"run_id": ctx.run_id, "stage": "stop", "provider": "local",
                    "status": "stopped", "reason": reason, "cost_usd": 0.0,
                    "mock": False}, ctx.ledger_path)
        return f"已停止：{reason}"

    return {"get_run_context": get_run_context, "plan_orbit": plan_orbit,
            "latest_framing_status": latest_framing_status,
            "build_seedance_prompt": build_seedance_prompt,
            "submit_seedance": submit_seedance, "stop": stop}
