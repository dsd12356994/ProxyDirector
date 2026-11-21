"""Replay the September compact-orbit reference and audit a centered digital crop.

assets/framing_lab bundles the original render_compact/framing.json and reference.mp4
from 05_紧凑取景实验_构图最好. The recorded bounds include the unencoded closure pose.
"""
import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse


router = APIRouter(prefix="/api/framing-lab", tags=["framing lab"])
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "framing_lab"
FPS = 30
VIDEO_FRAMES = 300


@lru_cache(maxsize=1)
def _recorded_bounds():
    report = json.loads((ASSETS / "framing.json").read_text(encoding="utf-8"))
    if report["samples"] != VIDEO_FRAMES + 1 or len(report["bounds"]) != report["samples"]:
        raise ValueError("Compact orbit needs 300 video frames and one closure pose")
    return tuple(tuple(bounds) for bounds in report["bounds"])


@router.get("/video")
def video():
    return FileResponse(ASSETS / "reference.mp4", media_type="video/mp4")


@router.get("/bounds")
def screen_bounds():
    """Original left / top / right / bottom coordinates for synchronized playback."""
    return [[left, 1 - top, right, 1 - bottom]
            for left, bottom, right, top in _recorded_bounds()]


@router.get("")
def inspect_framing(
    zoom: float = Query(1.0, ge=1.0, le=1.5),
    margin: float = Query(0.05, ge=0.0, le=0.2),
    frame: int = Query(0, ge=0, le=VIDEO_FRAMES),
):
    """Audit every recorded box after a centered, uniform screen-space scale.

    Bounds use top-left-origin normalized display coordinates. A negative edge
    margin means the geometry crosses the visible image edge at that pose.
    """
    source = _recorded_bounds()
    failed = []
    cropped = []
    edge_minima = dict.fromkeys(("left", "bottom", "right", "top"), 1.0)
    worst = None
    selected = None

    for index, recorded in enumerate(source):
        left, bottom_y, right, top_y = (0.5 + zoom * (v - 0.5) for v in recorded)
        margins = {"left": left, "bottom": bottom_y, "right": 1.0 - right, "top": 1.0 - top_y}
        edge = min(margins, key=margins.get)
        clearance = margins[edge]
        if clearance < margin:
            failed.append(index)
        if clearance < 0:
            cropped.append(index)
        for side, distance in margins.items():
            edge_minima[side] = min(edge_minima[side], distance)
        if worst is None or clearance < worst["margin"]:
            worst = {"frame": index, "time_s": index / FPS, "edge": edge, "margin": clearance}
        if index == frame:
            selected = {
                "index": index, "time_s": index / FPS, "video_frame_index": index % VIDEO_FRAMES,
                "bounds": {"left": left, "top": 1.0 - top_y, "right": right, "bottom": 1.0 - bottom_y},
                "margins": margins, "passed": clearance >= margin, "cropped": clearance < 0,
            }

    return {
        "zoom": zoom, "margin": margin, "passed": not failed, "cropped": bool(cropped),
        "min_margin": worst["margin"], "edge_minima": edge_minima, "worst": worst,
        "failed_poses": len(failed), "cropped_poses": len(cropped),
        "first_failed_frame": failed[0] if failed else None,
        "first_cropped_frame": cropped[0] if cropped else None,
        "pose_count": len(source), "video_frames": VIDEO_FRAMES, "fps": FPS,
        "duration_s": VIDEO_FRAMES / FPS, "frame": selected,
        "video_url": "/api/framing-lab/video",
    }
