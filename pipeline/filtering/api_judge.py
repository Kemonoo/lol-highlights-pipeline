"""Stage 3.5 — Gemini full-video judge (the quality layer local models can't provide).

Runs ONLY on clips the free local filter already kept (~7-12/day), so cost is pennies.
Answers the question local models fail at: "is the highlight the GAMEPLAY, and is the
play actually good?" — catches boring-kills, talk-driven hype, misplay deaths.

Per clip: shrink to 480p (inline API limit), send the whole video (with audio) to
Gemini, get structured judgment, cache it (api_partial.json — interruption-safe).
Decision rules (pure code, tunable in config):
    KEEP if clip_focus in (gameplay, reaction) and entertainment >= min_entertainment
    KEEP if play_quality >= min_play_quality
    else DROP
Kept clips are re-ranked by 0.4*entertainment + 0.6*play_quality and written back to
vlm_filtered.json (downstream stages unchanged). Also upgrades vlm_summary with
Gemini's description — the commentary stage gets real facts to work with.

Outputs: work/<date>/api_scored.json (audit), updated vlm_filtered.json.
Disabled automatically when GEMINI_API_KEY is missing.
"""
import base64
import json
import logging
import subprocess
import time
from pathlib import Path

import requests

from .vlm_filter import _parse_json

log = logging.getLogger("pipeline.api_judge")

PROMPT = """You are selecting clips for a daily "League of Legends best moments" YouTube video aimed at an English-speaking audience. Watch this Twitch clip (it has the streamer's audio).

Judge it honestly — most clips are boring and should be dropped. A clip is only worth keeping if a highlights viewer would enjoy it without any context: an impressive outplay, a chaotic teamfight, a multikill, a hilarious fail, or a genuinely funny gameplay moment. A streamer getting embarrassingly outplayed, destroyed, or dying in a comical way IS entertaining fail content — score its entertainment accordingly. Streamers talking, reacting to chat, queueing, or ordinary uneventful kills/deaths are NOT highlights.

CRITICAL: if there is NO real combat or meaningful play — the streamer is just walking around the map, sitting in base/fountain, recalling, farming quietly, singing, chatting, or AFK — it is NOT a highlight. Drop it with low play_quality AND low entertainment even when the audio is loud or the title is hype. Loud audio without on-screen action is not entertainment.

Answer ONLY JSON:
{
 "clip_focus": "gameplay" | "reaction" | "talk" | "other",  // what the clip is actually about
 "play_quality": 0,        // 0-10: how impressive/skillful/unusual the PLAY is
 "entertainment": 0,       // 0-10: fun for a highlights viewer (includes funny fails)
 "what_happens": "",       // 2 factual sentences describing the action
 "best_moment_s": 0        // second offset of the peak moment
}"""


def shrink(mp4: Path, out: Path, max_mb: float) -> Path | None:
    """480p re-encode so the clip fits the inline API limit. Returns path or None."""
    if not out.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp4),
             "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "30", "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(out)],
            capture_output=True,
        )
    if out.exists() and out.stat().st_size <= max_mb * 1024 * 1024:
        return out
    return None


def judge(mp4: Path, aj: dict) -> dict | None:
    model = aj["model"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={aj['api_key']}")
    body: dict = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "video/mp4",
                         "data": base64.b64encode(mp4.read_bytes()).decode()}},
        {"text": PROMPT},
    ]}]}
    last = None
    for attempt in range(4):
        try:
            resp = requests.post(url, json=body, timeout=180)
            if resp.status_code in (429, 500, 503):
                wait = 20 * (attempt + 1)
                # Log the actual Google error message so we can diagnose quota issues
                try:
                    err_detail = resp.json().get("error", {})
                    log.info("  API %s — %s (attempt %d/4, waiting %ss)",
                             resp.status_code,
                             err_detail.get("message", "no detail"),
                             attempt + 1, wait)
                except Exception:
                    log.info("  API %s — waiting %ss (attempt %d/4)",
                             resp.status_code, wait, attempt + 1)
                time.sleep(wait)
                last = RuntimeError(f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            return _parse_json(
                resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        except requests.RequestException as e:
            last = e
            time.sleep(10)
    raise last


def local_judge(clip: dict, aj: dict) -> None:
    """Score from free local signals when Gemini judging is unavailable.

    Action is king: confirmed kills / a multikill keep a clip. Loud audio WITHOUT
    motion is likely talk/singing/walking and does NOT rescue a no-combat clip, so
    boring clips still get dropped on quota-dead days (instead of fail-open KEEP)."""
    kills = max(int(clip.get("kills_confirmed") or 0), int(clip.get("kills_max") or 0))
    multi = bool(clip.get("multikill"))
    evt = bool(clip.get("eventlog_kill"))
    audio = float(clip.get("audio_score") or 0.0)
    motion = float(clip.get("motion_score") or 0.0)

    action = 10 if multi else min(9, kills * 3)
    if evt and action < 3:
        action = 3
    ent = min(10.0, 0.6 * action + 3.0 * audio + 18.0 * motion)
    clip["api_focus"] = "local"
    clip["api_what_happens"] = ""
    clip["api_play_quality"] = int(round(action))
    clip["api_entertainment"] = int(round(ent))
    clip["api_rank_score"] = round(0.45 * ent + 0.55 * action, 2)
    a_min = aj.get("fallback_audio_min", 0.72)
    m_min = aj.get("fallback_motion_min", 0.11)
    keep = (multi or kills >= 2 or (kills >= 1 and audio >= 0.5)
            or (audio >= a_min and motion >= m_min))
    clip["api_decision"] = "KEEP" if keep else "DROP"
    clip["api_reason"] = f"local_k{kills}{'m' if multi else ''}_a{audio:.2f}_mo{motion:.3f}"


def decide_api(clip: dict, aj: dict) -> None:
    focus = clip.get("api_focus", "other")
    if focus in ("unjudged", "local"):    # not a real Gemini verdict — score locally
        local_judge(clip, aj)
        return
    ent = clip.get("api_entertainment", 0)
    pq = clip.get("api_play_quality", 0)
    clip["api_rank_score"] = round(0.4 * ent + 0.6 * pq, 2)
    min_ent = (aj.get("min_entertainment_reaction", 7) if focus == "reaction"
               else aj.get("min_entertainment", 6))
    if focus in ("gameplay", "reaction") and ent >= min_ent:
        clip["api_decision"] = "KEEP"
    elif pq >= aj.get("min_play_quality", 7):
        clip["api_decision"] = "KEEP"
    else:
        clip["api_decision"] = "DROP"
    clip["api_reason"] = f"{focus}_ent{ent}_pq{pq}"


def run(cfg: dict, state, date_label: str) -> Path:
    aj = cfg.get("api_judge", {})
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label

    if not aj.get("enabled", False):
        log.info("api_judge disabled — keeping local selection")
        return work
    # (a missing api key is fine for clips already in the judgment cache)

    # Rebuild the local selection from vlm_scored.json (idempotent: api_judge
    # overwrites vlm_filtered.json, so it must not depend on it as input).
    scored_all = json.loads((work / "vlm_scored.json")
                            .read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    max_keep = cfg.get("vlm_filter", {}).get("max_keep", 12)
    clips = sorted((c for c in scored_all if c.get("decision") == "KEEP"),
                   key=lambda c: -c.get("keep_score", 0))[:max_keep]
    src = work / "vlm_filtered.json"
    raw_dir = data / "raw" / date_label
    tmp = work / "api_tmp"
    tmp.mkdir(exist_ok=True)

    partial_path = work / "api_partial_v2.json"
    cache = (json.loads(partial_path.read_text(encoding="utf-8"))
             if partial_path.exists() else {})

    judged = []
    consecutive_fails = 0
    for c in clips:
        if consecutive_fails >= 2:   # daily quota dead — stop hammering the API
            if not cache.get(c["id"]):
                local_judge(c, aj)
                judged.append(c)
                continue
        cached = cache.get(c["id"])
        if cached:
            c.update(cached)
            decide_api(c, aj)
            judged.append(c)
            continue

        mp4 = raw_dir / f"{c['id']}.mp4"
        if not mp4.exists():
            lp = c.get("local_path") or ""        # NB: Path("") is "." (exists!)
            if lp:
                mp4 = Path(lp)
        if not mp4.exists():
            continue
        if not aj.get("api_key"):
            log.warning("%s not in cache and no GEMINI_API_KEY — keeping unjudged", c["id"])
            local_judge(c, aj)
            judged.append(c)
            continue
        lq = raw_dir / "lq" / f"{c['id']}.mp4"   # prefilter's low-quality copy
        if lq.exists() and lq.stat().st_size <= aj.get("max_mb", 19) * 1024 * 1024:
            small = lq                            # already API-sized — skip re-encode
        else:
            small = shrink(mp4, tmp / f"{c['id']}.mp4", aj.get("max_mb", 19))
        if small is None:
            log.warning("%s too large even shrunk — keeping without API judgment", c["id"])
            local_judge(c, aj)
            judged.append(c)
            continue
        try:
            r = judge(small, aj)
            consecutive_fails = 0
            time.sleep(3)   # stay within free-tier RPM limits
        except Exception as e:
            consecutive_fails += 1
            if consecutive_fails >= 2:
                log.warning("API failing repeatedly (%s) — quota likely exhausted; "
                            "keeping remaining clips unjudged (retried next run)", e)
            else:
                log.warning("API judge failed for %s: %s — keeping clip", c["id"], e)
            r = None
        if r is None:  # failures not cached -> retried next run
            local_judge(c, aj)
            judged.append(c)
            continue
        c["api_focus"] = str(r.get("clip_focus", "other")).lower()
        c["api_entertainment"] = int(r.get("entertainment", 0) or 0)
        c["api_play_quality"] = int(r.get("play_quality", 0) or 0)
        c["api_what_happens"] = str(r.get("what_happens", ""))
        c["api_best_moment_s"] = int(r.get("best_moment_s", 0) or 0)
        cache[c["id"]] = {k: c[k] for k in
                          ("api_focus", "api_entertainment", "api_play_quality",
                           "api_what_happens", "api_best_moment_s")}
        partial_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        decide_api(c, aj)
        if c.get("api_what_happens"):
            c["vlm_summary"] = c["api_what_happens"]  # better facts for commentary
        judged.append(c)
        log.info("%-4s %-22s %s | %s", c["api_decision"], c["api_reason"],
                 c.get("title", "")[:38], c.get("api_what_happens", "")[:60])

    kept = sorted((c for c in judged if c.get("api_decision") == "KEEP"),
                  key=lambda c: -c.get("api_rank_score", 0))

    # ── duration-aware selection: fill to target length, order as a countdown ──
    v = cfg.get("video", {})
    ideal_s = v.get("target_minutes_ideal", 8) * 60
    max_s = v.get("target_minutes_max", 10) * 60
    total = sum(c.get("duration", 30) for c in kept)
    if total < ideal_s:
        fillers = sorted(
            (c for c in judged
             if c.get("api_decision") == "DROP"
             and c.get("api_focus") in ("gameplay", "reaction")
             and c.get("api_entertainment", 0) >= aj.get("filler_min_entertainment", 4)),
            key=lambda c: -c.get("api_rank_score", 0))
        for c in fillers:
            if total >= ideal_s:
                break
            c["api_decision"] = "KEEP"
            c["api_reason"] += "_FILLER"
            kept.append(c)
            total += c.get("duration", 30)
        if total < ideal_s:
            log.info("Only %.1f min of keepable content so far (ideal %d min)",
                     total / 60, ideal_s // 60)
    while total > max_s and len(kept) > 1:           # trim weakest
        cdrop = kept.pop()
        cdrop["api_decision"] = "DROP"
        cdrop["api_reason"] += "_OVER_LENGTH"
        total -= cdrop.get("duration", 30)

    if v.get("countdown_enabled", True):              # worst -> best, badges N..1
        kept.sort(key=lambda c: c.get("api_rank_score", 0))
        for i, c in enumerate(kept):
            c["countdown_rank"] = len(kept) - i
    log.info("Selection: %d clips, %.1f min", len(kept), total / 60)

    (work / "api_scored.json").write_text(
        json.dumps({"date": date_label, "clips": judged}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    src.write_text(
        json.dumps({"date": date_label, "clips": kept}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    log.info("API judge: %d -> %d kept", len(judged), len(kept))
    return work
