"""宿主侧：调 Blender 工人渲染 + ffmpeg 编码 + 帧数/大小核验。"""
import json
import subprocess
import time
from pathlib import Path

from . import config, ledger
from .models import FramingReport, ShotPlan


def require_programs(cfg: dict) -> tuple[str, str]:
    """The Blender and FFmpeg executables, or one clear error before any time is spent rendering."""
    blender, ffmpeg = config.blender_bin(cfg), config.ffmpeg_bin(cfg)
    if not config.find_program(blender):
        raise RuntimeError(f"Blender was not found ({blender}). Install Blender 5.x, or set {config.BLENDER_ENV} "
                           "or pipeline.blender_bin in config.yaml to the Blender executable.")
    if not config.find_program(ffmpeg):
        raise RuntimeError(f"FFmpeg was not found ({ffmpeg}). Install FFmpeg and put it on PATH, or set "
                           f"{config.FFMPEG_ENV} or pipeline.ffmpeg_bin in config.yaml.")
    return blender, ffmpeg


def render_orbit(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str,
                 glb: Path, plan: ShotPlan, tag: str) -> FramingReport:
    """免费环节：本地 Blender 渲染 + 取景审计，可无限次重跑。"""
    out = run_dir / f"render_{tag}"
    pl = cfg["pipeline"]
    t0 = time.time()
    blender, ffmpeg = require_programs(cfg)
    out.mkdir(parents=True, exist_ok=True)       # the logs below are written even if Blender dies at once
    cmd = [blender, "-b", "--factory-startup",
           "--python", str(Path(__file__).parent / "worker_render.py"), "--",
           "--glb", str(glb), "--plan", plan.model_dump_json(), "--out", str(out),
           "--width", str(pl["width"]), "--height", str(pl["height"]),
           "--fps", str(pl["fps"]), "--frames", str(pl["frames"]),
           "--normalize", str(pl["normalize_horizontal"])]
    # Decode as UTF-8 and never fail on a stray byte: the console code page of a Windows machine
    # is not UTF-8, and a decoding error here would hide the real render result.
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=pl["render_timeout_s"])
    (out / "worker_stdout.log").write_text(proc.stdout[-20000:], encoding="utf-8")
    (out / "worker_stderr.log").write_text(proc.stderr[-20000:], encoding="utf-8")
    # 注意：Blender 5.2 在脚本异常时退出码仍为 0，不能只看 returncode。
    # 双重判据：产物存在 + 输出无 Traceback。
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0 or "Traceback" in combined or not (out / "framing.json").exists():
        tail = combined.strip().splitlines()[-10:]
        raise RuntimeError("Blender render failed (exit code %s).\n%s" % (proc.returncode, "\n".join(tail)))
    report = json.loads((out / "framing.json").read_text(encoding="utf-8"))

    frames_dir = out / "frames"
    n_frames = len(list(frames_dir.glob("*.png")))
    if n_frames != pl["frames"]:
        raise RuntimeError(f"Expected {pl['frames']} frames but found {n_frames}.")

    encode = subprocess.run([ffmpeg, "-y", "-framerate", str(pl["fps"]),
                             "-start_number", "0", "-i", str(frames_dir / "%04d.png"),
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                             str(out / "reference.mp4")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if encode.returncode != 0:
        raise RuntimeError("Video encoding failed: " + encode.stderr[-500:])

    video = out / "reference.mp4"
    limit = cfg["runway"]["data_uri_limit_bytes"]
    if video.stat().st_size > limit * 0.95:      # 预留 base64 膨胀余量
        raise RuntimeError(f"The reference video is {video.stat().st_size // 1024} kB. The data URI limit is "
                           f"{limit // 1048576} MiB. Reduce the bitrate or resolution.")

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
