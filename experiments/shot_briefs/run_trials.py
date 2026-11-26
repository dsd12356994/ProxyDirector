"""Run every (backend, brief, repetition) trial of the shot-brief benchmark with real LLM directors.

  .venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --reps 3
  .venv/bin/python experiments/shot_briefs/run_trials.py --backends astra --reps 3   # permitted network only

Each finished trial is appended to data/experiments/shot_briefs/trials.jsonl as soon as it ends, and
a trial that already has a summary is not run again, so an interrupted batch resumes with the same
command. aggregate.py builds the tables. Cost: about USD 0.002-0.005 of model calls per trial;
nothing else is paid for.
"""
import argparse
import json
import random
import shutil
import subprocess
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from briefs import BRIEFS
from harness import FIXED_LENS_MM, REPO, collect_and_grade, run_llm_trial
from pipeline.config import load_config

OUT_ROOT = REPO / "data" / "experiments" / "shot_briefs"


def load_ground_truth(out: Path) -> dict:
    path = out / "ground_truth.json"
    return json.loads(path.read_text(encoding="utf-8")).get("ground_truth", {}) if path.exists() else {}


def run_task(task: tuple[str, str, int, str]) -> tuple[dict, bool]:
    """Run one trial. Returns (trial record, whether it ran in this invocation)."""
    backend, brief_id, rep, out_root = task
    brief = next(b for b in BRIEFS if b["id"] == brief_id)
    out = Path(out_root)
    run_dir = out / "trials" / backend / f"{brief_id}_r{rep}"
    if (run_dir / "summary.json").exists():
        return json.loads((run_dir / "summary.json").read_text(encoding="utf-8")), False
    shutil.rmtree(run_dir, ignore_errors=True)          # leftover of an interrupted trial
    run_dir.mkdir(parents=True)
    result = run_llm_trial(backend, brief, run_dir)
    result["rep"] = rep
    result["trial_dir"] = str(run_dir.relative_to(out))
    return collect_and_grade(brief, run_dir, result, load_ground_truth(out)), True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backends", nargs="+", required=True, help="Keys under backends: in config.yaml")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--briefs", nargs="*", default=None, help="Brief ids to run (default: every brief)")
    parser.add_argument("--workers", type=int, default=5, help="Trials running at the same time")
    parser.add_argument("--out", default=str(OUT_ROOT))
    args = parser.parse_args()

    briefs = [b for b in BRIEFS if not args.briefs or b["id"] in args.briefs]
    tasks = [(backend, b["id"], rep, args.out)
             for rep in range(1, args.reps + 1) for b in briefs for backend in args.backends]
    random.Random(0).shuffle(tasks)                    # interleave backends and briefs
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = load_config()
    protocol = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                                      text=True).stdout.strip(),
        "backends": {b: config["backends"][b].get("model") for b in args.backends},
        "reps": args.reps, "briefs": briefs,
        "fixed_lens_mm": FIXED_LENS_MM,
        "max_renders_per_trial": config["pipeline"]["max_orbit_iterations"],
        "system_prompt": "pipeline.director.SYSTEM_PROMPT without the team wiki (as in the November runs)",
        "baseline_plan_in_context": False, "video_submission": False,
        "model_settings": "provider defaults; no seed",
        "design_note": "Protocol fixed after a four-run pilot on one low-angle and one high-angle search "
                       "brief per backend; the pilot runs are not part of these results.",
    }
    (out / f"protocol_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=1), encoding="utf-8")

    ran = cached = failed = 0
    with (out / "trials.jsonl").open("a", encoding="utf-8") as record, \
            ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_task, task): task for task in tasks}
        for done, future in enumerate(as_completed(futures), 1):
            task = futures[future]
            try:
                result, was_run = future.result()
            except Exception:                          # e.g. a problem in grading or indexing
                result, was_run = {"backend": task[0], "brief": task[1], "rep": task[2], "renders": [],
                                   "error": traceback.format_exc()[-1500:]}, True
            if was_run:
                ran += 1
                failed += bool(result.get("error"))
                record.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                record.flush()
            else:
                cached += 1
            print(f"[{done}/{len(tasks)}] {task[0]:9s} {task[1]:22s} r{task[2]}  "
                  f"{result.get('outcome', 'ERROR'):>20s}  renders={len(result.get('renders', []))}"
                  f"  cost={result.get('cost_usd')}  {'ERROR' if result.get('error') else ''}"
                  f"{'' if was_run else '  (already finished)'}", flush=True)
    print(f"Finished: {ran} trial(s) run, {failed} with an agent error, {cached} already finished. "
          f"Run aggregate.py for the tables.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
