"""Shared harness for the shot-brief benchmark (protocol and rationale: README.md).

The production pipeline is imported read-only. Three differences from a production run are
applied in memory, never on disk:
  1. renders go to fixed_lens_worker/worker_render.py, a copy of the production worker with a
     fixed lens, so the framing audit can fail;
  2. get_run_context no longer hands the model the fixed baseline plan;
  3. video submission is disabled and the trial ledger lives inside the trial folder.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (str(REPO), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

from pydantic_ai import Agent, Tool                    # noqa: E402
from pipeline import blender_step, director            # noqa: E402
from pipeline.config import load_config                # noqa: E402
from pipeline.tools import ToolContext, make_tools     # noqa: E402

from briefs import GRADED_AGAINST_GROUND_TRUTH, RENDER_NOTE, grade, optimal_radius   # noqa: E402
from make_fixed_lens_worker import FIXED_LENS_MM                     # noqa: E402


class TrialContext(ToolContext):
    @property
    def ledger_path(self) -> Path:
        return self.run_dir / "trial_ledger.jsonl"     # data/ledger.jsonl is never written


def prepare(run_dir: Path) -> tuple[dict, dict, int]:
    """Build the production tools with the benchmark's in-memory changes. Returns (cfg, tools, max_renders)."""
    cfg = load_config()
    blender_step.__file__ = str(HERE / "fixed_lens_worker" / "worker_render.py")
    ctx = TrialContext(cfg=cfg, run_dir=run_dir, run_id=run_dir.name, dry_run=True, auto_approve=False)
    ctx.glb = REPO / cfg["pipeline"]["seed_glb"]
    tools = make_tools(ctx)
    production_context = tools["get_run_context"]

    def get_run_context() -> str:
        """Read the shot requirements, available budget and submission mode."""
        context = json.loads(production_context())
        del context["baseline_plan"]
        return json.dumps(context, ensure_ascii=False)

    def submit_seedance(quality: str, confirm: bool) -> str:
        """Video submission is disabled in this experiment."""
        return "Video submission is disabled in this experiment. Report your final camera plan."

    tools["get_run_context"] = get_run_context
    tools["submit_seedance"] = submit_seedance
    return cfg, tools, int(cfg["pipeline"]["max_orbit_iterations"])


def _transcript(run) -> tuple[list[dict], int, bool]:
    events, retries, stopped = [], 0, False
    for message in run.all_messages():
        for part in message.parts:
            kind = part.part_kind
            if kind == "tool-call":
                args = part.args if part.args is not None else str(getattr(part, "args_json", ""))[:300]
                events.append({"call": part.tool_name, "args": args})
                stopped = stopped or part.tool_name == "stop"
            elif kind == "tool-return":
                events.append({"return": str(part.content)[:400]})
            elif kind == "retry-prompt":
                retries += 1
                events.append({"retry": str(part.content)[:300]})
            elif kind == "text" and str(part.content).strip():
                events.append({"text": str(part.content)[:400]})
    return events, retries, stopped


def run_llm_trial(backend: str, brief: dict, run_dir: Path) -> dict:
    """One agent run. Returns the trial record; `error` is set when no agent run completed."""
    cfg, _, max_renders = prepare(run_dir)
    backend_cfg = dict(cfg["backends"][backend], _name=backend)
    result = {"backend": backend, "model": backend_cfg.get("model"), "brief": brief["id"],
              "family": brief["family"], "requests": None, "input_tokens": None, "output_tokens": None,
              "cost_usd": None, "validation_retries": 0, "stop_called": False, "final_text": "",
              "transcript": [], "error": None, "attempts": 0}
    prompt = f"{brief['text']}\n\n{RENDER_NOTE.format(n=max_renders)}"
    for attempt in range(3):                           # a provider rate limit restarts on an empty folder
        result["attempts"] = attempt + 1
        started = time.time()
        try:
            _, tools, _ = prepare(run_dir)
            agent = Agent(model=director.build_model(backend_cfg),
                          tools=[Tool(fn, sequential=True) for fn in tools.values()],
                          system_prompt=director.SYSTEM_PROMPT, retries=1)
            run = agent.run_sync(prompt, model_settings=backend_cfg.get("model_settings") or None)
        except Exception as exc:
            if "429" in str(exc) and attempt < 2:
                run_dir.rename(run_dir.with_name(f"{run_dir.name}_attempt{attempt + 1}_ratelimited"))
                run_dir.mkdir()
                time.sleep(30 * (attempt + 1))
                continue
            result["error"] = traceback.format_exc()[-1500:]
            break
        usage = run.usage
        result.update(requests=usage.requests, input_tokens=usage.input_tokens,
                      output_tokens=usage.output_tokens,
                      cost_usd=float(usage.cost) if usage.cost is not None else None,
                      final_text=str(run.output)[:1500])
        result["transcript"], result["validation_retries"], result["stop_called"] = _transcript(run)
        break
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


def collect_and_grade(brief: dict, run_dir: Path, result: dict, ground_truth: dict) -> dict:
    """Keep framing.json of every render, delete the heavy render output, grade the trial."""
    renders = []
    for folder in sorted(run_dir.glob("render_v*"), key=lambda p: int(p.name[len("render_v"):])):
        try:
            report = json.loads((folder / "framing.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            renders.append({"tag": folder.name, "passed": None, "plan": None})
            continue
        renders.append({"tag": folder.name, "plan": report["plan"], "passed": bool(report["passed"]),
                        "min_margin": round(min(report["min_margins_lbrt"]), 5)})
        (run_dir / "framing").mkdir(exist_ok=True)
        shutil.copy2(folder / "framing.json", run_dir / "framing" / f"{folder.name}.json")
        shutil.rmtree(folder, ignore_errors=True)      # 300 PNG frames, scene.blend and mp4 are not evidence
    renders = [r for r in renders if r["plan"] is not None]
    best = optimal_radius(brief["id"], ground_truth.get(brief["id"], {}))
    if brief["id"] not in GRADED_AGAINST_GROUND_TRUTH:
        best = None
    result["renders"] = renders
    result.update(grade(brief, renders, best))
    (run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                          encoding="utf-8")
    return result
