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
| Side order in the audit | mirrored for clockwise orbits starting at 90° or 270° | corrected in the worker copy |

The production code is imported read-only. `fixed_lens_worker/worker_render.py` is generated from
`pipeline/worker_render.py` by `make_fixed_lens_worker.py`, which stops if the production worker no
longer contains the lines it patches.

## Protocol

| Family | Briefs | What the director must do | Graded by |
|---|---|---|---|
| search | S1 (20°), S2 (70°), S3 (50°, 10% margin) | find the smallest passing radius in 0.5 steps | `optimal` if no looser than the best plan measured at the brief's elevation, else `pass_suboptimal` |
| fixed | C1 (60°, counter-clockwise, start at the back), C2 (85°, start 90°, 3% margin) | follow the parameters | `pass` if a compliant plan passed |
| language | L1 (low camera, as tight as possible), L2 (steep top-down, 10% margin, reverse, start at the back) | map wording to parameters | like search (L1) and fixed (L2) |
| infeasible | I1 (20°, radius at most 4.0) | report that no plan passes, without breaking the cap | `no_pass_within_cap`, `cap_violated` |

- Every trial: the production system prompt (no team wiki, as in the November runs), at most four
  local renders, provider-default decoding, no seed.
- Three repetitions per backend and brief, plus a scripted baseline with the same tools and budget:
  start at radius 6, widen by 1.0 until the audit passes, tighten by 0.5 if the brief asks for the
  tightest plan. It does not attempt the language briefs.
- `ground_truth.json` holds the smallest passing radius at every accepted elevation of the search
  briefs, measured with the same audit at 301 poses. The S briefs are graded against the best plan
  at the elevation they name; L1, which leaves the elevation open, against the best over its range.
- `aggregate.py` re-grades every trial from its saved renders, so grades do not depend on when the
  ground truth was measured.

The protocol was fixed after a four-run pilot (one low-angle and one high-angle search brief per
backend). The pilot runs are not part of the results.

## Run it

```bash
.venv/bin/python experiments/shot_briefs/make_fixed_lens_worker.py
/Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup \
    --python experiments/shot_briefs/ground_truth.py -- --out data/experiments/shot_briefs/ground_truth.json
.venv/bin/python experiments/shot_briefs/run_scripted_baseline.py
.venv/bin/python experiments/shot_briefs/run_trials.py --backends deepseek glm --reps 3
.venv/bin/python experiments/shot_briefs/aggregate.py
```

Each finished trial is appended to `trials.jsonl`, and a trial that already has a summary is not run
again. An interrupted batch therefore resumes with the same command, and a backend that needs
another network (Astra is region-blocked where the OpenAI API returns 403) can be added later with
it. Each trial costs about USD 0.002–0.005 of model calls; nothing else is paid for.

## Outputs

`data/experiments/shot_briefs/` holds `protocol_*.json` (briefs and settings as run),
`ground_truth.json`, `trials.jsonl` (one record per finished trial: render sequence, transcript,
token usage, cost), the same record in `trials/<backend>/<brief>_r<rep>/summary.json` with `framing/`
copies of each audit, `scripted_baseline.json`, `results.csv` and `summary.md`. Rendered frames and
reference videos are deleted after each trial. Like `data/ledger.jsonl`, the per-trial records stay
out of Git; the tables, the protocol and the ground truth are committed.

## Results

`data/experiments/shot_briefs/summary.md` and `results.csv` hold the tables of the 2026-11-26 run:
48 LLM trials (DeepSeek and GLM, three repetitions per brief) plus the scripted baseline, USD 0.20
of model calls in total. Astra was not run: the OpenAI API returned 403 on that network.

## Production issues this benchmark exposed

None of these could show up in the November batch, where every audit passed on the first render.

- `pipeline/worker_render.py`, `side_sequence`: mirrors the side order for clockwise orbits that
  start at 90° or 270° (the camera is at +X for an azimuth of 90°, which the function's own
  convention calls "right", but it reports "left"). Corrected in the worker copy.
- `pipeline/tools.py`: the prompt builder and the submit tool raise `GateRefused` instead of
  returning the refusal to the model, which ends the whole agent run. They also only accept the
  most recent render, so a passing plan becomes unreachable as soon as a later render fails. One
  trial found the best plan and then ended this way.
- `emphasis_notes`: the system prompt allows one sentence there and nothing filters camera
  wording, so the "frozen" video prompt can still receive camera instructions. 18 of the 48
  trials added such a sentence.

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
