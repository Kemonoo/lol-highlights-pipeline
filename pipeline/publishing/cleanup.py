"""Stage 12 — Free disk space after the day's video is done.

Keeps:
  - data/raw/<date>/clips.json  (metadata, tiny)
  - data/work/<date>/*.json     (all work JSONs, tiny)
  - data/output/<date>.meta.json (title, chapters, per-streamer credits — never pruned:
    it is the record of who was credited, and it costs kilobytes)
  - final MP4s for the last `keep_output_days` days, and ANY master this machine cannot
    prove was uploaded (see below)
  - raw MP4s for the last `keep_raw_days` days (for feedback/reprocessing)

Deletes (for days older than keep_output_days, when set):
  - data/output/<date>.mp4         (the published master, ~550MB-850MB each)

Deletes (for days older than keep_raw_days):
  - data/raw/<date>/*.mp4  + lq/   (raw clips, large; also raw/weekly_<date>/, the
                                    weekly compilation's re-downloads)
  - data/work/<date>/<MEDIA_DIRS>    (renders and re-downloads, large: judge re-encodes,
                                    segments, trimmed sources, Shorts, ranking-Short and
                                    weekly re-downloads, Claude's contact sheets, crops)
  The record of every published clip is data/clip_log.jsonl (Twitch link, scores,
  transcript) plus the work JSONs - everything a themed compilation needs is metadata;
  the clips themselves are re-downloaded from Twitch when one is built.

Deletes (`cache_max_age_days`, when set): cached nameplate / countdown animations not used
for that long (regenerated on demand; ~10-40 MB each at 60 fps).

Never blocks the pipeline.
"""
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("pipeline.cleanup")

# media under work/<date>/ that only matters until that night's uploads are done
MEDIA_DIRS = ("api_tmp", "segments", "segments_np", "shorts", "trimmed", "ranking", "src",
              "prescreen_sheets", "crops", "vo")
MEDIA_EXT = (".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a")


def purge_dates(dates: list[str], current: str, uploaded) -> list[str]:
    """Dates whose media may go under `purge_after_upload`. Pure (tested).
    A daily date: on or before tonight's, with a CONFIRMED YouTube id. A weekly_ label: its
    upload is confirmed, or it is more than a week old (an unpublished weekly test render
    stays a week so the owner can watch it)."""
    out = []
    cur = datetime.strptime(current, "%Y-%m-%d")
    for d in dates:
        try:
            when = _dir_date(d)
        except ValueError:
            continue
        if when > cur:
            continue
        if d.startswith("weekly_"):
            if uploaded(d) or (cur - when).days > 7:
                out.append(d)
        elif uploaded(d):
            out.append(d)
    return out


def _purge(data: Path, label: str) -> int:
    """Every media file of one date: raw clips (+ lq), work renders, the master. The
    metadata stays: clips.json, work/*.json, thumbnails, meta.json, data/clip_log.jsonl."""
    freed = 0
    for f in [*(data / "raw" / label).glob("*.mp4"), data / "output" / f"{label}.mp4"]:
        if f.exists():
            freed += f.stat().st_size
            f.unlink()
    for d in [data / "raw" / label / "lq", *(data / "work" / label / s for s in MEDIA_DIRS)]:
        if d.exists():
            freed += _dir_size(d)
            shutil.rmtree(d)
    w = data / "work" / label
    if w.exists():
        for f in w.iterdir():                    # stray renders at the top of work/<date>
            if f.is_file() and f.suffix.lower() in MEDIA_EXT:
                freed += f.stat().st_size
                f.unlink()
    return freed


def _dir_date(name: str) -> datetime:
    """A daily folder's date, or a weekly compilation's (`weekly_<date>`); ValueError
    for anything else."""
    return datetime.strptime(name.removeprefix("weekly_"), "%Y-%m-%d")


def run(cfg: dict, state, date_label: str) -> None:
    cl = cfg.get("cleanup", {})
    if not cl.get("enabled", False):
        return
    try:
        _run(cfg, cl, date_label, state)
    except Exception as e:
        log.warning("cleanup failed (non-fatal): %s", e)


def _prune_outputs(data: Path, cl: dict, current: datetime, state) -> int:
    """Delete published masters older than `keep_output_days`. Returns bytes freed.

    Off by default (0 = keep forever): a fresh clone must never delete its own work,
    and on a machine with uploading disabled these files are the ONLY copy.

    Two conditions, both required. Age is the obvious one. The other is proof of
    publication — `state.uploaded_id(date)` returns the YouTube id only after a
    confirmed upload, so a master is removed solely when a copy demonstrably exists off
    this machine. A run that failed to upload, or one made with upload disabled, keeps
    its video regardless of age. The .meta.json always stays.
    """
    keep_days = int(cl.get("keep_output_days", 0) or 0)
    if keep_days <= 0 or state is None:
        return 0
    cutoff = current - timedelta(days=keep_days)
    freed = 0
    for mp4 in sorted((data / "output").glob("*.mp4")):
        try:
            dir_date = _dir_date(mp4.stem)
        except ValueError:
            continue
        if dir_date >= cutoff:
            continue
        video_id = state.uploaded_id(mp4.stem)
        if not video_id:
            log.info("cleanup: keeping %s - no confirmed upload for that date", mp4.name)
            continue
        size = mp4.stat().st_size
        mp4.unlink()
        freed += size
        # Log where the surviving copy is, so the deletion is traceable from the log.
        log.info("cleanup: removed master %s (%.0f MB) - published as youtu.be/%s",
                 mp4.name, size / 1_048_576, video_id)
    return freed


def _run(cfg: dict, cl: dict, date_label: str, state=None) -> None:
    data = Path(cfg["paths"]["data_abs"])
    keep_days = cl.get("keep_raw_days", 1)
    current = datetime.strptime(date_label, "%Y-%m-%d")
    cutoff = current - timedelta(days=keep_days)

    freed = 0

    # ── raw clips ──────────────────────────────────────────────────────────────
    for date_dir in sorted((data / "raw").iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            dir_date = _dir_date(date_dir.name)
        except ValueError:
            continue
        if dir_date >= cutoff:
            continue

        for mp4 in date_dir.glob("*.mp4"):
            freed += mp4.stat().st_size
            mp4.unlink()
        lq = date_dir / "lq"
        if lq.exists():
            freed += _dir_size(lq)
            shutil.rmtree(lq)
        log.info("cleanup: cleared raw clips for %s", date_dir.name)

    # ── work scratch dirs (all dates except current) ───────────────────────────
    for date_dir in sorted((data / "work").iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            dir_date = _dir_date(date_dir.name)
        except ValueError:
            continue
        if dir_date >= current:
            continue  # keep today's work intact

        for subdir in MEDIA_DIRS:
            d = date_dir / subdir
            if d.exists():
                freed += _dir_size(d)
                shutil.rmtree(d)

    freed += _prune_outputs(data, cl, current, state)
    freed += _prune_cache(data, int(cl.get("cache_max_age_days", 0) or 0))

    # `purge_after_upload` (owner 2026-10-28: "only the metadata should be kept"): once a
    # date's video is confirmed on YouTube, ALL its media goes - tonight's included, since
    # cleanup is the last stage and runs only after the uploads. A failed or disabled
    # upload keeps everything, so a retry still has its files.
    if cl.get("purge_after_upload", False) and state is not None:
        labels = sorted({p.name for sub in ("raw", "work") for p in (data / sub).iterdir()
                         if p.is_dir()} | {p.stem for p in (data / "output").glob("*.mp4")})
        for label in purge_dates(labels, date_label, state.uploaded_id):
            n = _purge(data, label)
            if n:
                freed += n
                log.info("cleanup: purged %s media (%.0f MB) - metadata kept", label,
                         n / 1_048_576)

    log.info("cleanup: freed %.1f MB", freed / 1_048_576)


def _prune_cache(data: Path, days: int) -> int:
    """Delete rendered animation caches unused for `days` (0 = off). Returns bytes."""
    if days <= 0:
        return 0
    import time
    cutoff = time.time() - days * 86400
    freed = 0
    for sub in ("nameplates", "countdown"):
        for f in (data / "cache" / sub).glob("*") if (data / "cache" / sub).exists() else []:
            if f.is_file() and f.stat().st_mtime < cutoff:
                freed += f.stat().st_size
                f.unlink()
    if freed:
        log.info("cleanup: pruned %.0f MB of unused nameplate/countdown cache", freed / 1_048_576)
    return freed


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
