"""配置与 .env 加载。密钥只从 .env 读取，绝不打印、不写日志。

Also finds the two external programs the render step needs. config.yaml was written on macOS, so
blender_bin there is a macOS path: on Windows and Linux Blender is looked up on PATH and in the
usual install folders, and either program can be pinned with an environment variable (or a line
in .env): PROXYDIRECTOR_BLENDER, PROXYDIRECTOR_FFMPEG.
"""
import glob
import os
import re
import shutil
import sys
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
BLENDER_ENV = "PROXYDIRECTOR_BLENDER"
FFMPEG_ENV = "PROXYDIRECTOR_FFMPEG"


def load_env() -> dict:
    """读 .env（若存在），返回 {名: 值}。只在这里触碰密钥。"""
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config() -> dict:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["_root"] = str(ROOT)
    return cfg


def resolve(name: str) -> Path:
    """把配置里的相对路径解析到项目根下。"""
    p = Path(name)
    return p if p.is_absolute() else ROOT / p


def api_key(env_name: str) -> str | None:
    key = os.environ.get(env_name) or load_env().get(env_name)
    if key:
        return key.strip()
    return None


def find_program(candidate: str | None) -> str | None:
    """The path of a program that can be started: an existing file, or a name found on PATH."""
    if not candidate:
        return None
    if Path(candidate).is_file():
        return str(candidate)
    return shutil.which(candidate)


def _windows_blenders(bases) -> list[str]:
    """blender.exe under each "<base>\\Blender Foundation\\Blender <version>" folder, newest version first."""
    found = []
    for base in bases:
        if base:
            found += glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe"))
            found += glob.glob(os.path.join(base, "Programs", "Blender Foundation", "Blender*", "blender.exe"))
    return sorted(found, reverse=True,
                  key=lambda path: tuple(int(n) for n in re.findall(r"\d+", Path(path).parent.name)))


def _installed_blenders() -> list[str]:
    """Blender executables in the usual install folders of this platform, newest version first."""
    if sys.platform == "darwin":
        return ["/Applications/Blender.app/Contents/MacOS/Blender",
                str(Path.home() / "Applications" / "Blender.app" / "Contents" / "MacOS" / "Blender")]
    if os.name == "nt":
        return _windows_blenders(os.environ.get(name) for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
    return ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"]


def blender_bin(cfg: dict) -> str:
    """The Blender executable to start.

    An explicit PROXYDIRECTOR_BLENDER wins. Otherwise: pipeline.blender_bin from config.yaml if that
    file exists, `blender` on PATH, then this platform's usual install folders. When nothing is
    found the configured value comes back unchanged, so the error message can name it.
    """
    override = api_key(BLENDER_ENV)
    if override:
        return find_program(override) or override
    configured = cfg["pipeline"]["blender_bin"]
    for candidate in (configured, "blender", *_installed_blenders()):
        found = find_program(candidate)
        if found:
            return found
    return configured


def ffmpeg_bin(cfg: dict) -> str:
    """The FFmpeg executable to start: PROXYDIRECTOR_FFMPEG, else pipeline.ffmpeg_bin, else `ffmpeg` on PATH."""
    override = api_key(FFMPEG_ENV)
    if override:
        return find_program(override) or override
    configured = cfg["pipeline"].get("ffmpeg_bin") or "ffmpeg"
    return find_program(configured) or find_program("ffmpeg") or configured
