"""无头 Blender 工人：GLB -> 归一化 -> 按 camera plan 紧凑取景 -> 环绕渲染 + 逐帧取景审计。

在 Blender 自带 Python 里运行：
  blender -b --factory-startup --python worker_render.py -- --glb <path> --plan '<json>' --out <dir> ...

取景算法移植自 20260922 紧凑取景实验（render_compact.py，已验证的解析极值法）：
对全部顶点的整圈透视投影求解析极值 -> 定固定焦距与镜头偏移 -> 逐帧用真实相机矩阵独立审计。
"""
import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector


def extrema(a, b, n, c, e, d):
    """(n+a·cosθ+b·sinθ)/(d+c·cosθ+e·sinθ) 在整圆上的精确极值。要求所有顶点全程在相机前方。"""
    q = d * d - c * c - e * e
    if np.any(q <= 0) or np.any(d <= 0):
        raise ValueError("存在顶点在连续环绕中落到相机后方：请增大 radius 或降低 elevation_degrees")
    center = d * n - a * c - b * e
    disc = center * center - q * (n * n - a * a - b * b)
    if np.any(disc < -1e-10):
        raise ValueError("连续轨道极值判别式异常")
    spread = np.sqrt(np.maximum(disc, 0))
    return (center - spread) / q, (center + spread) / q


def world_vertices():
    arrays = []
    for obj in sorted([o for o in bpy.context.scene.objects if o.type == "MESH"], key=lambda o: o.name):
        co = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
        obj.data.vertices.foreach_get("co", co)
        m = np.array(obj.matrix_world)
        arrays.append(co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3])
    return np.concatenate(arrays)


def side_sequence(start: float, direction: str) -> list[str]:
    """环绕依次呈现的面（方位角->面名的约定：0°=front，逆时针 azimuth 递增为 right->back->left）。"""
    order = ["front", "left", "back", "right"] if direction == "cw" else ["front", "right", "back", "left"]
    k = round(((start % 360) / 90)) % 4
    return order[k:] + order[:k]


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    p = argparse.ArgumentParser()
    p.add_argument("--glb", required=True)
    p.add_argument("--plan", required=True, help="ShotPlan JSON")
    p.add_argument("--out", required=True)
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=540)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--frames", type=int, default=300)
    p.add_argument("--normalize", type=float, default=3.0, help="最长水平尺寸归一到该值")
    args = p.parse_args(argv)
    plan = json.loads(args.plan)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bpy.ops.import_scene.gltf(filepath=args.glb)
    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        raise ValueError("GLB 导入后没有网格对象")

    # 归一化：水平居中，最长水平尺寸 -> args.normalize，高度中心放到 z 中点
    pts = np.concatenate([np.array([list(o.matrix_world @ Vector(c[:])) for c in o.bound_box]) for o in meshes])
    hmin, hmax = pts[:, :2].min(axis=0), pts[:, :2].max(axis=0)
    zmin, zmax = pts[:, 2].min(), pts[:, 2].max()
    center = np.array([(hmin[0] + hmax[0]) / 2, (hmin[1] + hmax[1]) / 2, (zmin + zmax) / 2])
    scale = args.normalize / max(hmax - hmin)
    from mathutils import Matrix
    T = Matrix.Translation((-center).tolist())
    S = Matrix.Scale(scale, 4)
    for o in meshes:
        o.matrix_world = S @ T @ o.matrix_world

    xyz = world_vertices()
    geom_sha = hashlib.sha256(xyz.tobytes()).hexdigest()

    # 相机 rig：与紧凑实验同构——target 在原点上方 z 中点，相机绕水平圆
    target = Vector((0.0, 0.0, 0.0))          # 归一化后 z 中心=0（S@T 已把中心移到原点）
    radius = float(plan["radius"])
    elev = math.radians(float(plan["elevation_degrees"]))
    margin = float(plan["margin"])
    aspect = args.width / args.height

    x, y, z = xyz.T
    s, c = math.sin(elev), math.cos(elev)
    d = radius / c + s * (target.z - z)
    dc, ds = c * y, -c * x
    xlo, xhi = extrema(x, y, np.zeros(len(x)), dc, ds, d)
    ylo, yhi = extrema(s * y, -s * x, c * (z - target.z), dc, ds, d)
    ranges = np.array([xlo.min(), ylo.min(), xhi.max(), yhi.max()])
    rc = (ranges[:2] + ranges[2:]) / 2
    span = ranges[2:] - ranges[:2]
    tangent = max(span[0] / (2 * aspect * (1 - 2 * margin)), span[1] / (2 * (1 - 2 * margin)))
    tangent *= 1.0002                          # 单精度余量（同紧凑实验）

    cam_data = bpy.data.cameras.new("ORBIT_CAM")
    cam_data.sensor_fit = "VERTICAL"
    cam_data.sensor_height = 24.0
    cam_data.lens = cam_data.sensor_height / (2 * tangent)
    cam_data.shift_x = rc[0] / (2 * tangent)
    cam_data.shift_y = rc[1] / (2 * tangent)
    cam = bpy.data.objects.new("ORBIT_CAM", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam

    rig = bpy.data.objects.new("OrbitRig", None)
    scene.collection.objects.link(rig)
    rig.location = target
    cam.parent = rig
    cam.location = (0.0, -radius, radius * math.tan(elev))
    cam.rotation_euler = (-Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()
    rig.rotation_mode = "XYZ"

    start = float(plan["start_degrees"])
    end = start - 360 if plan["direction"] == "cw" else start + 360
    for frame, deg in [(1, start), (args.frames + 1, end)]:
        rig.rotation_euler.z = math.radians(deg)
        rig.keyframe_insert(data_path="rotation_euler", index=2, frame=frame)
    for layer in rig.animation_data.action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for key in curve.keyframe_points:
                        key.interpolation = "LINEAR"

    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = args.width
    scene.render.resolution_y = args.height
    scene.render.resolution_percentage = 100
    scene.render.fps = args.fps
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.frame_start, scene.frame_end = 1, args.frames

    # 逐帧独立审计：真实相机矩阵投影全部顶点（每帧+首末），不依赖解析解
    violations, bounds = [], []
    eps = 2e-5
    for i in range(args.frames + 1):
        scene.frame_set(1 + i)
        bpy.context.view_layer.update()
        m = np.array(cam.matrix_world)
        local = (xyz - m[:3, 3]) @ m[:3, :3]
        depth = -local[:, 2]
        uv = np.column_stack([.5 + local[:, 0] / (2 * depth * tangent * aspect) - cam_data.shift_x / aspect,
                              .5 + local[:, 1] / (2 * depth * tangent) - cam_data.shift_y])
        if depth.min() <= 0:
            violations.append(f"第{i}帧有顶点在相机后方")
        elif uv.min() < margin - eps or uv.max() > 1 - margin + eps:
            lo, hi = uv.min(axis=0), uv.max(axis=0)
            violations.append(
                f"第{i}帧(方位{(start + (end - start) * i / args.frames) % 360:.0f}°)越界："
                f"左{lo[0]:.3f}/下{lo[1]:.3f}/右{1 - hi[0]:.3f}/上{1 - hi[1]:.3f}（要求>{margin:.2f}）")
        bounds.append([float(uv[:, 0].min()), float(uv[:, 1].min()),
                       float(uv[:, 0].max()), float(uv[:, 1].max())])
    b = np.array(bounds)
    margins_lbrt = [float(b[:, 0].min()), float(b[:, 1].min()),
                    float(1 - b[:, 2].max()), float(1 - b[:, 3].max())]
    fov_v = math.degrees(2 * math.atan(tangent))

    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)
    render_t0 = time.time()
    for i in range(args.frames):
        scene.frame_set(i + 1)
        scene.render.filepath = str(frames_dir / f"{i:04d}.png")
        bpy.ops.render.render(write_still=True)
    scene.frame_set(args.frames + 1)
    scene.render.filepath = str(out / "closure_end.png")
    bpy.ops.render.render(write_still=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out / "scene.blend"))

    report = {
        "passed": not violations,
        "plan": plan,
        "fov_vertical_degrees": fov_v,
        "lens_mm": cam_data.lens,
        "shift": [cam_data.shift_x, cam_data.shift_y],
        "min_margins_lbrt": margins_lbrt,
        "side_sequence": side_sequence(start, plan["direction"]),
        "vertices": len(xyz),
        "frames": args.frames,
        "violations": violations[:20],
        "geometry_world_vertices_sha256": geom_sha,
        "render_seconds": round(time.time() - render_t0, 1),
        "method": "整圈解析极值定焦距+固定偏移；逐帧真实相机矩阵审计全部顶点（含首末）",
    }
    (out / "framing.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WORKER_DONE", json.dumps({"passed": report["passed"], "fov": round(fov_v, 2),
                                     "margins_lbrt": [round(m, 4) for m in margins_lbrt],
                                     "render_seconds": report["render_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
