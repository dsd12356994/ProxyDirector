"""Smallest passing radius for the search briefs, measured with the benchmark's own audit.

Runs inside Blender's Python:
  /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup \\
      --python experiments/shot_briefs/ground_truth.py -- --out data/experiments/shot_briefs/ground_truth.json

The framing audit runs before the render loop, so the worker source is loaded with its render and
save steps removed (in memory only) and the identical audit code is swept over the brief's whole
accepted elevation range at the trial resolution (300 frames, 301 audited poses).
"""
import contextlib
import io
import itertools
import json
import math
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORKER = HERE / "fixed_lens_worker" / "worker_render.py"
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


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    out_path = Path(argv[argv.index("--out") + 1])
    out_path.parent.mkdir(parents=True, exist_ok=True)
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
                                str(REPO / "assets" / "seed_tripo" / "model.glb"), "--plan", json.dumps(plan),
                                "--out", scratch, "--width", "64", "--height", "36", "--frames", "300"]
                    worker.main()
                    report = json.loads((Path(scratch) / "framing.json").read_text(encoding="utf-8"))
                    if report["passed"]:
                        passing.append(radius)
                per_elevation[str(elevation)] = min(passing) if passing else None
            best = min((v for v in per_elevation.values() if v is not None), default=None)
            truth[brief_id] = {"min_passing_radius": best, "per_elevation": per_elevation}

    import bpy
    rig, camera = bpy.data.objects["OrbitRig"], bpy.data.objects["ORBIT_CAM"]
    camera_x = {}
    for azimuth in (0, 90, 180, 270):
        rig.rotation_euler.z = math.radians(azimuth)
        bpy.context.view_layer.update()
        camera_x[azimuth] = round(camera.matrix_world.translation.x, 2)
    out_path.write_text(json.dumps({"ground_truth": truth,
                                    "side_order_check": {"camera_x_by_azimuth": camera_x,
                                                         "fixed_worker_cw_from_90": worker.side_sequence(90, "cw")}},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("GROUND_TRUTH", json.dumps(truth))
    print("SIDE_ORDER_CHECK", json.dumps({"camera_x_by_azimuth": camera_x,
                                          "fixed_worker_cw_from_90": worker.side_sequence(90, "cw")}))


if __name__ == "__main__":
    main()
