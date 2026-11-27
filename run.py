"""DSAI4207 Agent 管线入口。

常用命令：
  .venv/bin/python run.py check                          # 环境自检（Blender/ffmpeg/素材/密钥）
  .venv/bin/python run.py seed-tripo-cache               # 预置冻结 GLB 缓存（一次即可）
  .venv/bin/python run.py --backend mock --dry-run       # mock 全流程演练（免费，真实渲染）
  .venv/bin/python run.py --condition baseline --dry-run # 条件①固定方案（无 LLM）
  .venv/bin/python run.py --backend deepseek --seed 4208 # 真实 LLM；付费段仍要过闸门
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pipeline import director, ledger, tripo  # noqa: E402
from pipeline.blender_step import render_orbit  # noqa: E402
from pipeline.config import (BLENDER_ENV, FFMPEG_ENV, api_key, blender_bin, ffmpeg_bin,  # noqa: E402
                             find_program, load_config, resolve)
from pipeline.models import ShotPlan  # noqa: E402
from pipeline.prompt_template import render_prompt  # noqa: E402
from pipeline.tools import ToolContext  # noqa: E402


def utf8_console() -> None:
    """Print UTF-8 even when the output is redirected on Windows.

    The output contains characters such as the degree sign and an arrow. A redirected stream on
    Windows uses the legacy code page, where printing them can raise UnicodeEncodeError, and the
    Studio reads the run log back as UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def check(cfg: dict) -> int:
    """Report what this machine can run.

    The exit code covers the free local rehearsal only: Blender, FFmpeg and the frozen inputs. API
    keys are listed without failing the check, because only cloud directors and live generation
    need them.
    """
    pl = cfg["pipeline"]
    ready = True

    def required(found: bool, text: str, hint: str = "") -> None:
        nonlocal ready
        ready &= bool(found)
        print(("OK  " if found else "MISS") + "  " + text)
        if hint and not found:
            print("      " + hint)

    def optional(found: bool, text: str) -> None:
        print(("OK  " if found else "--  ") + "  " + text)

    blender = blender_bin(cfg)
    required(find_program(blender), f"Blender: {blender}",
             f"Install Blender 5.x, or set {BLENDER_ENV} or pipeline.blender_bin in config.yaml.")
    ffmpeg = ffmpeg_bin(cfg)
    required(find_program(ffmpeg), f"FFmpeg: {ffmpeg}",
             f"Install FFmpeg and put it on PATH, or set {FFMPEG_ENV} or pipeline.ffmpeg_bin in config.yaml.")
    views = resolve(pl["views_dir"])
    n_views = len(list(views.glob("view_[1-4].png"))) if views.exists() else 0
    required(n_views == 4, f"Frozen views: {n_views}/4 ({pl['views_dir']})")
    seed = resolve(pl["seed_glb"])
    required(seed.exists(), f"Frozen GLB: {pl['seed_glb']}")
    if n_views == 4 and seed.exists():
        view_files = {name: views / name for name in tripo.SLOT_OF_VIEW}
        cached = (Path(cfg["_root"]) / "data" / "cache" / "tripo"
                  / tripo.cache_key(view_files, tripo.frozen_params(cfg)) / "model.glb")
        optional(cached.exists(), "Frozen GLB cache" + ("" if cached.exists()
                                                         else ": not seeded yet. Run: python run.py seed-tripo-cache"))

    optional(api_key("TRIPO_API_KEY"), "TRIPO_API_KEY (required for uncached modeling)")
    optional(api_key("RUNWAYML_API_SECRET"), "RUNWAYML_API_SECRET (required for Runway generation)")
    video = cfg.get("video_backend", "runway")
    video_env = cfg["kling"]["api_key_env"] if video == "kling" else "RUNWAYML_API_SECRET"
    if video == "kling":
        key = api_key(video_env)
        optional(key, f"{video_env} (Kling video generation)")
        if key:
            from pipeline import kling as kling_mod
            try:
                packs = kling_mod.packs(cfg)
                if packs:
                    for p in packs:
                        print(f"     Credit pack {p['resource_pack_name']}: remaining "
                              f"{p.get('remaining_quantity')} ({p.get('resource_pack_applicable_models')})")
                else:
                    print("     No active Kling credit pack was reported.")
            except Exception as e:                       # 查询失败不阻断自检
                print(f"     Credit lookup failed. {str(e)[:120]}")
    for name, backend in cfg["backends"].items():
        if backend.get("api_key_env"):                   # every cloud director, whichever API style it uses
            optional(api_key(backend["api_key_env"]), f"{backend['api_key_env']} ({name} backend)")
    ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
    if ledger_path.exists():
        print(f"     {len(ledger_path.read_text(encoding='utf-8').splitlines())} ledger entries. Recorded spend "
              f"${ledger.spent_usd(ledger_path):.2f}")
    print("Rehearsal runs (--dry-run): " + ("ready." if ready else "not ready. See the MISS items above."))
    print(f"Live video generation ({video}): "
          + ("key configured." if api_key(video_env) else f"not configured, {video_env} is missing."))
    return 0 if ready else 1


def new_run_dir(cfg: dict, backend: str, condition: str, seed: int, *, chat: bool = False) -> Path:
    """Reserve the run directory before spawning work, including simultaneous web requests."""
    base = Path(cfg["_root"]) / "data" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S") + ("-chat" if chat else "")
    attempt = 1
    while True:
        suffix = f"-{attempt}" if attempt > 1 else ""
        directory = base / f"{timestamp}{suffix}_{backend}_{condition}_s{seed}"
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            attempt += 1


def video_duration(cfg: dict) -> int:
    return cfg["kling"]["duration"] if cfg.get("video_backend") == "kling" \
        else cfg["runway"]["duration"]


def _paid_gate_quote(cfg: dict) -> str:
    if cfg.get("video_backend") == "kling":
        from pipeline import kling as kling_mod
        est = kling_mod.estimate_credits(
            cfg, cfg["pipeline"]["frames"] / cfg["pipeline"]["fps"], cfg["kling"]["duration"])
        return (f"Kling {cfg['kling']['model']} {cfg['kling']['resolution']} "
                f"{cfg['kling']['duration']}s, estimated {est} credits")
    q = cfg["runway"]["qualities"]["480p"]
    return f"Runway 480p, estimated {q['credits']} credits (USD {q['usd']:.2f})"


def submit_request(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str,
                   fr, prompt: str, dry_run: bool) -> None:
    """按当前成片后端提交（dry_run=True 时只冻结请求不触网不扣费）。"""
    views_dir = resolve(cfg["pipeline"]["views_dir"])
    view_files = {n: views_dir / n for n in tripo.SLOT_OF_VIEW}
    if cfg.get("video_backend") == "kling":
        from pipeline import kling as kling_mod
        body, record = kling_mod.build_request(cfg, run_id, Path(fr.video_path),
                                               view_files, prompt, dry_run=dry_run)
        kling_mod.submit(cfg, run_dir, ledger_path, run_id, body, record, dry_run=dry_run)
    else:
        from pipeline import runway as rw
        body, record = rw.build_request(cfg, Path(fr.video_path), view_files, prompt, "480p")
        rw.submit(cfg, run_dir, ledger_path, run_id, body, record, dry_run=dry_run)


def expand_style(cfg: dict, run_dir: Path, run_id: str, idea: str,
                 backend: str | None) -> str:
    """提示词扩写：一句想法 → 结构化风格块。产物与账目落 run 目录，风格块供两条条件路径共用。"""
    from pipeline import prompt_expander
    spec, block = prompt_expander.expand(idea, cfg, backend, run_id,
                                         Path(cfg["_root"]) / "data" / "ledger.jsonl")
    (run_dir / "style_expansion.json").write_text(
        json.dumps({"idea": idea, "spec": spec.model_dump(), "style_block": block},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Style saved to style_expansion.json. {block[:120]}…")
    return block


def run_baseline(cfg: dict, run_dir: Path, run_id: str, dry_run: bool, auto_approve: bool,
                 style_block: str = "") -> None:
    """条件①：人工基线。固定相机方案（05 实验参数）+ 冻结模板，不经 LLM。"""
    ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
    glb = tripo.ensure_glb(cfg, run_dir, ledger_path, run_id, dry_run)
    plan = ShotPlan(**cfg["pipeline"]["baseline_plan"])
    fr = render_orbit(cfg, run_dir, ledger_path, run_id, glb, plan, "baseline")
    print("Baseline shot:", fr.summary())
    if not fr.passed:
        print("Baseline framing failed. Revise the fixed camera plan before retrying.")
        return
    prompt = render_prompt(fr, video_duration(cfg), style_block=style_block)
    (run_dir / "seedance_prompt.txt").write_text(prompt, encoding="utf-8")
    if dry_run:
        submit_request(cfg, run_dir, ledger_path, run_id, fr, prompt, dry_run=True)
        print("[dry-run] Baseline request saved to commercial/frozen_request.json.")
        return
    if not auto_approve:
        answer = input(f"\n[Paid baseline] {_paid_gate_quote(cfg)}. "
                       f"Type yes to submit: ").strip().lower()
        if answer != "yes":
            print("The user declined the baseline submission.")
            return
    submit_request(cfg, run_dir, ledger_path, run_id, fr, prompt, dry_run=False)
    print("Baseline output saved to commercial/video.mp4.")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", default="mock", help="Backend key from config.yaml, such as mock, astra, or deepseek")
    p.add_argument("--condition", choices=["agent", "baseline"], default="agent")
    p.add_argument("--seed", type=int, default=4207)
    p.add_argument("--dry-run", action="store_true", help="Save the video request locally without submitting it. Remote language models may still incur fees.")
    p.add_argument("--auto-approve", action="store_true", help="Use prior authorization for the paid video stage")
    p.add_argument("--run-id", default=None, help="Use this run directory name for a new or resumed run")
    p.add_argument("--style", default=None,
                   help="An appearance idea for the style model, such as matte black studio lighting")
    p.add_argument("--expand-backend", default=None,
                   help="Style backend (defaults to the active model or the configured style backend in mock mode)")
    args, extra = p.parse_known_args()
    utf8_console()
    if extra[1:] or (extra and extra[0] not in ("check", "seed-tripo-cache")):
        # A misspelt flag must not be dropped silently: "--dryrun" would otherwise start a live run.
        p.error("unrecognized arguments: " + " ".join(extra[1:] if extra[0] in ("check", "seed-tripo-cache") else extra))

    cfg = load_config()
    if extra and extra[0] in ("check", "seed-tripo-cache"):
        if extra[0] == "check":
            return check(cfg)
        ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        glb = tripo.seed_cache(cfg, ledger_path,
                               "20260921_mouse_multiview_01 Tripo output from the four frozen views. "
                               "Each view matches the SHA-256 entry in frozen.json.")
        print("Frozen GLB cached at", glb.relative_to(Path(cfg["_root"])))
        print("model_sha256 =", tripo.digest(glb))
        return 0

    if args.backend not in cfg["backends"]:
        print(f"Unknown backend {args.backend}. Available backends: {list(cfg['backends'])}")
        return 2

    runs_base = Path(cfg["_root"]) / "data" / "runs"
    if args.run_id:
        import re as _re
        if not _re.fullmatch(r"[0-9A-Za-z_.-]+", args.run_id):
            raise SystemExit("Invalid characters in --run-id.")
        run_dir = runs_base / args.run_id
        run_dir.mkdir(parents=True, exist_ok=True)   # 允许同目录恢复：付费任务只轮询不重发
    else:
        run_dir = new_run_dir(cfg, args.backend, args.condition, args.seed)
    run_id = run_dir.name
    print(f"Run directory: data/runs/{run_id}")

    style_block = ""
    if args.style:
        style_block = expand_style(cfg, run_dir, run_id, args.style,
                                   args.expand_backend or args.backend)

    if args.condition == "baseline":
        run_baseline(cfg, run_dir, run_id, args.dry_run, args.auto_approve, style_block)
    else:
        ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
        ctx = ToolContext(cfg=cfg, run_dir=run_dir, run_id=run_id,
                          dry_run=args.dry_run,
                          auto_approve=args.auto_approve or cfg["gate"]["auto_approve"],
                          style_block=style_block)
        ctx.glb = tripo.ensure_glb(cfg, run_dir, ledger_path, run_id, args.dry_run)
        output, usage = director.run_agent(ctx, args.backend)
        print("\n=== Director result ===")
        print(output)

    ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:                     # a half-written line must not fail a finished run
            continue
        if row.get("run_id") == run_id:
            rows.append(row)
    print(f"\n=== Run ledger ({len(rows)} entries) ===")
    for r in rows:
        cost = f"${r.get('cost_usd', 0):.2f}" if r.get("cost_usd") else "  -  "
        print(f"  {r.get('stage', '?'):16s} {r.get('status', '?'):24s} {cost}")
    print(f"Run spend: ${ledger.spent_usd(ledger_path, run_id):.2f}. Total recorded spend: "
          f"${ledger.spent_usd(ledger_path):.2f} (budget ${cfg['budget']['max_usd']:.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
