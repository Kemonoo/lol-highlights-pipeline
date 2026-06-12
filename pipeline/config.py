"""Config loader: YAML + ${ENV} expansion + .env support + data dirs."""
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _expand(obj):
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def load_config(path: Path | None = None) -> dict:
    _load_dotenv()
    cfg_path = path or Path(__file__).parent / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = _expand(yaml.safe_load(f))

    data = ROOT / cfg["paths"]["data"]
    for sub in ("raw", "work", "output"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    cfg["paths"]["data_abs"] = str(data)
    mp = cfg.get("video", {}).get("music_path")
    if mp and not Path(mp).is_absolute():
        cfg["video"]["music_path"] = str(ROOT / mp)
    return cfg
