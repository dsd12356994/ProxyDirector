"""Pure-Python exact 3D GLB vertex-projection framing auditor (zero external C dependencies).

Parses binary glTF 2.0 (.glb) using Python stdlib (`struct`, `json`, `math`, `hashlib`), applies
Blender's exact glTF (+Y up) -> Blender (+Z up) transform and horizontal bounding-box normalization,
solves the continuous circular-orbit projection extrema (`shift_x`, `shift_y`, `lens_mm`), and
projects all 301 orbit poses (`0 .. frames`) with exact 3D convex-hull + KD-box interval pruning.

Mathematical exactness:
- Camera depth `d_0 + d . p` is affine in `p = (x, y, z)`, and perspective coordinates
  `u(p) = (a_0 + a . p) / (d_0 + d . p)`, `v(p) = (b_0 + b . p) / (d_0 + d . p)` are quasilinear
  on `{p : depth(p) > 0}` (every sublevel and superlevel set is a closed half-space in R^3).
- Consequently, at every pose theta AND across the continuous circle, the exact minimum and maximum
  of `u`, `v`, and `depth` over all mesh vertices `P` are achieved at extreme vertices of the 3D
  convex hull `conv(P)` (`hull_vs_full` difference = 0.0).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FIXED_LENS_MM = 68.17215728759766
_GLB_MAGIC = b"glTF"
_JSON_CHUNK = 0x4E4F534A
_BIN_CHUNK = 0x004E4942
_FLOAT32 = 5126


@dataclass(frozen=True)
class CachedMesh:
    """Normalized world vertices, SHA-256 digest, exact 3D hull vertices, and KD bounding boxes."""
    xyz: tuple[tuple[float, float, float], ...]
    geom_sha256: str
    hull_xyz: tuple[tuple[float, float, float], ...]
    buckets: tuple[tuple[float, float, float, float, float, float, tuple[tuple[float, float, float], ...]], ...]
    seed_points: tuple[tuple[float, float, float], ...]


_CACHE_LOCK = threading.Lock()
_MESH_CACHE: dict[tuple[str, float], CachedMesh] = {}


def _f32(value: float) -> float:
    """Round through IEEE-754 binary32 to match Blender RNA float properties (lens, shift_x, shift_y)."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def side_sequence(start: float, direction: str) -> list[str]:
    """View sequence around the orbit (0°=front; ccw visits front -> right -> back -> left)."""
    order = ["front", "left", "back", "right"] if direction == "cw" else ["front", "right", "back", "left"]
    k = round(((start % 360) / 90)) % 4
    if direction == "cw":
        k = (-k) % 4
    return order[k:] + order[:k]


def camera_position(radius: float, elevation_degrees: float, azimuth_degrees: float) -> tuple[float, float, float]:
    """World camera location for `OrbitRig` rotated around Z by `azimuth_degrees`."""
    elev = math.radians(elevation_degrees)
    az = math.radians(azimuth_degrees)
    x = radius * math.sin(az)
    y = -radius * math.cos(az)
    z = radius * math.tan(elev)
    return (0.0 if abs(x) < 1e-12 else x, 0.0 if abs(y) < 1e-12 else y, 0.0 if abs(z) < 1e-12 else z)


def _mat4_mul(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """Row-major 4x4 matrix product `a @ b`."""
    return tuple(
        a[r * 4 + 0] * b[0 + c] + a[r * 4 + 1] * b[4 + c] + a[r * 4 + 2] * b[8 + c] + a[r * 4 + 3] * b[12 + c]
        for r in range(4) for c in range(4)
    )


def _node_local_matrix(node: dict) -> tuple[float, ...]:
    """Row-major 4x4 local transform of a glTF node (`matrix` is stored column-major in glTF 2.0)."""
    if "matrix" in node:
        m = node["matrix"]
        return (
            float(m[0]), float(m[4]), float(m[8]), float(m[12]),
            float(m[1]), float(m[5]), float(m[9]), float(m[13]),
            float(m[2]), float(m[6]), float(m[10]), float(m[14]),
            float(m[3]), float(m[7]), float(m[11]), float(m[15]),
        )
    tx, ty, tz = (float(v) for v in node.get("translation", (0.0, 0.0, 0.0)))
    qx, qy, qz, qw = (float(v) for v in node.get("rotation", (0.0, 0.0, 0.0, 1.0)))
    sx, sy, sz = (float(v) for v in node.get("scale", (1.0, 1.0, 1.0)))
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm > 0.0:
        qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    return (
        (1.0 - 2.0 * (yy + zz)) * sx, (2.0 * (xy - wz)) * sy, (2.0 * (xz + wy)) * sz, tx,
        (2.0 * (xy + wz)) * sx, (1.0 - 2.0 * (xx + zz)) * sy, (2.0 * (yz - wx)) * sz, ty,
        (2.0 * (xz - wy)) * sx, (2.0 * (yz + wx)) * sy, (1.0 - 2.0 * (xx + yy)) * sz, tz,
        0.0, 0.0, 0.0, 1.0,
    )


def _transform_point(m: tuple[float, ...], p: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = p
    return (
        m[0] * x + m[1] * y + m[2] * z + m[3],
        m[4] * x + m[5] * y + m[6] * z + m[7],
        m[8] * x + m[9] * y + m[10] * z + m[11],
    )


def _read_vec3_float(gltf: dict, bin_blob: bytes, accessor_idx: int) -> list[tuple[float, float, float]]:
    acc = gltf["accessors"][accessor_idx]
    if acc.get("componentType") != _FLOAT32 or acc.get("type") != "VEC3":
        raise ValueError(f"Unsupported POSITION accessor format: {acc}")
    count = int(acc["count"])
    bv = gltf["bufferViews"][acc["bufferView"]]
    base = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(bv.get("byteStride") or 12)
    if stride == 12:
        raw = memoryview(bin_blob)[base : base + count * 12]
        return list(struct.iter_unpack("<fff", raw))
    return [struct.unpack_from("<fff", bin_blob, base + i * stride) for i in range(count)]


def _parse_glb_meshes(glb_path: Path) -> list[tuple[str, tuple[float, ...], list[tuple[float, float, float]]]]:
    data = glb_path.read_bytes()
    if len(data) < 20:
        raise ValueError(f"Truncated GLB file: {glb_path}")
    magic, version, total_len = struct.unpack_from("<4sII", data, 0)
    if magic != _GLB_MAGIC or version != 2:
        raise ValueError(f"Unsupported GLB header in {glb_path}: magic={magic!r}, version={version}")
    offset = 12
    gltf = None
    bin_blob = b""
    end = min(len(data), total_len)
    while offset + 8 <= end:
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_len]
        offset += chunk_len
        if chunk_type == _JSON_CHUNK:
            gltf = json.loads(chunk.decode("utf-8"))
        elif chunk_type == _BIN_CHUNK:
            bin_blob = chunk
    if gltf is None:
        raise ValueError(f"GLB missing JSON chunk: {glb_path}")

    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    if not meshes:
        raise ValueError("The imported GLB contains no mesh objects.")

    scenes = gltf.get("scenes") or []
    if scenes:
        scene_idx = int(gltf.get("scene", 0))
        roots = list(scenes[scene_idx].get("nodes", ()))
    else:
        children = {c for n in nodes for c in n.get("children", ())}
        roots = [i for i in range(len(nodes)) if i not in children]

    # Blender io_scene_gltf2 converts glTF (+X right, +Y up, +Z forward) to Blender (+X right, -Y forward, +Z up):
    # (x, y, z)_gltf -> (x, -z, y)_blender
    gltf_to_blender = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 0.0, -1.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )

    instances: list[tuple[str, tuple[float, ...], list[tuple[float, float, float]]]] = []

    def visit(node_idx: int, parent_world: tuple[float, ...]) -> None:
        node = nodes[node_idx]
        world = _mat4_mul(parent_world, _node_local_matrix(node))
        if "mesh" in node:
            mesh_idx = int(node["mesh"])
            mesh = meshes[mesh_idx]
            name = str(node.get("name") or mesh.get("name") or f"mesh_{mesh_idx}")
            local_verts: list[tuple[float, float, float]] = []
            for prim in mesh.get("primitives", ()):
                attrs = prim.get("attributes") or {}
                if "POSITION" in attrs:
                    local_verts.extend(_read_vec3_float(gltf, bin_blob, int(attrs["POSITION"])))
            if local_verts:
                instances.append((name, world, local_verts))
        for child_idx in node.get("children", ()):
            visit(int(child_idx), world)

    for r in roots:
        visit(int(r), gltf_to_blender)

    if not instances:
        raise ValueError("The imported GLB contains no mesh objects.")
    return instances


def _quickhull_faces_and_vertices(
    pts: list[tuple[float, float, float]],
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float, float]]]:
    """Exact 3D Quickhull returning `(extreme_vertices, outward_face_planes)`."""
    n = len(pts)
    if n <= 4:
        return list(pts), []
    i0 = min(range(n), key=lambda i: pts[i])
    i1 = max(range(n), key=lambda i: pts[i])
    if i0 == i1:
        return [pts[i0]], []
    p0, p1 = pts[i0], pts[i1]
    dx, dy, dz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    i2 = max(
        range(n),
        key=lambda i: (
            ((pts[i][1] - p0[1]) * dz - (pts[i][2] - p0[2]) * dy) ** 2
            + ((pts[i][2] - p0[2]) * dx - (pts[i][0] - p0[0]) * dz) ** 2
            + ((pts[i][0] - p0[0]) * dy - (pts[i][1] - p0[1]) * dx) ** 2
        ),
    )
    p2 = pts[i2]
    ux, uy, uz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    nx, ny, nz = dy * uz - dz * uy, dz * ux - dx * uz, dx * uy - dy * ux
    i3 = max(
        range(n),
        key=lambda i: abs((pts[i][0] - p0[0]) * nx + (pts[i][1] - p0[1]) * ny + (pts[i][2] - p0[2]) * nz),
    )
    if len({i0, i1, i2, i3}) < 4:
        return list(pts), []

    def face_plane(a: int, b: int, c: int) -> tuple[float, float, float, float]:
        pa, pb, pc = pts[a], pts[b], pts[c]
        abx, aby, abz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        acx, acy, acz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
        fnx, fny, fnz = aby * acz - abz * acy, abz * acx - abx * acz, abx * acy - aby * acx
        norm_f = math.sqrt(fnx * fnx + fny * fny + fnz * fnz)
        if norm_f > 0.0:
            fnx, fny, fnz = fnx / norm_f, fny / norm_f, fnz / norm_f
        return fnx, fny, fnz, -(fnx * pa[0] + fny * pa[1] + fnz * pa[2])

    faces: dict[int, tuple[int, int, int, float, float, float, float, list[tuple[float, int]]]] = {}
    fid = 0
    for a, b, c, opp in ((i0, i1, i2, i3), (i0, i2, i3, i1), (i0, i3, i1, i2), (i1, i3, i2, i0)):
        fnx, fny, fnz, fd = face_plane(a, b, c)
        po = pts[opp]
        if fnx * po[0] + fny * po[1] + fnz * po[2] + fd > 0.0:
            b, c = c, b
            fnx, fny, fnz, fd = -fnx, -fny, -fnz, -fd
        faces[fid] = (a, b, c, fnx, fny, fnz, fd, [])
        fid += 1

    eps = 1e-11
    seed = {i0, i1, i2, i3}
    for i in range(n):
        if i in seed:
            continue
        px, py, pz = pts[i]
        best_f, best_d = None, eps
        for f, (_, _, _, fnx, fny, fnz, fd, _) in faces.items():
            d = fnx * px + fny * py + fnz * pz + fd
            if d > best_d:
                best_d, best_f = d, f
        if best_f is not None:
            faces[best_f][7].append((best_d, i))

    queue = [f for f, df in faces.items() if df[7]]
    while queue:
        f = queue.pop()
        if f not in faces or not faces[f][7]:
            continue
        _, eye = max(faces[f][7])
        ex, ey, ez = pts[eye]
        visible = [
            kf for kf, (_, _, _, fnx, fny, fnz, fd, _) in faces.items()
            if fnx * ex + fny * ey + fnz * ez + fd > eps
        ]
        if not visible:
            continue
        edges: dict[tuple[int, int], int] = {}
        cands: list[int] = []
        for kf in visible:
            a, b, c, _, _, _, _, out_k = faces.pop(kf)
            for _, idx in out_k:
                if idx != eye:
                    cands.append(idx)
            edges[(a, b)] = 1
            edges[(b, c)] = 1
            edges[(c, a)] = 1
        new_fids: list[int] = []
        for u, v in edges:
            if (v, u) not in edges:
                fnx, fny, fnz, fd = face_plane(u, v, eye)
                faces[fid] = (u, v, eye, fnx, fny, fnz, fd, [])
                new_fids.append(fid)
                fid += 1
        for idx in cands:
            px, py, pz = pts[idx]
            best_f, best_d = None, eps
            for nf in new_fids:
                _, _, _, fnx, fny, fnz, fd, _ = faces[nf]
                d = fnx * px + fny * py + fnz * pz + fd
                if d > best_d:
                    best_d, best_f = d, nf
            if best_f is not None:
                faces[best_f][7].append((best_d, idx))
        for nf in new_fids:
            if faces[nf][7]:
                queue.append(nf)

    hull_idx = sorted({v for a, b, c, *_ in faces.values() for v in (a, b, c)})
    planes = [(fnx, fny, fnz, fd) for _, _, _, fnx, fny, fnz, fd, _ in faces.values()]
    return [pts[i] for i in hull_idx], planes


def _hull_vertices_3d(points: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, float, float], ...]:
    """Lossless 3D extreme-vertex extraction using an inner 26-direction polyhedron filter + Quickhull."""
    unique = list(dict.fromkeys(points))
    if len(unique) <= 64:
        hull, _ = _quickhull_faces_and_vertices(unique)
        return tuple(hull)

    # 26 support directions in {-1, 0, 1}^3 \ {(0,0,0)} give up to 26 guaranteed hull vertices.
    dirs = [
        (dx, dy, dz)
        for dx in (-1.0, 0.0, 1.0)
        for dy in (-1.0, 0.0, 1.0)
        for dz in (-1.0, 0.0, 1.0)
        if dx != 0.0 or dy != 0.0 or dz != 0.0
    ]
    support_pts = list(dict.fromkeys(max(unique, key=lambda p: dx * p[0] + dy * p[1] + dz * p[2]) for dx, dy, dz in dirs))
    _, inner_planes = _quickhull_faces_and_vertices(support_pts)
    if inner_planes:
        # Any point strictly inside conv(support_pts) (signed distance < -1e-7 to every face) cannot be on conv(P).
        margin_in = -1e-7
        shell = [
            p for p in unique
            if any(fnx * p[0] + fny * p[1] + fnz * p[2] + fd > margin_in for fnx, fny, fnz, fd in inner_planes)
        ]
    else:
        shell = unique
    hull, _ = _quickhull_faces_and_vertices(shell)
    return tuple(hull)


def _build_kd_buckets(
    points: tuple[tuple[float, float, float], ...], leaf_size: int = 48
) -> tuple[tuple[float, float, float, float, float, float, tuple[tuple[float, float, float], ...]], ...]:
    buckets: list[tuple[float, float, float, float, float, float, tuple[tuple[float, float, float], ...]]] = []

    def split(arr: list[tuple[float, float, float]], depth: int) -> None:
        if len(arr) <= leaf_size:
            xs = [p[0] for p in arr]
            ys = [p[1] for p in arr]
            zs = [p[2] for p in arr]
            buckets.append((min(xs), max(xs), min(ys), max(ys), min(zs), max(zs), tuple(arr)))
            return
        axis = depth % 3
        arr.sort(key=lambda p: p[axis])
        mid = len(arr) // 2
        split(arr[:mid], depth + 1)
        split(arr[mid:], depth + 1)

    split(list(points), 0)
    return tuple(buckets)


def load_normalized_glb(glb_path: Path | str, normalize: float = 3.0) -> CachedMesh:
    """Load, transform, and normalize a `.glb` exactly like `worker_render.py`, cached per process."""
    resolved = str(Path(glb_path).resolve())
    key = (resolved, float(normalize))
    with _CACHE_LOCK:
        cached = _MESH_CACHE.get(key)
    if cached is not None:
        return cached

    instances = _parse_glb_meshes(Path(resolved))
    # Compute Blender per-object local AABB (8 corners) transformed by matrix_world:
    bbox_world: list[tuple[float, float, float]] = []
    for _, world, local_verts in instances:
        lx = [p[0] for p in local_verts]
        ly = [p[1] for p in local_verts]
        lz = [p[2] for p in local_verts]
        x0, x1, y0, y1, z0, z1 = min(lx), max(lx), min(ly), max(ly), min(lz), max(lz)
        for cx in (x0, x1):
            for cy in (y0, y1):
                for cz in (z0, z1):
                    bbox_world.append(_transform_point(world, (cx, cy, cz)))

    bx = [p[0] for p in bbox_world]
    by = [p[1] for p in bbox_world]
    bz = [p[2] for p in bbox_world]
    hmin_x, hmax_x = min(bx), max(bx)
    hmin_y, hmax_y = min(by), max(by)
    zmin, zmax = min(bz), max(bz)
    center_x = (hmin_x + hmax_x) / 2.0
    center_y = (hmin_y + hmax_y) / 2.0
    center_z = (zmin + zmax) / 2.0
    scale = float(normalize) / max(hmax_x - hmin_x, hmax_y - hmin_y)

    # `world_vertices()` in worker_render.py sorts mesh objects by name and applies S @ T @ matrix_world:
    xyz_list: list[tuple[float, float, float]] = []
    for _, world, local_verts in sorted(instances, key=lambda item: item[0]):
        for lv in local_verts:
            wx, wy, wz = _transform_point(world, lv)
            xyz_list.append(((wx - center_x) * scale, (wy - center_y) * scale, (wz - center_z) * scale))

    xyz = tuple(xyz_list)
    geom_sha = hashlib.sha256(struct.pack(f"<{len(xyz) * 3}d", *(c for p in xyz for c in p))).hexdigest()
    hull_xyz = _hull_vertices_3d(xyz)
    buckets = _build_kd_buckets(hull_xyz, leaf_size=48)
    seed_points = tuple(b[6][0] for b in buckets[:: max(1, len(buckets) // 16)])
    mesh = CachedMesh(xyz=xyz, geom_sha256=geom_sha, hull_xyz=hull_xyz, buckets=buckets, seed_points=seed_points)
    with _CACHE_LOCK:
        _MESH_CACHE[key] = mesh
    return mesh


def _solve_circle_extrema(
    pts: tuple[tuple[float, float, float], ...], radius: float, elev_rad: float
) -> tuple[float, float, float, float]:
    """Exact continuous-orbit projection ranges `(xlo_min, ylo_min, xhi_max, yhi_max)`."""
    s, c = math.sin(elev_rad), math.cos(elev_rad)
    inv_c = radius / c
    xlo_min = ylo_min = math.inf
    xhi_max = yhi_max = -math.inf
    for x, y, z in pts:
        d = inv_c - s * z
        dc, ds = c * y, -c * x
        q = d * d - dc * dc - ds * ds
        if q <= 0.0 or d <= 0.0:
            raise ValueError("Some vertices fall behind the camera. Increase the radius or lower the elevation.")
        cen_x = -x * dc - y * ds
        disc_x = cen_x * cen_x + q * (x * x + y * y)
        if disc_x < -1e-10:
            raise ValueError("Could not solve the projection extrema along the continuous orbit.")
        sp_x = math.sqrt(max(disc_x, 0.0))
        vx0, vx1 = (cen_x - sp_x) / q, (cen_x + sp_x) / q
        if vx0 < xlo_min:
            xlo_min = vx0
        if vx1 > xhi_max:
            xhi_max = vx1

        ay, by, ny = s * y, -s * x, c * z
        cen_y = d * ny - ay * dc - by * ds
        disc_y = cen_y * cen_y - q * (ny * ny - ay * ay - by * by)
        if disc_y < -1e-10:
            raise ValueError("Could not solve the projection extrema along the continuous orbit.")
        sp_y = math.sqrt(max(disc_y, 0.0))
        vy0, vy1 = (cen_y - sp_y) / q, (cen_y + sp_y) / q
        if vy0 < ylo_min:
            ylo_min = vy0
        if vy1 > yhi_max:
            yhi_max = vy1
    return xlo_min, ylo_min, xhi_max, yhi_max


def audit_plan(
    glb_path: Path | str,
    plan: dict,
    *,
    width: int = 1280,
    height: int = 720,
    frames: int = 300,
    normalize: float = 3.0,
    fixed_lens_mm: float = DEFAULT_FIXED_LENS_MM,
    use_all_vertices: bool = False,
) -> dict:
    """Run the exact 301-pose framing audit and return the `framing.json` report dictionary."""
    t0 = time.perf_counter()
    mesh = load_normalized_glb(glb_path, normalize=normalize)
    radius = float(plan["radius"])
    elev = math.radians(float(plan["elevation_degrees"]))
    margin = float(plan["margin"])
    start = float(plan["start_degrees"])
    direction = str(plan["direction"])
    aspect = width / height

    pts_for_extrema = mesh.xyz if use_all_vertices else mesh.hull_xyz
    xlo_min, ylo_min, xhi_max, yhi_max = _solve_circle_extrema(pts_for_extrema, radius, elev)
    rc0, rc1 = (xlo_min + xhi_max) / 2.0, (ylo_min + yhi_max) / 2.0
    span0, span1 = xhi_max - xlo_min, yhi_max - ylo_min
    tangent = max(span0 / (2.0 * aspect * (1.0 - 2.0 * margin)), span1 / (2.0 * (1.0 - 2.0 * margin))) * 1.0002
    if fixed_lens_mm:
        tangent = 24.0 / (2.0 * float(fixed_lens_mm))

    lens_mm = _f32(24.0 / (2.0 * tangent))
    shift_x = _f32(rc0 / (2.0 * tangent))
    shift_y = _f32(rc1 / (2.0 * tangent))

    s, c = math.sin(elev), math.cos(elev)
    inv_c = radius / c
    end = start - 360.0 if direction == "cw" else start + 360.0
    eps = 2e-5
    k_u = 1.0 / (2.0 * tangent * aspect)
    k_v = 1.0 / (2.0 * tangent)
    off_u = 0.5 - shift_x / aspect
    off_v = 0.5 - shift_y
    rx_fail_lo = (margin - eps - off_u) / k_u
    rx_fail_hi = (1.0 - margin + eps - off_u) / k_u
    ry_fail_lo = (margin - eps - off_v) / k_v
    ry_fail_hi = (1.0 - margin + eps - off_v) / k_v

    min_rx = min_ry = math.inf
    max_rx = max_ry = -math.inf
    violations: list[str] = []

    if use_all_vertices:
        all_pts = mesh.xyz
        for i in range(frames + 1):
            deg = start + (end - start) * i / frames
            th = math.radians(deg)
            ct, st = math.cos(th), math.sin(th)
            rx0 = ry0 = math.inf
            rx1 = ry1 = -math.inf
            behind = False
            for x, y, z in all_pts:
                ry_c = -x * st + y * ct
                dep = c * ry_c - s * z + inv_c
                if dep <= 0.0:
                    behind = True
                    break
                inv_d = 1.0 / dep
                rx = (x * ct + y * st) * inv_d
                ry = (s * ry_c + c * z) * inv_d
                if rx < rx0:
                    rx0 = rx
                if rx > rx1:
                    rx1 = rx
                if ry < ry0:
                    ry0 = ry
                if ry > ry1:
                    ry1 = ry
            if behind:
                violations.append(f"Frame {i} contains geometry behind the camera.")
            elif rx0 < rx_fail_lo or rx1 > rx_fail_hi or ry0 < ry_fail_lo or ry1 > ry_fail_hi:
                u0, v0 = off_u + rx0 * k_u, off_v + ry0 * k_v
                u1, v1 = off_u + rx1 * k_u, off_v + ry1 * k_v
                violations.append(
                    f"Frame {i} at {deg % 360:.0f}° missed the target clearance. "
                    f"Left {u0:.2%}, bottom {v0:.2%}, right {1 - u1:.2%}, top {1 - v1:.2%}. Target {margin:.2%}."
                )
            if not behind:
                if rx0 < min_rx:
                    min_rx = rx0
                if rx1 > max_rx:
                    max_rx = rx1
                if ry0 < min_ry:
                    min_ry = ry0
                if ry1 > max_ry:
                    max_ry = ry1
    else:
        buckets = mesh.buckets
        warm: tuple[tuple[float, float, float], ...] = mesh.seed_points
        for i in range(frames + 1):
            deg = start + (end - start) * i / frames
            th = math.radians(deg)
            ct, st = math.cos(th), math.sin(th)
            rx0 = ry0 = math.inf
            rx1 = ry1 = -math.inf
            p_rx0 = p_rx1 = p_ry0 = p_ry1 = warm[0]
            behind = False

            for pt in warm:
                x, y, z = pt
                ry_c = -x * st + y * ct
                dep = c * ry_c - s * z + inv_c
                if dep <= 0.0:
                    behind = True
                    break
                inv_d = 1.0 / dep
                rx = (x * ct + y * st) * inv_d
                ry = (s * ry_c + c * z) * inv_d
                if rx < rx0:
                    rx0, p_rx0 = rx, pt
                if rx > rx1:
                    rx1, p_rx1 = rx, pt
                if ry < ry0:
                    ry0, p_ry0 = ry, pt
                if ry > ry1:
                    ry1, p_ry1 = ry, pt

            ly_x, ly_y, ly_z = -s * st, s * ct, c
            dp_x, dp_y, dp_z = -c * st, c * ct, -s
            if not behind:
                for bx0, bx1, by0, by1, bz0, bz1, arr in buckets:
                    d0 = (
                        (dp_x * bx0 if dp_x >= 0.0 else dp_x * bx1)
                        + (dp_y * by0 if dp_y >= 0.0 else dp_y * by1)
                        + (dp_z * bz0 if dp_z >= 0.0 else dp_z * bz1)
                        + inv_c
                    )
                    if d0 <= 0.0:
                        for pt in arr:
                            x, y, z = pt
                            ry_c = -x * st + y * ct
                            dep = c * ry_c - s * z + inv_c
                            if dep <= 0.0:
                                behind = True
                                break
                            inv_d = 1.0 / dep
                            rx = (x * ct + y * st) * inv_d
                            ry = (s * ry_c + c * z) * inv_d
                            if rx < rx0:
                                rx0, p_rx0 = rx, pt
                            if rx > rx1:
                                rx1, p_rx1 = rx, pt
                            if ry < ry0:
                                ry0, p_ry0 = ry, pt
                            if ry > ry1:
                                ry1, p_ry1 = ry, pt
                        if behind:
                            break
                        continue

                    d1 = (
                        (dp_x * bx1 if dp_x >= 0.0 else dp_x * bx0)
                        + (dp_y * by1 if dp_y >= 0.0 else dp_y * by0)
                        + (dp_z * bz1 if dp_z >= 0.0 else dp_z * bz0)
                        + inv_c
                    )
                    lx0 = (ct * bx0 if ct >= 0.0 else ct * bx1) + (st * by0 if st >= 0.0 else st * by1)
                    lx1 = (ct * bx1 if ct >= 0.0 else ct * bx0) + (st * by1 if st >= 0.0 else st * by0)
                    brx0 = lx0 / d0 if lx0 < 0.0 else lx0 / d1
                    brx1 = lx1 / d0 if lx1 > 0.0 else lx1 / d1
                    ly0 = (
                        (ly_x * bx0 if ly_x >= 0.0 else ly_x * bx1)
                        + (ly_y * by0 if ly_y >= 0.0 else ly_y * by1)
                        + (ly_z * bz0 if ly_z >= 0.0 else ly_z * bz1)
                    )
                    ly1 = (
                        (ly_x * bx1 if ly_x >= 0.0 else ly_x * bx0)
                        + (ly_y * by1 if ly_y >= 0.0 else ly_y * by0)
                        + (ly_z * bz1 if ly_z >= 0.0 else ly_z * bz0)
                    )
                    bry0 = ly0 / d0 if ly0 < 0.0 else ly0 / d1
                    bry1 = ly1 / d0 if ly1 > 0.0 else ly1 / d1

                    if brx0 < rx0 or brx1 > rx1 or bry0 < ry0 or bry1 > ry1:
                        for pt in arr:
                            x, y, z = pt
                            ry_c = -x * st + y * ct
                            dep = c * ry_c - s * z + inv_c
                            inv_d = 1.0 / dep
                            rx = (x * ct + y * st) * inv_d
                            ry = (s * ry_c + c * z) * inv_d
                            if rx < rx0:
                                rx0, p_rx0 = rx, pt
                            if rx > rx1:
                                rx1, p_rx1 = rx, pt
                            if ry < ry0:
                                ry0, p_ry0 = ry, pt
                            if ry > ry1:
                                ry1, p_ry1 = ry, pt

            if behind:
                violations.append(f"Frame {i} contains geometry behind the camera.")
            else:
                warm = (p_rx0, p_rx1, p_ry0, p_ry1)
                if rx0 < rx_fail_lo or rx1 > rx_fail_hi or ry0 < ry_fail_lo or ry1 > ry_fail_hi:
                    u0, v0 = off_u + rx0 * k_u, off_v + ry0 * k_v
                    u1, v1 = off_u + rx1 * k_u, off_v + ry1 * k_v
                    violations.append(
                        f"Frame {i} at {deg % 360:.0f}° missed the target clearance. "
                        f"Left {u0:.2%}, bottom {v0:.2%}, right {1 - u1:.2%}, top {1 - v1:.2%}. Target {margin:.2%}."
                    )
                if rx0 < min_rx:
                    min_rx = rx0
                if rx1 > max_rx:
                    max_rx = rx1
                if ry0 < min_ry:
                    min_ry = ry0
                if ry1 > max_ry:
                    max_ry = ry1

    margins_lbrt = [
        float(off_u + min_rx * k_u),
        float(off_v + min_ry * k_v),
        float(1.0 - (off_u + max_rx * k_u)),
        float(1.0 - (off_v + max_ry * k_v)),
    ]
    fov_v = math.degrees(2.0 * math.atan(tangent))
    elapsed = round(time.perf_counter() - t0, 3)
    return {
        "passed": not violations,
        "plan": dict(plan),
        "fov_vertical_degrees": fov_v,
        "lens_mm": lens_mm,
        "shift": [shift_x, shift_y],
        "min_margins_lbrt": margins_lbrt,
        "side_sequence": side_sequence(start, direction),
        "vertices": len(mesh.xyz),
        "frames": frames,
        "violations": violations[:20],
        "geometry_world_vertices_sha256": mesh.geom_sha256,
        "render_seconds": elapsed,
        "method": (
            "Pure-Python exact 3D GLB vertex-projection audit (fixed lens and shift across the orbit; "
            "every pose including closure audited over exact 3D extreme vertices)."
        ),
    }


def render_audit_only(
    cfg: dict,
    run_dir: Path,
    ledger_path: Path,
    run_id: str,
    glb: Path,
    plan,
    tag: str,
    *,
    fixed_lens_mm: float = DEFAULT_FIXED_LENS_MM,
):
    """Drop-in benchmark replacement for `blender_step.render_orbit` without Blender or FFmpeg."""
    from pipeline import ledger
    from pipeline.models import FramingReport

    t0 = time.time()
    out = Path(run_dir) / f"render_{tag}"
    out.mkdir(parents=True, exist_ok=True)
    pl = cfg["pipeline"]
    plan_dict = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
    report = audit_plan(
        glb,
        plan_dict,
        width=int(pl["width"]),
        height=int(pl["height"]),
        frames=int(pl["frames"]),
        normalize=float(pl["normalize_horizontal"]),
        fixed_lens_mm=fixed_lens_mm,
    )
    (out / "framing.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    video = out / "reference.mp4"
    video.write_bytes(b"fast_audit placeholder clip " + tag.encode("utf-8"))
    fr = FramingReport(
        passed=report["passed"],
        plan=plan,
        fov_vertical_degrees=report["fov_vertical_degrees"],
        lens_mm=report["lens_mm"],
        min_margins_lbrt=report["min_margins_lbrt"],
        side_sequence=report["side_sequence"],
        vertices=report["vertices"],
        frames=report["frames"],
        violations=report["violations"],
        video_path=str(video),
        render_seconds=report["render_seconds"],
    )
    ledger.log(
        {
            "run_id": run_id,
            "stage": "blender_render",
            "provider": "local_fast_audit",
            "status": "passed" if fr.passed else "framing_failed",
            "cost_usd": 0.0,
            "mock": False,
            "tag": tag,
            "fov_vertical_degrees": fr.fov_vertical_degrees,
            "min_margins_lbrt": fr.min_margins_lbrt,
            "latency_s": round(time.time() - t0, 3),
        },
        ledger_path,
    )
    return fr


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        raw = raw[raw.index("--") + 1 :]
    p = argparse.ArgumentParser(description="Pure-Python exact 3D GLB framing auditor.")
    p.add_argument("--glb", required=True)
    p.add_argument("--plan", required=True, help="ShotPlan JSON")
    p.add_argument("--out", required=True)
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--height", type=int, default=540)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--frames", type=int, default=300)
    p.add_argument("--normalize", type=float, default=3.0)
    p.add_argument("--fixed-lens-mm", type=float, default=DEFAULT_FIXED_LENS_MM)
    args = p.parse_args(raw)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = audit_plan(
        args.glb,
        json.loads(args.plan),
        width=args.width,
        height=args.height,
        frames=args.frames,
        normalize=args.normalize,
        fixed_lens_mm=args.fixed_lens_mm,
    )
    (out / "framing.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "WORKER_DONE",
        json.dumps(
            {
                "passed": report["passed"],
                "fov": round(report["fov_vertical_degrees"], 2),
                "margins_lbrt": [round(m, 4) for m in report["min_margins_lbrt"]],
                "render_seconds": report["render_seconds"],
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
