"""Stage 12 — Free disk space after the day's video is done.

Keeps:
  - data/raw/<date>/clips.json  (metadata, tiny)
  - data/work/<date>/*.json     (all work JSONs, tiny)
  - data/output/                (final videos and meta)
  - raw MP4s for the last `keep_raw_days` days (for feedback/reprocessing)

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
        _run(cfg, cl, date_label)
    except Exception as e:
        log.warning("cleanup failed (non-fatal): %s", e)


def _run(cfg: dict, cl: dict, date_label: str) -> None:
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

    log.info("cleanup: freed %.1f MB", freed / 1_048_576)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
