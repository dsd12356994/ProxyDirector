"""Agent 控制台后端。启动：.venv/bin/python ui/server.py  →  http://localhost:4217

只读为主；POST /api/runs 发起新运行（默认 dry-run；真实扣费需显式 ack_paid，
UI 弹窗确认即闸门，等价于命令行的交互确认）。密钥永不返回，只返回是否已配置。

导演对话（/api/chat/*）：常驻多轮会话，工具调用与文本以事件流形式暴露，
前端轮询渲染——这是产品的主交互面（对齐 Agent UX 共识：过程透明+审批闸门）。
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import ledger, tripo  # noqa: E402
from pipeline.config import api_key, load_config  # noqa: E402
from pipeline.eval_pack import classify  # noqa: E402
from ui.framing_lab import router as framing_lab_router  # noqa: E402
from ui.evidence import router as evidence_router  # noqa: E402

CFG = load_config()
RUNS_DIR = ROOT / "data" / "runs"
UI_LOGS = ROOT / "ui" / "logs"
LEDGER = ROOT / "data" / "ledger.jsonl"
UI_LOGS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DSAI4207 Agent Console")
app.include_router(framing_lab_router)
app.include_router(evidence_router)
PROCS: dict[str, subprocess.Popen] = {}

STAGE_KEYS = ["tripo_model", "blender_render", "prompt", "seedance", "llm", "stop"]
STAGE_LABEL = {"tripo_model": "Tripo modeling", "blender_render": "Render and framing audit",
               "prompt": "Generation prompt", "seedance": "Seedance", "llm": "LLM", "stop": "Stopped"}


def file_stamp(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=1)
def ledger_index(path: Path, stamp) -> dict:
    grouped = {}
    if not stamp:
        return grouped
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        grouped.setdefault(row.get("run_id"), []).append(row)
    return grouped


def run_rows(run_id: str) -> list[dict]:
    return ledger_index(LEDGER, file_stamp(LEDGER)).get(run_id, [])


@lru_cache(maxsize=256)
def read_json(path: Path, stamp):
    if stamp:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return None


@lru_cache(maxsize=1)
def glb_digest(path: Path, stamp):
    return tripo.digest(path)[:16] if stamp else None


def run_status(exit_code, worker_running, session, stages, has_output):
    if worker_running or (session and session["busy"]):
        return "running"
    events = session["events"] if session else []
    if exit_code not in (0, None) or (events and events[-1]["kind"] == "error"):
        return "failed"
    video_stage = stages.get("seedance", {}).get("status")
    if video_stage == "terminal_failed":
        return "failed"
    if "stop" in stages or video_stage == "gate_declined_by_human":
        return "stopped"
    if has_output:
        return "ready"
    if video_stage == "dry_run_frozen":
        return "dry_run"
    if session and not events:
        return "idle"
    return "partial"


def latest_render(run_dir: Path):
    def version(path: Path):
        match = re.fullmatch(r"render_v(\d+)", path.name)
        return int(match[1]) if match else 0, path.stat().st_mtime_ns

    return max((p for p in run_dir.glob("render_*") if p.is_dir()), key=version, default=None)


def parse_run(run_dir: Path, rows=None) -> dict:
    rid = run_dir.name
    rows = run_rows(rid) if rows is None else rows
    m = re.fullmatch(r"\d{8}_\d{6}(?:-chat)?(?:-\d+)?_(\w+)_(agent|baseline)_s(-?\d+)", rid)
    backend, condition, seed = m.groups() if m else ("?", "?", "?")
    historical = classify(rid)
    if not m and historical:
        _, backend, seed = historical
    stages = {}
    for r in rows:
        k = r.get("stage")
        if k == "llm" and r.get("backend"):
            backend = r["backend"]
        if k in STAGE_KEYS:
            stages[k] = {"status": r.get("status"), "cost_usd": r.get("cost_usd") or 0,
                         "detail": r.get("fov_vertical_degrees") or r.get("task_id") or ""}
    proc = PROCS.get(rid)
    rc = proc.poll() if proc else None
    session = next((s for s in CHAT_SESSIONS.copy().values() if s["run_id"] == rid), None)
    cost = round(sum(r.get("cost_usd") or 0 for r in rows if not r.get("mock")), 6)
    render = latest_render(run_dir)
    framing = None
    video = None
    if render:
        fj = render / "framing.json"
        framing = read_json(fj, file_stamp(fj))
        if (render / "reference.mp4").is_file():
            video = f"/api/media/{rid}/{render.name}/reference.mp4"
    video_status = stages.get("seedance", {}).get("status")
    if backend == "mock" and video_status in ("submitted", "downloaded", "terminal_failed"):
        condition = "baseline"
    output = run_dir / "commercial/video.mp4"
    output_video = None
    if video_status == "downloaded" and output.is_file() and output.stat().st_size:
        output_video = f"/api/media/{rid}/commercial/video.mp4"
    status = run_status(rc, bool(proc and rc is None), session, stages, bool(output_video))
    prompt_file = run_dir / "seedance_prompt.txt"
    return {"id": rid, "backend": backend, "condition": condition, "seed": seed,
            "mtime": max(run_dir.stat().st_mtime, rows[-1].get("ts", 0) if rows else 0),
            "status": status, "exit_code": rc, "output_video": output_video,
            "stages": stages, "cost_usd": cost, "framing": framing, "video": video,
            "has_prompt": prompt_file.exists()}


@app.get("/api/overview")
def overview():
    backends = {}
    for name, b in CFG["backends"].items():
        if b.get("api_key_env"):
            backends[name] = {"model": b.get("model"), "has_key": bool(api_key(b["api_key_env"]))}
        else:
            backends[name] = {"model": "scripted", "has_key": True}
    runs_total = sum(p.is_dir() for p in RUNS_DIR.glob("*"))
    grouped = ledger_index(LEDGER, file_stamp(LEDGER))
    spent = sum(r.get("cost_usd") or 0 for rows in grouped.values() for r in rows if not r.get("mock"))
    glb = ROOT / CFG["pipeline"]["seed_glb"]
    k = CFG.get("kling")
    kling_info = None
    if k:
        kling_info = {"model": k["model"], "resolution": k["resolution"],
                      "duration": k["duration"],
                      "credits_per_video": round(2 * k["duration"] * k["rates"][k["model"]][k["resolution"]], 2)}
    return {
        "backends": backends,
        "video_backend": CFG.get("video_backend", "runway"),
        "budget": {"max_usd": CFG["budget"]["max_usd"],
                   "spent_usd": round(spent, 4), "runs_total": runs_total},
        "runway_key": bool(api_key("RUNWAYML_API_SECRET")),
        "kling_key": bool(api_key(k["api_key_env"])) if k else False,
        "tripo_key": bool(api_key("TRIPO_API_KEY")),
        "frozen_glb_sha256": glb_digest(glb, file_stamp(glb)),
        "qualities": CFG["runway"]["qualities"],
        "kling": kling_info,
    }


_KLING_PACK_CACHE = {"t": 0.0, "data": None}


def studio_error(error: Exception) -> str:
    """Turn upstream HTTP errors into a usable message without exposing response bodies."""
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    if status in (401, 403):
        return f"The provider denied access (HTTP {status}). Check the selected model and API credentials."
    if status == 429:
        return "The provider rate limit was reached. Wait before trying again."
    if isinstance(status, int):
        return f"The provider returned HTTP {status}. Review the run before retrying."
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "The operation timed out. Check the run status before retrying."
    message = str(error).replace(str(ROOT) + os.sep, "").replace(ROOT.as_posix() + "/", "")
    if isinstance(error, OSError):
        return error.strerror or "Could not access a required local file or program."
    if re.search(r"[\u3400-\u9fff]", message):
        return "The operation failed. Check the run log and provider status for details."
    return message[:400] or "The operation failed."


@app.get("/api/kling/packs")
def kling_packs():
    """Read Kling credit packs. The balance is cached for 60 seconds and may lag provider billing."""
    k = CFG.get("kling")
    if not k or not api_key(k["api_key_env"]):
        return {"packs": [], "remaining": None}
    now = time.time()
    if _KLING_PACK_CACHE["data"] is None or now - _KLING_PACK_CACHE["t"] > 60:
        from pipeline import kling as kling_mod
        try:
            packs = kling_mod.packs(CFG)
            _KLING_PACK_CACHE["data"] = {
                "packs": [{"name": p.get("resource_pack_name"), "tier": p.get("resource_pack_tier"),
                           "models": p.get("resource_pack_applicable_models"),
                           "remaining": p.get("remaining_quantity"),
                           "total": p.get("total_quantity"), "status": p.get("status")}
                          for p in packs],
                "remaining": kling_mod.credits_remaining(CFG)}
            _KLING_PACK_CACHE["t"] = now
        except Exception as e:
            return {"packs": [], "remaining": None, "error": studio_error(e)}
    return _KLING_PACK_CACHE["data"]


@app.get("/api/runs")
def list_runs():
    if not RUNS_DIR.exists():
        return []
    grouped = ledger_index(LEDGER, file_stamp(LEDGER))
    runs = [parse_run(d, grouped.get(d.name, [])) for d in RUNS_DIR.iterdir() if d.is_dir()]
    return sorted(runs, key=lambda item: item["mtime"], reverse=True)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    d = (RUNS_DIR / run_id).resolve()
    if d.parent != RUNS_DIR.resolve() or not d.is_dir():
        raise HTTPException(404, "Run not found.")
    rows = run_rows(run_id)
    info = parse_run(d, rows)
    info["ledger"] = rows
    pf = d / "seedance_prompt.txt"
    info["prompt_text"] = pf.read_text(encoding="utf-8") if pf.exists() else None
    fr = d / "commercial" / "frozen_request.json"
    info["frozen_request"] = read_json(fr, file_stamp(fr))
    log = UI_LOGS / f"{run_id}.log"
    if log.exists():
        info["log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-4000:]
    frames = []
    last_render = latest_render(d)
    if last_render:
        fdir = last_render / "frames"
        names = sorted(fdir.glob("*.png")) if fdir.exists() else []
        if names:
            frames = [f"/api/media/{run_id}/{last_render.name}/frames/{n.name}"
                      for n in [names[0], names[len(names) // 3], names[2 * len(names) // 3], names[-1]]]
    info["frames"] = frames
    return info


MEDIA_OK = re.compile(r"render_[\w.]+/(reference\.mp4|closure_end\.png|frames/\d{4}\.png)|commercial/video\.mp4")


@app.get("/api/views/{name}")
def frozen_view(name: str):
    """Return one of the four frozen appearance views used by the pipeline."""
    if not re.fullmatch(r"view_[1-4]\.png", name):
        raise HTTPException(400, "Choose a reference from view_1.png through view_4.png.")
    f = ROOT / CFG["pipeline"]["views_dir"] / name
    if not f.exists():
        raise HTTPException(404, "File not found.")
    return FileResponse(f)


@app.get("/api/media/{run_id}/{path:path}")
def media(run_id: str, path: str):
    if not re.fullmatch(r"[0-9A-Za-z_-][0-9A-Za-z_.-]*", run_id) or not MEDIA_OK.fullmatch(path):
        raise HTTPException(400, "Invalid media path.")
    f = (RUNS_DIR / run_id / path).resolve()
    if not f.is_relative_to(RUNS_DIR.resolve()) or not f.is_file() or not f.stat().st_size:
        raise HTTPException(404, "File not found.")
    return FileResponse(f)


class RunSpec(BaseModel):
    backend: str
    condition: str = "agent"
    seed: int = 4207
    dry_run: bool = True
    ack_paid: bool = False
    style: str = ""            # 提示词扩写：一句风格想法（空=完全走原冻结模板）


class ExpandSpec(BaseModel):
    idea: str
    backend: str | None = None


@app.post("/api/expand_prompt")
def expand_prompt(spec: ExpandSpec):
    """Expand an appearance idea into a structured style block without launching a video run."""
    idea = spec.idea.strip()
    if not idea or len(idea) > 300:
        raise HTTPException(400, "Enter a style idea between 1 and 300 characters.")
    if spec.backend and spec.backend not in CFG["backends"]:
        raise HTTPException(400, "Unknown style backend.")
    from pipeline import prompt_expander
    try:
        ps, block = prompt_expander.expand(idea, CFG, spec.backend, "-",
                                           LEDGER)
    except ValueError as e:                     # 镜头词汇红线拦截
        raise HTTPException(422, studio_error(e))
    except RuntimeError as e:
        raise HTTPException(400, studio_error(e))
    return {"spec": ps.model_dump(), "style_block": block}


@app.post("/api/runs")
def launch(spec: RunSpec):
    if spec.backend not in CFG["backends"]:
        raise HTTPException(400, f"Unknown backend: {spec.backend}.")
    if spec.condition not in ("agent", "baseline"):
        raise HTTPException(400, "Choose the agent or baseline condition.")
    if spec.dry_run and spec.ack_paid:
        raise HTTPException(400, "Rehearsal mode cannot authorize a paid submission.")
    if not spec.dry_run and not spec.ack_paid:
        raise HTTPException(400, "Confirm the cost before submitting a paid run.")
    from run import new_run_dir
    run_id = new_run_dir(CFG, spec.backend, spec.condition, spec.seed).name
    # The interpreter that runs the Studio also runs the pipeline: .venv/bin/python on macOS and
    # Linux, .venv\Scripts\python.exe on Windows.
    cmd = [sys.executable, "run.py", "--backend", spec.backend,
           "--condition", spec.condition, "--seed", str(spec.seed), "--run-id", run_id]
    if spec.style.strip():
        cmd += ["--style", spec.style.strip()]
    if spec.dry_run:
        cmd.append("--dry-run")
    elif spec.ack_paid:
        cmd.append("--auto-approve")        # UI 弹窗即人工闸门
    log = UI_LOGS / f"{run_id}.log"
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}     # the log is read back as UTF-8
    with log.open("w", encoding="utf-8") as stream:
        PROCS[run_id] = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                                         stderr=subprocess.STDOUT, env=child_env)
    return {"run_id": run_id, "dry_run": spec.dry_run,
            "note": "Paid video generation authorized." if not spec.dry_run else "The video request will be saved locally."}


@app.get("/api/health")
def health():
    return {"ok": True, "service": "proxydirector", "version": "studio-202611-en"}


@app.get("/user-guide")
def user_guide():
    filename = "ProxyDirector User Guide.docx"
    guide = ROOT / filename
    if not guide.is_file():
        raise HTTPException(404, "The user guide is not available.")
    return FileResponse(guide, filename=filename,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


# ─────────────────────────────────────────────────────────────────────────────
# 导演对话会话：多轮、工具内联可见。dry-run 默认开；付费须每轮显式 ack（闸门）。
# ─────────────────────────────────────────────────────────────────────────────
CHAT_SESSIONS: dict[str, dict] = {}


class ChatNew(BaseModel):
    backend: str = "mock"
    seed: int = 4207


class ChatSend(BaseModel):
    text: str
    dry_run: bool = True
    ack_paid: bool = False       # true = 本轮允许真实扣费（UI 红色确认后传）


def _emit(sess: dict, kind: str, **kw) -> None:
    sess["events"].append({"id": len(sess["events"]), "t": round(time.time(), 2),
                           "kind": kind, **kw})


async def _run_turn(sess: dict, text: str) -> None:
    """跑一轮 agent：事件（文本/工具调用/结果）实时进 sess['events']。
    工具事件由包装层发出（对 mock/真实后端统一），不依赖模型层流式能力。"""
    from pydantic_ai import Agent, RunUsage, Tool
    from pipeline import director
    from pipeline.tools import REFUSED, make_tools

    ctx = sess["ctx"]
    ctx.dry_run = sess["pending_dry_run"]
    ctx.auto_approve = sess["pending_ack"]
    ctx.turn_start_iteration = ctx.iterations
    _emit(sess, "user", text=text)
    _emit(sess, "mode", dry_run=ctx.dry_run, ack_paid=ctx.auto_approve)
    try:
        backend_cfg = dict(CFG["backends"][sess["backend"]], _name=sess["backend"])
        raw_tools = make_tools(ctx)

        def wrap(name, fn):
            import functools

            @functools.wraps(fn)                  # 保留原签名：pydantic-ai 按签名生成工具 schema
            def wrapped(*a, **k):
                args = json.dumps({"args": a, **k}, ensure_ascii=False, default=str)[:400]
                _emit(sess, "tool_call", tool=name, args=args)
                try:
                    out = fn(*a, **k)
                except Exception as e:                        # noqa: BLE001 —— a broken step also enters the stream
                    _emit(sess, "tool_result", tool=name, output=studio_error(e), ok=False, refused=False)
                    raise
                refused = str(out).startswith(REFUSED)        # the gate said no; the director can still act
                _emit(sess, "tool_result", tool=name, output=str(out)[:600], ok=not refused, refused=refused)
                return out
            return wrapped

        tools = {name: wrap(name, fn) for name, fn in raw_tools.items()}
        agent = Agent(model=director.build_model(backend_cfg),
                      tools=[Tool(fn, sequential=True) for fn in tools.values()],
                      system_prompt=director.SYSTEM_PROMPT + director._load_wiki()
                      + "\nWrite all user-facing studio replies in English.",
                      retries=1)
        usage = RunUsage()       # filled in place, so the calls of a turn that breaks are still on record
        status = "chat_turn_failed"
        try:
            result = await agent.run(text, message_history=sess["history"], usage=usage)
            status = "chat_turn"
        finally:
            ledger.log(director.usage_entry(sess["run_id"], sess["backend"], backend_cfg.get("model", "mock"),
                                            usage, status), LEDGER)
        sess["history"] = result.all_messages()
        _emit(sess, "done", output=result.output)
    except Exception as e:                                    # noqa: BLE001 —— 会话不死，错误入流
        _emit(sess, "error", message=studio_error(e))
    finally:
        sess["busy"] = False


@app.post("/api/chat/new")
def chat_new(spec: ChatNew):
    if spec.backend not in CFG["backends"]:
        raise HTTPException(400, f"Unknown backend: {spec.backend}.")
    from pipeline.tools import ToolContext
    from run import new_run_dir
    sid = uuid.uuid4().hex[:10]
    run_dir = new_run_dir(CFG, spec.backend, "agent", spec.seed, chat=True)
    run_id = run_dir.name
    ledger_path = LEDGER
    try:
        glb = tripo.ensure_glb(CFG, run_dir, ledger_path, run_id, dry_run=True)
    except (OSError, RuntimeError) as e:
        raise HTTPException(503, studio_error(e)) from e
    ctx = ToolContext(cfg=CFG, run_dir=run_dir, run_id=run_id,
                      dry_run=True, auto_approve=False, glb=glb, chat_session=True)
    CHAT_SESSIONS[sid] = {"sid": sid, "backend": spec.backend, "seed": spec.seed,
                          "run_id": run_id, "created": time.time(),
                          "events": [], "history": [], "ctx": ctx,
                          "busy": False, "pending_dry_run": True, "pending_ack": False}
    if len(CHAT_SESSIONS) > 20:                             # 保留最近 20 个会话
        idle = [k for k, s in CHAT_SESSIONS.items() if not s["busy"] and k != sid]
        for k in sorted(idle, key=lambda s: CHAT_SESSIONS[s]["created"])[:len(CHAT_SESSIONS) - 20]:
            CHAT_SESSIONS.pop(k, None)
    return {"sid": sid, "run_id": run_id}


@app.post("/api/chat/{sid}/send")
async def chat_send(sid: str, spec: ChatSend):
    sess = CHAT_SESSIONS.get(sid)
    if not sess:
        raise HTTPException(404, "Session not found.")
    if sess["busy"]:
        raise HTTPException(409, "The previous turn is still running.")
    if not spec.text.strip():
        raise HTTPException(400, "Enter a message.")
    if spec.ack_paid and spec.dry_run:
        raise HTTPException(400, "Rehearsal mode cannot authorize a paid submission.")
    if not spec.dry_run and not spec.ack_paid:
        raise HTTPException(400, "Confirm the cost for this turn before submitting.")
    if sess["ctx"].paid_record_exists():
        raise HTTPException(409, "This run already has a paid submission. Start a new session to change the shot.")
    sess["pending_dry_run"] = spec.dry_run
    sess["pending_ack"] = spec.ack_paid
    sess["busy"] = True
    asyncio.create_task(_run_turn(sess, spec.text.strip()))
    return {"ok": True}


@app.get("/api/chat/{sid}/events")
def chat_events(sid: str, since: int = 0):
    sess = CHAT_SESSIONS.get(sid)
    if not sess:
        raise HTTPException(404, "Session not found.")
    return {"events": sess["events"][since:], "busy": sess["busy"],
            "run_id": sess["run_id"], "backend": sess["backend"], "seed": sess["seed"]}


@app.get("/api/chat/sessions")
def chat_sessions():
    return [{"sid": s["sid"], "backend": s["backend"], "run_id": s["run_id"],
             "created": s["created"], "busy": s["busy"],
             "title": next((e["text"][:54] for e in s["events"] if e["kind"] == "user"), None),
             "n_events": len(s["events"])}
            for s in sorted(CHAT_SESSIONS.values(), key=lambda x: -x["created"])]


app.mount("/static", StaticFiles(directory=ROOT / "ui" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "ui" / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PROXYDIRECTOR_PORT", 4217)),
                log_level="warning")
