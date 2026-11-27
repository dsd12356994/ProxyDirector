"""Part A local deterministic sweep (`Reply #004 §3.1`, zero API calls).

Sweeps 16 elevations (`10, 15, ..., 85` degrees) x 19 radii (`3.0, 3.5, ..., 12.0`) = 304 camera
plans on the frozen mouse mesh (`assets/seed_tripo/model.glb`) at 5% margin under both:
  1. Production fitted lens (`fixed_lens_mm=0.0` -> 304/304 pass; baseline 50 deg / r=6 fits 68.17216 mm)
  2. Fixed 68.17215728759766 mm lens (`~223/304` pass; r=6 passes at >=50 deg = 8/16 elevations,
     tightest at only 2/16)

Also computes exact ground-truth smallest passing radii for the 6 main benchmark briefs plus
pre-flight brief `C2_fixed_e85_ccw_90`, and writes:
  - `data/experiments/audit_loop/geometry_map.json`
  - `data/experiments/audit_loop/ground_truth.json`
  - `data/experiments/audit_loop/min_radius_by_elevation.svg`
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (str(REPO), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import fast_audit  # noqa: E402
from briefs import BRIEF_BY_ID, MAIN_BRIEF_IDS, NOMINAL_ELEVATION, PREFLIGHT_BRIEF_ID  # noqa: E402
from make_fixed_lens_worker import FIXED_LENS_MM  # noqa: E402

GLB = REPO / "assets" / "seed_tripo" / "model.glb"
DEFAULT_OUT = REPO / "data" / "experiments" / "audit_loop"

ELEVATIONS_16 = list(range(10, 86, 5))                      # 10, 15, ..., 85 (16 values)
RADII_19 = [round(3.0 + 0.5 * i, 1) for i in range(19)]     # 3.0, 3.5, ..., 12.0 (19 values)


def run_grid_sweep() -> dict:
    """Evaluate all 16 x 19 = 304 plans under both fitted lens and fixed 68.17 mm lens."""
    mesh = fast_audit.load_normalized_glb(GLB, normalize=3.0)
    fitted_pass = 0
    fixed_pass = 0
    baseline_fitted_lens_mm = None

    per_elevation_summary: dict[str, dict] = {}
    cells_fixed: list[dict] = []
    monotone_all = True

    for elev in ELEVATIONS_16:
        elev_row_passed: list[bool] = []
        elev_row_margins: list[float] = []
        passing_radii: list[float] = []
        r6_report = None
        tightest_report = None

        for r in RADII_19:
            plan = {
                "elevation_degrees": float(elev),
                "radius": float(r),
                "start_degrees": 0.0,
                "direction": "cw",
                "margin": 0.05,
            }
            rep_fitted = fast_audit.audit_plan(GLB, plan, width=1280, height=720, frames=300, fixed_lens_mm=0.0)
            if rep_fitted["passed"]:
                fitted_pass += 1
            if elev == 50 and abs(r - 6.0) < 1e-9:
                baseline_fitted_lens_mm = rep_fitted["lens_mm"]

            rep_fixed = fast_audit.audit_plan(
                GLB, plan, width=1280, height=720, frames=300, fixed_lens_mm=FIXED_LENS_MM
            )
            passed = bool(rep_fixed["passed"])
            min_margin = round(min(rep_fixed["min_margins_lbrt"]), 6)
            if passed:
                fixed_pass += 1
                passing_radii.append(r)
                if tightest_report is None:
                    tightest_report = rep_fixed
            if abs(r - 6.0) < 1e-9:
                r6_report = rep_fixed

            elev_row_passed.append(passed)
            elev_row_margins.append(min_margin)
            cells_fixed.append({
                "elevation_degrees": elev,
                "radius": r,
                "passed": passed,
                "min_margin": min_margin,
                "fitted_passed": bool(rep_fitted["passed"]),
                "fitted_lens_mm": round(float(rep_fitted["lens_mm"]), 5),
            })

        # Monotonicity: once True, never False at larger radius; min_margin strictly non-decreasing
        seen_pass = False
        elev_monotone = True
        for idx, p in enumerate(elev_row_passed):
            if p:
                seen_pass = True
            elif seen_pass:
                elev_monotone = False
            if idx > 0 and elev_row_margins[idx] + 1e-9 < elev_row_margins[idx - 1]:
                elev_monotone = False
        monotone_all = monotone_all and elev_monotone

        min_r = min(passing_radii) if passing_radii else None
        r6_min_margin = round(min(r6_report["min_margins_lbrt"]), 6) if r6_report else None
        r6_passed = bool(r6_report["passed"]) if r6_report else False
        per_elevation_summary[str(elev)] = {
            "elevation_degrees": elev,
            "min_passing_radius": min_r,
            "passing_count_of_19": len(passing_radii),
            "r6_passed": r6_passed,
            "r6_min_margin": r6_min_margin,
            "r6_is_tightest": (min_r == 6.0),
            "tightest_min_margin": (
                round(min(tightest_report["min_margins_lbrt"]), 6) if tightest_report else None
            ),
            "monotone_in_radius": elev_monotone,
        }

    total_plans = len(ELEVATIONS_16) * len(RADII_19)
    r6_pass_elevs = [e for e in ELEVATIONS_16 if per_elevation_summary[str(e)]["r6_passed"]]
    r6_tight_elevs = [e for e in ELEVATIONS_16 if per_elevation_summary[str(e)]["r6_is_tightest"]]

    return {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "glb_path": "assets/seed_tripo/model.glb",
        "geometry_world_vertices_sha256": mesh.geom_sha256,
        "vertices": len(mesh.xyz),
        "hull_vertices": len(mesh.hull_xyz),
        "fixed_lens_mm": FIXED_LENS_MM,
        "baseline_50deg_r6_fitted_lens_mm": baseline_fitted_lens_mm,
        "grid": {
            "elevations_degrees": ELEVATIONS_16,
            "radii": RADII_19,
            "margin": 0.05,
            "direction": "cw",
            "start_degrees": 0.0,
            "total_plans": total_plans,
        },
        "summary": {
            "fitted_lens_passed": fitted_pass,
            "fitted_lens_total": total_plans,
            "fixed_lens_passed": fixed_pass,
            "fixed_lens_failed": total_plans - fixed_pass,
            "fixed_lens_total": total_plans,
            "r6_passing_elevations": r6_pass_elevs,
            "r6_passing_elevation_count": len(r6_pass_elevs),
            "r6_tightest_elevations": r6_tight_elevs,
            "r6_tightest_elevation_count": len(r6_tight_elevs),
            "monotone_in_radius_all_16_elevations": monotone_all,
        },
        "per_elevation": per_elevation_summary,
        "cells": cells_fixed,
    }


def compute_ground_truth() -> dict:
    """Measure exact ground truth for the 6 main briefs plus pre-flight `C2_fixed_e85_ccw_90`."""
    mesh = fast_audit.load_normalized_glb(GLB, normalize=3.0)
    target_ids = (*MAIN_BRIEF_IDS, PREFLIGHT_BRIEF_ID)
    truth: dict[str, dict] = {}

    for brief_id in target_ids:
        brief = BRIEF_BY_ID[brief_id]
        spec = brief["spec"]
        lo, hi = int(spec["elevation"][0]), int(spec["elevation"][1])
        direction = spec["direction"]
        start = float(spec["start"])
        margin = float(spec["margin"])
        cap = spec.get("radius_max")
        allowed_radii = [r for r in RADII_19 if cap is None or r <= cap + 1e-9]

        per_elevation: dict[str, float | None] = {}
        for elev in range(lo, hi + 1):
            passing = []
            for r in allowed_radii:
                plan = {
                    "elevation_degrees": float(elev),
                    "radius": float(r),
                    "start_degrees": start,
                    "direction": direction,
                    "margin": margin,
                }
                rep = fast_audit.audit_plan(
                    GLB, plan, width=1280, height=720, frames=300, fixed_lens_mm=FIXED_LENS_MM
                )
                if rep["passed"]:
                    passing.append(r)
            per_elevation[str(elev)] = min(passing) if passing else None

        non_null = [v for v in per_elevation.values() if v is not None]
        best = min(non_null) if non_null else None

        nominal = NOMINAL_ELEVATION.get(brief_id, lo if lo == hi else (lo + hi) // 2)
        probe_r = min(6.0, cap) if cap is not None else 6.0
        probe_rep = fast_audit.audit_plan(
            GLB,
            {
                "elevation_degrees": float(nominal),
                "radius": float(probe_r),
                "start_degrees": start,
                "direction": direction,
                "margin": margin,
            },
            width=1280,
            height=720,
            frames=300,
            fixed_lens_mm=FIXED_LENS_MM,
        )
        entry: dict = {
            "family": brief["family"],
            "accepted_elevation_range": [lo, hi],
            "nominal_elevation": nominal,
            "direction": direction,
            "start_degrees": start,
            "margin": margin,
            "radius_max": cap,
            "min_passing_radius": best,
            "nominal_optimal_radius": per_elevation.get(str(nominal), best),
            "default_probe_radius": probe_r,
            "default_probe_passed": bool(probe_rep["passed"]),
            "default_probe_min_margin": round(min(probe_rep["min_margins_lbrt"]), 5),
            "per_elevation": per_elevation,
        }
        truth[brief_id] = entry

    camera_x = {
        str(az): round(fast_audit.camera_position(8.0, 30.0, az)[0], 2)
        for az in (0, 90, 180, 270)
    }
    return {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "glb_path": "assets/seed_tripo/model.glb",
        "geometry_world_vertices_sha256": mesh.geom_sha256,
        "fixed_lens_mm": FIXED_LENS_MM,
        "radii_grid": RADII_19,
        "ground_truth": truth,
        "side_order_check": {
            "camera_x_by_azimuth": camera_x,
            "fixed_worker_cw_from_90": fast_audit.side_sequence(90, "cw"),
        },
    }


def build_svg(geometry_map: dict) -> str:
    """Render a crisp publication-quality vector SVG chart of min passing radius vs elevation."""
    W, H = 880, 520
    pad_l, pad_r, pad_t, pad_b = 78, 36, 68, 66
    pw = W - pad_l - pad_r
    ph = H - pad_t - pad_b

    x_min, x_max = 10.0, 85.0
    y_min, y_max = 2.5, 12.0

    def sx(elev: float) -> float:
        return pad_l + (elev - x_min) / (x_max - x_min) * pw

    def sy(radius: float) -> float:
        return pad_t + (y_max - radius) / (y_max - y_min) * ph

    per_elev = geometry_map["per_elevation"]
    pts = [(float(e), float(per_elev[str(e)]["min_passing_radius"])) for e in ELEVATIONS_16]

    # Step-after path (exact discrete 0.5 radius threshold across 5-degree elevation bins) + direct polyline
    step_cmds = [f"M {sx(pts[0][0]):.2f} {sy(pts[0][1]):.2f}"]
    for i in range(len(pts) - 1):
        e0, r0 = pts[i]
        e1, r1 = pts[i + 1]
        emid = (e0 + e1) / 2.0
        step_cmds.append(f"L {sx(emid):.2f} {sy(r0):.2f}")
        step_cmds.append(f"L {sx(emid):.2f} {sy(r1):.2f}")
    step_cmds.append(f"L {sx(pts[-1][0]):.2f} {sy(pts[-1][1]):.2f}")
    step_path = " ".join(step_cmds)

    # Shaded feasible region polygon (from curve up to r = 12.0)
    feasible_cmds = list(step_cmds) + [
        f"L {sx(x_max):.2f} {sy(12.0):.2f}",
        f"L {sx(x_min):.2f} {sy(12.0):.2f}",
        "Z",
    ]
    feasible_path = " ".join(feasible_cmds)

    # Shaded infeasible region polygon (below curve down to r = 3.0)
    infeasible_cmds = list(step_cmds) + [
        f"L {sx(x_max):.2f} {sy(3.0):.2f}",
        f"L {sx(x_min):.2f} {sy(3.0):.2f}",
        "Z",
    ]
    infeasible_path = " ".join(infeasible_cmds)

    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
        "  <style>",
        "    text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; fill: #1e293b; }",
        "    .title { font-size: 16px; font-weight: 700; fill: #0f172a; }",
        "    .subtitle { font-size: 12px; fill: #475569; }",
        "    .axis-label { font-size: 12.5px; font-weight: 600; fill: #1e293b; }",
        "    .tick { font-size: 11px; fill: #475569; }",
        "    .note { font-size: 11px; fill: #0f172a; }",
        "    .note-muted { font-size: 10.5px; fill: #475569; }",
        "    .badge { font-size: 10.5px; font-weight: 600; }",
        "  </style>",
        f'  <rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff" rx="8"/>',
        '  <text x="78" y="30" class="title">Smallest Passing Orbit Radius by Camera Elevation (Fixed 68.17 mm Lens, 5% Margin)</text>',
        (
            '  <text x="78" y="49" class="subtitle">'
            f'Frozen Tripo mouse mesh (49,530 vertices, 301 orbit poses): '
            f'{geometry_map["summary"]["fixed_lens_passed"]}/304 grid plans pass under fixed lens vs. '
            f'{geometry_map["summary"]["fitted_lens_passed"]}/304 under fitted lens'
            "</text>"
        ),
        # Shaded regions
        f'  <path d="{feasible_path}" fill="#ecfdf5" opacity="0.85"/>',
        f'  <path d="{infeasible_path}" fill="#fef2f2" opacity="0.72"/>',
    ]

    # Gridlines & Y ticks (3.0 .. 12.0)
    for i in range(19):
        r = 3.0 + 0.5 * i
        y = sy(r)
        is_major = abs(r - round(r)) < 1e-9
        stroke = "#cbd5e1" if is_major else "#f1f5f9"
        sw = "1" if is_major else "0.8"
        out.append(f'  <line x1="{pad_l}" y1="{y:.2f}" x2="{W - pad_r}" y2="{y:.2f}" stroke="{stroke}" stroke-width="{sw}"/>')
        if is_major:
            out.append(f'  <text x="{pad_l - 10}" y="{y + 4:.2f}" text-anchor="end" class="tick">{r:.1f}</text>')

    # Vertical gridlines & X ticks (10 .. 85)
    for e in ELEVATIONS_16:
        x = sx(float(e))
        out.append(f'  <line x1="{x:.2f}" y1="{pad_t}" x2="{x:.2f}" y2="{H - pad_b}" stroke="#e2e8f0" stroke-width="0.9"/>')
        out.append(f'  <line x1="{x:.2f}" y1="{H - pad_b}" x2="{x:.2f}" y2="{H - pad_b + 5}" stroke="#475569" stroke-width="1.2"/>')
        out.append(f'  <text x="{x:.2f}" y="{H - pad_b + 19}" text-anchor="middle" class="tick">{e}&#176;</text>')

    # Plot border
    out.append(f'  <rect x="{pad_l}" y="{pad_t}" width="{pw}" height="{ph}" fill="none" stroke="#64748b" stroke-width="1.2"/>')

    # Region labels
    out.append(f'  <text x="{sx(26):.1f}" y="{sy(10.6):.1f}" class="badge" fill="#047857">FEASIBLE REGION (passes 5% clearance across all 301 poses)</text>')
    out.append(f'  <text x="{sx(13):.1f}" y="{sy(3.45):.1f}" class="badge" fill="#b91c1c">INFEASIBLE REGION (clips / misses 5% clearance)</text>')

    # Dashed r = 6.0 baseline line
    y6 = sy(6.0)
    out.append(f'  <line x1="{pad_l}" y1="{y6:.2f}" x2="{W - pad_r}" y2="{y6:.2f}" stroke="#d97706" stroke-width="2.2" stroke-dasharray="7,5"/>')
    out.append(
        f'  <rect x="{sx(11.0):.1f}" y="{y6 - 24:.1f}" width="318" height="19" rx="4" fill="#fffbeb" stroke="#f59e0b" stroke-width="0.8"/>'
    )
    out.append(
        f'  <text x="{sx(12.0):.1f}" y="{y6 - 11:.1f}" class="badge" fill="#b45309">'
        "Default radius r = 6.0: fails &lt;50&#176;, passes &#8805;50&#176; (8/16), tightest at 50&#176;&#8211;55&#176;"
        "</text>"
    )

    # Threshold step curve + markers
    out.append(f'  <path d="{step_path}" fill="none" stroke="#0284c7" stroke-width="2.8"/>')
    prev_r = None
    for e, r in pts:
        cx, cy = sx(e), sy(r)
        is_r6_tight = abs(r - 6.0) < 1e-9
        fill_col = "#d97706" if is_r6_tight else "#0284c7"
        out.append(f'  <circle cx="{cx:.2f}" cy="{cy:.2f}" r="4.3" fill="{fill_col}" stroke="#ffffff" stroke-width="1.5"/>')
        if r != prev_r or e in (10.0, 20.0, 50.0, 70.0, 85.0):
            dy = -9 if r >= 5.5 else -10
            out.append(f'  <text x="{cx:.2f}" y="{cy + dy:.2f}" text-anchor="middle" class="note" font-weight="600">{r:.1f}</text>')
        prev_r = r

    # Callout annotations for S1 (20 deg), Baseline (50 deg), S2 (70 deg), I1 (20 deg, r=4.0)
    # I1 marker at (20 deg, r=4.0)
    ix, iy = sx(20.0), sy(4.0)
    out.append(f'  <circle cx="{ix:.2f}" cy="{iy:.2f}" r="4.5" fill="#dc2626" stroke="#ffffff" stroke-width="1.5"/>')
    out.append(f'  <line x1="{ix + 6:.1f}" y1="{iy:.1f}" x2="{sx(24.5):.1f}" y2="{sy(4.35):.1f}" stroke="#dc2626" stroke-width="1.1"/>')
    out.append(
        f'  <text x="{sx(25.0):.1f}" y="{sy(4.42):.1f}" class="note" fill="#991b1b">'
        f'{html.escape("I1 cap (20°, r ≤ 4.0): infeasible (-26% cutoff)")}'
        "</text>"
    )

    # S1 callout at (20 deg, r=6.5)
    s1x, s1y = sx(20.0), sy(6.5)
    out.append(f'  <line x1="{s1x + 5:.1f}" y1="{s1y - 5:.1f}" x2="{sx(25.0):.1f}" y2="{sy(8.1):.1f}" stroke="#0369a1" stroke-width="1.1"/>')
    out.append(
        f'  <text x="{sx(25.5):.1f}" y="{sy(8.2):.1f}" class="note">'
        f'{html.escape("S1 (20°): tightest r = 6.5 (r = 6.0 fails at 1.5%)")}'
        "</text>"
    )

    # Baseline 50 deg callout
    bx, by = sx(50.0), sy(6.0)
    out.append(f'  <line x1="{bx + 5:.1f}" y1="{by - 5:.1f}" x2="{sx(55.5):.1f}" y2="{sy(7.55):.1f}" stroke="#b45309" stroke-width="1.1"/>')
    out.append(
        f'  <text x="{sx(56.0):.1f}" y="{sy(7.65):.1f}" class="note">'
        f'{html.escape("Baseline (50°, r = 6.0): fits 68.17 mm lens")}'
        "</text>"
    )

    # S2 callout at (70 deg, r=4.0)
    s2x, s2y = sx(70.0), sy(4.0)
    out.append(f'  <line x1="{s2x - 4:.1f}" y1="{s2y + 6:.1f}" x2="{sx(61.5):.1f}" y2="{sy(3.15):.1f}" stroke="#0369a1" stroke-width="1.1"/>')
    out.append(
        f'  <text x="{sx(61.0):.1f}" y="{sy(3.1):.1f}" text-anchor="end" class="note">'
        f'{html.escape("S2 (70°): tightest r = 4.0 (r = 6.0 wide at 21.4%)")}'
        "</text>"
    )

    # Axis titles
    out.append(f'  <text x="{pad_l + pw / 2:.1f}" y="{H - 18}" text-anchor="middle" class="axis-label">Camera Elevation (degrees)</text>')
    out.append(
        f'  <text x="24" y="{pad_t + ph / 2:.1f}" text-anchor="middle" class="axis-label" '
        f'transform="rotate(-90 24 {pad_t + ph / 2:.1f})">Orbit Radius (0.5 grid steps, [3.0, 12.0])</text>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Part A 304-plan local sweep and ground-truth generation.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output directory (default: data/experiments/audit_loop)")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    geom_map = run_grid_sweep()
    gt = compute_ground_truth()
    svg_text = build_svg(geom_map)
    elapsed = round(time.perf_counter() - t0, 2)

    (out_dir / "geometry_map.json").write_text(json.dumps(geom_map, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "ground_truth.json").write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "min_radius_by_elevation.svg").write_text(svg_text, encoding="utf-8")

    s = geom_map["summary"]
    print(
        f"Part A sweep finished in {elapsed}s -> {out_dir}\n"
        f"  Fitted lens: {s['fitted_lens_passed']}/{s['fitted_lens_total']} pass "
        f"(50 deg / r=6 lens = {geom_map['baseline_50deg_r6_fitted_lens_mm']:.5f} mm)\n"
        f"  Fixed 68.17 mm lens: {s['fixed_lens_passed']}/{s['fixed_lens_total']} pass; "
        f"r=6 passes at {s['r6_passing_elevation_count']}/16 elevations {s['r6_passing_elevations']} "
        f"and is tightest at {s['r6_tightest_elevation_count']}/16 {s['r6_tightest_elevations']}\n"
        f"  Monotone in radius across all 16 elevations: {s['monotone_in_radius_all_16_elevations']}"
    )
    for bid, entry in gt["ground_truth"].items():
        print(
            f"  {bid:22s}: min_passing_radius={entry['min_passing_radius']} "
            f"(nominal={entry['nominal_optimal_radius']}, r={entry['default_probe_radius']} "
            f"passed={entry['default_probe_passed']} margin={entry['default_probe_min_margin']:.4f})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
