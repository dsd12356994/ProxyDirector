"""Build `results.csv` and `summary.md` for the 4-Planner x 6-Brief 2x2 Audit-Loop Benchmark.

  .venv/bin/python experiments/shot_briefs/aggregate.py --out data/experiments/audit_loop

Re-grades every indexed trial (`trials.jsonl` + `scripted_baseline.json`) against `ground_truth.json`,
applies per-1M fallback pricing if `cost_usd` is `None`, refuses to overwrite a larger `results.csv`
unless `--allow-fewer-trials` is given, refreshes `manifest_sha256.json`, and outputs:
  1. The 4-Planner x 6-Brief Main Table (`Script (1 audit)` | `Script (loop, <=6)` |
     `LLM (1 audit)` | `LLM (loop, <=6)` — both pooled and per-model);
  2. The Pre-Stated Paired Comparison on `(S1, S2, S3, L1)` (`tight > wide > fail`) with exact
     one-sided and two-sided sign-test p-values;
  3. Per-arm / per-model Audits Used, Requests, Input/Output Tokens, USD Cost, Radius Gap to
     Tightest, L2 `[60°, 85°]` vs `[70°, 85°]` sensitivity, and Prompt Camera-Word Leakage (`0`).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from briefs import (
    BRIEFS,
    GRADED_AGAINST_GROUND_TRUTH,
    MAIN_BRIEF_IDS,
    STRICT_SUCCESS,
    SUCCESS,
    compliant,
    grade,
    is_goal_success,
    optimal_radius,
)
from harness import REPO, estimate_cost_usd, write_sha256_manifest
from pipeline.camera_words import camera_terms, framing_terms

OUT = REPO / "data" / "experiments" / "audit_loop"

SHORT = {
    "optimal": "tight",
    "pass_suboptimal": "wide",
    "pass": "pass",
    "no_pass": "fail",
    "no_pass_within_cap": "honest",
    "cap_violated": "cap+",
    "unexpected_pass": "pass?!",
    "not_attempted": "n/a",
}
PLANNER_ARMS = (
    ("script_one", "Script (1 audit)"),
    ("script_loop", "Script (loop, <=6)"),
    ("llm_one", "LLM (1 audit)"),
    ("llm_loop", "LLM (loop, <=6)"),
)
THRESHOLD_BRIEFS = ("S1_search_e20", "S2_search_e70", "S3_search_e50_m10", "L1_lang_low_tight")


def infer_arm(trial: dict) -> str:
    arm = trial.get("arm")
    if arm in ("script_one", "script_loop", "llm_one", "llm_loop"):
        return str(arm)
    backend = str(trial.get("backend") or "")
    if backend in ("script_one", "script_loop"):
        return backend
    max_r = trial.get("max_renders")
    if backend == "scripted":
        return "script_one" if max_r == 1 else "script_loop"
    return "llm_one" if max_r == 1 else "llm_loop"


def load_trials(out_dir: Path) -> list[dict]:
    """Load indexed LLM trials (`trials.jsonl`) plus deterministic scripted arms (`scripted_baseline.json`)."""
    trials: list[dict] = []
    record = out_dir / "trials.jsonl"
    if record.exists():
        trials += [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]
    scripted = out_dir / "scripted_baseline.json"
    if scripted.exists():
        trials += json.loads(scripted.read_text(encoding="utf-8"))
    return trials


def regrade(trial: dict, truth: dict) -> dict:
    updated = dict(trial)
    updated["arm"] = infer_arm(updated)
    if updated.get("max_renders") is None:
        updated["max_renders"] = 1 if updated["arm"] in ("script_one", "llm_one") else 6

    # Ensure cost_usd uses explicit per-1M fallback if provider/genai_prices left it None
    if updated["arm"] in ("script_one", "script_loop"):
        updated["cost_usd"] = 0.0
        updated["all_attempts_cost_usd"] = 0.0
        updated.setdefault("cost_source", "deterministic_free")
    elif updated.get("cost_usd") is None and (
        updated.get("input_tokens") is not None or updated.get("output_tokens") is not None
    ):
        est, src = estimate_cost_usd(
            updated.get("provider_model") or updated.get("model"),
            updated.get("backend"),
            updated.get("input_tokens"),
            updated.get("output_tokens"),
            reported_cost=None,
        )
        updated["cost_usd"] = est
        updated["cost_source"] = src
        if not updated.get("all_attempts_cost_usd") and est is not None:
            updated["all_attempts_cost_usd"] = est

    if updated.get("outcome") == "not_attempted":
        updated.setdefault("render_count", 0)
        updated.setdefault("passing_compliant_radius", None)
        updated.setdefault("optimal_radius", None)
        updated.setdefault("radius_gap", None)
        updated["goal_success"] = False
        return updated

    brief = next((b for b in BRIEFS if b["id"] == updated["brief"]), None)
    if brief is None:
        return updated
    renders = [r for r in updated.get("renders", []) if r.get("plan")]
    best = (
        optimal_radius(updated["brief"], truth.get(updated["brief"], {}))
        if updated["brief"] in GRADED_AGAINST_GROUND_TRUTH
        else None
    )
    return {**updated, **grade(brief, renders, best)}


def fmt_mean(values: list, digits: int = 3) -> str:
    clean = [float(v) for v in values if v is not None]
    return f"{statistics.mean(clean):.{digits}g}" if clean else "-"


def has_camera_or_framing_words(note: str) -> bool:
    return bool(camera_terms(note) or framing_terms(note))


def emphasis_stats(trial: dict) -> tuple[int, int, int]:
    """Return `(nonempty_notes, with_camera_words, leaked_into_prompt)` for one trial."""
    events = list(trial.get("transcript", []))
    nonempty = 0
    cam_attempted = 0
    cam_leaked_transcript = 0
    for idx, event in enumerate(events):
        if event.get("call") != "build_seedance_prompt":
            continue
        args = event.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        note = (args.get("emphasis_notes") or "").strip() if isinstance(args, dict) else ""
        if not note:
            continue
        nonempty += 1
        if has_camera_or_framing_words(note):
            cam_attempted += 1
            ret_text = ""
            for nxt in events[idx + 1 :]:
                if "return" in nxt:
                    ret_text = str(nxt["return"])
                    break
                if "call" in nxt:
                    break
            if ret_text and not ret_text.startswith("Refused:") and "The emphasis note was left out:" not in ret_text:
                cam_leaked_transcript += 1

    if trial.get("emphasis_camera_leaked") is not None:
        leaked = int(trial["emphasis_camera_leaked"])
    else:
        leaked = cam_leaked_transcript
    if trial.get("emphasis_camera_attempted") is not None:
        cam_attempted = max(cam_attempted, int(trial["emphasis_camera_attempted"]))
    return nonempty, cam_attempted, leaked


def format_cell(records: list[dict]) -> str:
    if not records:
        return "not run"
    if len(records) == 1:
        r = records[0]
        label = SHORT.get(r["outcome"], r["outcome"])
        rad = r.get("passing_compliant_radius")
        return f"{label} (`r={rad:g}`)" if rad is not None else label
    counts = Counter(SHORT.get(r["outcome"], r["outcome"]) for r in records)
    return " · ".join(f"{n} {name}" for name, n in counts.most_common())


def threshold_rank(outcome: str) -> int:
    """Pre-stated ordinal ranking on threshold briefs `(S1, S2, S3, L1)`: `tight > wide > fail`."""
    if outcome == "optimal":
        return 2
    if outcome == "pass_suboptimal":
        return 1
    return 0


def exact_sign_test_pvalues(wins: int, losses: int) -> tuple[float | None, float | None]:
    """Exact binomial sign-test `(one_sided_p, two_sided_p)` under `H0: p = 0.5` over non-tied pairs."""
    n = wins + losses
    if n == 0:
        return None, None
    denom = 2 ** n
    one_sided = sum(math.comb(n, k) for k in range(wins, n + 1)) / denom
    tail_extreme = sum(math.comb(n, k) for k in range(max(wins, losses), n + 1)) / denom
    two_sided = min(1.0, 2.0 * tail_extreme)
    return one_sided, two_sided


def paired_threshold_comparison(trials: list[dict]) -> dict:
    """Compare `(backend, rep, brief)` pairs between `llm_one` and `llm_loop` on `(S1, S2, S3, L1)`."""
    one_map: dict[tuple[str, int, str], dict] = {}
    loop_map: dict[tuple[str, int, str], dict] = {}
    for t in trials:
        b_id = t.get("brief")
        if b_id not in THRESHOLD_BRIEFS:
            continue
        key = (str(t.get("backend")), int(t.get("rep") or 1), str(b_id))
        if t.get("arm") == "llm_one":
            one_map[key] = t
        elif t.get("arm") == "llm_loop":
            loop_map[key] = t

    shared_keys = sorted(set(one_map) & set(loop_map))
    wins = ties = losses = 0
    details = []
    for key in shared_keys:
        t_one, t_loop = one_map[key], loop_map[key]
        r_one, r_loop = threshold_rank(t_one["outcome"]), threshold_rank(t_loop["outcome"])
        if r_loop > r_one:
            wins += 1
            verdict = "loop_win"
        elif r_loop < r_one:
            losses += 1
            verdict = "one_win"
        else:
            ties += 1
            verdict = "tie"
        details.append({
            "backend": key[0],
            "rep": key[1],
            "brief": key[2],
            "llm_one_outcome": t_one["outcome"],
            "llm_one_radius": t_one.get("passing_compliant_radius"),
            "llm_loop_outcome": t_loop["outcome"],
            "llm_loop_radius": t_loop.get("passing_compliant_radius"),
            "verdict": verdict,
        })

    p1_nontied, p2_nontied = exact_sign_test_pvalues(wins, losses)
    # Conservative all-pairs binomial test treating ties as non-wins (`wins` vs `ties + losses`):
    p1_all, p2_all = exact_sign_test_pvalues(wins, ties + losses)
    return {
        "pairs_total": len(shared_keys),
        "loop_wins": wins,
        "ties": ties,
        "one_wins": losses,
        "one_sided_p_nontied": p1_nontied,
        "two_sided_p_nontied": p2_nontied,
        "one_sided_p_all_pairs": p1_all,
        "two_sided_p_all_pairs": p2_all,
        "details": details,
    }


def trials_in_table(path: Path) -> int:
    """How many trials a `results.csv` on disk lists (0 when absent or unreadable)."""
    if not path.exists():
        return 0
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build results.csv and summary.md from recorded trials.")
    parser.add_argument("--out", default=None, help="Experiment folder (default: data/experiments/audit_loop)")
    parser.add_argument("--allow-fewer-trials", action="store_true",
                        help="Replace existing tables even when fewer trials are indexed than currently listed")
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_path = out_dir / "ground_truth.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8")).get("ground_truth", {}) if truth_path.exists() else {}

    trials = [regrade(t, truth) for t in load_trials(out_dir)]
    if not trials:
        raise SystemExit("No trials indexed yet.")

    listed = trials_in_table(out_dir / "results.csv")
    if len(trials) < listed and not args.allow_fewer_trials:
        reason = (
            ""
            if (out_dir / "trials.jsonl").exists()
            else " trials.jsonl is not in this folder: it holds the LLM trials and is kept out of Git, so a "
                 "fresh clone only has the scripted baseline."
        )
        raise SystemExit(
            f"Not writing: {out_dir / 'results.csv'} lists {listed} trials and this run has only "
            f"{len(trials)}, so the tables would lose results.{reason} Copy the per-trial records "
            "from the machine that ran the batch, or pass --allow-fewer-trials to replace the tables anyway."
        )

    columns = (
        "arm",
        "backend",
        "model",
        "provider_model",
        "brief",
        "family",
        "rep",
        "max_renders",
        "outcome",
        "goal_success",
        "success",
        "success_strict",
        "render_count",
        "first_try",
        "passing_compliant_radius",
        "optimal_radius",
        "radius_gap",
        "requests",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "all_attempts_cost_usd",
        "cost_source",
        "emphasis_camera_attempted",
        "emphasis_camera_leaked",
        "validation_retries",
        "stop_called",
        "elapsed_s",
        "attempts",
        "error",
    )
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for t in trials:
            family = t.get("family")
            _, cam_att, cam_leak = emphasis_stats(t)
            row = {c: t.get(c) for c in columns}
            row.update(
                goal_success=is_goal_success(t["brief"], t["outcome"]),
                success=t["outcome"] in SUCCESS.get(family, ()),
                success_strict=t["outcome"] in STRICT_SUCCESS.get(family, ()),
                emphasis_camera_attempted=cam_att,
                emphasis_camera_leaked=cam_leak,
                error=bool(t.get("error")),
            )
            writer.writerow(row)

    present_brief_ids = [b["id"] for b in BRIEFS if any(t["brief"] == b["id"] for t in trials)]
    table_brief_ids = list(MAIN_BRIEF_IDS) if set(MAIN_BRIEF_IDS).issubset(present_brief_ids) else present_brief_ids
    extra_brief_ids = [bid for bid in present_brief_ids if bid not in table_brief_ids]
    all_brief_ids = table_brief_ids + extra_brief_ids

    by_arm_brief: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trials:
        by_arm_brief[(t["arm"], t["brief"])].append(t)

    llm_backends = sorted({t["backend"] for t in trials if t["arm"] in ("llm_one", "llm_loop")})

    lines = [
        f"# 2×2 Audit-Loop Camera-Planning Benchmark Results ({time.strftime('%Y-%m-%d %H:%M')})",
        "",
        "Generated automatically by `experiments/shot_briefs/aggregate.py` from `scripted_baseline.json`, "
        "`trials.jsonl`, and `ground_truth.json` (fixed lens `68.17 mm` on `assets/seed_tripo/model.glb`, "
        "camera planning only).",
        "",
        "## 1. Main Table: Four Planners on Six Briefs (Pooled)",
        "",
        "Goal success per brief: `tight` (`optimal`) on `S1, S2, S3, L1`; `pass` on `L2`; `honest` "
        "(`no_pass_within_cap`) on `I1`.",
        "",
        "| Brief | Family | Tightest Radius | " + " | ".join(label for _, label in PLANNER_ARMS) + " |",
        "|---|---|---:|---|---|---|---|",
    ]
    for bid in all_brief_ids:
        b = next(x for x in BRIEFS if x["id"] == bid)
        best = optimal_radius(bid, truth.get(bid, {})) if bid in GRADED_AGAINST_GROUND_TRUTH else None
        cells = [
            format_cell(sorted(by_arm_brief[(arm_key, bid)], key=lambda r: (str(r.get("backend")), int(r.get("rep", 1)))))
            for arm_key, _ in PLANNER_ARMS
        ]
        lines.append(f"| {bid} | {b['family']} | {best if best is not None else 'n/a'} | " + " | ".join(cells) + " |")

    # Summary row across the 6 main briefs
    goal_cells = []
    for arm_key, _ in PLANNER_ARMS:
        arm_main = [t for bid in table_brief_ids for t in by_arm_brief[(arm_key, bid)]]
        if not arm_main:
            goal_cells.append("not run")
        else:
            hits = sum(1 for t in arm_main if is_goal_success(t["brief"], t["outcome"]))
            per_6 = hits * len(table_brief_ids) / len(arm_main)
            if len(arm_main) == len(table_brief_ids):
                goal_cells.append(f"**{hits}/{len(arm_main)}**")
            else:
                goal_cells.append(f"**{hits}/{len(arm_main)}** ({per_6:.1f}/{len(table_brief_ids)})")
    lines.append(f"| **Goal Successes** | **all ({len(table_brief_ids)} briefs)** | - | " + " | ".join(goal_cells) + " |")
    lines += [
        "",
        "`tight` = passed at smallest passing radius on 0.5 grid; `wide` = passed but looser than tightest; "
        "`pass` = compliant pass (no tighter target); `fail` = no compliant passing plan; "
        "`honest` = infeasible brief, cap respected and no pass claimed; `n/a` = plain-language brief not parseable by regex.",
    ]

    # Per-model breakdown table when LLM trials exist
    if llm_backends:
        col_specs: list[tuple[str, str | None, str]] = [
            ("script_one", None, "Script (1 audit)"),
            ("script_loop", None, "Script (loop, <=6)"),
        ]
        for b in llm_backends:
            mname = next((t.get("provider_model") or t.get("model") for t in trials if t["backend"] == b), None)
            short_b = f"{b} (`{mname}`)" if mname else b
            if any(t["backend"] == b and t["arm"] == "llm_one" for t in trials):
                col_specs.append(("llm_one", b, f"{short_b} 1-audit"))
            if any(t["backend"] == b and t["arm"] == "llm_loop" for t in trials):
                col_specs.append(("llm_loop", b, f"{short_b} loop"))

        lines += [
            "",
            "### Per-Model 4-Planner Breakdown",
            "",
            "| Brief | " + " | ".join(hdr for _, _, hdr in col_specs) + " |",
            "|---|" + "---|" * len(col_specs),
        ]
        for bid in all_brief_ids:
            row_cells = []
            for arm_key, b_filter, _ in col_specs:
                recs = [
                    t for t in by_arm_brief[(arm_key, bid)]
                    if b_filter is None or t.get("backend") == b_filter
                ]
                row_cells.append(format_cell(sorted(recs, key=lambda r: int(r.get("rep", 1)))))
            lines.append(f"| {bid} | " + " | ".join(row_cells) + " |")
        tot_cells = []
        for arm_key, b_filter, _ in col_specs:
            recs = [
                t for bid in table_brief_ids for t in by_arm_brief[(arm_key, bid)]
                if b_filter is None or t.get("backend") == b_filter
            ]
            hits = sum(1 for t in recs if is_goal_success(t["brief"], t["outcome"]))
            tot_cells.append(f"**{hits}/{len(recs)}**" if recs else "not run")
        lines.append("| **Goal Successes** | " + " | ".join(tot_cells) + " |")

    # Section 2: Pre-Stated Paired Comparison on (S1, S2, S3, L1)
    paired = paired_threshold_comparison(trials)
    lines += [
        "",
        "## 2. Pre-Stated Paired Comparison on Threshold Briefs (`S1, S2, S3, L1`)",
        "",
    ]
    if paired["pairs_total"] == 0:
        lines.append("No matched `(llm_one, llm_loop)` pairs recorded yet.")
    else:
        p1_nt = f"{paired['one_sided_p_nontied']:.4f}" if paired["one_sided_p_nontied"] is not None else "n/a"
        p2_nt = f"{paired['two_sided_p_nontied']:.4f}" if paired["two_sided_p_nontied"] is not None else "n/a"
        p1_all = f"{paired['one_sided_p_all_pairs']:.4f}" if paired["one_sided_p_all_pairs"] is not None else "n/a"
        p2_all = f"{paired['two_sided_p_all_pairs']:.4f}" if paired["two_sided_p_all_pairs"] is not None else "n/a"
        lines += [
            f"- **Matched pairs (`4 briefs × models × reps`)**: **{paired['pairs_total']}**",
            f"- **Outcome ranking (`tight > wide > fail`)**: Loop wins **{paired['loop_wins']}/{paired['pairs_total']}**, "
            f"Ties **{paired['ties']}/{paired['pairs_total']}**, One-audit wins **{paired['one_wins']}/{paired['pairs_total']}**",
            f"- **Exact sign test (non-tied pairs)**: one-sided `p = {p1_nt}`, two-sided `p = {p2_nt}`",
            f"- **Exact sign test (all {paired['pairs_total']} pairs, ties counted against loop)**: "
            f"one-sided `p = {p1_all}`, two-sided `p = {p2_all}`",
            "",
            "| Backend | Rep | Brief | LLM (1 audit) | LLM (loop, <=6) | Pair Result |",
            "|---|---:|---|---|---|---|",
        ]
        for d in paired["details"]:
            o_str = SHORT.get(d["llm_one_outcome"], d["llm_one_outcome"])
            if d["llm_one_radius"] is not None:
                o_str += f" (`r={d['llm_one_radius']:g}`)"
            l_str = SHORT.get(d["llm_loop_outcome"], d["llm_loop_outcome"])
            if d["llm_loop_radius"] is not None:
                l_str += f" (`r={d['llm_loop_radius']:g}`)"
            lines.append(f"| {d['backend']} | {d['rep']} | {d['brief']} | {o_str} | {l_str} | {d['verdict']} |")

    # Section 3: Per-Arm & Per-Model Audits, Tokens, USD Cost, Radius Gap & Camera-Word Leakage
    lines += [
        "",
        "## 3. Audits, Token Usage, Cost, Radius Gap & Prompt Camera-Word Leakage",
        "",
        "| Arm | Backend | Model | Trials | Goal Success | Mean Audits | Mean Requests | Input Tokens | Output Tokens | USD Cost (All Attempts) | Mean Radius Gap | Camera Leakage | Mean Sec |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    group_keys: list[tuple[str, str]] = []
    for arm_key, _ in PLANNER_ARMS:
        b_list = sorted({t["backend"] for t in trials if t["arm"] == arm_key})
        for b in b_list:
            group_keys.append((arm_key, b))

    cost_sources_used: set[str] = set()
    for arm_key, b in group_keys:
        grp = [t for t in trials if t["arm"] == arm_key and t["backend"] == b]
        attempted_grp = [t for t in grp if t["outcome"] != "not_attempted"]
        mname = next((t.get("provider_model") or t.get("model") for t in grp), "-") or "-"
        hits = sum(1 for t in grp if is_goal_success(t["brief"], t["outcome"]))
        in_sum = sum(int(t["input_tokens"]) for t in grp if t.get("input_tokens") is not None)
        out_sum = sum(int(t["output_tokens"]) for t in grp if t.get("output_tokens") is not None)
        usd_sum = sum(float(t["cost_usd"]) for t in grp if t.get("cost_usd") is not None)
        all_att_sum = sum(
            float(t.get("all_attempts_cost_usd") if t.get("all_attempts_cost_usd") is not None else (t.get("cost_usd") or 0.0))
            for t in grp
        )
        for t in grp:
            if t.get("cost_source"):
                cost_sources_used.add(str(t["cost_source"]))
        gaps = [t["radius_gap"] for t in grp if t.get("radius_gap") is not None]
        estats = [emphasis_stats(t) for t in grp]
        cam_att_total = sum(s[1] for s in estats)
        cam_leak_total = sum(s[2] for s in estats)
        lines.append(
            f"| {arm_key} | {b} | {mname} | {len(grp)} | {hits}/{len(grp)} | "
            f"{fmt_mean([t.get('render_count', len(t.get('renders', []))) for t in attempted_grp])} | "
            f"{fmt_mean([t.get('requests') for t in attempted_grp])} | "
            f"{in_sum:,} | {out_sum:,} | "
            f"${usd_sum:.4f} (${all_att_sum:.4f}) | "
            f"{fmt_mean(gaps)} | {cam_leak_total} (of {cam_att_total} tried) | "
            f"{fmt_mean([t.get('elapsed_s') for t in attempted_grp])} |"
        )

    # Notes: L2 dual cutoff sensitivity, emphasis leakage summary, errors, total cost
    lines += ["", "## 4. Diagnostic Notes", ""]
    errors = [t for t in trials if t.get("error")]
    if errors:
        lines.append("Trials ending with an agent error (graded from audits recorded before the error):")
        for t in errors:
            err_tail = str(t["error"]).strip().splitlines()[-1][:160]
            lines.append(f"- `{t['arm']}` `{t['backend']}` `{t['brief']}` rep `{t.get('rep')}`: `{err_tail}`")
        lines.append("")

    steep = next((b for b in BRIEFS if b["id"] == "L2_lang_top_back"), None)
    if steep is not None:
        lines.append(
            'L2 ("look down steeply") is graded at an elevation in `[60°, 85°]`; sensitivity under a stricter '
            "`[70°, 85°]` cutoff:"
        )
        at_60 = {**steep["spec"], "elevation": (60, 85)}
        at_70 = {**steep["spec"], "elevation": (70, 85)}
        for arm_key, b in group_keys:
            grp = [
                t for t in trials
                if t["arm"] == arm_key and t["backend"] == b and t["brief"] == steep["id"] and t["outcome"] != "not_attempted"
            ]
            if not grp:
                continue
            pass_60 = sum(any(r["passed"] and compliant(r["plan"], at_60) for r in t.get("renders", [])) for t in grp)
            pass_70 = sum(any(r["passed"] and compliant(r["plan"], at_70) for r in t.get("renders", [])) for t in grp)
            elevs = sorted({r["plan"]["elevation_degrees"] for t in grp for r in t.get("renders", []) if r.get("passed")})
            lines.append(
                f"- {b} ({arm_key}): {pass_60}/{len(grp)} pass at [60°, 85°], {pass_70}/{len(grp)} at [70°, 85°]; "
                f"passing elevations: {elevs}"
            )
        lines.append("")

    lines.append(
        "Emphasis sentence passed to `build_seedance_prompt(emphasis_notes=...)` vs. leaked into the built video prompt:"
    )
    for arm_key, b in group_keys:
        if arm_key in ("script_one", "script_loop"):
            continue
        grp = [t for t in trials if t["arm"] == arm_key and t["backend"] == b]
        if grp:
            stats = [emphasis_stats(t) for t in grp]
            n_note = sum(s[0] > 0 for s in stats)
            n_cam = sum(s[1] > 0 for s in stats)
            n_leak = sum(s[2] > 0 for s in stats)
            lines.append(
                f"- {b} ({arm_key}): {n_note} of {len(grp)} trials passed an emphasis note; "
                f"{n_cam} passed camera/framing wording; {n_leak} leaked into the built prompt"
            )

    total_usd = sum(float(t["cost_usd"]) for t in trials if t.get("cost_usd") is not None)
    total_all_attempts = sum(
        float(t.get("all_attempts_cost_usd") if t.get("all_attempts_cost_usd") is not None else (t.get("cost_usd") or 0.0))
        for t in trials
    )
    lines += [
        "",
        f"Total model cost of recorded trials: USD {total_usd:.4f} "
        f"(all attempts including pre-audit retries: USD {total_all_attempts:.4f}).",
        f"Pricing sources used: `{', '.join(sorted(cost_sources_used)) or 'none'}`.",
        "",
    ]

    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    write_sha256_manifest(out_dir)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
