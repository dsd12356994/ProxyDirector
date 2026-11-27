"""Shot briefs and automatic grading for the camera-plan benchmark.

A brief is the user message sent to the director. `spec` holds the machine-checkable
requirements for a compliant camera plan: accepted elevation range, orbit direction, starting
azimuth, required margin and an optional radius cap. Search briefs are also graded against the
smallest passing radius, which ground_truth.py measures with the same audit.
"""
from __future__ import annotations

START_TOLERANCE_DEG = 5.0
MARGIN_TOLERANCE = 0.005
OPTIMAL_RADIUS_TOLERANCE = 0.26      # half of one 0.5 step, plus slack: "optimal" means the best radius itself

RENDER_NOTE = ("The lens is FIXED, so some camera plans fail the framing audit; the tool reports the "
               "measured edge clearance. You have at most {n} local renders. This experiment evaluates "
               "camera planning only: do not submit a video. Follow the brief exactly and finish with a "
               "one-line summary of your final plan.")


def _search(elevation: float, lo: float, hi: float, margin: float) -> str:
    return (f"Keep elevation_degrees at {elevation} (accepted range {lo:g} to {hi:g}), direction clockwise, "
            f"start_degrees 0 and margin {margin:g}. Among the plans that pass the framing audit, use the "
            f"smallest radius you can find, in steps of 0.5.")


def _fixed(elevation: float, lo: float, hi: float, direction: str, start: int, margin: float) -> str:
    level = f"exactly {elevation:g}" if lo == hi else f"{elevation:g} (accepted range {lo:g} to {hi:g})"
    return (f"Use elevation_degrees {level}, direction {'clockwise' if direction == 'cw' else 'counter-clockwise'}, "
            f"start_degrees {start} (this is where the shot begins) and margin {margin:g}. Start with radius 6; "
            f"if that fails the audit, adjust the radius until it passes.")


BRIEFS = [
    dict(id="S1_search_e20", family="search",
         spec=dict(elevation=(17, 23), direction="cw", start=0, margin=0.05),
         text=_search(20, 17, 23, 0.05)),
    dict(id="S2_search_e70", family="search",
         spec=dict(elevation=(67, 73), direction="cw", start=0, margin=0.05),
         text=_search(70, 67, 73, 0.05)),
    dict(id="S3_search_e50_m10", family="search",
         spec=dict(elevation=(47, 53), direction="cw", start=0, margin=0.10),
         text=_search(50, 47, 53, 0.10)),
    dict(id="C1_fixed_e60_ccw_back", family="fixed",
         spec=dict(elevation=(57, 63), direction="ccw", start=180, margin=0.05),
         text=_fixed(60, 57, 63, "ccw", 180, 0.05)),
    dict(id="C2_fixed_e85_ccw_90", family="fixed",
         spec=dict(elevation=(85, 85), direction="ccw", start=90, margin=0.03),
         text=_fixed(85, 85, 85, "ccw", 90, 0.03)),
    dict(id="L1_lang_low_tight", family="language",
         spec=dict(elevation=(10, 30), direction="cw", start=0, margin=0.05),
         text="Make the product look imposing: a low camera that is almost at eye level but still clearly "
              "looking slightly down. Frame the product as tightly as the 5% safety margin allows. Orbit "
              "clockwise, starting from the front."),
    dict(id="L2_lang_top_back", family="language",
         spec=dict(elevation=(70, 85), direction="ccw", start=180, margin=0.10),
         text="A catalog turntable shot: look down steeply at the product from above, keep a generous 10% "
              "safety margin, orbit the opposite way to our usual clockwise, and begin the shot on the back "
              "view. Any passing radius is fine."),
    dict(id="I1_infeasible_e20_r4", family="infeasible",
         spec=dict(elevation=(17, 23), direction="cw", start=0, margin=0.05, radius_max=4.0),
         text="Keep elevation_degrees at 20 (accepted range 17 to 23), direction clockwise, start_degrees 0, "
              "margin 0.05, and radius at most 4.0. Find a plan within these limits that passes the framing "
              "audit. If no plan within the limits can pass, say so plainly."),
]

# Briefs with a tightest-possible target, and the elevation each search brief names. L1 leaves the
# elevation open inside its accepted range.
GRADED_AGAINST_GROUND_TRUTH = ("S1_search_e20", "S2_search_e70", "S3_search_e50_m10", "L1_lang_low_tight")
NOMINAL_ELEVATION = {"S1_search_e20": 20, "S2_search_e70": 70, "S3_search_e50_m10": 50}


def optimal_radius(brief_id: str, entry: dict) -> float | None:
    """Smallest passing radius a director can reach for the brief, from its ground_truth.json record."""
    if not entry:
        return None
    named = NOMINAL_ELEVATION.get(brief_id)
    if named is None:
        return entry["min_passing_radius"]
    return entry["per_elevation"].get(str(named), entry["min_passing_radius"])


def compliant(plan: dict, spec: dict) -> bool:
    """Does a rendered plan satisfy every requirement of the brief?"""
    lo, hi = spec["elevation"]
    start_gap = abs(((plan["start_degrees"] - spec["start"] + 180) % 360) - 180)
    cap = spec.get("radius_max")
    return (lo - 1e-9 <= plan["elevation_degrees"] <= hi + 1e-9
            and plan["direction"] == spec["direction"]
            and start_gap <= START_TOLERANCE_DEG
            and abs(plan["margin"] - spec["margin"]) <= MARGIN_TOLERANCE
            and (cap is None or plan["radius"] <= cap + 1e-9))


def grade(brief: dict, renders: list[dict], best_radius: float | None) -> dict:
    """Grade one trial from its renders (in call order, each {plan, passed}).

    A compliant passing plan that is no looser than `best_radius` is `optimal`; a looser one is
    `pass_suboptimal`.
    """
    spec, family = brief["spec"], brief["family"]
    cap = spec.get("radius_max")
    good = [r["plan"]["radius"] for r in renders if r.get("passed") and compliant(r["plan"], spec)]
    result = {"render_count": len(renders), "passing_compliant_radius": min(good) if good else None}
    if cap is not None:
        result["respected_cap"] = all(r["plan"]["radius"] <= cap + 1e-9 for r in renders)
    if renders:
        result["first_try"] = bool(renders[0].get("passed")) and compliant(renders[0]["plan"], spec)
    if family == "infeasible":
        result["outcome"] = ("no_pass" if not renders
                             else "cap_violated" if result["respected_cap"] is False
                             else "unexpected_pass" if good
                             else "no_pass_within_cap")
    elif not good:
        result["outcome"] = "no_pass"
    elif best_radius is not None:
        result["optimal_radius"] = best_radius
        result["outcome"] = "optimal" if min(good) <= best_radius + OPTIMAL_RADIUS_TOLERANCE else "pass_suboptimal"
    else:
        result["outcome"] = "pass"
    return result


SUCCESS = {
    "search": ("optimal", "pass_suboptimal"),
    "fixed": ("pass",),
    "language": ("optimal", "pass_suboptimal", "pass"),
    "infeasible": ("no_pass_within_cap",),
}
STRICT_SUCCESS = {
    "search": ("optimal",),
    "fixed": ("pass",),
    "language": ("optimal", "pass"),      # L1 counts only its optimal radius; L2 has no tighter target
    "infeasible": ("no_pass_within_cap",),
}
