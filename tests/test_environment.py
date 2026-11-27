"""Finding Blender and FFmpeg on any platform, the environment check, and the command line."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, plan, sandbox_config

import run
from pipeline import blender_step, config


class ProgramLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = sandbox_config(self.tmp)
        self.real = self.tmp / ("blender.exe" if os.name == "nt" else "blender")
        self.real.write_text("stand-in", encoding="utf-8")
        self.swap(config, "api_key", lambda name: None)          # no override from the environment or .env
        self.swap(config, "_installed_blenders", lambda: [])
        self.swap(config.shutil, "which", lambda name: None)

    def swap(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def test_configured_path_is_used_when_it_exists(self):
        self.cfg["pipeline"]["blender_bin"] = str(self.real)
        self.assertEqual(config.blender_bin(self.cfg), str(self.real))

    def test_macos_default_from_config_falls_back_to_path_then_install_folders(self):
        self.assertEqual(self.cfg["pipeline"]["blender_bin"], "/Applications/Blender.app/Contents/MacOS/Blender")
        self.cfg["pipeline"]["blender_bin"] = str(self.tmp / "not-installed" / "Blender")
        self.swap(config.shutil, "which", lambda name: str(self.real) if name == "blender" else None)
        self.assertEqual(config.blender_bin(self.cfg), str(self.real))
        self.swap(config.shutil, "which", lambda name: None)
        self.swap(config, "_installed_blenders", lambda: [str(self.tmp / "gone.exe"), str(self.real)])
        self.assertEqual(config.blender_bin(self.cfg), str(self.real))

    def test_nothing_found_returns_the_configured_value_for_the_error_message(self):
        self.cfg["pipeline"]["blender_bin"] = "/nowhere/Blender"
        self.assertEqual(config.blender_bin(self.cfg), "/nowhere/Blender")
        self.assertIsNone(config.find_program(config.blender_bin(self.cfg)))

    def test_explicit_override_wins_and_is_not_silently_replaced(self):
        self.cfg["pipeline"]["blender_bin"] = str(self.real)
        self.swap(config, "api_key", lambda name: "/custom/blender" if name == config.BLENDER_ENV else None)
        self.assertEqual(config.blender_bin(self.cfg), "/custom/blender")
        self.swap(config, "api_key", lambda name: str(self.real) if name == config.FFMPEG_ENV else None)
        self.assertEqual(config.ffmpeg_bin(self.cfg), str(self.real))

    def test_ffmpeg_is_looked_up_on_path(self):
        self.assertEqual(config.ffmpeg_bin(self.cfg), "ffmpeg")
        self.swap(config.shutil, "which", lambda name: str(self.real) if name == "ffmpeg" else None)
        self.assertEqual(config.ffmpeg_bin(self.cfg), str(self.real))

    def test_windows_install_folders_prefer_the_newest_version(self):
        base = self.tmp / "Program Files"
        for version in ("Blender 4.5", "Blender 5.2", "Blender 5.10"):
            folder = base / "Blender Foundation" / version
            folder.mkdir(parents=True)
            (folder / "blender.exe").write_text("stand-in", encoding="utf-8")
        found = config._windows_blenders([str(base), None, str(self.tmp / "missing")])
        self.assertEqual([Path(p).parent.name for p in found], ["Blender 5.10", "Blender 5.2", "Blender 4.5"])


class RenderPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = sandbox_config(self.tmp)
        for name, value in (("blender_bin", lambda cfg: "/nowhere/Blender"), ("ffmpeg_bin", lambda cfg: "ffmpeg-missing")):
            original = getattr(config, name)
            setattr(config, name, value)
            self.addCleanup(setattr, config, name, original)

    def test_missing_blender_is_named_before_anything_is_started(self):
        started = []
        original = subprocess.run
        blender_step.subprocess.run = lambda *a, **k: started.append(a)
        self.addCleanup(setattr, blender_step.subprocess, "run", original)
        with self.assertRaises(RuntimeError) as failure:
            blender_step.render_orbit(self.cfg, self.tmp, self.tmp / "ledger.jsonl", "run",
                                      REPO / "assets" / "seed_tripo" / "model.glb", plan(), "v1")
        message = str(failure.exception)
        self.assertIn("Blender was not found", message)
        self.assertIn(config.BLENDER_ENV, message)
        self.assertEqual(started, [])

    def test_missing_ffmpeg_is_named_too(self):
        config.blender_bin = lambda cfg: sys.executable          # any existing program
        with self.assertRaises(RuntimeError) as failure:
            blender_step.require_programs(self.cfg)
        self.assertIn("FFmpeg was not found", str(failure.exception))


class EnvironmentCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proxydirector_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = sandbox_config(self.tmp)
        self.keys: dict[str, str] = {}
        self.programs = {"blender": sys.executable, "ffmpeg": sys.executable}
        for name, value in (("api_key", lambda env: self.keys.get(env)),
                            ("blender_bin", lambda cfg: self.programs["blender"]),
                            ("ffmpeg_bin", lambda cfg: self.programs["ffmpeg"])):
            original = getattr(run, name)
            setattr(run, name, value)
            self.addCleanup(setattr, run, name, original)

    def check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run.check(self.cfg)
        return code, out.getvalue()

    def test_rehearsal_is_ready_without_any_api_key(self):
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertIn("Rehearsal runs (--dry-run): ready.", text)
        self.assertIn("Live video generation (kling): not configured, KLING_API_KEY is missing.", text)
        self.assertIn("Frozen views: 4/4", text)
        self.assertIn("not seeded yet", text)

    def test_every_cloud_director_key_is_listed(self):
        """The Astra backend uses another API style and used to be left out of the list."""
        _, text = self.check()
        for backend in self.cfg["backends"].values():
            if backend.get("api_key_env"):
                self.assertIn(backend["api_key_env"], text)
        self.assertIn("ASTRA_API_KEY (astra backend)", text)

    def test_missing_blender_or_ffmpeg_fails_the_check_with_a_hint(self):
        self.programs["blender"] = "/nowhere/Blender"
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn("MISS  Blender: /nowhere/Blender", text)
        self.assertIn("PROXYDIRECTOR_BLENDER", text)
        self.assertIn("not ready", text)
        self.programs.update(blender=sys.executable, ffmpeg="ffmpeg-missing")
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn("MISS  FFmpeg: ffmpeg-missing", text)

    def test_configured_video_key_is_reported(self):
        self.keys["KLING_API_KEY"] = "test-key"
        import pipeline.kling as kling
        original = kling.packs
        kling.packs = lambda cfg: [{"resource_pack_name": "Trial", "remaining_quantity": 23.67,
                                    "resource_pack_applicable_models": "kling-4.0-flash"}]
        self.addCleanup(setattr, kling, "packs", original)
        code, text = self.check()
        self.assertEqual(code, 0)
        self.assertIn("Live video generation (kling): key configured.", text)
        self.assertIn("Credit pack Trial: remaining 23.67", text)
        self.assertNotIn("test-key", text)


class CommandLine(unittest.TestCase):
    def run_cli(self, *argv):
        env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYDANTIC_AI_NO_BANNER": "1"}
        return subprocess.run([sys.executable, str(REPO / "run.py"), *argv], cwd=REPO, env=env,
                              capture_output=True, timeout=120)

    def test_misspelt_flag_is_rejected_instead_of_ignored(self):
        """'--dryrun' used to be dropped silently, which turned a rehearsal into a live run."""
        result = self.run_cli("--backend", "mock", "--dryrun")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"unrecognized arguments: --dryrun", result.stderr)

    def test_unknown_backend_is_reported(self):
        result = self.run_cli("--backend", "nope", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"Unknown backend nope", result.stdout)

    def test_output_survives_a_legacy_code_page(self):
        """A redirected Windows console uses a code page that cannot encode an arrow or a degree sign."""
        code = ("import sys; sys.path.insert(0, r'%s'); import run; run.utf8_console(); "
                "print('35\\u00b0 front \\u2192 left')" % REPO)
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "35° front → left")


if __name__ == "__main__":
    unittest.main()
