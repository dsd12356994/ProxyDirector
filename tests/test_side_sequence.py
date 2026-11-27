"""The view order reported by the render worker, and the benchmark's copy of that worker."""
import unittest

from tests.support import BENCHMARK, REPO, lift_function

PRODUCTION = REPO / "pipeline" / "worker_render.py"
BENCHMARK_COPY = BENCHMARK / "fixed_lens_worker" / "worker_render.py"

# The worker's own convention: azimuth 0 shows the front, and a counter-clockwise orbit (azimuth
# increasing) shows right, back, left. A clockwise orbit decreases the azimuth.
FACE_AT = {0: "front", 90: "right", 180: "back", 270: "left"}


def expected(start: float, direction: str) -> list[str]:
    nearest = (round((start % 360) / 90) % 4) * 90
    step = 90 if direction == "ccw" else -90
    return [FACE_AT[(nearest + i * step) % 360] for i in range(4)]


class SideSequence(unittest.TestCase):
    STARTS = (0, 90, 180, 270, -90, -180, 360, 450, 44, 46, 269.5)

    def check(self, path):
        side_sequence = lift_function(path, "side_sequence")
        for direction in ("cw", "ccw"):
            for start in self.STARTS:
                with self.subTest(direction=direction, start=start):
                    self.assertEqual(side_sequence(float(start), direction), expected(start, direction))

    def test_production_worker(self):
        self.check(PRODUCTION)

    def test_benchmark_copy(self):
        self.check(BENCHMARK_COPY)

    def test_clockwise_from_the_right_side(self):
        """The case the benchmark exposed: clockwise from 90 degrees starts on the right, then the front."""
        side_sequence = lift_function(PRODUCTION, "side_sequence")
        self.assertEqual(side_sequence(90.0, "cw"), ["right", "front", "left", "back"])
        self.assertEqual(side_sequence(270.0, "cw"), ["left", "back", "right", "front"])


class BenchmarkCopy(unittest.TestCase):
    def test_copy_is_what_the_generator_produces(self):
        """The committed fixed-lens worker must be exactly the generator's output for the current
        production worker, so the two cannot drift apart unnoticed."""
        import make_fixed_lens_worker
        self.assertEqual(BENCHMARK_COPY.read_text(encoding="utf-8"), make_fixed_lens_worker.generate())

    def test_copy_differs_from_production_only_by_the_fixed_lens(self):
        production = PRODUCTION.read_text(encoding="utf-8").splitlines()
        copy = BENCHMARK_COPY.read_text(encoding="utf-8").splitlines()
        added = [line for line in copy if line not in production]
        self.assertTrue(added, "the copy should add the fixed-lens option")
        for line in added:
            self.assertTrue(line.startswith("#") or "fixed_lens" in line or "fixed-lens" in line
                            or "Fixed lens" in line, f"unexpected difference from production: {line!r}")
        self.assertEqual([line for line in production if line not in copy], [],
                         "the copy must keep every production line")


if __name__ == "__main__":
    unittest.main()
