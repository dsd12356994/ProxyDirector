"""Non-LLM baseline for the shot-brief benchmark: a fixed policy driving the same tools.

Policy: start at radius 6. If the audit fails, widen by 1.0 until it passes. If it passes and the
brief asks for the tightest plan, tighten by 0.5 while it still passes. The render budget is the
same four renders the LLM directors get. Natural-language briefs are not attempted.

  .venv/bin/python experiments/shot_briefs/run_scripted_baseline.py
"""
import json
import time
from pathlib import Path

from briefs import BRIEFS
from harness import REPO, collect_and_grade, prepare
from pipeline.models import ShotPlan

OUT_ROOT = REPO / "data" / "experiments" / "shot_briefs"


def run_brief(brief: dict, out_root: Path, ground_truth: dict) -> dict:
    run_dir = out_root / "scripted" / brief["id"]
    if run_dir.exists():
        run_dir = run_dir.with_name(f"{brief['id']}_{time.strftime('%H%M%S')}")
    run_dir.mkdir(parents=True)
    result = {"backend": "scripted", "model": "fixed policy (radius 6, widen 1.0, tighten 0.5)",
              "brief": brief["id"], "family": brief["family"], "rep": 1, "requests": 0,
              "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "validation_retries": 0,
              "stop_called": False, "final_text": "", "transcript": [], "error": None, "renders": []}
    spec = brief["spec"]
    if brief["family"] == "language":
        result["outcome"] = "not_attempted"
        (run_dir / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result

    _, tools, budget = prepare(run_dir)
    lo, hi = spec["elevation"]
    elevation = lo if lo == hi else round((lo + hi) / 2, 1)

    def attempt(radius: float) -> str:
        text = tools["plan_orbit"](ShotPlan(elevation_degrees=elevation, radius=radius,
                                            start_degrees=spec["start"], direction=spec["direction"],
                                            margin=spec["margin"]))
        result["transcript"].append({"scripted plan": f"elevation {elevation}, radius {radius}"})
        return text

    def passed(text: str) -> bool:
        return text.startswith("Framing passed.")

    cap = spec.get("radius_max")
    if cap is not None:
        attempt(cap)
        result.update(stop_called=True, final_text=tools["stop"]("No plan within the radius cap passes the framing audit."))
    else:
        radius, used, best, step = 6.0, 1, None, (0.5 if brief["family"] == "search" else 1.0)
        best = radius if passed(attempt(radius)) else None
        while best is None and used < budget:
            radius += 1.0
            used += 1
            if passed(attempt(radius)):
                best = radius
        while best is not None and brief["family"] == "search" and used < budget and radius - step >= 3.0:
            radius -= step
            used += 1
            if not passed(attempt(radius)):
                break
            best = radius
        result.update(stop_called=True, final_text=tools["stop"](f"Scripted policy finished. Smallest passing radius tried: {best}."))
    return collect_and_grade(brief, run_dir, result, ground_truth)


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    truth_path = OUT_ROOT / "ground_truth.json"
    ground_truth = (json.loads(truth_path.read_text(encoding="utf-8")).get("ground_truth", {})
                    if truth_path.exists() else {})
    results = [run_brief(brief, OUT_ROOT, ground_truth) for brief in BRIEFS]
    (OUT_ROOT / "scripted_baseline.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for r in results:
        print(f"scripted  {r['brief']:22s} {r['outcome']:>20s}  renders={len(r['renders'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
