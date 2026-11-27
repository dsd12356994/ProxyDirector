"""Shared helpers for the test suite. Everything here runs offline."""
from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

REPO = Path(__file__).resolve().parents[1]
BENCHMARK = REPO / "experiments" / "shot_briefs"
for entry in (str(REPO), str(BENCHMARK)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from pipeline import ledger                               # noqa: E402
from pipeline.config import load_config                   # noqa: E402
from pipeline.models import FramingReport, ShotPlan       # noqa: E402
from pipeline.tools import ToolContext                    # noqa: E402


def lift_function(path: Path, name: str):
    """Compile one top-level function out of a module that cannot be imported here (it needs bpy)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace: dict = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def sandbox_config(root: Path) -> dict:
    """The project configuration with every writable path moved under `root`.

    The frozen inputs (reference views, seed mesh) still point at the repository, read only.
    """
    cfg = copy.deepcopy(load_config())
    cfg["_root"] = str(root)
    cfg["pipeline"]["views_dir"] = str(REPO / "assets" / "views")
    cfg["pipeline"]["seed_glb"] = str(REPO / "assets" / "seed_tripo" / "model.glb")
    (root / "data").mkdir(parents=True, exist_ok=True)
    return cfg


def ledger_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeRenderer:
    """Stands in for blender_step.render_orbit: no Blender, one scripted audit verdict per call.

    A verdict is True (the audit passes), False (it fails) or an exception instance (the render
    itself breaks). The last verdict repeats once the list is used up.
    """

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts) or [True]
        self.calls: list[tuple[str, ShotPlan]] = []

    def __call__(self, cfg, run_dir, ledger_path, run_id, glb, plan, tag):
        verdict = self.verdicts[min(len(self.calls), len(self.verdicts) - 1)]
        self.calls.append((tag, plan))
        if isinstance(verdict, Exception):
            raise verdict
        out = Path(run_dir) / f"render_{tag}"
        out.mkdir(parents=True, exist_ok=True)
        video = out / "reference.mp4"
        video.write_bytes(b"placeholder reference clip " + tag.encode())
        tightest = plan.margin + (0.0001 if verdict else -0.02)
        report = FramingReport(
            passed=bool(verdict), plan=plan, fov_vertical_degrees=19.97, lens_mm=68.17,
            min_margins_lbrt=[tightest + 0.05, tightest, tightest + 0.05, tightest],
            side_sequence=["front", "left", "back", "right"], vertices=41000, frames=300,
            violations=[] if verdict else ["Frame 12 at 346° missed the target clearance."],
            video_path=str(video), render_seconds=0.1)
        (out / "framing.json").write_text(report.model_dump_json(), encoding="utf-8")
        ledger.log({"run_id": run_id, "stage": "blender_render", "provider": "local",
                    "status": "passed" if verdict else "framing_failed", "cost_usd": 0.0,
                    "mock": False, "tag": tag}, ledger_path)
        return report


class SandboxCase(unittest.TestCase):
    """A test case with a temporary project root and a fake renderer in place of Blender."""

    verdicts: tuple = (True,)

    def setUp(self):
        from pipeline import blender_step
        self.root = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cfg = sandbox_config(self.root)
        self.renderer = FakeRenderer(*self.verdicts)
        original = blender_step.render_orbit
        blender_step.render_orbit = self.renderer
        self.addCleanup(setattr, blender_step, "render_orbit", original)

    def make_context(self, **overrides) -> ToolContext:
        run_dir = self.root / "data" / "runs" / overrides.pop("run_id", "20260101_000000_mock_agent_s4207")
        run_dir.mkdir(parents=True, exist_ok=True)
        settings = dict(cfg=self.cfg, run_dir=run_dir, run_id=run_dir.name, dry_run=True, auto_approve=False,
                        glb=REPO / "assets" / "seed_tripo" / "model.glb")
        settings.update(overrides)
        return ToolContext(**settings)

    @property
    def ledger_path(self) -> Path:
        return self.root / "data" / "ledger.jsonl"

    def rows(self, **match) -> list[dict]:
        return [row for row in ledger_rows(self.ledger_path)
                if all(row.get(key) == value for key, value in match.items())]


def plan(**changes) -> ShotPlan:
    """The fixed baseline plan with the given fields changed."""
    values = dict(elevation_degrees=50.0, radius=6.0, start_degrees=0.0, direction="cw", margin=0.05)
    values.update(changes)
    return ShotPlan(**values)
