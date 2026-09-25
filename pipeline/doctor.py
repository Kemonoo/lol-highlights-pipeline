"""Environment check - `python -m pipeline.doctor`.

Answers "will a run work on this machine, with this config?" in a couple of seconds,
and prints the exact command to fix whatever is missing. Also runs automatically as a
fast preflight at the top of run_daily (`--skip-doctor` to bypass), because the
alternative is discovering that Ollama isn't running twenty minutes into a run that
has already downloaded a few hundred megabytes of clips.

Checks are classified, and the distinction is the whole point:

  FAIL  the run cannot produce a video          -> preflight aborts
  WARN  the run works, with reduced quality     -> preflight continues
  OK / SKIP                                     -> nothing to do

Degradation is a feature here, not an accident: no Gemini key means local scoring and
a PIL thumbnail, no GPU means CPU transcription. Those are WARNs, never FAILs.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import hardware
from .config import ROOT, load_config

FAIL, WARN, OK, SKIP = "FAIL", "WARN", "OK", "SKIP"

_MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL ", SKIP: " --   "}
# Only colour when attached to a terminal that isn't Windows' legacy console.
_COLOR = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "", fix: str = "") -> None:
        self.rows.append((status, name, detail, fix))

    @property
    def failed(self) -> list[tuple[str, str, str, str]]:
        return [r for r in self.rows if r[0] == FAIL]

    @property
    def warned(self) -> list[tuple[str, str, str, str]]:
        return [r for r in self.rows if r[0] == WARN]

    def print(self, *, verbose: bool = True) -> None:
        use_color = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        for status, name, detail, fix in self.rows:
            if not verbose and status in (OK, SKIP):
                continue
            mark = _MARK[status]
            if use_color:
                mark = f"{_COLOR[status]}{mark}\033[0m"
            print(f"[{mark}] {name:<26} {detail}")
            if fix and status in (FAIL, WARN):
                print(f"           -> {fix}")


# ── individual checks ─────────────────────────────────────────────────────────

def check_python(r: Report) -> None:
    v = sys.version_info
    if v < (3, 10):
        r.add(FAIL, "python", f"{v.major}.{v.minor} - 3.10+ required",
              "install Python 3.10 or newer (the code uses `X | Y` type syntax)")
    else:
        r.add(OK, "python", f"{v.major}.{v.minor}.{v.micro}")


def check_ffmpeg(r: Report) -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if hardware.have(binary):
            r.add(OK, binary, shutil.which(binary) or "")
        else:
            r.add(FAIL, binary, "not on PATH",
                  "install FFmpeg and add it to PATH - https://ffmpeg.org/download.html "
                  "(Windows: winget install Gyan.FFmpeg)")


def check_encoder(r: Report, cfg: dict) -> None:
    if not hardware.have("ffmpeg"):
        r.add(SKIP, "video encoder", "needs ffmpeg")
        return
    configured = str(cfg.get("video", {}).get("encoder", "auto"))
    chosen = hardware.pick_encoder(configured)
    available = ", ".join(sorted(hardware.ffmpeg_encoders())) or "none"
    if chosen == "libx264":
        r.add(WARN, "video encoder", f"libx264 (CPU). available: {available}",
              "assembly is CPU-bound; a GPU with NVENC/QSV/VideoToolbox cuts render "
              "time substantially. Nothing to fix if this machine has no GPU.")
    else:
        r.add(OK, "video encoder", f"{chosen} (hardware)")


def check_env_keys(r: Report, cfg: dict) -> None:
    tw = cfg.get("twitch", {})
    # Expanded at load time: an unset ${VAR} becomes "".
    if os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"):
        r.add(OK, "twitch credentials", "TWITCH_CLIENT_ID / _SECRET set")
    else:
        r.add(FAIL, "twitch credentials", "missing - this is where clips come from",
              "copy .env.example to .env and fill in TWITCH_CLIENT_ID / "
              "TWITCH_CLIENT_SECRET from https://dev.twitch.tv/console/apps")
    if tw.get("mode") == "broadcasters" and not tw.get("broadcasters"):
        r.add(WARN, "twitch mode", "mode: broadcasters but the list is empty",
              "add logins to twitch.broadcasters, or set mode: top_clips")


def check_roles(r: Report, cfg: dict, *, only: set[str] | None = None) -> None:
    """Resolve and health-check llm roles.

    Roles are always really checked, including in preflight: "is Ollama running?" is
    precisely the question preflight exists to answer, and a config-only check would
    happily wave through a dead host. Each role costs one cheap request, so the whole
    sweep is a second or two."""
    from .providers import ProviderUnavailable, get_provider, role_config

    roles = (cfg.get("llm") or {}).get("roles") or {}
    if only is not None:
        roles = {k: v for k, v in roles.items() if k in only}
        if not roles:
            return
    if not roles:
        r.add(FAIL, "llm.roles", "no roles configured",
              "config.yaml lost its `llm:` block - restore it from the repo copy")
        return

    # Which roles matter depends on what's switched on; don't nag about commentary
    # when lean mode has it disabled.
    enabled = {
        "vlm": cfg.get("vlm_filter", {}).get("enabled", True),
        "judge": cfg.get("api_judge", {}).get("enabled", True),
        "commentary": cfg.get("commentary", {}).get("enabled", False),
        "feedback": cfg.get("feedback", {}).get("enabled", True),
        "thumbnail_image": (cfg.get("thumbnail", {}).get("enabled", True)
                            and cfg.get("thumbnail", {}).get("provider") == "gemini"),
    }
    why_off = {
        "thumbnail_image": "thumbnail.provider is 'local' (free, no key needed)",
        "commentary": "commentary.enabled is false (lean mode)",
    }
    for role in roles:
        if not enabled.get(role, True):
            r.add(SKIP, f"role: {role}", why_off.get(role, "stage disabled in config"))
            continue
        try:
            rc = role_config(cfg, role)
        except ProviderUnavailable as e:
            r.add(WARN, f"role: {role}", str(e))
            continue
        # `vlm` is the only role with no fallback path: every other role degrades to
        # local scoring, a template line, or the PIL thumbnail, so its problems are
        # warnings. A broken vlm role means the filter cannot run at all.
        status_if_broken = FAIL if role == "vlm" else WARN
        try:
            provider = get_provider(cfg, role, vision=(role == "vlm"))
        except ProviderUnavailable as e:
            lines = str(e).splitlines()
            r.add(status_if_broken, f"role: {role}", lines[0],
                  "\n           -> ".join(lines[1:]))
            continue
        ok, detail = provider.health()
        r.add(OK if ok else status_if_broken, f"role: {role}",
              f"{rc.describe()} - {detail}" if ok else detail)


def check_degradation(r: Report, cfg: dict) -> None:
    """Spell out what a keyless/GPU-less run will actually produce."""
    if not os.environ.get("GEMINI_API_KEY"):
        r.add(WARN, "gemini key", "not set - the pipeline still produces a video",
              "clip selection falls back to local signals (less well curated) and "
              "the thumbnail uses the free PIL design. Add GEMINI_API_KEY in .env "
              "for the full quality path.")


def check_transcribe(r: Report, cfg: dict) -> None:
    tc = cfg.get("transcribe", {})
    if not tc.get("enabled", True):
        r.add(SKIP, "transcribe", "disabled in config")
        return
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        r.add(WARN, "faster-whisper", "not installed - clips get no burned captions",
              'pip install -e ".[transcribe]"')
        return
    device, compute = hardware.whisper_device(cfg)
    if device == "cuda":
        r.add(OK, "transcribe", f"cuda / {compute}")
    else:
        ok, reason = hardware.cuda_ready()
        r.add(WARN if hardware.gpu() else OK, "transcribe", f"cpu / {compute} ({reason})",
              "" if not hardware.gpu() else
              "you have a GPU but transcription is running on CPU - see the reason above")


def check_upload(r: Report, cfg: dict) -> None:
    up = cfg.get("upload", {})
    if not up.get("enabled", False):
        r.add(SKIP, "youtube upload", "disabled (safe default)")
        return
    if not (ROOT / "client_secret.json").exists():
        r.add(FAIL, "youtube oauth", "client_secret.json missing but upload.enabled is true",
              "create an OAuth desktop client in Google Cloud Console, download the "
              "JSON as client_secret.json in the repo root - or set upload.enabled: false")
        return
    token = Path(cfg["paths"]["data_abs"]) / "yt_token.json"
    r.add(OK if token.exists() else WARN, "youtube oauth",
          "token cached" if token.exists() else "first run will open a browser to authorise")
    if up.get("privacy") == "public":
        r.add(WARN, "upload privacy", "public - finished videos go live immediately",
              "set upload.privacy: private until you've reviewed a few outputs")


def check_assets(r: Report, cfg: dict) -> None:
    v = cfg.get("video", {})
    needs_music = v.get("music_enabled", False) or v.get("outro_music", False)
    if not needs_music:
        r.add(SKIP, "music", "music_enabled and outro_music are both off")
        return
    tracks = [t.get("path", "") for t in (v.get("music_tracks") or [])]
    missing = [t for t in tracks if t and not (ROOT / t).exists()]
    if missing:
        r.add(WARN, "music", f"{len(missing)}/{len(tracks)} configured tracks missing",
              "see assets/music/README.md to source them, or run "
              "`python -m pipeline.tools.gen_music` for a generated bed")
    else:
        r.add(OK, "music", f"{len(tracks)} tracks present")


def check_disk(r: Report, cfg: dict) -> None:
    data = Path(cfg["paths"]["data_abs"])
    data.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(data).free / 1024 ** 3
    if free_gb < 5:
        r.add(FAIL, "disk space", f"{free_gb:.1f} GB free in {data}",
              "a run downloads hundreds of MB of clips and renders a multi-GB master; "
              "free up space or lower cleanup.keep_raw_days")
    elif free_gb < 20:
        r.add(WARN, "disk space", f"{free_gb:.1f} GB free")
    else:
        r.add(OK, "disk space", f"{free_gb:.0f} GB free")


def check_gpu(r: Report) -> None:
    g = hardware.gpu()
    if g:
        name, vram = g
        r.add(OK, "gpu", f"{name} ({vram} MB VRAM)" if vram else name)
    else:
        r.add(OK, "gpu", "none detected - CPU paths will be used")


# ── entry points ──────────────────────────────────────────────────────────────

# What each stage actually depends on, so `--only assemble` doesn't fail on a Twitch
# key it will never use or probe a model it will never call.
_STAGE_NEEDS = {
    "fetch": {"twitch"},
    "prefilter": {"ffmpeg"},
    "vlm_filter": {"ffmpeg", "role:vlm"},
    "api_judge": {"ffmpeg", "role:judge"},
    "transcribe": {"transcribe"},
    "commentary": {"role:commentary"},
    "tts": set(),
    "assemble": {"ffmpeg", "assets"},
    "credits": set(),
    "thumbnail": {"role:thumbnail_image"},
    "upload": {"upload"},
    "shorts": {"ffmpeg", "role:commentary"},   # generates the Short's English title
    "clip_log": set(),
    "cleanup": set(),
    "feedback": {"role:feedback"},
}


def build_report(cfg: dict, *, quick: bool = False, stages: set[str] | None = None) -> Report:
    r = Report()
    needs = set().union(*(_STAGE_NEEDS.get(s, set()) for s in stages)) if stages else None

    def wanted(tag: str) -> bool:
        return needs is None or tag in needs

    roles = ({t.split(":", 1)[1] for t in needs if t.startswith("role:")}
             if needs is not None else None)

    check_python(r)
    check_ffmpeg(r)
    if not quick:
        check_gpu(r)
        check_encoder(r, cfg)
    if wanted("twitch"):
        check_env_keys(r, cfg)
    if roles is None or roles:
        check_roles(r, cfg, only=roles)
        if not quick:
            check_degradation(r, cfg)
    if wanted("transcribe") and not quick:
        check_transcribe(r, cfg)
    if wanted("upload"):
        check_upload(r, cfg)
    if wanted("assets") and not quick:
        check_assets(r, cfg)
    check_disk(r, cfg)
    return r


def preflight(cfg: dict, stages: set[str] | None = None) -> bool:
    """Gate for run_daily. True = safe to proceed."""
    r = build_report(cfg, quick=True, stages=stages)
    if not r.failed:
        return True
    print("\nPre-run check failed:\n")
    r.print(verbose=False)
    print("\nFix the items above, or re-run with --skip-doctor to try anyway.")
    print("Full report: python -m pipeline.doctor\n")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Check this machine against the pipeline config")
    ap.add_argument("--config", default="", help="YAML overlay(s) merged over config.yaml, comma-separated")
    args = ap.parse_args()

    cfg = load_config(overlay=args.config or None)
    print(f"\nChecking {ROOT}\n")
    r = build_report(cfg)
    r.print()

    n_fail, n_warn = len(r.failed), len(r.warned)
    print()
    if n_fail:
        print(f"{n_fail} blocking problem(s), {n_warn} warning(s). "
              "A run will not produce a video until the FAILs are fixed.")
        return 1
    if n_warn:
        print(f"Ready to run, with {n_warn} warning(s) - those reduce quality, "
              "not function.")
    else:
        print("Everything checks out.")
    print("\nNext:  python -m pipeline.run_daily --date YYYY-MM-DD\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
