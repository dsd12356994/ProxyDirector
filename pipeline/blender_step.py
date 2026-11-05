"""宿主侧：调 Blender 工人渲染 + ffmpeg 编码 + 帧数/大小核验。"""
import json
import subprocess
import time
from pathlib import Path

from . import ledger
from .models import FramingReport, ShotPlan


def render_orbit(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str,
                 glb: Path, plan: ShotPlan, tag: str) -> FramingReport:
    """免费环节：本地 Blender 渲染 + 取景审计，可无限次重跑。"""
    out = run_dir / f"render_{tag}"
    pl = cfg["pipeline"]
    t0 = time.time()
    cmd = [pl["blender_bin"], "-b", "--factory-startup",
           "--python", str(Path(__file__).parent / "worker_render.py"), "--",
           "--glb", str(glb), "--plan", plan.model_dump_json(), "--out", str(out),
           "--width", str(pl["width"]), "--height", str(pl["height"]),
           "--fps", str(pl["fps"]), "--frames", str(pl["frames"]),
           "--normalize", str(pl["normalize_horizontal"])]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=pl["render_timeout_s"])
    (out / "worker_stdout.log").write_text(proc.stdout[-20000:], encoding="utf-8")
    (out / "worker_stderr.log").write_text(proc.stderr[-20000:], encoding="utf-8")
    # 注意：Blender 5.2 在脚本异常时退出码仍为 0，不能只看 returncode。
    # 双重判据：产物存在 + 输出无 Traceback。
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0 or "Traceback" in combined or not (out / "framing.json").exists():
        tail = combined.strip().splitlines()[-10:]
        raise RuntimeError("Blender 工人失败（退出码 %s）：\n%s" % (proc.returncode, "\n".join(tail)))
    report = json.loads((out / "framing.json").read_text(encoding="utf-8"))

    frames_dir = out / "frames"
    n_frames = len(list(frames_dir.glob("*.png")))
    if n_frames != pl["frames"]:
        raise RuntimeError(f"帧数不符：期望 {pl['frames']}，实际 {n_frames}")

    encode = subprocess.run([pl["ffmpeg_bin"], "-y", "-framerate", str(pl["fps"]),
                             "-start_number", "0", "-i", str(frames_dir / "%04d.png"),
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                             str(out / "reference.mp4")],
                            capture_output=True, text=True, timeout=300)
    if encode.returncode != 0:
        raise RuntimeError("ffmpeg 编码失败：" + encode.stderr[-500:])

    video = out / "reference.mp4"
    limit = cfg["runway"]["data_uri_limit_bytes"]
    if video.stat().st_size > limit * 0.95:      # 预留 base64 膨胀余量
        raise RuntimeError(f"参考视频 {video.stat().st_size // 1024}KB 过大（data-URI 上限 "
                           f"{limit // 1048576}MiB）：降低 crf 或分辨率")

    fr = FramingReport(
        passed=report["passed"], plan=plan,
        fov_vertical_degrees=report["fov_vertical_degrees"],
        lens_mm=report["lens_mm"], min_margins_lbrt=report["min_margins_lbrt"],
        side_sequence=report["side_sequence"], vertices=report["vertices"],
        frames=report["frames"], violations=report["violations"],
        video_path=str(video), render_seconds=report["render_seconds"])
    ledger.log({"run_id": run_id, "stage": "blender_render", "provider": "local",
                "status": "passed" if fr.passed else "framing_failed",
                "cost_usd": 0.0, "mock": False, "tag": tag,
                "fov_vertical_degrees": fr.fov_vertical_degrees,
                "min_margins_lbrt": fr.min_margins_lbrt,
                "latency_s": round(time.time() - t0, 1)}, ledger_path)
    return fr
