"""配置与 .env 加载。密钥只从 .env 读取，绝不打印、不写日志。"""
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


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
