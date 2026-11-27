"""Regenerate fixed_lens_worker/worker_render.py from the production worker:

  python experiments/shot_briefs/make_fixed_lens_worker.py

The copy lets the framing audit fail, which the production worker cannot do for any schema-valid
plan (it fits the lens to every plan). Fails loudly if the production worker no longer contains
the expected lines, so the copy cannot drift away from the code it was derived from.
tests/test_side_sequence.py checks that the committed copy equals generate().
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / "pipeline" / "worker_render.py"
TARGET = HERE / "fixed_lens_worker" / "worker_render.py"

FIXED_LENS_MM = 68.17215728759766      # the lens the production worker fitted for the 50°/6 baseline plan

HEADER = """# Benchmark copy of pipeline/worker_render.py. Do not edit by hand; regenerate it with
# experiments/shot_briefs/make_fixed_lens_worker.py. One difference from the production worker:
#   --fixed-lens-mm: the lens is fixed instead of fitted to each plan, so a plan can fail the
#   framing audit.
"""

EDITS = [
    ('    p.add_argument("--normalize", type=float, default=3.0, help="Normalize the longest horizontal dimension to this value")\n',
     '    p.add_argument("--normalize", type=float, default=3.0, help="Normalize the longest horizontal dimension to this value")\n'
     '    p.add_argument("--fixed-lens-mm", type=float, default=68.17215728759766,\n'
     '                 help="Fixed lens; 0 keeps the production behaviour of fitting the lens to the plan")\n'),
    ('    tangent *= 1.0002                          # 单精度余量（同紧凑实验）\n',
     '    tangent *= 1.0002                          # 单精度余量（同紧凑实验）\n'
     '    if args.fixed_lens_mm:\n'
     '        tangent = 24.0 / (2 * args.fixed_lens_mm)      # benchmark: fixed lens, so the audit can fail\n'),
]


def generate() -> str:
    """The text of the benchmark worker for the production worker as it is now."""
    source = SOURCE.read_text(encoding="utf-8")
    for old, new in EDITS:
        if source.count(old) != 1:
            raise SystemExit(f"Anchor line not found exactly once in {SOURCE}:\n{old}")
        source = source.replace(old, new)
    return HEADER + source


def main() -> int:
    text = generate()
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
