"""盲评打包：把全部成片匿名化复制到 data/eval/videos/，映射表单独存放。

用法：python -m pipeline.eval_pack [--shuffle-seed 4207] [--force]
评分类别从 run 目录名推断：
  *_baseline_* / mock_agent / mock_kling_real* → baseline（固定基线方案）
  astra_agent    → agent_gpt（GPT 系）
  deepseek_agent → agent_alt（其他 LLM）
  glm_agent      → agent_alt（其他 LLM）
"""
import argparse
import json
import random
import re
import shutil
from pathlib import Path

from .config import load_config


def classify(run_id: str) -> tuple[str, str, str] | None:
    """返回 (condition, backend, seed)；不认识的运行返回 None。"""
    # Interactive camera edits are deliberately outside the fixed evaluation conditions.
    m = re.match(r"\d{8}_\d{6}(?:-\d+)?_(\w+)_(agent|baseline)_s(\d+)$", run_id)
    if m:
        backend, condition, seed = m.group(1), m.group(2), m.group(3)
    elif re.match(r"\d{8}_mock_kling_real\d*$", run_id):
        backend, condition, seed = "mock", "agent", "4207"
    else:
        return None
    if condition == "baseline" or backend == "mock":  # baseline 条件或 mock 导演 = 固定基线方案（见 director.py）
        cond = "baseline"
    elif backend == "astra":
        cond = "agent_gpt"
    elif backend in ("deepseek", "glm"):
        cond = "agent_alt"
    else:
        return None
    return cond, backend, seed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--shuffle-seed", type=int, default=4207)
    p.add_argument("--force", action="store_true",
                   help="Back up (.bak) and overwrite existing evaluation sheet and key.")
    args = p.parse_args(argv)

    cfg = load_config()
    runs_dir = Path(cfg["_root"]) / "data" / "runs"
    if not runs_dir.is_dir():
        print(f"Runs directory not found: {runs_dir}. Run the pipeline to generate clips before building the evaluation pack.")
        return 1

    out_dir = Path(cfg["_root"]) / "data" / "eval"
    sheet_path = out_dir / "评分表.md"
    key_path = out_dir / "key.json"
    existing = [f for f in (sheet_path, key_path) if f.exists()]
    if existing and not args.force:
        names = ", ".join(f.name for f in existing)
        print(f"Refusing to overwrite existing evaluation files ({names}). Pass --force to back them up (.bak) and rebuild.")
        return 1

    entries = []
    for d in sorted(runs_dir.iterdir()):
        video = d / "commercial" / "video.mp4"
        if not video.exists() or video.stat().st_size < 100_000:   # dry-run 占位是空文件
            continue
        meta = classify(d.name)
        if not meta:
            continue
        framing = sorted(d.glob("render_*/framing.json"))
        entries.append({"run_id": d.name, "condition": meta[0], "backend": meta[1],
                        "seed": meta[2], "src": str(video),
                        "framing_passed": bool(json.loads(framing[-1].read_text(encoding="utf-8"))["passed"])
                        if framing else None})
    if not entries:
        print("No eligible video clips found in data/runs.")
        return 1

    rnd = random.Random(args.shuffle_seed)
    order = list(range(len(entries)))
    rnd.shuffle(order)

    if existing and args.force:
        for f in existing:
            shutil.copy2(f, f.with_name(f.name + ".bak"))

    videos_dir = out_dir / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    for old in videos_dir.glob("eval_*.mp4"):
        old.unlink()

    key = []
    for i, idx in enumerate(order, start=1):
        e = entries[idx]
        anon = f"eval_{i:02d}"
        shutil.copy2(e["src"], videos_dir / f"{anon}.mp4")
        key.append({"anon": anon, **{k: e[k] for k in ("run_id", "condition", "backend", "seed")}})
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")

    conds = {}
    for e in entries:
        conds.setdefault(e["condition"], []).append(e)
    lines = ["# ProxyDirector 盲评评分表", "",
             f"- 共 {len(entries)} 条成片，匿名编号见 data/eval/videos/（映射表 key.json 在盲评结束后才公开）",
             "- 每条按 1–5 分打分；最后三行为通过性判定（是/否）", "",
             "## 评分维度", "",
             "1. **构图紧凑度**：物体是否撑满画面、四边留白是否均匀（1=很松/被裁，5=紧凑均衡）",
             "2. **部件完整性**：鼠标/线缆/USB 是否全程可见无缺失变形（1=严重缺失,5=完好）",
             "3. **环绕完成度**：是否一次连续完整 360°、无切镜无反转（1=断裂,5=完美闭合）",
             "4. **外观还原**：与参考照片的黑壳/按键/滚轮是否一致（1=走样,5=还原）",
             "5. **整体质量**：作为产品视频的综合观感（1=不可用,5=可直接商用）", "",
             "## 通过性判定", "",
             "- 全程无部件出画（是/否）",
             "- 无水印/字幕/多余物（是/否）",
             "- 无场景切换/镜头方向突变（是/否）", "",
             "## 评分", "",
             "| 编号 | 构图 | 完整性 | 环绕 | 外观 | 整体 | 无出画 | 无水印 | 无切镜 | 备注 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i in range(1, len(entries) + 1):
        lines.append(f"| eval_{i:02d} |  |  |  |  |  |  |  |  |  |")
    sheet_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"打包完成：{len(entries)} 条 → {videos_dir}")
    for e in key:
        print(f"  {e['anon']} <- {e['run_id']} ({e['condition']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
