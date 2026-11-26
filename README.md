# ProxyDirector

**Any connectable LLM can direct the camera.**

ProxyDirector is an agent pipeline that turns four photographs of a static object into a
10-second, single-take 360° product video: it reconstructs proxy geometry with Tripo,
renders a compact-framing orbit reference in headless Blender, and hands it to the
configured video model (Kling 4.0 omni-reference by default, Runway Seedance 2.5 optional) —
with the **LLM acting only as the camera director** (camera plan, prompt, retry decisions),
never writing geometry or executing generated code.

- **Thesis** — an agent shell with *any* connectable LLM API retains usable camera control at
  comparable or lower generation cost, when proxy geometry comes from a fixed multiview
  reconstruction service.
- **Pluggable by config** — swap GPT / DeepSeek / any OpenAI-compatible backend by editing
  `config.yaml` + `.env`; swap the video layer with one `video_backend:` line. No code changes.
- **Prompt expansion (style channel)** — a one-line user idea is expanded by any configured
  LLM into a structured style block (lighting / background / mood), appended to the frozen
  template. Camera vocabulary is rejected at the door: the style channel can never touch
  camera behavior. No idea given → template stays byte-identical (experiment comparability).
- **Team wiki** — `knowledge/wiki.md` is injected into the director's system prompt:
  framing-failure fixes, API pitfalls, money rules. Edit the file to teach the agent;
  no vector DB, no retrieval infrastructure (by design).
- **Money-safe by construction** — free local loop for camera iteration, a machine framing
  audit + human confirmation gate before any paid call, a JSONL ledger as the single source
  of cost truth, and no-resubmit state files for paid tasks.

DSAI4207 course project (Type D: Evaluation & Analysis) — ZHUANG Shixin, LIU Yubo, Xu Wancheng.

## Pipeline

```
        ┌────────────────── free local loop (LLM may iterate) ──────────────────┐
photos ─▶ Tripo multiview ─▶ GLB checks ─▶ Blender orbit render ─▶ framing audit ─┘
 (frozen)   (cached by hash)             (compact framing)      (all vertices,
                                                                every frame)
                                                              │ pass?
                                              ┌───────────────┴───────────────┐
                                              ▼                               ▼
                                    prompt (frozen template)            adjust & retry
                                              │
                                        GATE: human confirm
                                              ▼
                            video model (config: kling | runway) ─▶ ledger ─▶ eval
```

## Quickstart

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
# For cloud directors, create .env from .env.example and set the corresponding key.

.venv/bin/python run.py check
.venv/bin/python run.py seed-tripo-cache
.venv/bin/python run.py --backend mock --dry-run
.venv/bin/python run.py --condition baseline --dry-run
```

On macOS, double-click **Launch ProxyDirector.command**. The launcher opens the studio in your browser and selects another port if port 4217 is in use. To start the server manually, run `.venv/bin/python ui/server.py` and open <http://localhost:4217>. Blender and FFmpeg are required for new orbit renders. The Framing Lab and Case Replay read local files.

Download the [English user guide](<ProxyDirector User Guide.docx>) or select **User guide** in the studio sidebar.

The [class presentation materials](docs/presentation/) include the final English deck, the three-minute agent architecture talk, the editable control-loop diagram and the paired demo video.

- **Director Chat** accepts three local presets. Select **Standard orbit**, **35° elevation**, or **Counterclockwise orbit**, then send the request. Expand a tool entry to inspect its arguments and result. A counterclockwise follow-up keeps the current elevation. For free-form direction, select a configured cloud model. Freezing the video request still allows a cloud director or style expansion to call an LLM.
- **Framing Lab** recalculates a centered crop of the recorded compact orbit. The audit covers 300 encoded frames and one closure pose. With a 5% margin and 1.15× zoom, the smallest margin occurs at 3.60 s.
- **Case Replay** plays the fixed baseline and DeepSeek clips side by side. Each case pairs its Blender reference with its generated output. The repository includes the classroom clips and their evaluation summary for offline playback. The studio can also use the original experiment archives when they are available.
- **Run History** shows camera parameters, audit results, prompts, saved requests, cost entries, and available videos. Live generation requires approval. To try cloud style expansion from the CLI, use `.venv/bin/python run.py --backend deepseek --dry-run --style "Cool studio lighting"`; this calls the cloud director.

## Experimental conditions

| Condition | Camera and prompt | Command |
| --- | --- | --- |
| Fixed baseline | Fixed camera plan and prompt template | `run.py --condition baseline` |
| Agent with Astra | Configured GPT director | `run.py --backend astra` |
| Agent with another LLM | Configured DeepSeek or GLM director | `run.py --backend deepseek` or `--backend glm` |

The conditions share one frozen Tripo GLB, four reference photographs and a prompt template. The seed is recorded as a pipeline run label. The archived Kling requests did not specify a video generation seed.

## Layout

```
run.py                  CLI for environment checks, cache setup, and runs
config.yaml             Backend registry, camera defaults, and budgets
pipeline/
  models.py             Camera plan and framing report schemas
  director.py           Pydantic AI director and local preset controller
  tools.py              Camera planning, render, audit, prompt, and submission tools
  tripo.py              Multiview reconstruction, view slots, and geometry cache
  blender_step.py       Blender execution and FFmpeg encoding
  worker_render.py      Headless Blender render and per-pose projection audit
  runway.py             Seedance generation, state files, and cost entries
  kling.py              Kling reference uploads, generation, and credit tracking
  prompt_template.py    Frozen prompt with slots for measured framing
ui/                     FastAPI service, offline experiments, and web client
assets/                 Frozen photos, seed GLB, and Framing Lab recording
experiments/            Camera-plan benchmark with a fixed lens; see experiments/shot_briefs/README.md
data/                   Run artifacts, cache, and cost log (excluded from Git), plus the benchmark tables in data/experiments/
```

## Runtime checks

- Credentials come from the environment or the local `.env` file. The studio reports whether a key is configured.
- Tripo receives explicitly named front, right, back, and left views, a model version, and a polling timeout.
- The Runway request specifies ten seconds with audio disabled. The adapter checks the 5 MiB data URI limit for the reference clip.
- In live Kling mode, the adapter publishes the reference clip and photographs as GitHub release assets using the configured repository, then checks their public URLs. The silent reference supplies the audio track. At the recorded Flash 720p rate, a ten-second input and ten-second output had an estimated cost of 8 credits.
- Paid jobs save provider state. A known task ID lets the adapter resume polling. If submission was interrupted before an ID was saved, reconcile the original request before retrying.
- The render worker checks Blender output for tracebacks and expected files. In tests, a Blender 5.x script error could return exit code zero.
- The compact reference experiments measured vertical fields of view of 19.97° and 20.03°. Both had a smallest vertical margin of approximately 5.01%. The audit uses the camera matrices for each sampled pose.

## November 2026 evaluation batch

The team reviewed 11 archived clips of one wired mouse. All 11 passed the team's visual orbit check for framing, visible parts, and watermarks. The independent rating sheet remains unfilled.

| Director | Finished clips |
| --- | ---: |
| Fixed baseline | 3 |
| Astra | 3 |
| DeepSeek | 3 |
| GLM | 2 |

The batch used Kling 4.0 Flash at 720p with a silent ten-second request. The team's historical reconciliation recorded 8.03 credits per clip, or 88.33 credits from 112 available, leaving 23.67. The model ledger recorded approximately $0.19 overall. Cached Tripo geometry incurred no new reconstruction charge. The recorded LLM estimates were under 0.2 cents per DeepSeek run, about 0.2 cents per GLM run, and about 4.5 cents per Astra run.

The recorded director plans matched the fixed camera settings of 50° elevation, radius 6, and clockwise motion. In an earlier dry run, DeepSeek chose 35° and radius 6.5 and passed the audit on the first attempt.

The anonymized evaluation package is stored in `data/eval/`, including the clips under `videos/`, the rating sheet, and a separate `key.json` lookup. To rebuild the package, run `.venv/bin/python -m pipeline.eval_pack`.

## Shot-brief benchmark (November 26)

The framing audit above fits the lens to every plan, so any schema-valid plan passes it. The benchmark in `experiments/shot_briefs/` runs the director with a fixed lens, so plans can fail, on eight briefs instead of one fixed message. It evaluates camera planning only and imports the production pipeline unchanged. Each LLM cell below is three repetitions per brief.

| Director | Trials | Success | Strict success | Search trials solved optimally |
| --- | ---: | ---: | ---: | ---: |
| Scripted policy, no LLM | 6 | 6 | 5 | 2 of 3 (one run per brief) |
| DeepSeek | 24 | 19 | 17 | 8 of 9 |
| GLM | 24 | 19 | 16 | 4 of 9 |

- The scripted policy cannot attempt the two natural-language briefs. On the three search briefs it was optimal twice and ran out of its four renders once. DeepSeek was optimal on all three runs of that brief; GLM failed all three runs of another.
- No model produced an invalid camera plan in 48 trials. On the infeasible brief, all six LLM runs kept to the radius cap and reported that no plan passes.
- Of the two natural-language briefs, GLM passed five of six trials and DeepSeek two of six. All three of DeepSeek's failures on the turntable brief depend on the cut-off used for the word "steeply"; `summary.md` reports that next to the result.
- One DeepSeek trial ended with an agent error after it had already rendered a passing plan.
- Astra was not run: the OpenAI API returned 403 on the experiment network. Model calls for the batch cost about USD 0.20.

The tables are in `data/experiments/shot_briefs/summary.md` and `results.csv`. The protocol, the production issues the benchmark exposed, and its limits are in `experiments/shot_briefs/README.md`.
