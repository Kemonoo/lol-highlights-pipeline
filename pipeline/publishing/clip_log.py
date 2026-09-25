"""Stage 13 — permanent log of every clip that made it into a published video.

One JSON line per (date, clip) in `data/clip_log.jsonl`: the Twitch link and title, the
streamer, everything the filters measured (audio/motion, VLM kill detection, the judge's
scores and description of what happens, its best moment), the English transcript, where
the clip sat in the countdown, and the YouTube ids of the video and of the Short cut
from it, when there was one.

Why: the work/ folders are the only record of what a day contained, and they are scattered
across dozens of folders in half a dozen formats. Compilation formats that reuse past clips
("top 5 pentakills of the week", "best of <streamer> this month", "outplays of the month")
need to query ALL of them at once — this file is that index. It is text only, a few KB
per day, so it is kept forever and never pruned by cleanup (the clips themselves can be
re-downloaded from the Twitch url while Twitch keeps them).

Idempotent: a day's lines are rebuilt from its work files and replace any earlier lines
for that date, so the bat's crash-retry (or a re-render) never duplicates a clip. Runs
after upload + shorts so the YouTube ids are known; runs again harmlessly every night.

    python -m pipeline.publishing.clip_log --backfill     # every date that has a video
"""
import argparse
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("pipeline.clip_log")

LOG_NAME = "clip_log.jsonl"

# clip fields that are machine-local or bulky and mean nothing outside this machine
_DROP = {"local_path", "kill_frames_detail", "crops"}


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8").rstrip("\x00"))
    except (OSError, ValueError):
        return default


def build_entries(date_label: str, chapters: list, clips: list, *,
                  transcripts: dict | None = None, shorts_done: dict | None = None,
                  video: dict | None = None, episode: int | None = None) -> list[dict]:
    """Pure: one record per chapter (= per clip in the published video), in video order.

    `clips` supplies the filter data (vlm_filtered/api_scored rows); a chapter whose clip
    is missing from it is still logged with what the chapter itself knows.
    """
    by_id = {c.get("id"): c for c in clips or [] if isinstance(c, dict)}
    video = video or {}
    out = []
    for pos, ch in enumerate(chapters or [], 1):
        cid = ch.get("clip_id")
        c = {k: v for k, v in (by_id.get(cid) or {}).items() if k not in _DROP}
        tr = (transcripts or {}).get(cid) or {}
        short = (shorts_done or {}).get(cid) or {}
        rec = {
            "date": date_label,
            "episode": episode,
            "video_youtube_id": video.get("youtube_id"),
            "video_title": video.get("title"),
            "position": pos,
            "countdown_rank": ch.get("rank"),
            "start_in_video_s": ch.get("start"),
            "clip_id": cid,
            "clip_url": c.get("url") or (f"https://clips.twitch.tv/{cid}" if cid else None),
            "twitch_title": c.get("title", ch.get("title")),
            "broadcaster": c.get("broadcaster_name", ch.get("broadcaster")),
            "broadcaster_url": ch.get("broadcaster_url"),
            "speech_lang": tr.get("lang"),
            "speech_en": tr.get("text"),
            "short_youtube_id": short.get("youtube_id"),
            "short_format": short.get("format"),     # "clip" | "ranking:<key>" (A/B)
            "filter": c,
        }
        out.append(rec)
    return out


def _video_for(state, date_label: str) -> dict:
    vids = (getattr(state, "_d", {}) or {}).get("videos", []) if state else []
    for v in vids:
        if v.get("date") == date_label and v.get("youtube_id"):
            return v
    return {}


def entries_for_date(cfg: dict, state, date_label: str) -> list[dict]:
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    chapters = _read(work / "chapters.json", [])
    if not chapters:
        return []
    clips = []
    for name in ("api_scored.json", "vlm_filtered.json", "vlm_scored.json"):
        doc = _read(work / name)
        if doc:
            rows = doc.get("clips", []) if isinstance(doc, dict) else doc
            known = {c.get("id") for c in clips}
            clips += [c for c in rows if isinstance(c, dict) and c.get("id") not in known]
    episodes = (getattr(state, "_d", {}) or {}).get("episodes", {}) if state else {}
    return build_entries(
        date_label, chapters, clips,
        transcripts=_read(work / "transcripts.json", {}),
        shorts_done=_read(work / "shorts" / "done.json", {}),
        video=_video_for(state, date_label),
        episode=episodes.get(date_label))


def upsert(path: Path, date_label: str, entries: list[dict]) -> int:
    """Replace `date_label`'s lines in the log with `entries`; keeps the file date-sorted.

    Fields the old line had and the rebuild lacks are carried over: cleanup deletes
    work/<date>/shorts/ (and with it the Short's YouTube id) the same night, so a later
    rebuild of that date must not erase what the first one recorded."""
    lines, old = [], {}
    if path.exists():
        for ln in path.read_text(encoding="utf-8").rstrip("\x00").splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if rec.get("date") != date_label:
                lines.append(rec)
            else:
                old[rec.get("clip_id")] = rec
    for e in entries:
        for k, v in (old.get(e.get("clip_id")) or {}).items():
            if e.get(k) in (None, "", {}) and v not in (None, "", {}):
                e[k] = v
    lines += entries
    lines.sort(key=lambda r: (r.get("date") or "", r.get("position") or 0))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
                   encoding="utf-8")
    os.replace(tmp, path)
    return len(entries)


def run(cfg: dict, state, date_label: str) -> Path | None:
    if not cfg.get("clip_log", {}).get("enabled", True):
        return None
    path = Path(cfg["paths"]["data_abs"]) / LOG_NAME
    try:
        n = upsert(path, date_label, entries_for_date(cfg, state, date_label))
        log.info("clip log: %d clip(s) for %s -> %s", n, date_label, path.name)
    except Exception as e:                        # a log must never cost an upload
        log.warning("clip log failed (non-fatal): %s", e)
    return path


def backfill(cfg: dict, state) -> int:
    data = Path(cfg["paths"]["data_abs"])
    path = data / LOG_NAME
    total = 0
    for d in sorted(p.name for p in (data / "work").iterdir() if p.is_dir()):
        ents = entries_for_date(cfg, state, d)
        if ents:
            total += upsert(path, d, ents)
    return total


if __name__ == "__main__":
    from ..config import load_config
    from ..state import State

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true", help="log every saved date")
    ap.add_argument("--date", help="log one date")
    ap.add_argument("--config", help="overlay yaml(s), comma-separated")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(overlay=a.config.split(",") if a.config else None)
    st = State(cfg["paths"]["data_abs"])
    if a.backfill:
        print(f"logged {backfill(cfg, st)} clip(s)")
    elif a.date:
        run(cfg, st, a.date)
