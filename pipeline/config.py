"""Config loader: YAML + ${ENV} expansion + .env support + overlays + data dirs.

An OVERLAY is a small YAML file holding only the keys that differ from config.yaml,
deep-merged over it:

    python -m pipeline.run_daily --config config.kemono.yaml

That is the supported way to keep your own settings — forking the whole 250-line
config.yaml means every upstream change becomes a merge conflict.
"""
import logging
import os
import random
import re
from pathlib import Path

import yaml

log = logging.getLogger("pipeline.config")

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


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` into `base`. Lists replace rather than concatenate —
    half-overridden lists (music_tracks, title_styles, blacklist) are never what
    anyone means."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Keys that moved into llm.roles when provider selection was centralised. Mapped
# forward with a warning so an existing config keeps working instead of silently
# ignoring the model someone set.
_LEGACY_ROLE_KEYS = {
    ("vlm_filter", "ollama_model"): ("vlm", "model"),
    ("vlm_filter", "ollama_url"): ("vlm", "base_url"),
    ("vlm_filter", "gemini_model"): ("vlm", "model"),
    ("api_judge", "model"): ("judge", "model"),
    ("api_judge", "api_key"): ("judge", "api_key"),
    ("commentary", "gemini_model"): ("commentary", "model"),
    ("commentary", "ollama_url"): ("commentary", "base_url"),
    ("thumbnail", "gemini_model"): ("thumbnail_image", "model"),
    ("thumbnail", "gemini_api_key"): ("thumbnail_image", "api_key"),
}


def _migrate_legacy_keys(cfg: dict) -> None:
    """Fold pre-llm.roles config keys forward, in place."""
    roles = cfg.setdefault("llm", {}).setdefault("roles", {})
    moved = []
    for (section, key), (role, field) in _LEGACY_ROLE_KEYS.items():
        value = (cfg.get(section) or {}).pop(key, None)
        if value in (None, ""):
            continue
        roles.setdefault(role, {})[field] = value
        moved.append(f"{section}.{key} -> llm.roles.{role}.{field}")
    if moved:
        log.warning("config: migrated legacy keys (update config.yaml to silence this):"
                    "\n  " + "\n  ".join(moved))


def _overlay_names(overlay) -> list[str]:
    """Accept one path, a comma-separated string, or a sequence of paths."""
    if not overlay:
        return []
    if isinstance(overlay, (str, Path)):
        return [s.strip() for s in str(overlay).split(",") if s.strip()]
    return [str(o) for o in overlay]


def load_config(path: Path | None = None, overlay=None) -> dict:
    _load_dotenv()
    cfg_path = path or Path(__file__).parent / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = _expand(yaml.safe_load(f))

    # Overlays compose left to right, each merged over the result so far — so you can
    # keep concerns in separate files (channel identity, throttling, a one-off tweak)
    # and combine them per run instead of maintaining every combination.
    for name in _overlay_names(overlay):
        op = Path(name)
        if not op.is_absolute():
            # Look next to config.yaml first, then relative to the repo root.
            op = next((c for c in (cfg_path.parent / name, ROOT / name)
                       if c.exists()), op)
        if not op.exists():
            raise FileNotFoundError(f"config overlay not found: {name}")
        with open(op, encoding="utf-8") as f:
            cfg = _deep_merge(cfg, _expand(yaml.safe_load(f) or {}))
        log.info("config overlay applied: %s", op.name)

    _migrate_legacy_keys(cfg)

    data = ROOT / cfg["paths"]["data"]
    for sub in ("raw", "work", "output"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    cfg["paths"]["data_abs"] = str(data)
    v = cfg.setdefault("video", {})
    tracks = v.get("music_tracks") or []
    if not v.get("music_enabled", True):
        v["music_path"] = ""          # lean mode: no music bed — clip audio carries the video
        v["music_attribution"] = ""
        tracks = []
    if tracks:
        pick = random.choice(tracks)
        p = pick.get("path", "")
        v["music_path"] = str(ROOT / p) if p and not Path(p).is_absolute() else p
        v["music_attribution"] = (
            f"{pick.get('artist', '')} - {pick.get('title', '')} | NoCopyrightSounds (NCS)"
        )
    else:
        mp = v.get("music_path", "")
        if mp and not Path(mp).is_absolute():
            v["music_path"] = str(ROOT / mp)

    # Outro music: a track for the branded outro drop, picked independently of the bed
    # (lean mode has no bed but still gets an outro song). Credited via music_attribution.
    all_tracks = v.get("music_tracks") or []
    if v.get("outro_music", False) and all_tracks:
        pick = random.choice(all_tracks)
        p = pick.get("path", "")
        v["outro_music_path"] = str(ROOT / p) if p and not Path(p).is_absolute() else p
        attr = f"{pick.get('artist', '')} - {pick.get('title', '')} | NoCopyrightSounds (NCS)"
        v["outro_music_attribution"] = attr
        if not v.get("music_attribution"):
            v["music_attribution"] = attr        # ensure the NCS outro track is credited
    else:
        v["outro_music_path"] = ""
    return cfg
