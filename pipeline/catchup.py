"""Which dates still need a video? For nights the PC was off (owner, 2026-09-30).

The scheduled task normally renders yesterday. When a night is missed (PC off, or the
owner postponed a late start), the next run catches up: every date after the newest
FINISHED one, up to yesterday, oldest first, capped at `schedule.catch_up_days`.
"Finished" = uploaded (state.json) when upload is on, else a master + meta.json exist.
A fresh clone has nothing finished, so it just renders yesterday — as before.

Spacing the uploads so a catch-up night doesn't dump three videos at once lives in
publishing/upload.py (`upload.min_gap_hours`, scheduled publishing).

    python -m pipeline.catchup [--config overlay.yaml]    # prints one date per line
"""
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def missed_dates(yesterday: date, finished: set[str], days: int) -> list[str]:
    """Dates after the newest finished one, through `yesterday`, at most `days`, oldest
    first. Always includes yesterday when it isn't finished. Pure (tested)."""
    days = max(int(days), 1)
    done = {d for d in finished if d <= yesterday.isoformat()}
    last = max(done) if done else None
    out = []
    for k in range(days - 1, -1, -1):
        d = (yesterday - timedelta(days=k)).isoformat()
        if d in done or (last is not None and d <= last):
            continue
        if last is None and k > 0:          # nothing ever finished: yesterday only
            continue
        out.append(d)
    return out


def finished_dates(cfg: dict, state) -> set[str]:
    if cfg.get("upload", {}).get("enabled", False):
        return state.uploaded_dates()
    out = Path(cfg["paths"]["data_abs"]) / "output"
    return {p.name[:10] for p in out.glob("*.meta.json")} if out.exists() else set()


def main() -> int:
    ap = argparse.ArgumentParser(description="print the dates the daily run should render")
    ap.add_argument("--config", default="")
    args = ap.parse_args()
    from .config import load_config
    from .state import State
    cfg = load_config(overlay=args.config or None)
    state = State(cfg["paths"]["data_abs"])
    tz = ZoneInfo(cfg["twitch"]["timezone"])
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    days = int(cfg.get("schedule", {}).get("catch_up_days", 3))
    for d in missed_dates(yesterday, finished_dates(cfg, state), days):
        print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
