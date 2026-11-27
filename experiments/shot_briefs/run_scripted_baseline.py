"""Fair deterministic scripted arms (`script_one` and `script_loop`) for the 2x2 benchmark.

Policy constraints (`Reply #004 §3.2`):
  - Inspects ONLY `brief["text"]` (the exact string shown to LLM directors) via regular expressions —
    NEVER `brief["spec"]` or `brief["family"]`.
  - When `elevation_degrees` is not given numerically in `brief["text"]` (`L1` and `L2`), records
    `outcome = "not_attempted"`.
  - Runs two deterministic policies across `MAIN_BRIEF_IDS`:
      1. `script_one` (`max_renders = 1`): tries `min(6.0, radius_cap)` once (`1/6` goal successes).
      2. `script_loop` (`max_renders = 6`): bisects `[3.0, radius_cap]` in `0.5` steps (`4/6` goal successes).

  .venv/bin/python experiments/shot_briefs/run_scripted_baseline.py
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

from briefs import BRIEF_BY_ID, MAIN_BRIEF_IDS, is_goal_success
from harness import BENCHMARK_MAX_RENDERS, REPO, collect_and_grade, prepare
from pipeline.models import ShotPlan

DEFAULT_OUT = REPO / "data" / "experiments" / "audit_loop"
POLICY_ONE = "regex brief-text parser + single probe min(6.0, cap)"
POLICY_LOOP = "regex brief-text parser + binary search over [3.0, cap]"


def parse_brief_text(text: str) -> dict | None:
    """Parse explicit numeric parameters from `brief['text']` alone, or `None` if underspecified."""
    elev_match = re.search(r"\belevation_degrees\s+(?:at\s+|exactly\s+)?(-?\d+(?:\.\d+)?)", text, re.I)
    if not elev_match:
        return None
    elevation = float(elev_match.group(1))

    if re.search(r"\bcounter-?clockwise\b|\bccw\b", text, re.I):
        direction = "ccw"
    else:
        direction = "cw"

    start_match = re.search(r"\bstart_degrees\s+(-?\d+(?:\.\d+)?)", text, re.I)
    start = float(start_match.group(1)) if start_match else 0.0

    margin_match = re.search(r"\bmargin\s+(\d+(?:\.\d+)?)", text, re.I)
    margin = float(margin_match.group(1)) if margin_match else 0.05

    cap_match = re.search(r"\bradius\s+at\s+most\s+(\d+(?:\.\d+)?)", text, re.I)
    radius_cap = min(12.0, float(cap_match.group(1))) if cap_match else 12.0

    start_r_match = re.search(r"\bstart\s+with\s+radius\s+(\d+(?:\.\d+)?)", text, re.I)
    start_radius = float(start_r_match.group(1)) if start_r_match else None

    step_match = re.search(r"\bin\s+steps\s+of\s+(\d+(?:\.\d+)?)", text, re.I)
    step = float(step_match.group(1)) if step_match else 0.5

    minimize_radius = bool(re.search(r"\bsmallest\s+radius\b|\btightest\b", text, re.I))
    return {
        "elevation": elevation,
        "direction": direction,
        "start": start,
        "margin": margin,
        "radius_cap": radius_cap,
        "start_radius": start_radius,
        "step": step,
        "minimize_radius": minimize_radius,
    }


def run_brief(brief: dict, out_root: Path, ground_truth: dict, *, arm: str = "script_loop") -> dict:
    """Run one deterministic scripted arm (`script_one` or `script_loop`) on `brief`."""
    if arm not in ("script_one", "script_loop"):
        raise ValueError(f"Unsupported scripted arm: {arm!r}")
    max_renders = 1 if arm == "script_one" else BENCHMARK_MAX_RENDERS
    policy_label = POLICY_ONE if arm == "script_one" else POLICY_LOOP

    run_dir = out_root / "scripted" / arm / brief["id"]
    if run_dir.exists():
        run_dir = run_dir.with_name(f"{brief['id']}_{time.strftime('%H%M%S')}")
    run_dir.mkdir(parents=True)

    # IMPORTANT: Only `brief["text"]` may be inspected to choose camera plans.
    parsed = parse_brief_text(brief["text"])
    result: dict = {
        "arm": arm,
        "backend": arm,
        "model": policy_label,
        "brief": brief["id"],
        "family": brief.get("family"),
        "rep": 1,
        "max_renders": max_renders,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "all_attempts_cost_usd": 0.0,
        "cost_source": "deterministic_free",
        "validation_retries": 0,
        "stop_called": False,
        "final_text": "",
        "transcript": [],
        "error": None,
        "renders": [],
        "emphasis_camera_attempted": 0,
        "emphasis_camera_leaked": 0,
    }
    if parsed is None:
        result.update(
            outcome="not_attempted",
            render_count=0,
            compliant_render_count=0,
            passing_compliant_radius=None,
            optimal_radius=None,
            radius_gap=None,
            goal_success=False,
        )
        (run_dir / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result

    _, tools, budget = prepare(run_dir, max_renders=max_renders)
    elevation = parsed["elevation"]
    direction = parsed["direction"]
    start = parsed["start"]
    margin = parsed["margin"]
    step = parsed["step"]
    cap = parsed["radius_cap"]
    minimize_radius = parsed["minimize_radius"]

    def attempt(radius: float) -> bool:
        text = tools["plan_orbit"](
            ShotPlan(
                elevation_degrees=elevation,
                radius=radius,
                start_degrees=start,
                direction=direction,
                margin=margin,
            )
        )
        result["transcript"].append({"scripted plan": f"elevation {elevation:g}, radius {radius:g}"})
        return text.startswith("Framing passed.")

    best: float | None = None
    if arm == "script_one":
        probe_r = min(parsed["start_radius"] if parsed["start_radius"] is not None else 6.0, cap)
        probe_r = max(3.0, round(probe_r, 4))
        if attempt(probe_r):
            best = probe_r
    else:
        n_steps = max(0, int(round((cap - 3.0) / step)))
        candidates = [round(3.0 + i * step, 4) for i in range(n_steps + 1) if 3.0 + i * step <= cap + 1e-9]
        if not candidates or candidates[-1] < cap - 1e-9:
            candidates.append(round(cap, 4))

        lo, hi = 0, len(candidates) - 1
        used = 0
        first_idx: int | None = None
        if parsed["start_radius"] is not None:
            sr = parsed["start_radius"]
            first_idx = min(range(len(candidates)), key=lambda i: abs(candidates[i] - sr))

        while lo <= hi and used < budget and (best is None or minimize_radius):
            if used == 0 and first_idx is not None and lo <= first_idx <= hi:
                idx = first_idx
            else:
                idx = (lo + hi) // 2
            radius = candidates[idx]
            used += 1
            if attempt(radius):
                best = radius if best is None else min(best, radius)
                hi = idx - 1
            else:
                lo = idx + 1

    if best is None:
        stop_msg = tools["stop"]("No plan within the brief's limits passed the framing audit.")
    else:
        stop_msg = tools["stop"](f"Scripted policy ({arm}) finished. Smallest passing radius tried: {best}.")
    result.update(stop_called=True, final_text=stop_msg)
    return collect_and_grade(brief, run_dir, result, ground_truth)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run both deterministic scripted arms (`script_one`, `script_loop`).")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output directory (default: data/experiments/audit_loop)")
    parser.add_argument("--briefs", nargs="*", default=None, help="Brief IDs (default: 6 MAIN_BRIEF_IDS)")
    args = parser.parse_args(argv)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    truth_path = out_root / "ground_truth.json"
    ground_truth = (
        json.loads(truth_path.read_text(encoding="utf-8")).get("ground_truth", {})
        if truth_path.exists()
        else {}
    )
    shutil.rmtree(out_root / "scripted", ignore_errors=True)

    brief_ids = tuple(args.briefs) if args.briefs else MAIN_BRIEF_IDS
    briefs = [BRIEF_BY_ID[bid] for bid in brief_ids]

    results: list[dict] = []
    for arm in ("script_one", "script_loop"):
        arm_records = [run_brief(b, out_root, ground_truth, arm=arm) for b in briefs]
        results.extend(arm_records)
        goal_hits = sum(1 for r in arm_records if is_goal_success(r["brief"], r["outcome"]))
        for r in arm_records:
            print(
                f"{arm:12s} {r['brief']:22s} {r['outcome']:>20s}  "
                f"r={str(r.get('passing_compliant_radius')):>4s}  renders={len(r['renders'])}  "
                f"goal={'YES' if r.get('goal_success') else 'no'}"
            )
        print(f"  -> {arm}: {goal_hits}/{len(arm_records)} brief-goal successes\n")

    (out_root / "scripted_baseline.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
