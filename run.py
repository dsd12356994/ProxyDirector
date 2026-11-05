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
from pipeline.config import api_key, load_config, resolve  # noqa: E402
from pipeline.models import ShotPlan  # noqa: E402
from pipeline.prompt_template import render_prompt  # noqa: E402
from pipeline.tools import ToolContext  # noqa: E402


def check(cfg: dict) -> int:
    ok = True
    pl = cfg["pipeline"]
    blender = Path(pl["blender_bin"])
    print(("OK  " if blender.exists() else "MISS") + f"  Blender: {blender}")
    ok &= blender.exists()
    views = resolve(pl["views_dir"])
    missing = [p.name for p in sorted(views.glob("view_*.png")) if not p.exists()]
    n_views = len(list(views.glob("view_*.png"))) if views.exists() else 0
    print(("OK  " if n_views == 4 and not missing else "MISS") +
          f"  冻结视图: {n_views}/4 {views}")
    ok &= n_views == 4
    seed = resolve(pl["seed_glb"])
    print(("OK  " if seed.exists() else "MISS") + f"  冻结 GLB: {seed}")
    ok &= seed.exists()
    key = api_key("TRIPO_API_KEY")
    print(("OK  " if key else "--  ") + "  TRIPO_API_KEY（建模用，缓存命中可不需要）")
    key = api_key("RUNWAYML_API_SECRET")
    print(("OK  " if key else "--  ") + "  RUNWAYML_API_SECRET（成片生成必需）")
    for b in cfg["backends"]:
        if cfg["backends"][b].get("kind") == "openai_compatible":
            env = cfg["backends"][b]["api_key_env"]
            key = api_key(env)
            print(("OK  " if key else "--  ") + f"  {env}（后端 {b}）")
    ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
    if ledger_path.exists():
        print(f"     账本已记 {len(ledger_path.read_text().splitlines())} 行，累计真实花费 "
              f"${ledger.spent_usd(ledger_path):.2f}")
    print("自检" + ("通过" if ok else "有缺项：见上") )
    return 0 if ok else 1


def new_run_dir(cfg: dict, backend: str, condition: str, seed: int) -> Path:
    d = Path(cfg["_root"]) / "data" / "runs" / f"{time.strftime('%Y%m%d_%H%M%S')}_{backend}_{condition}_s{seed}"
    d.mkdir(parents=True)
    return d


def run_baseline(cfg: dict, run_dir: Path, run_id: str, dry_run: bool, auto_approve: bool) -> None:
    """条件①：人工基线。固定相机方案（05 实验参数）+ 冻结模板，不经 LLM。"""
    ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
    glb = tripo.ensure_glb(cfg, run_dir, ledger_path, run_id, dry_run)
    plan = ShotPlan(**cfg["pipeline"]["baseline_plan"])
    fr = render_orbit(cfg, run_dir, ledger_path, run_id, glb, plan, "baseline")
    print("基线取景：", fr.summary())
    if not fr.passed:
        print("基线取景未通过——需要人工调参后重跑（这正是基线条件的语义）")
        return
    prompt = render_prompt(fr, cfg["runway"]["duration"])
    (run_dir / "seedance_prompt.txt").write_text(prompt, encoding="utf-8")
    if dry_run:
        from pipeline import runway as rw
        views_dir = resolve(cfg["pipeline"]["views_dir"])
        body, record = rw.build_request(cfg, Path(fr.video_path),
                                        {n: views_dir / n for n in tripo.SLOT_OF_VIEW},
                                        prompt, "480p")
        rw.submit(cfg, run_dir, ledger_path, run_id, body, record, dry_run=True)
        print("[dry-run] 基线请求已冻结：", run_dir / "commercial" / "frozen_request.json")
    else:
        print("基线成片提交请在闸门确认后执行真实运行（去掉 --dry-run）")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", default="mock", help="config.yaml 里的 backends 键：mock/astra/deepseek")
    p.add_argument("--condition", choices=["agent", "baseline"], default="agent")
    p.add_argument("--seed", type=int, default=4207)
    p.add_argument("--dry-run", action="store_true", help="不触网、不扣费；Seedance 只冻结请求")
    p.add_argument("--auto-approve", action="store_true", help="付费前跳过人工确认（UI 弹窗确认后等效）")
    p.add_argument("--run-id", default=None, help="指定运行目录名（UI 发起时用），只允许安全字符")
    sub = p.add_mutually_exclusive_group()
    args, extra = p.parse_known_args()

    cfg = load_config()
    if extra and extra[0] in ("check", "seed-tripo-cache"):
        if extra[0] == "check":
            return check(cfg)
        ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        glb = tripo.seed_cache(cfg, ledger_path,
                               "20260921_mouse_multiview_01 批次 Tripo 输出（输入=03 文件夹白底四图，"
                               "SHA-256 与 frozen.json 逐项一致）")
        print("冻结 GLB 缓存已预置：", glb)
        print("model_sha256 =", tripo.digest(glb))
        return 0

    if args.backend not in cfg["backends"]:
        print(f"未知后端 {args.backend}；可选：{list(cfg['backends'])}")
        return 2

    runs_base = Path(cfg["_root"]) / "data" / "runs"
    if args.run_id:
        import re as _re
        if not _re.fullmatch(r"[0-9A-Za-z_.-]+", args.run_id):
            raise SystemExit("--run-id 含非法字符")
        run_dir = runs_base / args.run_id
        run_dir.mkdir(parents=True)
    else:
        run_dir = new_run_dir(cfg, args.backend, args.condition, args.seed)
    run_id = run_dir.name
    print(f"运行目录：{run_dir}")

    if args.condition == "baseline":
        run_baseline(cfg, run_dir, run_id, args.dry_run, args.auto_approve)
    else:
        ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
        ctx = ToolContext(cfg=cfg, run_dir=run_dir, run_id=run_id,
                          dry_run=args.dry_run,
                          auto_approve=args.auto_approve or cfg["gate"]["auto_approve"])
        ctx.glb = tripo.ensure_glb(cfg, run_dir, ledger_path, run_id, args.dry_run)
        output, usage = director.run_agent(ctx, args.backend)
        print("\n=== Agent 结论 ===")
        print(output)

    ledger_path = Path(cfg["_root"]) / "data" / "ledger.jsonl"
    rows = [json.loads(l) for l in ledger_path.read_text().splitlines()
            if json.loads(l).get("run_id") == run_id]
    print(f"\n=== 本次账本（{len(rows)} 行）===")
    for r in rows:
        cost = f"${r.get('cost_usd', 0):.2f}" if r.get("cost_usd") else "  -  "
        print(f"  {r.get('stage', '?'):16s} {r.get('status', '?'):24s} {cost}")
    print(f"本次真实花费：${ledger.spent_usd(ledger_path, run_id):.2f} / 全部累计 "
          f"${ledger.spent_usd(ledger_path):.2f}（预算 ${cfg['budget']['max_usd']:.0f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
