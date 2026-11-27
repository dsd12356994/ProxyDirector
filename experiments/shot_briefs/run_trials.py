"""Run every (arm, backend, brief, repetition) LLM trial of the 2x2 audit-loop benchmark.

  .venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --arm llm_one --reps 1 --workers 2
  .venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --arm llm_loop --reps 1 --workers 2

Each finished trial is saved under `data/experiments/audit_loop/trials/<arm>/<backend>/<brief>_r<rep>/`
(with full `messages.json`, `framing/*.json`, `trial_ledger.jsonl`, and `summary.json`) and indexed in
`trials.jsonl`. Completed trials are skipped on resume, and `manifest_sha256.json` records SHA-256
digests of all saved artifacts.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import fast_audit
from briefs import BRIEFS, MAIN_BRIEF_IDS
from harness import (
    BENCHMARK_MAX_RENDERS,
    FIXED_LENS_MM,
    REPO,
    collect_and_grade,
    resolve_repo_commit,
    run_llm_trial,
    write_sha256_manifest,
)
from pipeline.config import load_config
from preflight import parse_kv_overrides

OUT_ROOT = REPO / "data" / "experiments" / "audit_loop"


def load_ground_truth(out: Path) -> dict:
    path = out / "ground_truth.json"
    return json.loads(path.read_text(encoding="utf-8")).get("ground_truth", {}) if path.exists() else {}


def resolve_backend_targets(
    out: Path,
    backends: list[str],
    cli_models: dict[str, str],
    cli_urls: dict[str, str],
) -> dict[str, dict[str, str | None]]:
    """Resolve `(model, base_url)` per backend from CLI overrides -> `preflight_models.json` -> `config.yaml`."""
    cfg = load_config()
    pf_path = out / "preflight_models.json"
    pf_backends = (
        json.loads(pf_path.read_text(encoding="utf-8")).get("backends", {})
        if pf_path.is_file()
        else {}
    )
    resolved: dict[str, dict[str, str | None]] = {}
    for b in backends:
        bcfg = cfg["backends"][b]
        pf = pf_backends.get(b, {})
        model = cli_models.get(b) or pf.get("resolved_model") or bcfg.get("model")
        base_url = cli_urls.get(b) or pf.get("base_url") or bcfg.get("base_url")
        resolved[b] = {"model": model, "base_url": base_url}
    return resolved


def _append_unique_trial(jsonl_path: Path, record: dict) -> None:
    """Append `record` to `trials.jsonl` unless `(arm, backend, brief, rep)` is already listed."""
    key = (record.get("arm"), record.get("backend"), record.get("brief"), int(record.get("rep", 1)))
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            existing = json.loads(line)
            ekey = (existing.get("arm"), existing.get("backend"), existing.get("brief"), int(existing.get("rep", 1)))
            if ekey == key:
                return
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run_task(
    task: tuple[str, int, str, str, int, str, bool, bool, str | None, str | None]
) -> tuple[dict, bool]:
    """Run one trial. Returns `(trial record, whether it ran in this invocation)`."""
    arm, max_renders, backend, brief_id, rep, out_root, include_wiki, fast_audit_mode, model_ov, url_ov = task
    brief = next(b for b in BRIEFS if b["id"] == brief_id)
    out = Path(out_root)
    run_dir = out / "trials" / arm / backend / f"{brief_id}_r{rep}"
    if (run_dir / "summary.json").exists():
        saved = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return saved, False
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    result = run_llm_trial(
        backend,
        brief,
        run_dir,
        arm=arm,
        max_renders=max_renders,
        include_wiki=include_wiki,
        fast_audit_mode=fast_audit_mode,
        model_override=model_ov,
        base_url_override=url_ov,
    )
    result["rep"] = rep
    result["trial_dir"] = run_dir.relative_to(out).as_posix()
    return collect_and_grade(brief, run_dir, result, load_ground_truth(out)), True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Part B 2x2 LLM camera-planning trials.")
    parser.add_argument("--backends", nargs="+", required=True, help="Keys under backends: in config.yaml")
    parser.add_argument("--arm", choices=("llm_one", "llm_loop", "both"), default=None,
                        help="Condition arm (`llm_one` = 1 audit, `llm_loop` = <=6 audits, `both` = both arms)")
    parser.add_argument("--max-renders", type=int, default=None,
                        help="Explicit render/audit budget per trial (defaults to 1 for llm_one, 6 for llm_loop)")
    parser.add_argument("--reps", type=int, default=1, help="Repetitions per cell (default: 1)")
    parser.add_argument("--briefs", nargs="*", default=None, help="Brief IDs to run (default: 6 MAIN_BRIEF_IDS)")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent trials (default: 2; 1 = sequential)")
    parser.add_argument("--executor", choices=("thread", "process"), default="thread")
    parser.add_argument("--model-override", nargs="*", default=None, help="backend=model_id overrides")
    parser.add_argument("--base-url-override", nargs="*", default=None, help="backend=base_url overrides")
    parser.add_argument("--no-wiki", action="store_true", help="Omit knowledge/wiki.md from system prompt")
    parser.add_argument("--blender", action="store_true", help="Use headless Blender + FFmpeg instead of fast_audit")
    parser.add_argument("--out", default=str(OUT_ROOT), help="Output directory (default: data/experiments/audit_loop)")
    args = parser.parse_args(argv)

    if args.arm == "both":
        arms = [("llm_one", 1), ("llm_loop", args.max_renders or BENCHMARK_MAX_RENDERS)]
    elif args.arm == "llm_one":
        arms = [("llm_one", args.max_renders or 1)]
    elif args.arm == "llm_loop":
        arms = [("llm_loop", args.max_renders or BENCHMARK_MAX_RENDERS)]
    elif args.max_renders == 1:
        arms = [("llm_one", 1)]
    else:
        arms = [("llm_loop", args.max_renders or BENCHMARK_MAX_RENDERS)]

    include_wiki = not args.no_wiki
    fast_audit_mode = not args.blender
    target_brief_ids = tuple(args.briefs) if args.briefs else MAIN_BRIEF_IDS
    briefs = [b for b in BRIEFS if b["id"] in target_brief_ids]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cli_models = parse_kv_overrides(args.model_override)
    cli_urls = parse_kv_overrides(args.base_url_override)
    targets = resolve_backend_targets(out, args.backends, cli_models, cli_urls)

    cfg = load_config()
    glb_path = REPO / cfg["pipeline"]["seed_glb"]
    mesh_sha = fast_audit.load_normalized_glb(glb_path).geom_sha256

    tasks = [
        (
            arm_name,
            arm_budget,
            backend,
            b["id"],
            rep,
            str(out),
            include_wiki,
            fast_audit_mode,
            targets[backend]["model"],
            targets[backend]["base_url"],
        )
        for rep in range(1, args.reps + 1)
        for arm_name, arm_budget in arms
        for b in briefs
        for backend in args.backends
    ]
    random.Random(0).shuffle(tasks)

    arm_tag = "_".join(a for a, _ in arms)
    protocol = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo_commit": resolve_repo_commit(REPO),
        "geometry_world_vertices_sha256": mesh_sha,
        "arms": [{"arm": a, "max_renders": m} for a, m in arms],
        "backends": targets,
        "reps": args.reps,
        "briefs": briefs,
        "fixed_lens_mm": FIXED_LENS_MM,
        "audit_backend": "blender" if args.blender else "fast_audit",
        "system_prompt": (
            "pipeline.director.SYSTEM_PROMPT (no wiki ablation)"
            if args.no_wiki
            else "pipeline.director.SYSTEM_PROMPT + pipeline.director._load_wiki()"
        ),
        "baseline_plan_in_context": False,
        "usage_limits_enforced": False,
        "video_submission": False,
        "model_settings": "provider defaults; no seed",
        "stopping_rule": (
            "Stop after 24 trials (2 models x 2 arms x 6 briefs) if llm_loop beats llm_one on at least "
            "7 of 8 paired threshold trials (S1, S2, S3, L1); otherwise run one additional repetition."
        ),
    }
    (out / f"protocol_{arm_tag}_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ran = cached = failed = 0
    jsonl_path = out / "trials.jsonl"
    pool_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor

    def handle_finished(done_idx: int, task_item, res: dict, was_run: bool) -> None:
        nonlocal ran, cached, failed
        if was_run:
            ran += 1
            failed += bool(res.get("error"))
        else:
            cached += 1
        _append_unique_trial(jsonl_path, res)
        print(
            f"[{done_idx}/{len(tasks)}] {task_item[0]:8s} {task_item[2]:9s} {task_item[3]:22s} r{task_item[4]}  "
            f"{res.get('outcome', 'ERROR'):>20s}  renders={len(res.get('renders', []))}  "
            f"cost={res.get('cost_usd')}  {'ERROR' if res.get('error') else ''}"
            f"{'' if was_run else '  (already finished)'}",
            flush=True,
        )

    if args.workers <= 1:
        for done, task in enumerate(tasks, 1):
            try:
                result, was_run = run_task(task)
            except Exception:
                result, was_run = {
                    "arm": task[0],
                    "max_renders": task[1],
                    "backend": task[2],
                    "brief": task[3],
                    "rep": task[4],
                    "renders": [],
                    "error": traceback.format_exc()[-1500:],
                }, True
            handle_finished(done, task, result, was_run)
    else:
        with pool_cls(max_workers=args.workers) as pool:
            futures = {pool.submit(run_task, task): task for task in tasks}
            for done, future in enumerate(as_completed(futures), 1):
                task = futures[future]
                try:
                    result, was_run = future.result()
                except Exception:
                    result, was_run = {
                        "arm": task[0],
                        "max_renders": task[1],
                        "backend": task[2],
                        "brief": task[3],
                        "rep": task[4],
                        "renders": [],
                        "error": traceback.format_exc()[-1500:],
                    }, True
                handle_finished(done, task, result, was_run)

    manifest = write_sha256_manifest(out)
    print(
        f"Finished: {ran} trial(s) run, {failed} with an agent error, {cached} already finished. "
        f"Manifest: {manifest}. Run aggregate.py for the tables.",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
