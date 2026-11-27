"""Smallest passing radius for the search briefs, measured with the benchmark's own 301-pose audit.

Runs directly in Python (via pure-Python `fast_audit.py`, no Blender required):
  .venv/bin/python experiments/shot_briefs/ground_truth.py --out data/experiments/shot_briefs/ground_truth.json

Also remains compatible with headless Blender's Python when invoked via `blender -b --python ... -- --out ...`.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (str(REPO), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

WORKER = HERE / "fixed_lens_worker" / "worker_render.py"
GLB = REPO / "assets" / "seed_tripo" / "model.glb"
RADII = [round(3.0 + 0.5 * i, 1) for i in range(11)]                 # 3.0 .. 8.0
ELEVATIONS = {
    "S1_search_e20": range(17, 24, 1),
    "S2_search_e70": range(67, 74, 1),
    "S3_search_e50_m10": range(47, 54, 1),
    "L1_lang_low_tight": range(10, 31, 2),
}
MARGIN = {"S3_search_e50_m10": 0.10}


def load_audit_only_worker():
    source = WORKER.read_text(encoding="utf-8")
    start = source.index('    frames_dir = out / "frames"\n')
    end = source.index("    report = {\n")
    code = source[:start] + "    render_t0 = time.time()\n" + source[end:]
    module = types.ModuleType("audit_only_worker")
    exec(compile(code, str(WORKER), "exec"), module.__dict__)
    return module


def _measure_with_blender() -> tuple[dict, dict]:
    import bpy

    worker = load_audit_only_worker()
    truth = {}
    with tempfile.TemporaryDirectory() as scratch, contextlib.redirect_stdout(io.StringIO()):
        for brief_id, elevations in ELEVATIONS.items():
            per_elevation = {}
            for elevation in elevations:
                passing = []
                for radius in RADII:
                    plan = {"elevation_degrees": float(elevation), "radius": radius, "start_degrees": 0.0,
                            "direction": "cw", "margin": MARGIN.get(brief_id, 0.05)}
                    sys.argv = ["blender", "--background", "--python", str(WORKER), "--", "--glb",
                                str(GLB), "--plan", json.dumps(plan),
                                "--out", scratch, "--width", "64", "--height", "36", "--frames", "300"]
                    worker.main()
                    report = json.loads((Path(scratch) / "framing.json").read_text(encoding="utf-8"))
                    if report["passed"]:
                        passing.append(radius)
                per_elevation[str(elevation)] = min(passing) if passing else None
            best = min((v for v in per_elevation.values() if v is not None), default=None)
            truth[brief_id] = {"min_passing_radius": best, "per_elevation": per_elevation}

    rig, camera = bpy.data.objects["OrbitRig"], bpy.data.objects["ORBIT_CAM"]
    camera_x = {}
    for azimuth in (0, 90, 180, 270):
        rig.rotation_euler.z = math.radians(azimuth)
        bpy.context.view_layer.update()
        camera_x[str(azimuth)] = round(camera.matrix_world.translation.x, 2)
    return truth, {"camera_x_by_azimuth": camera_x, "fixed_worker_cw_from_90": worker.side_sequence(90, "cw")}


def _measure_with_fast_audit() -> tuple[dict, dict]:
    import fast_audit

    truth = {}
    for brief_id, elevations in ELEVATIONS.items():
        per_elevation = {}
        for elevation in elevations:
            passing = []
            for radius in RADII:
                plan = {"elevation_degrees": float(elevation), "radius": radius, "start_degrees": 0.0,
                        "direction": "cw", "margin": MARGIN.get(brief_id, 0.05)}
                report = fast_audit.audit_plan(GLB, plan, width=64, height=36, frames=300)
                if report["passed"]:
                    passing.append(radius)
            per_elevation[str(elevation)] = min(passing) if passing else None
        best = min((v for v in per_elevation.values() if v is not None), default=None)
        truth[brief_id] = {"min_passing_radius": best, "per_elevation": per_elevation}

    camera_x = {
        str(azimuth): round(fast_audit.camera_position(RADII[-1], 30.0, azimuth)[0], 2)
        for azimuth in (0, 90, 180, 270)
    }
    return truth, {"camera_x_by_azimuth": camera_x, "fixed_worker_cw_from_90": fast_audit.side_sequence(90, "cw")}


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        raw = raw[raw.index("--") + 1 :]
    parser = argparse.ArgumentParser(description="Measure ground-truth smallest passing radii across search briefs.")
    parser.add_argument("--out", default=str(REPO / "data" / "experiments" / "audit_loop" / "ground_truth.json"))
    args = parser.parse_args(raw)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import bpy  # noqa: F401
        has_bpy = True
    except ImportError:
        has_bpy = False

    truth, side_check = _measure_with_blender() if has_bpy else _measure_with_fast_audit()
    out_path.write_text(
        json.dumps({"ground_truth": truth, "side_order_check": side_check}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("GROUND_TRUTH", json.dumps(truth))
    print("SIDE_ORDER_CHECK", json.dumps(side_check))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
