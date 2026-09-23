"""Run progress — `python -m pipeline.progress [--watch]`.

Answers "how far along is it, and is it still moving?" without reading the log.

Progress is read entirely from what a run leaves on disk: the per-clip caches
(`vlm_partial_v4.json`, `api_partial_v3.json`, ...), the per-stage output files that
`run_daily` uses to decide what to skip, and `run.json` — a small manifest the run
writes at startup recording which stages it is actually configured to perform.

The manifest matters more than it looks. Whether a stage is enabled comes from config,
and the person watching may not have passed the same `--config` overlays the run did.
Reading the watcher's config instead of the run's would let this tool report "upload
disabled" for a run that is about to publish. So config-derived state comes from the
manifest, and only falls back to config for a date that never ran.

Everything else is inherently in sync, because it is the pipeline's own bookkeeping.
Works against a run started by anything (the .bat, cron, another terminal).

    python -m pipeline.progress                 # latest run, one shot
    python -m pipeline.progress --watch         # refresh until the run finishes
    python -m pipeline.progress --date 2026-07-27
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import load_config

DONE, RUNNING, PENDING, SKIPPED = "done", "running", "pending", "skipped"

_MARK = {DONE: "[x]", RUNNING: "[>]", PENDING: "[ ]", SKIPPED: "[-]"}
_COLOR = {DONE: "\033[32m", RUNNING: "\033[36m", PENDING: "\033[90m", SKIPPED: "\033[90m"}


def _count(path: Path) -> int:
    """Number of entries in a JSON dict/list cache, 0 if absent or mid-write."""
    try:
        data = json.loads(path.read_text(encoding="utf-8").rstrip("\x00"))
    except Exception:
        return 0                      # a partially-flushed cache is not an error here
    if isinstance(data, dict):
        return len(data.get("clips", data))
    return len(data) if isinstance(data, list) else 0


def _mtime(*paths: Path) -> float:
    return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)


def _manifest(work: Path) -> dict:
    """What the RUN said it would do (work/<date>/run.json), if it left a manifest.

    This is preferred over the caller's config on purpose: whoever is watching may not
    have passed the same overlays the run did, and a monitor that reports 'upload
    disabled' for a run that is about to publish is worse than no monitor."""
    try:
        return json.loads((work / "run.json").read_text(encoding="utf-8").rstrip("\x00"))
    except Exception:
        return {}


def _uploaded(data: Path, date_label: str) -> str:
    """YouTube id recorded for this date, if any.

    upload is the one stage that leaves no output file, so state.json is the only
    place its completion is written down. Without this the row sits at PENDING
    forever and `--watch` never reaches 'Run complete'."""
    try:
        st = json.loads((data / "state.json").read_text(encoding="utf-8").rstrip("\x00"))
    except Exception:
        return ""
    for v in reversed(st.get("videos", [])):
        if v.get("date") == date_label and v.get("youtube_id"):
            return str(v["youtube_id"])
    return ""


def collect(cfg: dict, date_label: str) -> tuple[list[dict], float, float]:
    """(stage rows, started_at, last_activity) — all derived from files on disk."""
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    raw = data / "raw" / date_label
    out = data / "output"

    man = _manifest(work)
    man_stages = man.get("stages") or {}

    def enabled(stage: str, cfg_section: str, default: bool = True) -> bool:
        """Manifest first, config as the fallback for a date that never ran."""
        if stage in man_stages:
            return bool(man_stages[stage])
        return bool(cfg.get(cfg_section, {}).get("enabled", default))

    upload_on = enabled("upload", "upload", False)
    privacy = cfg.get("upload", {}).get("privacy", "private")
    yt_id = _uploaded(data, date_label)

    kept_prefilter = _count(work / "prefiltered.json")
    kept_vlm = _count(work / "vlm_filtered.json")
    n_chapters = _count(work / "chapters.json")
    shorts_dir = work / "shorts"

    def row(name: str, *, done: Path | None, complete: bool = False, have: int = 0,
            total: int = 0, note: str = "", enabled: bool = True) -> dict:
        finished = complete or bool(done and done.exists())
        if not enabled:
            state = SKIPPED
        elif finished:
            state = DONE
        elif have:
            state = RUNNING
        else:
            state = PENDING
        return {"name": name, "state": state, "have": have, "total": total,
                "note": note}

    rows = [
        row("fetch", done=raw / "clips.json",
            note=f"{_count(raw / 'clips.json')} clips" if (raw / "clips.json").exists() else ""),
        row("prefilter", done=work / "prefiltered.json",
            have=_count(work / "prefilter_partial.json"),
            note=f"{kept_prefilter} kept" if kept_prefilter else "scoring"),
        row("vlm_filter", done=work / "vlm_scored.json",
            have=_count(work / "vlm_partial_v4.json"), total=kept_prefilter,
            note=f"{kept_vlm} kept" if kept_vlm else ""),
        row("api_judge", done=work / "api_scored.json",
            have=_count(work / "api_partial_v3.json"),
            total=kept_vlm or cfg.get("vlm_filter", {}).get("max_keep", 0),
            enabled=enabled("api_judge", "api_judge")),
        row("transcribe", done=work / "transcripts.done.json",
            have=_count(work / "transcripts.json"),
            enabled=enabled("transcribe", "transcribe")),
        row("commentary", done=work / "commentary.json",
            have=_count(work / "commentary.json"),
            note="off - lean mode, the clips carry the video",
            enabled=enabled("commentary", "commentary", False)),
        row("tts", done=work / "vo" / "timings.json",
            have=len(list((work / "vo").glob("*.mp3"))) if (work / "vo").exists() else 0,
            note="off - needs commentary",
            enabled=enabled("tts", "tts", False)),
        row("assemble", done=out / f"{date_label}.mp4",
            have=len(list((work / "segments").glob("*.mp4")))
            if (work / "segments").exists() else 0,
            total=n_chapters or kept_vlm),
        row("credits", done=out / f"{date_label}.meta.json"),
        row("thumbnail", done=work / "thumbnail.jpg",
            enabled=enabled("thumbnail", "thumbnail")),
        row("upload", done=None, complete=bool(yt_id), enabled=upload_on,
            note=(f"https://youtu.be/{yt_id}" if yt_id
                  else f"will publish as {man.get('upload_privacy', privacy)}" if upload_on
                  else "off - renders but publishes nothing")),
        row("shorts", done=shorts_dir / "done.json",
            have=len(list(shorts_dir.glob("*.mp4"))) if shorts_dir.exists() else 0,
            total=man.get("shorts_count", cfg.get("shorts", {}).get("count", 0)),
            enabled=enabled("shorts", "shorts")),
    ]
    # `assemble` writes the final mp4 only at the very end, so treat a rendered-segment
    # count as real progress even though the done-marker is still absent.
    started = man.get("started_at") or _mtime(raw / "clips.json",
                                             work / "prefiltered.json")
    last = _mtime(*(p for p in work.rglob("*") if p.is_file()),
                  out / f"{date_label}.mp4") if work.exists() else 0.0
    return rows, started, last


def _bar(have: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return ""
    filled = min(width, round(width * have / total))
    return f"[{'#' * filled}{'.' * (width - filled)}] {have}/{total}"


def render(rows: list[dict], date_label: str, started: float, last: float,
           *, color: bool) -> str:
    now = time.time()
    lines = [""]
    head = f"  Run progress - {date_label}"
    if started:
        # From the manifest this is when the current run began; without one it falls
        # back to the date's first artifact, which also covers earlier attempts.
        head += f"    running {timedelta(seconds=int(now - started))}"
    lines += [head, ""]

    for r in rows:
        mark = _MARK[r["state"]]
        if color:
            mark = f"{_COLOR[r['state']]}{mark}\033[0m"
        bar = _bar(r["have"], r["total"])
        if not bar and r["state"] == RUNNING and r["have"]:
            bar = f"{r['have']} done"
        # Notes describe work in flight or a config fact; suppress them for stages
        # that haven't started, where they read as claims about the present.
        note = r["note"] if (r["state"] != PENDING or not bar) else ""
        detail = "  ".join(x for x in (bar, note) if x)
        lines.append(f"  {mark} {r['name']:<12} {detail}")

    lines.append("")
    if last:
        idle = int(now - last)
        stalled = idle > 600
        msg = f"  last file written {timedelta(seconds=idle)} ago"
        if stalled:
            msg += "  <- nothing for 10min; check the log or whether it died"
        lines.append(msg)
    return "\n".join(lines) + "\n"


def _latest_date(cfg: dict) -> str:
    work_root = Path(cfg["paths"]["data_abs"]) / "work"
    dates = sorted(p.name for p in work_root.iterdir() if p.is_dir()) \
        if work_root.exists() else []
    if dates:
        return dates[-1]
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg["twitch"]["timezone"])
    return (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description="Show progress of a pipeline run")
    ap.add_argument("--date", help="YYYY-MM-DD (default: most recent run)")
    ap.add_argument("--config", default="", help="YAML overlay(s) merged over config.yaml, comma-separated")
    ap.add_argument("--watch", action="store_true",
                    help="refresh until every enabled stage is done")
    ap.add_argument("--interval", type=float, default=10.0, help="--watch seconds")
    args = ap.parse_args()

    cfg = load_config(overlay=args.config or None)
    date_label = args.date or _latest_date(cfg)
    color = sys.stdout.isatty()

    while True:
        rows, started, last = collect(cfg, date_label)
        text = render(rows, date_label, started, last, color=color)
        if args.watch:
            # Repaint in place rather than scrolling a wall of near-identical reports.
            print("\033[2J\033[H" + text if color else text, flush=True)
            if all(r["state"] in (DONE, SKIPPED) for r in rows):
                print("  Run complete.\n")
                return 0
            try:
                time.sleep(args.interval)
            except KeyboardInterrupt:
                return 130
        else:
            print(text)
            return 0


if __name__ == "__main__":
    sys.exit(main())
