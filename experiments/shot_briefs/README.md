# 2×2 Audit-Loop Camera-Planning Benchmark

A self-contained camera-planning benchmark for ProxyDirector on the frozen Tripo mouse mesh (`assets/seed_tripo/model.glb`), outputting to `data/experiments/audit_loop/`.

## Motivation & Part A Geometry Sweep

In the production pipeline, `worker_render.py` fits the camera focal length (`lens_mm`) to every schema-valid orbit plan, so `304/304` grid plans (`10°–85°` × `r = 3.0–12.0` at `5%` margin) pass the framing audit automatically (`lens_mm = 68.17216` mm on the `50° / r=6 / 5%` baseline plan). Fixing the lens at `68.17216` mm turns framing into a genuine geometric constraint (`223/304` grid plans pass; the default `r = 6.0` passes at `>= 50°` = `8/16` elevations and is tightest at only `2/16`).

`part_a_sweep.py` runs this free deterministic sweep using pure-Python exact 3D GLB projection (`fast_audit.py`, 49,530 vertices reduced losslessly via 3D Quickhull) and writes:
- `data/experiments/audit_loop/geometry_map.json` — full `16 × 19` (`304`-plan) pass/margin map under both fitted and fixed lens.
- `data/experiments/audit_loop/ground_truth.json` — exact per-elevation smallest passing radii for all benchmark briefs.
- `data/experiments/audit_loop/min_radius_by_elevation.svg` — vector chart of smallest passing radius across `10°–85°` vs. the `r = 6.0` default line.

## Part B: 4-Planner × 6-Brief 2×2 Design

| Planner | One audit (`max_renders=1`) | Audit loop (`max_renders=6`) |
|---|---|---|
| **Script** (`brief["text"]` regex only, zero `spec` access) | `script_one`: tries `min(6.0, cap)` once (`1/6` goal successes) | `script_loop`: bisects `[3.0, cap]` in `0.5` steps (`4/6` goal successes) |
| **LLM Director** (configured cloud models + `knowledge/wiki.md`) | `llm_one`: first camera plan is final | `llm_loop`: plan, inspect framing audit, revise |

### Six Main Briefs (`MAIN_BRIEF_IDS`) & Goal-Success Criteria

| Brief | Family | Requirement | Goal Success |
|---|---|---|---|
| `S1_search_e20` | `search` | `20°`, `cw`, `0°`, `5%` margin, smallest passing radius (`0.5` steps) | `optimal` (`r = 6.5`) |
| `S2_search_e70` | `search` | `70°`, `cw`, `0°`, `5%` margin, smallest passing radius (`0.5` steps) | `optimal` (`r = 4.0`) |
| `S3_search_e50_m10` | `search` | `50°`, `cw`, `0°`, `10%` margin, smallest passing radius (`0.5` steps) | `optimal` (`r = 7.0`) |
| `L1_lang_low_tight` | `language` | Plain language: low camera `[10°, 30°]`, tightest `5%` margin, `cw`, front | `optimal` (`r = 6.5`) |
| `L2_lang_top_back` | `language` | Plain language: steep top-down `[60°, 85°]`, `10%` margin, `ccw`, start at back (`180°`) | `pass` (also reports `[70°, 85°]` sensitivity) |
| `I1_infeasible_e20_r4` | `infeasible` | `20°`, `cw`, `0°`, `5%` margin, `radius <= 4.0` (infeasible: `-26%` cutoff at `4.0`) | `no_pass_within_cap` (compliant probe within cap, no pass) |

Pre-flight uses brief `C2_fixed_e85_ccw_90` (`85°`, `ccw`, `90°`, `3%` margin, where `r = 6.0` passes on audit #1 with `~42%` clearance) under `data/experiments/audit_loop/preflight/` and never mixes pre-flight runs into `trials.jsonl`.

### Protocol Safeguards

- `get_run_context` removes `baseline_plan` and uses brief-neutral clearance wording so it never contradicts `10%` (`S3`, `L2`) or `3%` (`C2`) briefs.
- Every LLM trial captures full message history (`messages.json` via `capture_run_messages()`, preserving provider model names, response IDs, timestamps, and per-request usage even on errors).
- Only the experiment's own `max_renders` (`1` vs `6`) framing-audit budget limits local renders; no artificial token/request truncation cap is imposed on `agent.run_sync`.
- A provider error before the first audit retries at most once, preserving `*_attempt1_failed/` and summing `all_attempts_cost_usd` / tokens.
- `run_trials.py` and `aggregate.py` write `manifest_sha256.json` covering all saved artifacts.

## Commands (No Blender or FFmpeg Required)

macOS / Linux:

```bash
# 1. Free deterministic Part A sweep + ground truth + SVG figure + both scripted arms (0 tokens)
.venv/bin/python experiments/shot_briefs/part_a_sweep.py
.venv/bin/python experiments/shot_briefs/run_scripted_baseline.py

# 2. Pre-flight check (GET /models + 1-audit C2 trial per provider)
.venv/bin/python experiments/shot_briefs/preflight.py --backends deepseek glm

# 3. Main 2x2 LLM arms (12 one-audit trials + 12 loop trials)
.venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --arm llm_one --reps 1 --workers 2
.venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --arm llm_loop --reps 1 --workers 2

# 4. Build results.csv, summary.md, and SHA-256 manifest
.venv/bin/python experiments/shot_briefs/aggregate.py
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe experiments\shot_briefs\part_a_sweep.py
.\.venv\Scripts\python.exe experiments\shot_briefs\run_scripted_baseline.py
.\.venv\Scripts\python.exe experiments\shot_briefs\preflight.py --backends deepseek glm
.\.venv\Scripts\python.exe experiments\shot_briefs\run_trials.py --backends deepseek glm --arm llm_one --reps 1 --workers 2
.\.venv\Scripts\python.exe experiments\shot_briefs\run_trials.py --backends deepseek glm --arm llm_loop --reps 1 --workers 2
.\.venv\Scripts\python.exe experiments\shot_briefs\aggregate.py
```
