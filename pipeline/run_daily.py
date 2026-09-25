"""Pipeline orchestrator with resume support.

Usage:
    python -m pipeline.run_daily                          # yesterday, all stages
    python -m pipeline.run_daily --date 2026-06-09
    python -m pipeline.run_daily --stop-after vlm_filter
    python -m pipeline.run_daily --skip vlm_filter,upload
    python -m pipeline.run_daily --only fetch
    python -m pipeline.run_daily --force                  # redo stages even if output exists

Resume: a stage whose output file already exists is skipped (use --force to redo).
prefilter and vlm_filter additionally save per-clip progress, so an interrupted run
continues where it left off. Unimplemented stages are skipped with a warning.
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config
from .enrichment import hud_ocr, match_linker, transcribe
from .feedback import feedback
from .filtering import api_judge, prefilter, vlm_filter
from .ingestion import fetch
from .production import assemble, commentary, credits, thumbnail, tts
from .publishing import cleanup, clip_log, shorts, upload
from .state import State

log = logging.getLogger("pipeline")

STAGES = [
    ("feedback", feedback.run),   # reads comments on past uploads; never blocks
    ("fetch", fetch.run),
    ("prefilter", prefilter.run),
    ("vlm_filter", vlm_filter.run),
    ("api_judge", api_judge.run),
    ("transcribe", transcribe.run),   # streamer speech -> English (commentary + shorts captions)
    ("match_linker", match_linker.run),
    ("hud_ocr", hud_ocr.run),
    ("commentary", commentary.run),
    ("tts", tts.run),
    ("assemble", assemble.run),
    ("credits", credits.run),
    ("thumbnail", thumbnail.run),
    ("upload", upload.run),
    ("shorts", shorts.run),    # vertical clips for YouTube Shorts (runs after main upload)
    ("clip_log", clip_log.run),  # data/clip_log.jsonl: every published clip, forever
    ("cleanup", cleanup.run),  # delete old raw MP4s to free disk space
]


def _selection(cfg: dict, d: str) -> list:
    import json
    f = Path(cfg["paths"]["data_abs"]) / "work" / d / "vlm_filtered.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8").rstrip("\x00"))["clips"]


def _selection_minutes(cfg: dict, d: str) -> float:
    return sum(c.get("duration", 30) for c in _selection(cfg, d)) / 60.0


def _selection_short(cfg: dict, d: str) -> bool:
    """Below video.target_clips when set, else below target_minutes_ideal."""
    v = cfg.get("video", {})
    target = int(v.get("target_clips", 0) or 0)
    if target:
        return len(_selection(cfg, d)) < target
    return _selection_minutes(cfg, d) < v.get("target_minutes_ideal", 8)


def _expand_selection_if_short(cfg: dict, state, date_label: str) -> None:
    """Dig deeper into the day's top clips until the selection reaches the ideal
    video length (or the fetch cap). Per-clip caches make every extra round
    incremental — only the newly fetched clips cost prefilter/VLM/judge time."""
    tw = cfg["twitch"]
    step = tw.get("expand_step", 150)
    max_fetch = tw.get("max_fetch", 600)
    while (_selection_short(cfg, date_label)
           and tw["fetch_count"] + step <= max_fetch):
        tw["fetch_count"] += step
        cfg["prefilter"]["max_keep"] += tw.get("expand_keep_step", 25)
        cfg["vlm_filter"]["max_keep"] += 8
        log.info("Selection at %d clips / %.1f min (short of target) — expanding fetch "
                 "to %d clips", len(_selection(cfg, date_label)),
                 _selection_minutes(cfg, date_label), tw["fetch_count"])
        fetch.run(cfg, state, date_label)
        prefilter.run(cfg, state, date_label)
        vlm_filter.run(cfg, state, date_label)
        api_judge.run(cfg, state, date_label)
    if _selection_short(cfg, date_label):
        log.info("Selection finalized at %d clips / %.1f min - day's pool exhausted",
                 len(_selection(cfg, date_label)), _selection_minutes(cfg, date_label))


def _stage_outputs(cfg: dict, d: str) -> dict:
    """Output file per stage — if it exists, the stage is considered done."""
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / d
    return {
        "fetch": data / "raw" / d / "clips.json",
        "prefilter": work / "prefiltered.json",
        "vlm_filter": work / "vlm_filtered.json",
        "api_judge": work / "api_scored.json",
        # NOT transcripts.json: that is an incremental per-clip cache which exists as
        # soon as the first clip lands, so a mid-stage crash looked "done" and the
        # retry skipped the remaining clips (they shipped with no captions).
        "transcribe": work / "transcripts.done.json",
        "commentary": work / "commentary.json",
        "tts": work / "vo" / "timings.json",
        "assemble": data / "output" / f"{d}.mp4",
        "credits": data / "output" / f"{d}.meta.json",
        "thumbnail": work / "thumbnail.jpg",
        "shorts": work / "shorts" / "done.json",
    }


def _write_run_manifest(cfg: dict, date_label: str, *, overlays: str,
                        skip: set, only: set) -> None:
    """Record what THIS run is actually configured to do.

    Without it, `pipeline.progress` has to re-read config.yaml to know whether a stage
    is enabled — and reports on whichever overlay the person watching happened to pass,
    not the one the run loaded. That turns the monitor into a source of confident
    misinformation (an upload that IS going to happen shown as 'disabled'). Progress is
    read-only over artifacts, so the run has to leave this behind."""
    import json
    import os
    import time

    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    work.mkdir(parents=True, exist_ok=True)
    up = cfg.get("upload", {})
    sh = cfg.get("shorts", {})

    def on(section: str, default: bool = True) -> bool:
        return bool(cfg.get(section, {}).get("enabled", default))

    manifest = {
        "started_at": time.time(),
        "pid": os.getpid(),
        "overlays": [o.strip() for o in overlays.split(",") if o.strip()],
        "only": sorted(only), "skip": sorted(skip),
        "stages": {
            "api_judge": on("api_judge"),
            "transcribe": on("transcribe"),
            "commentary": on("commentary", False),
            "tts": on("tts", False),
            "thumbnail": on("thumbnail"),
            "upload": bool(up.get("enabled", False)),
            "shorts": bool(sh.get("enabled", True)),
        },
        "upload_privacy": up.get("privacy", "private"),
        "shorts_count": int(sh.get("count", 0)),
        "shorts_upload": bool(sh.get("upload", False)),
    }
    try:
        (work / "run.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except OSError as e:                    # never let bookkeeping stop a run
        log.debug("could not write run manifest: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Twitch -> YouTube daily pipeline")
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--skip", default="", help="comma-separated stage names to skip")
    parser.add_argument("--only", default="", help="run only these stages (comma-separated)")
    parser.add_argument("--stop-after", default="", help="stop after this stage")
    parser.add_argument("--force", action="store_true",
                        help="re-run stages even if their output already exists")
    parser.add_argument("--config", default="",
                        help="YAML overlay(s) merged over config.yaml - only the keys "
                             "you want to change. Comma-separate to compose several, "
                             "applied left to right "
                             "(e.g. config.kemono.yaml,config.local.throttled.yaml)")
    parser.add_argument("--skip-doctor", action="store_true",
                        help="skip the pre-run environment check")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    cfg = load_config(overlay=args.config or None)

    # Before any heavy import: the thread-count env vars are read by OpenMP/BLAS at
    # import time, so applying them later would silently do nothing.
    from .hardware import apply_limits
    apply_limits(cfg)

    state = State(cfg["paths"]["data_abs"])

    if args.date:
        date_label = args.date
    elif args.only or args.force:
        # iterating on an existing run: target the most recent work dir, not the
        # calendar (avoids the midnight rollover trap)
        work_root = Path(cfg["paths"]["data_abs"]) / "work"
        dates = sorted(p.name for p in work_root.iterdir() if p.is_dir()) \
            if work_root.exists() else []
        if dates:
            date_label = dates[-1]
            log.info("No --date given - using latest run: %s", date_label)
        else:
            tz = ZoneInfo(cfg["twitch"]["timezone"])
            date_label = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        tz = ZoneInfo(cfg["twitch"]["timezone"])
        date_label = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    if not cfg["prefilter"]["enabled"]:
        skip.add("prefilter")
    if not cfg["vlm_filter"]["enabled"]:
        skip.add("vlm_filter")
    if not cfg["match_linker"]["enabled"]:
        skip.add("match_linker")

    # Fail in seconds on a misconfigured machine rather than 20 minutes in, after
    # fetch has already downloaded a few hundred MB of clips.
    if not args.skip_doctor:
        from .doctor import preflight
        planned = (only or {name for name, _ in STAGES}) - skip
        if not preflight(cfg, stages=planned):
            return 1

    outputs = _stage_outputs(cfg, date_label)
    _write_run_manifest(cfg, date_label, overlays=args.config, skip=skip, only=only)

    log.info("=== Pipeline run for %s ===", date_label)
    for name, fn in STAGES:
        if only and name not in only:
            continue
        if name in skip:
            log.info("[%s] skipped", name)
            continue
        out = outputs.get(name)
        if out is not None and out.exists() and not args.force:
            log.info("[%s] already done (%s) — skipping, use --force to redo",
                     name, out.name)
            if args.stop_after == name:
                break
            continue
        log.info("[%s] starting", name)
        try:
            fn(cfg, state, date_label)
            if name == "api_judge":
                _expand_selection_if_short(cfg, state, date_label)
        except NotImplementedError as e:
            log.warning("[%s] not implemented yet - skipping (%s)", name, e)
        except FileNotFoundError as e:
            log.error("[%s] missing input - did an earlier stage run? (%s)", name, e)
            return 1
        except KeyboardInterrupt:
            log.warning("[%s] interrupted - progress saved, re-run to continue", name)
            return 130
        except Exception as e:
            log.error("[%s] failed: %s", name, e)
            log.error("Progress is saved - fix the issue and re-run; "
                      "completed stages and clips will be skipped.")
            return 1
        if args.stop_after == name:
            log.info("Stopped after [%s]", name)
            break

    # mark clips processed once a video exists for the day
    out_video = Path(cfg["paths"]["data_abs"]) / "output" / f"{date_label}.mp4"
    if out_video.exists():
        import json
        ch_file = Path(cfg["paths"]["data_abs"]) / "work" / date_label / "chapters.json"
        if ch_file.exists():
            chapters = json.loads(ch_file.read_text(encoding="utf-8"))
            state.mark_processed([c["clip_id"] for c in chapters])
            state.add_video({"date": date_label, "path": str(out_video),
                             "clips": len(chapters)})
    log.info("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
