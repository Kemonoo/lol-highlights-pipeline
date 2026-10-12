"""Stage 2 — Cheap local pre-filter (no API costs).

Uses keyword / tournament / audio-hype / motion scoring from
pipeline/filtering/scoring.py (hand-designed signals with hand-set thresholds; see
that module's docstring for what the labelled-clip regression did and did not do).
Operates on the already-downloaded MP4s (extracts audio via ffmpeg).
Also applies the broadcaster blacklist (co-streamers, watch parties).

Writes data/work/<date>/prefiltered.json with surviving clips, best-first.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from ..ingestion.fetch import download_clip
from .scoring import compute_audio_score, compute_motion_score, has_positive_keyword, is_tournament

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

    restatus(clip, pf_cfg)
    return clip


_SCORED = {"KEYWORD_PASS", "AUDIO_EXCLUDE", "MOTION_EXCLUDE", "AUDIO_PASS",
           "BORDERLINE_PASS"}


def restatus(clip: dict, pf_cfg: dict) -> None:
    """Pass/exclude from the cached scores — pure, so threshold changes apply to cached
    clips too. `quiet_gate: false` stops excluding quiet clips: the owner rated clips
    good that it dropped (supertf/tyler1 at audio 0.11, 2026-10-11), and loudness
    correlated negatively with the judge. They then compete on rank instead."""
    if clip.get("prefilter_status") not in _SCORED | {None}:   # None = just scored
        return
    a, m = clip.get("audio_score", 0.0), clip.get("motion_score", 0.0)
    if clip.get("keyword"):
        st = "KEYWORD_PASS"
    elif a < pf_cfg["audio_exclude"] and pf_cfg.get("quiet_gate", True):
        st = "AUDIO_EXCLUDE"
    elif m < pf_cfg["motion_exclude"]:
        st = "MOTION_EXCLUDE"
    elif a >= pf_cfg["audio_pass"]:
        st = "AUDIO_PASS"
    else:
        st = "BORDERLINE_PASS"
    clip["prefilter_status"] = st


def eligible(clip: dict, pf: dict, blacklist: set) -> bool:
    """Language + blacklist gate, shared with fetch (`twitch.eligible_only`). Pure.
    `language_allow_streamers` pass whatever their channel language: owner reviews
    (2026-10-12) rated 3 of 26 ja/zh clips good or ok vs 39 of 97 others, and in a
    no-ban replay ja/zh took 41 of 60 judge slots for 2 real outplays - but 36 recurring
    streamers make 86% of those clips, so the few good ones are easy to list."""
    who = (clip.get("broadcaster_name", "") or "").lower()
    if who in blacklist:
        return False
    if who in {x.lower() for x in pf.get("language_allow_streamers", []) or []}:
        return True
    lang = (clip.get("language", "") or "").lower()
    include = {l.lower() for l in pf.get("include_languages", [])}
    exclude = {l.lower() for l in pf.get("exclude_languages", [])}
    return lang in include if include else lang not in exclude


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    raw = json.loads((data / "raw" / date_label / "clips.json").read_text(encoding="utf-8"))
    pf = cfg["prefilter"]

    blacklist = {b.lower() for b in cfg.get("blacklist", {}).get("broadcasters", [])}
    clips = [c for c in raw["clips"]
             if c.get("broadcaster_name", "").lower() not in blacklist]
    if len(clips) < len(raw["clips"]):
        log.info("Blacklist removed %d clips", len(raw["clips"]) - len(clips))

    # Language gating on the Twitch broadcaster language, before any download.
    # include_languages (allowlist) wins when set — e.g. ["en"] for an English-only
    # channel; otherwise exclude_languages drops specific ones (ja/zh). Twitch tags are
    # imperfect, so the expansion loop backfills if this thins the pool, and transcribe.py
    # later detects the *actual* spoken language.
    include_langs = {l.lower() for l in pf.get("include_languages", [])}
    exclude_langs = {l.lower() for l in pf.get("exclude_languages", [])}
    if include_langs or exclude_langs:
        before = len(clips)

        clips = [c for c in clips if eligible(c, pf, set())]
        if len(clips) < before:
            rule = (f"only {','.join(sorted(include_langs))}" if include_langs
                    else f"exclude {','.join(sorted(exclude_langs))}")
            log.info("Language filter (%s) removed %d clips", rule, before - len(clips))

    work = data / "work" / date_label
    work.mkdir(parents=True, exist_ok=True)

    # One clip per moment, and no moment we already published (filtering/moments.py).
    # Before any download, so duplicates cost nothing.
    from .moments import dedupe, published_clips, rank_score, view_percentile
    if pf.get("dedupe_moments", False):
        before = len(clips)
        pub = published_clips(data, date_label, int(pf.get("republish_lookback_days", 14)))
        everyone = clips
        clips = dedupe(clips, pub, float(pf.get("moment_window_s", 90)))
        kept_ids = {c["id"] for c in clips}
        (work / "moments.json").write_text(json.dumps(   # for the review queue's trace
            {c["id"]: {"status": c.get("prefilter_status"), "duplicate_of": c.get("duplicate_of")}
             for c in everyone if c["id"] not in kept_ids}), encoding="utf-8")
        log.info("Moments: %d clips -> %d (same moment clipped twice, or published in "
                 "the last %s days)", before, len(clips), pf.get("republish_lookback_days", 14))
    view_percentile(clips)

    partial_path = work / "prefilter_partial.json"
    cache = (json.loads(partial_path.read_text(encoding="utf-8"))
             if partial_path.exists() else {})

    lq_dir = data / "raw" / date_label / "lq"
    scored = []
    for i, c in enumerate(clips, 1):
        cached = cache.get(c["id"])
        if cached:
            c.update(cached)
            restatus(c, pf)
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

    # streamers whose clips are reliably good get a head start into the judge's queue
    boost = {k.lower(): float(v) for k, v in (pf.get("boost_streamers") or {}).items()}
    for c in passed:
        c["rank_score"] = round(rank_score(c, float(pf.get("audio_weight", 0.10)))
                                + boost.get(c.get("broadcaster_name", "").lower(), 0.0), 3)
    if pf.get("rank", "loudness") == "signals":
        # moment clusters, self-clips, view rank, title keyword; loudness a small term
        passed.sort(key=lambda c: -c["rank_score"])
    else:
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
