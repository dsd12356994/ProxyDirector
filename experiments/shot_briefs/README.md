# Shot-brief benchmark

A camera-planning benchmark for the ProxyDirector director, run on the frozen mouse mesh.

**Why it exists.** The production worker fits the lens to every camera plan, so every schema-valid
plan passes the framing audit (300 of 300 in a sweep, 3 of 3 at the production resolution). The
director's "read the audit, revise the plan" branch therefore never ran with a real LLM, and all
eleven November runs converged on the fixed baseline plan. This benchmark fixes the lens so plans
can fail, stops handing the model the baseline plan, and gives the director eight different briefs.

## Differences from a production run

| | Production | Benchmark |
|---|---|---|
| Lens | fitted to each plan | fixed at 68.17 mm (the lens fitted for the 50°/6 baseline plan) |
| `baseline_plan` in the run context | yes | removed |
| Brief | one fixed message | eight briefs, below |
| Video submission | paid, gated | disabled |
| Ledger | `data/ledger.jsonl` | one `trial_ledger.jsonl` per trial |
| Side order in the audit | mirrored for clockwise 90°/270° starts at `ce34d05` (since fixed in `pipeline/worker_render.py`) | corrected in the worker copy |

The production code is imported read-only. `fixed_lens_worker/worker_render.py` is generated from
`pipeline/worker_render.py` by `make_fixed_lens_worker.py`, which stops if the production worker no
longer contains the lines it patches.

## Protocol

| Family | Briefs | What the director must do | Graded by |
|---|---|---|---|
| search | S1 (20°), S2 (70°), S3 (50°, 10% margin) | find the smallest passing radius in 0.5 steps | `optimal` if no looser than the best plan measured at the brief's elevation, else `pass_suboptimal` |
| fixed | C1 (60°, counter-clockwise, start at the back), C2 (85°, counter-clockwise, start 90°, 3% margin) | follow the parameters | `pass` if a compliant plan passed |
| language | L1 (low camera, as tight as possible), L2 (steep top-down, 10% margin, reverse, start at the back) | map wording to parameters | like search (L1) and fixed (L2) |
| infeasible | I1 (20°, radius at most 4.0) | stay within the cap and report that no plan passes | `no_pass_within_cap` (cap respected, no compliant render passed), `cap_violated` |

- Every trial: the production system prompt (no team wiki, as in the November runs), at most four
  local renders, provider-default decoding, no seed.
- Three repetitions per backend and brief, plus a scripted baseline with the same tools and budget:
  start at radius 6, widen by 1.0 until the audit passes, tighten by 0.5 if the brief asks for the
  tightest plan. It does not attempt the language briefs.
- If a provider returns HTTP 429, `harness.py` renames the partial folder to `*_attemptN_ratelimited`,
  sleeps (`30s`, then `60s`), and restarts the trial on a clean folder (up to three attempts total,
  recorded in the `attempts` column of `results.csv`; usage from rate-limited attempts is discarded).
- `ground_truth.json` holds the smallest passing radius at sampled elevations across the search
  briefs (`1°` steps on `S1`–`S3`, `2°` steps on `L1`), measured with the same audit at 301 poses. The S briefs are graded against the best plan
  at the elevation they name; L1, which leaves the elevation open, against the best over its range.
- `aggregate.py` re-grades every trial from its saved renders, so grades do not depend on when the
  ground truth was measured.

The protocol was fixed after a four-run pilot (one low-angle and one high-angle search brief per
backend). The pilot runs are not part of the results.

## Run it

macOS / Linux (on Linux, replace the macOS Blender path below with `blender`):

```bash
.venv/bin/python experiments/shot_briefs/make_fixed_lens_worker.py
/Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup \
    --python experiments/shot_briefs/ground_truth.py -- --out data/experiments/shot_briefs/ground_truth.json
.venv/bin/python experiments/shot_briefs/run_scripted_baseline.py
.venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --reps 3
.venv/bin/python experiments/shot_briefs/aggregate.py
```

Windows (PowerShell; substitute your Blender 4.4+ executable path if `blender.exe` is not on `PATH`, or set `PROXYDIRECTOR_BLENDER` for trial runs):

```powershell
.\.venv\Scripts\python.exe experiments\shot_briefs\make_fixed_lens_worker.py
blender.exe -b --factory-startup --python experiments\shot_briefs\ground_truth.py -- --out data\experiments\shot_briefs\ground_truth.json
.\.venv\Scripts\python.exe experiments\shot_briefs\run_scripted_baseline.py
.\.venv\Scripts\python.exe experiments\shot_briefs\run_trials.py --backends deepseek glm --reps 3
.\.venv\Scripts\python.exe experiments\shot_briefs\aggregate.py
```

Each finished trial is appended to `trials.jsonl`, and a trial that already has a summary is not run
again. An interrupted batch therefore resumes with the same command, and a backend that needs
another network (Astra is region-blocked where the OpenAI API returns 403) can be added later with
it. `aggregate.py` requires the local git-ignored `trials.jsonl` to rebuild the full LLM + scripted
tables; on a fresh clone without `trials.jsonl`, it refuses to overwrite the committed `results.csv`
and `summary.md` with fewer trials unless `--allow-fewer-trials` is passed. Model calls averaged
about USD 0.004 per trial (roughly USD 0.001 on single-render fixed briefs to USD 0.019 on four-render
search trials); nothing else is paid for.

## Outputs

`data/experiments/shot_briefs/` holds `protocol_*.json` (briefs and settings as run),
`ground_truth.json`, `trials.jsonl` (one record per finished trial: render sequence, transcript,
token usage, cost), the same record in `trials/<backend>/<brief>_r<rep>/summary.json` with `framing/`
copies of each audit, `scripted_baseline.json`, `results.csv` and `summary.md`. Rendered frames and
reference videos are deleted after each trial. Like `data/ledger.jsonl`, `trials.jsonl` and `trials/`
stay out of Git; the tables, the protocol and the ground truth are committed.

## Results

`data/experiments/shot_briefs/summary.md` and `results.csv` hold the tables of the 2026-11-26 run
(recorded on the `ce34d05` pipeline before the benchmark was committed in `cc32d17` and before the
post-benchmark fixes below): 48 LLM trials (DeepSeek and GLM, three repetitions per brief) plus the
scripted baseline, USD 0.1958 (`~USD 0.20`) of logged model calls across 47 completed runs (one
DeepSeek `GateRefused` run ended before usage was recorded). Astra was not run: the OpenAI API
returned 403 on that network.

## Production issues this benchmark exposed

None of these could show up in the November batch, where every audit passed on the first render and
every orbit started at 0°. The 2026-11-26 benchmark exposed all three on the `ce34d05` codebase;
each is now fixed on this branch (so a new benchmark run exercises the updated tool layer):

- `pipeline/worker_render.py`, `side_sequence`: at `ce34d05`, mirrored the side order for clockwise
  orbits starting at 90° or 270° (for an azimuth of 90° the camera sits at `+X`, which the function's
  own convention calls `right`, yet it reported `left`). Corrected in the benchmark's worker copy at
  the time, and **now fixed** in `pipeline/worker_render.py`.
- `pipeline/tools.py` and `pipeline/director.py`: at `ce34d05`, `build_seedance_prompt` and
  `submit_seedance` raised `GateRefused` instead of returning the refusal to the model (ending the
  whole agent run and dropping its usage record), and only accepted the most recent render, so an
  earlier passing plan became unreachable once a later render failed (DeepSeek `S1` rep 1 found the
  optimal radius and then failed this way). **Now fixed**: gate refusals are returned to the director
  as `"Refused: ..."` tool text, `build_seedance_prompt(use_render=...)` keeps earlier passing
  renders selectable across the run, and `director.run_agent` logs `RunUsage` in `finally` even when
  a run errors out.
- `emphasis_notes` (`pipeline/tools.py`, `pipeline/camera_words.py`): at `ce34d05`, the system prompt
  allowed one short sentence in `emphasis_notes` with no vocabulary filter, letting camera or framing
  instructions enter the "frozen" video prompt (18 of the 48 trials added such a sentence). **Now
  fixed**: `_clean_emphasis` drops notes that exceed one short sentence or match whole-word English /
  substring Chinese camera and framing terms in `pipeline/camera_words.py`.

## What it does not show

- One mesh, one lens. The pass region depends on both.
- Camera planning only. A passed audit says nothing about the finished video, which still needs
  the blind review.
- For the natural-language briefs the acceptable ranges are the experimenter's choice. L2 ("look
  down steeply") is graded at 70° or more; at 60° or more both backends pass it three times out of
  three. `summary.md` reports that sensitivity next to the result.
- Four renders per trial is the production cap. It binds on search briefs whose passing radius is
  far from the first radius tried, so search outcomes measure the cap together with the director.
- Provider-default decoding and no seeds, so repetition-to-repetition variation is uncontrolled.
- `elapsed_s` is wall time. Trials ran while other Blender jobs were running, so it is not a clean
  latency measurement.
