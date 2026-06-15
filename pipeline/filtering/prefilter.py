"""Stage 2 — Cheap local pre-filter (no API costs).

Uses keyword / tournament / audio-hype / motion scoring from
pipeline/filtering/scoring.py (functions originally trained on labeled clips).
Operates on the already-downloaded MP4s (extracts audio via ffmpeg).
Also applies the broadcaster blacklist (co-streamers, watch parties).

Writes data/work/<date>/prefiltered.json with surviving clips, best-first.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from .scoring import compute_audio_score, compute_motion_score, has_positive_keyword, is_tournament
from ..ingestion.fetch import download_clip

log = logging.getLogger("pipeline.prefilter")


def _extract_audio(mp4: Path) -> str:
    out = Path(tempfile.gettempdir()) / (mp4.stem + "_pf.mp3")
    cmd = ["ffmpeg", "-y", "-i", str(mp4), "-vn", "-b:a", "96k", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return str(out)
    except Exception as e:
        log.warning("audio extract failed for %s: %s", mp4.name, e)
        return ""


def score_clip(clip: dict, pf_cfg: dict, lq_dir: Path | None = None) -> dict:
    """Adds audio_score / motion_score / prefilter_status to the clip dict.

    Downloads a small LOW-QUALITY copy on demand for scoring (worst format,
    ~1-4MB) — full quality is only fetched later for filter survivors. Title-level
    exclusions happen before any download, so excluded clips cost nothing."""
    title = clip.get("title", "")
    kw_match, kw = has_positive_keyword(title)
    clip["keyword"] = kw

    if is_tournament(title):
        clip["prefilter_status"] = "TOURNAMENT_EXCLUDE"
        return clip

    lp = clip.get("local_path") or ""
    mp4 = Path(lp) if lp else None     # NB: Path("") is "." and .exists() is True!
    if (mp4 is None or not mp4.exists()) and lq_dir is not None:
        mp4 = lq_dir / f"{clip['id']}.mp4"
        if not mp4.exists() and not download_clip(clip.get("url", ""), mp4,
                                                  quality="worst"):
            clip["prefilter_status"] = "NO_FILE"
            return clip
    if mp4 is None or not mp4.exists():
        clip["prefilter_status"] = "NO_FILE"
        return clip

    mp3 = _extract_audio(mp4)
    a = compute_audio_score(mp3)
    if mp3:
        Path(mp3).unlink(missing_ok=True)
    m = compute_motion_score(str(mp4))
    clip["audio_score"] = round(float(a), 3)
    clip["motion_score"] = round(float(m), 4)

    if kw_match:
        clip["prefilter_status"] = "KEYWORD_PASS"
    elif a < pf_cfg["audio_exclude"]:
        clip["prefilter_status"] = "AUDIO_EXCLUDE"
    elif m < pf_cfg["motion_exclude"]:
        clip["prefilter_status"] = "MOTION_EXCLUDE"
    elif a >= pf_cfg["audio_pass"]:
        clip["prefilter_status"] = "AUDIO_PASS"
    else:
        clip["prefilter_status"] = "BORDERLINE_PASS"
    return clip


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    raw = json.loads((data / "raw" / date_label / "clips.json").read_text(encoding="utf-8"))
    pf = cfg["prefilter"]

    blacklist = {b.lower() for b in cfg.get("blacklist", {}).get("broadcasters", [])}
    clips = [c for c in raw["clips"]
             if c.get("broadcaster_name", "").lower() not in blacklist]
    if len(clips) < len(raw["clips"]):
        log.info("Blacklist removed %d clips", len(raw["clips"]) - len(clips))

    work = data / "work" / date_label
    work.mkdir(parents=True, exist_ok=True)
    partial_path = work / "prefilter_partial.json"
    cache = (json.loads(partial_path.read_text(encoding="utf-8"))
             if partial_path.exists() else {})

    lq_dir = data / "raw" / date_label / "lq"
    scored = []
    for i, c in enumerate(clips, 1):
        cached = cache.get(c["id"])
        if cached:
            c.update(cached)
        else:
            score_clip(c, pf, lq_dir)
            if c["prefilter_status"] != "NO_FILE":   # retry missing files next run
                cache[c["id"]] = {k: c[k] for k in
                                  ("prefilter_status", "keyword", "audio_score", "motion_score")
                                  if k in c}
                partial_path.write_text(json.dumps(cache), encoding="utf-8")
        if i % 25 == 0:
            log.info("  prefilter progress: %d/%d", i, len(clips))
        scored.append(c)
    passed = [c for c in scored if c["prefilter_status"].endswith("PASS")]

    # rank: keyword first, then audio score, then views
    passed.sort(key=lambda c: (
        c["prefilter_status"] != "KEYWORD_PASS",
        -c.get("audio_score", 0),
        -c.get("view_count", 0),
    ))
    kept = passed[: pf["max_keep"]]
    log.info("Prefilter %s: %d scored -> %d passed -> %d kept",
             date_label, len(scored), len(passed), len(kept))

    (work / "prefiltered.json").write_text(
        json.dumps({"date": date_label, "clips": kept}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return work
