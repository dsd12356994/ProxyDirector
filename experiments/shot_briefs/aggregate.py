"""Build results.csv and summary.md from every indexed trial (LLM directors and the scripted policy).

  .venv/bin/python experiments/shot_briefs/aggregate.py

Trials are re-graded here with the measured ground truth, so a trial's grade does not depend on
whether ground_truth.json already existed when that trial finished.
"""
import csv
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from briefs import (BRIEFS, GRADED_AGAINST_GROUND_TRUTH, STRICT_SUCCESS, SUCCESS, compliant, grade,
                    optimal_radius)
from harness import REPO

OUT = REPO / "data" / "experiments" / "shot_briefs"
SHORT = {"optimal": "opt", "pass_suboptimal": "sub", "pass": "pass", "no_pass": "fail",
         "no_pass_within_cap": "honest", "cap_violated": "cap+", "unexpected_pass": "pass?!"}
BACKEND_ORDER = ("scripted", "deepseek", "glm", "astra")


def load_trials() -> list[dict]:
    """Every indexed LLM trial (trials.jsonl, one record per line) plus the scripted baseline."""
    trials = []
    record = OUT / "trials.jsonl"
    if record.exists():
        trials += [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]
    scripted = OUT / "scripted_baseline.json"
    if scripted.exists():
        trials += json.loads(scripted.read_text(encoding="utf-8"))
    return trials


def regrade(trial: dict, truth: dict) -> dict:
    if trial.get("outcome") == "not_attempted":
        return trial
    brief = next(b for b in BRIEFS if b["id"] == trial["brief"])
    renders = [r for r in trial.get("renders", []) if r.get("plan")]
    best = (optimal_radius(trial["brief"], truth.get(trial["brief"], {}))
            if trial["brief"] in GRADED_AGAINST_GROUND_TRUTH else None)
    return {**trial, **grade(brief, renders, best)}


def mean(values: list) -> str:
    values = [v for v in values if v is not None]
    return f"{statistics.mean(values):.3g}" if values else "-"


CAMERA_WORDS = re.compile(r"orbit|camera|framing|frame|zoom|lens|clearance|margin|angle|rotat|elevation|"
                          r"环|镜头|取景|旋转|入画", re.I)


def emphasis_notes(trial: dict) -> list[str]:
    """Emphasis sentences the director passed to the prompt builder in this trial."""
    found = []
    for event in trial.get("transcript", []):
        if event.get("call") != "build_seedance_prompt":
            continue
        args = event.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        note = args.get("emphasis_notes", "") if isinstance(args, dict) else ""
        found.append(note if isinstance(note, str) else "")
    return found


def cell(outcomes: list[str]) -> str:
    counts = Counter(SHORT.get(o, o) for o in outcomes)
    return " · ".join(f"{n} {name}" for name, n in counts.most_common()) if counts else "not run"


def main() -> int:
    truth_path = OUT / "ground_truth.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8")).get("ground_truth", {}) if truth_path.exists() else {}
    trials = [regrade(t, truth) for t in load_trials()]
    if not trials:
        raise SystemExit("No trials indexed yet.")
    backends = [b for b in BACKEND_ORDER if any(t["backend"] == b for t in trials)]
    backends += sorted({t["backend"] for t in trials} - set(BACKEND_ORDER))

    columns = ("backend", "model", "brief", "family", "rep", "outcome", "success", "success_strict", "render_count",
               "first_try", "passing_compliant_radius", "optimal_radius", "requests", "input_tokens",
               "output_tokens", "cost_usd", "validation_retries", "stop_called", "elapsed_s", "attempts", "error")
    with (OUT / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for t in trials:
            family = t.get("family")
            row = {c: t.get(c) for c in columns}
            row.update(success=t["outcome"] in SUCCESS.get(family, ()),
                       success_strict=t["outcome"] in STRICT_SUCCESS.get(family, ()),
                       error=bool(t.get("error")))
            writer.writerow(row)

    grouped = defaultdict(list)
    for t in trials:
        grouped[(t["brief"], t["backend"])].append(t)
    models = {b: next((t.get("model") for t in trials if t["backend"] == b), None) for b in backends}
    names = [b if b == "scripted" or not models[b] else f"{b} ({models[b]})" for b in backends]
    lines = [f"# Shot-brief benchmark results ({time.strftime('%Y-%m-%d %H:%M')})", "",
             "Fixed lens 68.17 mm on the frozen mouse mesh; four local renders per trial; camera planning only "
             "(no video). Trials per cell: one for the scripted policy, three for each LLM backend.", "",
             "## Outcome by brief", "",
             "| Brief | Family | Best possible radius | " + " | ".join(names) + " |",
             "|---|---|---:|" + "---|" * len(backends)]
    for brief in BRIEFS:
        best = (optimal_radius(brief["id"], truth.get(brief["id"], {}))
                if brief["id"] in GRADED_AGAINST_GROUND_TRUTH else None)
        lines.append(f"| {brief['id']} | {brief['family']} | {best if best is not None else 'n/a'} | "
                     + " | ".join(cell([t["outcome"] for t in sorted(grouped[(brief["id"], b)], key=lambda t: t.get("rep", 0))])
                                 for b in backends) + " |")
    lines += ["", "opt = passed and no looser than the best plan measured at the brief's elevation; sub = passed "
              "but looser; pass = passed (no tighter target); fail = no compliant passing plan; honest = infeasible "
              "brief, radius cap respected and no pass claimed; cap+ = radius cap exceeded.", "",
              "## Success rate and cost by backend", "",
              "| Backend | Family | Trials | Success | Strict success | Mean renders | Mean requests | Mean cost (USD) | Mean seconds |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for backend in backends:
        whole = []
        for family in ("search", "fixed", "language", "infeasible"):
            group = [t for t in trials if t["backend"] == backend and t.get("family") == family
                     and t["outcome"] != "not_attempted"]
            if not group:
                continue
            whole += group
            ok = sum(t["outcome"] in SUCCESS[family] for t in group)
            strict = sum(t["outcome"] in STRICT_SUCCESS[family] for t in group)
            lines.append(f"| {backend} | {family} | {len(group)} | {ok}/{len(group)} | {strict}/{len(group)} | "
                         f"{mean([len(t.get('renders', [])) for t in group])} | {mean([t.get('requests') for t in group])} | "
                         f"{mean([t.get('cost_usd') for t in group])} | {mean([t.get('elapsed_s') for t in group])} |")
        if whole:
            lines.append(f"| {backend} | **all** | {len(whole)} | "
                         f"**{sum(t['outcome'] in SUCCESS[t['family']] for t in whole)}/{len(whole)}** | "
                         f"**{sum(t['outcome'] in STRICT_SUCCESS[t['family']] for t in whole)}/{len(whole)}** | "
                         f"{mean([len(t.get('renders', [])) for t in whole])} | {mean([t.get('requests') for t in whole])} | "
                         f"{mean([t.get('cost_usd') for t in whole])} | {mean([t.get('elapsed_s') for t in whole])} |")
    errors = [t for t in trials if t.get("error")]
    lines += ["", "## Notes", ""]
    if errors:
        lines.append("Trials that ended with an agent error (graded from the renders made before the error):")
        lines += [f"- {t['backend']} {t['brief']} rep {t.get('rep')}: {t['error'].strip().splitlines()[-1][:160]}"
                  for t in errors]
        lines.append("")
    steep = next(b for b in BRIEFS if b["id"] == "L2_lang_top_back")
    lines += ['L2 ("look down steeply") is graded at an elevation of at least 70°. The same trials, with every '
              'other requirement met and an elevation of at least 60°:']
    for backend in backends:
        group = [t for t in trials if t["backend"] == backend and t["brief"] == steep["id"]
                 and t["outcome"] != "not_attempted"]
        if not group:
            continue
        looser = {**steep["spec"], "elevation": (60, 85)}
        would = sum(any(r["passed"] and compliant(r["plan"], looser) for r in t.get("renders", [])) for t in group)
        elevations = sorted({r["plan"]["elevation_degrees"] for t in group for r in t.get("renders", []) if r["passed"]})
        lines.append(f"- {backend}: {would}/{len(group)} would pass; elevations of its passing renders: {elevations}")
    lines += ["", "First radius tried on the search briefs (the schema allows 3.0 to 12.0):"]
    for backend in backends:
        group = [t for t in trials if t["backend"] == backend and t.get("family") == "search" and t.get("renders")]
        if group:
            lines.append(f"- {backend}: {sum(t['renders'][0]['plan']['radius'] == 3.0 for t in group)} of "
                         f"{len(group)} trials start at 3.0")
    lines += ["", "Emphasis sentence added to the video prompt. The production system prompt allows one short "
              "sentence there, and nothing filters camera wording on that channel:"]
    for backend in backends:
        group = [t for t in trials if t["backend"] == backend and t["backend"] != "scripted"]
        if group:
            used = [n for t in group for n in emphasis_notes(t) if n.strip()]
            lines.append(f"- {backend}: {len(used)} of {len(group)} trials added one; "
                         f"{sum(bool(CAMERA_WORDS.search(n)) for n in used)} of those with camera or framing wording")
    total = sum(float(t["cost_usd"]) for t in trials if t.get("cost_usd") is not None)
    lines += ["", f"Total model cost of the LLM trials: USD {total:.4f}.", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
