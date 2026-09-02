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
  - data/raw/<date>/*.mp4  + lq/   (raw clips, large)
  - data/work/<date>/api_tmp/       (re-encoded judge clips, large)
  - data/work/<date>/segments/      (assembled segment renders, large)
  - data/work/<date>/shorts/        (short renders, large — already uploaded)

Never blocks the pipeline.
"""
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("pipeline.cleanup")


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
            dir_date = datetime.strptime(mp4.stem, "%Y-%m-%d")
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
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
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
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if dir_date >= current:
            continue  # keep today's work intact

        for subdir in ("api_tmp", "segments", "shorts"):
            d = date_dir / subdir
            if d.exists():
                freed += _dir_size(d)
                shutil.rmtree(d)

    freed += _prune_outputs(data, cl, current, state)

    log.info("cleanup: freed %.1f MB", freed / 1_048_576)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
