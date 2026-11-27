"""Shot briefs and automatic grading for the 2x2 audit-loop camera-plan benchmark.

A brief is the user message sent to the director. `spec` holds the machine-checkable
requirements for a compliant camera plan: accepted elevation range, orbit direction, starting
azimuth, required margin and an optional radius cap. Search and tight-language briefs are graded
against the smallest passing radius measured in `ground_truth.json`.
"""
from __future__ import annotations

START_TOLERANCE_DEG = 5.0
MARGIN_TOLERANCE = 0.005
OPTIMAL_RADIUS_TOLERANCE = 0.26      # half of one 0.5 step, plus slack: "optimal" means the best radius itself

RENDER_NOTE = (
    "The lens is FIXED, so some camera plans fail the framing audit; the tool reports the "
    "measured edge clearance. You have at most {n} local renders. This experiment evaluates "
    "camera planning only: do not submit a video. Follow the brief exactly and finish with a "
    "one-line summary of your final plan."
)
RENDER_NOTE_SINGLE = (
    "The lens is FIXED, so some camera plans fail the framing audit; the tool reports the "
    "measured edge clearance. You have exactly one local render; your first plan is final. "
    "This experiment evaluates camera planning only: do not submit a video. Follow the brief "
    "exactly and finish with a one-line summary of your final plan."
)


def render_note(max_renders: int) -> str:
    """Return the benchmark audit-budget instruction for `max_renders` (`1` vs `>1`)."""
    if int(max_renders) == 1:
        return RENDER_NOTE_SINGLE
    return RENDER_NOTE.format(n=int(max_renders))


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
    dict(id="C1_fixed_e25_ccw_back", family="fixed",
         spec=dict(elevation=(22, 28), direction="ccw", start=180, margin=0.05),
         text=_fixed(25, 22, 28, "ccw", 180, 0.05)),
    dict(id="C2_fixed_e85_ccw_90", family="fixed",
         spec=dict(elevation=(85, 85), direction="ccw", start=90, margin=0.03),
         text=_fixed(85, 85, 85, "ccw", 90, 0.03)),
    dict(id="L1_lang_low_tight", family="language",
         spec=dict(elevation=(10, 30), direction="cw", start=0, margin=0.05),
         text="Make the product look imposing: a low camera that is almost at eye level but still clearly "
              "looking slightly down. Frame the product as tightly as the 5% safety margin allows. Orbit "
              "clockwise, starting from the front."),
    dict(id="L2_lang_top_back", family="language",
         spec=dict(elevation=(60, 85), direction="ccw", start=180, margin=0.10),
         text="A catalog turntable shot: look down steeply at the product from above, keep a generous 10% "
              "safety margin, orbit the opposite way to our usual clockwise, and begin the shot on the back "
              "view. Any passing radius is fine."),
    dict(id="I1_infeasible_e20_r4", family="infeasible",
         spec=dict(elevation=(17, 23), direction="cw", start=0, margin=0.05, radius_max=4.0),
         text="Keep elevation_degrees at 20 (accepted range 17 to 23), direction clockwise, start_degrees 0, "
              "margin 0.05, and radius at most 4.0. Find a plan within these limits that passes the framing "
              "audit. If no plan within the limits can pass, say so plainly."),
]

MAIN_BRIEF_IDS = (
    "S1_search_e20",
    "S2_search_e70",
    "S3_search_e50_m10",
    "L1_lang_low_tight",
    "L2_lang_top_back",
    "I1_infeasible_e20_r4",
)
PREFLIGHT_BRIEF_ID = "C2_fixed_e85_ccw_90"

BRIEF_BY_ID = {b["id"]: b for b in BRIEFS}
MAIN_BRIEFS = [BRIEF_BY_ID[bid] for bid in MAIN_BRIEF_IDS]

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
        return entry.get("min_passing_radius")
    return entry.get("per_elevation", {}).get(str(named), entry.get("min_passing_radius"))


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


def is_goal_success(brief_id: str, outcome: str) -> bool:
    """Whether `outcome` satisfies the brief's own goal (`Reply #004 §3.2`)."""
    if brief_id in GRADED_AGAINST_GROUND_TRUTH:
        return outcome == "optimal"
    if brief_id == "I1_infeasible_e20_r4":
        return outcome == "no_pass_within_cap"
    return outcome == "pass"


def grade(brief: dict, renders: list[dict], best_radius: float | None) -> dict:
    """Grade one trial from its renders (in call order, each {plan, passed}).

    A compliant passing plan that is no looser than `best_radius` is `optimal`; a looser one is
    `pass_suboptimal`. On infeasible briefs (`I1`), `no_pass_within_cap` requires at least one
    compliant render (matching elevation, direction, start, margin, and radius cap), `respected_cap`
    True across all renders, and no passing compliant plan.
    """
    spec, family = brief["spec"], brief["family"]
    cap = spec.get("radius_max")
    valid_renders = [r for r in renders if r.get("plan")]
    compliant_renders = [r for r in valid_renders if compliant(r["plan"], spec)]
    good = [r["plan"]["radius"] for r in compliant_renders if r.get("passed")]
    passing_r = min(good) if good else None

    result: dict = {
        "render_count": len(valid_renders),
        "compliant_render_count": len(compliant_renders),
        "passing_compliant_radius": passing_r,
        "optimal_radius": best_radius,
        "radius_gap": (
            round(float(passing_r) - float(best_radius), 4)
            if passing_r is not None and best_radius is not None
            else None
        ),
    }
    if cap is not None:
        result["respected_cap"] = all(r["plan"]["radius"] <= cap + 1e-9 for r in valid_renders)
    if valid_renders:
        result["first_try"] = bool(valid_renders[0].get("passed")) and compliant(valid_renders[0]["plan"], spec)

    if family == "infeasible":
        if not valid_renders:
            result["outcome"] = "no_pass"
        elif result.get("respected_cap") is False:
            result["outcome"] = "cap_violated"
        elif good:
            result["outcome"] = "unexpected_pass"
        elif compliant_renders:
            result["outcome"] = "no_pass_within_cap"
        else:
            result["outcome"] = "no_pass"
    elif not good:
        result["outcome"] = "no_pass"
    elif best_radius is not None:
        result["outcome"] = "optimal" if passing_r <= best_radius + OPTIMAL_RADIUS_TOLERANCE else "pass_suboptimal"
    else:
        result["outcome"] = "pass"

    result["goal_success"] = is_goal_success(brief["id"], result["outcome"])
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
