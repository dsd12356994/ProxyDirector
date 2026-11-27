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
  audit + human confirmation gate before any paid video submission, a JSONL ledger as the single source
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

macOS / Linux (Python 3.12+):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# For cloud directors or live video generation, copy .env.example to .env and set the keys you need.

.venv/bin/python run.py check
.venv/bin/python run.py seed-tripo-cache
.venv/bin/python run.py --backend mock --dry-run
.venv/bin/python run.py --condition baseline --dry-run
.venv/bin/python -m unittest
```

Windows (PowerShell):

```powershell
py -3 -m venv .venv; .\.venv\Scripts\pip.exe install -r requirements.txt

.\.venv\Scripts\python.exe run.py check
.\.venv\Scripts\python.exe run.py seed-tripo-cache
.\.venv\Scripts\python.exe run.py --backend mock --dry-run
.\.venv\Scripts\python.exe run.py --condition baseline --dry-run
.\.venv\Scripts\python.exe -m unittest
```

`run.py check` auto-detects **Blender 4.4+** (tested with 5.x; `worker_render.py` uses the Blender 4.4+ layered `Action` channelbags API) and **FFmpeg** on `PATH` and in standard macOS, Windows, and Linux install locations. Set `PROXYDIRECTOR_BLENDER` or `PROXYDIRECTOR_FFMPEG` in the environment or `.env` to pin custom binary paths. The offline `unittest` suite runs without Blender, FFmpeg, or network access.

On macOS, double-click **Launch ProxyDirector.command**. The launcher opens the studio in your browser and selects another port if port 4217 is in use. To start the server manually, run `.venv/bin/python ui/server.py` (Windows: `.\.venv\Scripts\python.exe ui/server.py`) and open <http://localhost:4217>. Blender and FFmpeg are required for new orbit renders. The Framing Lab and Case Replay read local files.

Download the [English user guide](<ProxyDirector User Guide.docx>) or select **User guide** in the studio sidebar.

The [class presentation materials](docs/presentation/) include the final English deck, the three-minute agent architecture talk, the editable control-loop diagram and the paired demo video. The [final report working draft and figure pack](docs/report/) contains the ACL-style Word working draft, PDF preview, and high-resolution figures.

- **Director Chat** accepts three local presets. Select **Standard orbit**, **35° elevation**, or **Counterclockwise orbit**, then send the request. Expand a tool entry to inspect its arguments and result. A counterclockwise follow-up keeps the current elevation. For free-form direction, select a configured cloud model. Freezing the video request still allows a cloud director or style expansion to call an LLM.
- **Framing Lab** recalculates a centered crop of the recorded compact orbit. The audit covers 300 encoded frames and one closure pose. With a 5% margin and 1.15× zoom, the smallest margin occurs at 3.60 s.
- **Case Replay** plays the fixed baseline and DeepSeek clips side by side. Each case pairs its Blender reference with its generated output. The repository includes the classroom clips and their evaluation summary for offline playback. The studio can also use the original experiment archives when they are available.
- **Run History** shows camera parameters, audit results, prompts, saved requests, cost entries, and available videos. Live generation requires approval. To try cloud style expansion from the CLI, use `.venv/bin/python run.py --backend deepseek --dry-run --style "Cool studio lighting"`; this calls the cloud director.

## Experimental conditions

| Condition | Camera and prompt | Command |
| --- | --- | --- |
| Fixed baseline | Fixed `50° / r=6 / cw` camera plan and frozen prompt template | `run.py --condition baseline` |
| Agent with Astra | Configured GPT director | `run.py --backend astra` |
| Agent with another LLM | Configured DeepSeek or GLM director | `run.py --backend deepseek` or `--backend glm` |

The conditions share one frozen Tripo GLB, four reference photographs, and one frozen prompt template (`--condition baseline` applies the same Kling `@video_1` / `@image_1..4` reference bindings as the agent path whenever `video_backend: kling` is active). The seed is recorded as a pipeline run label.

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
assets/                 Frozen photos, seed GLB, Framing Lab recording, and Case Replay snapshots
docs/                   Class presentation materials and final report working draft (docs/report/)
experiments/            Camera-plan benchmark with a fixed lens; see experiments/shot_briefs/README.md
data/                   Local runtime artifacts (runs/, eval/, cache/, ledger.jsonl; excluded from Git), plus committed benchmark tables in data/experiments/
```

## Runtime checks

- Credentials come from the environment or the local `.env` file. The studio reports whether a key is configured.
- Tripo receives explicitly named front, right, back, and left views, a model version, and a polling timeout.
- The Runway request specifies ten seconds with audio disabled. The adapter checks the 5 MiB data URI limit for the reference clip.
- In live Kling mode, the adapter publishes the reference clip and photographs as GitHub release assets using the configured repository, then checks their public URLs. The silent reference supplies the audio track. At the recorded Flash 720p rate, a ten-second input and ten-second output had an estimated cost of 8 credits.
- Paid jobs save provider state. A known task ID lets the adapter resume polling. If submission was interrupted before an ID was saved, reconcile the original request before retrying.
- The render worker checks Blender output for tracebacks and expected files. In tests, a Blender 5.x script error could return exit code zero.
- The compact reference experiments measured vertical fields of view of 19.97° and 20.03°. Both had a smallest vertical margin of approximately 5.01%. The audit uses the camera matrices for each sampled pose.

## Case Replay and blind-evaluation pack

Studio's **Case Replay** view loads the committed `assets/case_replay/` snapshots (paired Blender orbit reference `.mp4` clips and generated output `.mp4` clips) as visual examples for offline side-by-side playback.

`data/runs/` and the anonymized blind-review package in `data/eval/` (`videos/`, the rating sheet, and `key.json`) are local runtime folders excluded from Git. On a machine with local `data/runs/` video clips, run `.venv/bin/python -m pipeline.eval_pack` (Windows: `.\.venv\Scripts\python.exe -m pipeline.eval_pack`) to assemble an anonymized blind-review package in `data/eval/`.

## 2×2 audit-loop camera-planning benchmark

Because the production render worker fits the focal length (`lens_mm`) to every schema-valid plan (`304/304` grid plans pass), `experiments/shot_briefs/` fixes the lens at `68.17 mm` (the lens fitted for the `50° / r=6 / 5%` baseline plan) so plans can genuinely fail (`223/304` grid plans pass) and evaluates camera planning across four planners on six briefs (`S1`, `S2`, `S3`, `L1`, `L2`, `I1`), writing artifacts to `data/experiments/audit_loop/`:

- **Part A (`part_a_sweep.py`)** — deterministic local sweep over `16` elevations (`10°–85°`) × `19` radii (`3.0–12.0`) = `304` plans under both fitted and fixed lens, producing `geometry_map.json`, `ground_truth.json`, and `min_radius_by_elevation.svg`.
- **Part B (`run_scripted_baseline.py`, `preflight.py`, `run_trials.py`, `aggregate.py`)** — compares four planners (`Script (1 audit)`, `Script (loop, <=6)`, `LLM (1 audit)`, `LLM (loop, <=6)`) on the six briefs, verifies live endpoints/models on pre-flight brief `C2` before paid trials, preserves full message histories (`messages.json`) and SHA-256 manifests (`manifest_sha256.json`), and builds `results.csv` and `summary.md` directly from recorded trials.

See [experiments/shot_briefs/README.md](experiments/shot_briefs/README.md) for protocol details and exact commands.
