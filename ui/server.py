"""Agent 控制台后端。启动：.venv/bin/python ui/server.py  →  http://localhost:4207

只读为主；POST /api/runs 发起新运行（默认 dry-run；真实扣费需显式 ack_paid，
UI 弹窗确认即闸门，等价于命令行的交互确认）。密钥永不返回，只返回是否已配置。
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import ledger, tripo  # noqa: E402
from pipeline.config import api_key, load_config  # noqa: E402

CFG = load_config()
RUNS_DIR = ROOT / "data" / "runs"
UI_LOGS = ROOT / "ui" / "logs"
LEDGER = ROOT / "data" / "ledger.jsonl"
UI_LOGS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DSAI4207 Agent Console")
PROCS: dict[str, subprocess.Popen] = {}

STAGE_KEYS = ["tripo_model", "blender_render", "prompt", "seedance", "llm", "stop"]
STAGE_LABEL = {"tripo_model": "Tripo 建模", "blender_render": "渲染+取景审计",
               "prompt": "提示词", "seedance": "Seedance", "llm": "LLM", "stop": "停止"}


def run_rows(run_id: str) -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("run_id") == run_id:
            rows.append(r)
    return rows


def parse_run(run_dir: Path) -> dict:
    rid = run_dir.name
    rows = run_rows(rid)
    m = re.match(r"\d{8}_\d{6}_(\w+)_(agent|baseline)_s(\d+)$", rid)
    backend, condition, seed = (m.groups() if m else ("?", "?", "?"))
    stages = {}
    for r in rows:
        k = r.get("stage")
        if k in STAGE_KEYS:
            stages[k] = {"status": r.get("status"), "cost_usd": r.get("cost_usd") or 0,
                         "detail": r.get("fov_vertical_degrees") or r.get("task_id") or ""}
    proc = PROCS.get(rid)
    if proc is not None:
        alive = proc.poll() is None
    else:
        alive = False
    cost = round(sum(r.get("cost_usd") or 0 for r in rows if not r.get("mock")), 2)
    renders = sorted(run_dir.glob("render_*"))
    framing = None
    if renders:
        fj = renders[-1] / "framing.json"
        if fj.exists():
            framing = json.loads(fj.read_text(encoding="utf-8"))
    video = None
    if renders and (renders[-1] / "reference.mp4").exists():
        video = f"/api/media/{rid}/{renders[-1].name}/reference.mp4"
    prompt_file = run_dir / "seedance_prompt.txt"
    return {"id": rid, "backend": backend, "condition": condition, "seed": seed,
            "mtime": run_dir.stat().st_mtime, "status": "running" if alive else "done",
            "exit_code": None if alive else (proc.returncode if proc else 0),
            "stages": stages, "cost_usd": cost, "framing": framing, "video": video,
            "has_prompt": prompt_file.exists()}


@app.get("/api/overview")
def overview():
    backends = {}
    for name, b in CFG["backends"].items():
        if b.get("kind") == "openai_compatible":
            backends[name] = {"model": b.get("model"), "has_key": bool(api_key(b["api_key_env"]))}
        else:
            backends[name] = {"model": "scripted", "has_key": True}
    cache_dir = ROOT / "data" / "cache" / "tripo"
    runs = sorted(RUNS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if RUNS_DIR.exists() else []
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
                   "spent_usd": ledger.spent_usd(LEDGER),
                   "runs_total": len(runs)},
        "runway_key": bool(api_key("RUNWAYML_API_SECRET")),
        "kling_key": bool(api_key(k["api_key_env"])) if k else False,
        "tripo_key": bool(api_key("TRIPO_API_KEY")),
        "frozen_glb_sha256": (tripo.digest(ROOT / CFG["pipeline"]["seed_glb"])
                              if (ROOT / CFG["pipeline"]["seed_glb"]).exists() else None)[:16],
        "qualities": CFG["runway"]["qualities"],
        "kling": kling_info,
    }


_KLING_PACK_CACHE = {"t": 0.0, "data": None}


@app.get("/api/kling/packs")
def kling_packs():
    """Kling 资源包实时余量（免费接口，60s 缓存；余量口径有 12h 延迟）。"""
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
            return {"packs": [], "remaining": None, "error": str(e)[:200]}
    return _KLING_PACK_CACHE["data"]


@app.get("/api/runs")
def list_runs():
    if not RUNS_DIR.exists():
        return []
    out = []
    for d in sorted(RUNS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir():
            out.append(parse_run(d))
    return out


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    d = RUNS_DIR / run_id
    if not d.exists():
        raise HTTPException(404, "run 不存在")
    info = parse_run(d)
    info["ledger"] = run_rows(run_id)
    pf = d / "seedance_prompt.txt"
    info["prompt_text"] = pf.read_text(encoding="utf-8") if pf.exists() else None
    fr = d / "commercial" / "frozen_request.json"
    info["frozen_request"] = json.loads(fr.read_text(encoding="utf-8")) if fr.exists() else None
    log = UI_LOGS / f"{run_id}.log"
    if log.exists():
        info["log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-4000:]
    frames = []
    last_render = sorted(d.glob("render_*"))[-1] if sorted(d.glob("render_*")) else None
    if last_render:
        fdir = last_render / "frames"
        names = sorted(fdir.glob("*.png")) if fdir.exists() else []
        if names:
            frames = [f"/api/media/{run_id}/{last_render.name}/frames/{n.name}"
                      for n in [names[0], names[len(names) // 3], names[2 * len(names) // 3], names[-1]]]
    info["frames"] = frames
    return info


MEDIA_OK = re.compile(r"^render_[\w.]+/(reference\.mp4|closure_end\.png|frames/\d{4}\.png)$")


@app.get("/api/media/{run_id}/{path:path}")
def media(run_id: str, path: str):
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", run_id) or not MEDIA_OK.match(path):
        raise HTTPException(400, "路径不合法")
    f = (RUNS_DIR / run_id / path).resolve()
    if not str(f).startswith(str(RUNS_DIR.resolve())) or not f.exists():
        raise HTTPException(404, "文件不存在")
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
    """提示词扩写预览：LLM 把一句想法扩成结构化非镜头风格块（不发起运行）。"""
    idea = spec.idea.strip()
    if not idea or len(idea) > 300:
        raise HTTPException(400, "idea 需为 1-300 字")
    from pipeline import prompt_expander
    try:
        ps, block = prompt_expander.expand(idea, CFG, spec.backend, "-",
                                           LEDGER)
    except ValueError as e:                     # 镜头词汇红线拦截
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"spec": ps.model_dump(), "style_block": block}


@app.post("/api/runs")
def launch(spec: RunSpec):
    if spec.backend not in CFG["backends"]:
        raise HTTPException(400, f"未知后端 {spec.backend}")
    if spec.condition not in ("agent", "baseline"):
        raise HTTPException(400, "condition 只能是 agent/baseline")
    if not spec.dry_run and not spec.ack_paid:
        raise HTTPException(400, "真实扣费运行需要 ack_paid=true（UI 确认弹窗）")
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{spec.backend}_{spec.condition}_s{spec.seed}"
    cmd = [str(ROOT / ".venv/bin/python"), "run.py", "--backend", spec.backend,
           "--condition", spec.condition, "--seed", str(spec.seed), "--run-id", run_id]
    if spec.style.strip():
        cmd += ["--style", spec.style.strip()]
    if spec.dry_run:
        cmd.append("--dry-run")
    elif spec.ack_paid:
        cmd.append("--auto-approve")        # UI 弹窗即人工闸门
    log = UI_LOGS / f"{run_id}.log"
    PROCS[run_id] = subprocess.Popen(cmd, cwd=ROOT, stdout=open(log, "w"),
                                     stderr=subprocess.STDOUT)
    return {"run_id": run_id, "dry_run": spec.dry_run,
            "note": "真实扣费" if not spec.dry_run else "dry-run（不触网）"}


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=ROOT / "ui" / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "ui" / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=4207, log_level="warning")
