"""Stage 3.5 — Gemini full-video judge (the quality layer local models can't provide).

Runs ONLY on clips the free local filter already kept (~7-12/day), so cost is pennies.
Answers the question local models fail at: "is the highlight the GAMEPLAY, and is the
play actually good?" — catches boring-kills, talk-driven hype, misplay deaths.

Per clip: shrink to `shrink_height` (720p by default), send the whole video with its
audio to the `judge` role, get a schema-constrained verdict, cache it
(api_partial_v3.json — interruption-safe). Clips too large to inline go through the
provider's file-upload path rather than being degraded further.
Decision rules (pure code, tunable in config):
    KEEP if clip_focus in (gameplay, reaction) and entertainment >= min_entertainment
    KEEP if play_quality >= min_play_quality
    else DROP
Kept clips are re-ranked by 0.4*entertainment + 0.6*play_quality and written back to
vlm_filtered.json (downstream stages unchanged). Also upgrades vlm_summary with
Gemini's description — the commentary stage gets real facts to work with.

Outputs: work/<date>/api_scored.json (audit), updated vlm_filtered.json.
Degrades to local_judge() when no video-capable provider is configured (no API key,
quota exhausted, or the role points somewhere that can't ingest video), so the pipeline
still finishes — with a less well-curated selection.
"""
import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("pipeline.api_judge")

# Structured output: the judge's answer drives selection and ordering, so a malformed
# response costs a clip. The provider maps this onto native JSON-schema decoding.
#
# `required` is load-bearing, not decoration. Without it Gemini treats every property as
# optional and intermittently returns only {clip_focus, play_quality, what_happens} —
# measured at 57% of 233 cached verdicts, and 4/8 in a controlled retest that dropped to
# 0/8 once this list was added. The two fields it omits are always `entertainment` and
# `best_moment_s`, i.e. exactly the ones selection and ranking depend on.
SCHEMA = {
    "type": "object",
    "properties": {
        "clip_focus": {"type": "string",
                       "enum": ["gameplay", "reaction", "talk", "other"]},
        "play_quality": {"type": "integer"},
        "entertainment": {"type": "integer"},
        "what_happens": {"type": "string"},
        "best_moment_s": {"type": "number"},
    },
    "required": ["clip_focus", "play_quality", "entertainment",
                 "what_happens", "best_moment_s"],
}

# Scores the verdict is worthless without. A response missing any of them is not a
# verdict of zero — it is no verdict at all (see parse_verdict).
_REQUIRED_SCORES = ("play_quality", "entertainment")

# A bad/disabled key does not heal partway through a run, so stop calling immediately
# rather than burning the retry ladder on every remaining clip. (Measured: 401s and
# "prepayment credits are depleted" 429s silently downgraded 14 consecutive August days
# to local_judge — every clip, no alarm.)
_DEAD_KEY_STATUS = {401, 403}


def classify_failure(e: Exception) -> str:
    """'refusal' (this clip only) | 'dead' (stop calling) | 'transient' (count it).

    The old code counted every exception the same way, so two network timeouts — or two
    clips the model declined on content grounds — silently switched the REST OF THE RUN
    to local scoring. A per-clip refusal says nothing about the next clip.
    """
    if getattr(e, "reason", ""):          # RECITATION / SAFETY: about this clip's content
        return "refusal"
    if getattr(e, "status", None) in _DEAD_KEY_STATUS:
        return "dead"
    return "transient"


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


def shrink(mp4: Path, out: Path, aj: dict) -> Path | None:
    """Re-encode to keep the payload manageable. Returns the path, or None if even
    the shrunk clip is over budget (the provider then routes it via the Files API).

    Height and CRF are config now: the old hard-coded 480p/CRF-30 existed only to fit
    the 20MB inline cap Google raised to 100MB in Jan 2026, and it was costing the
    judge real detail on exactly the fast teamfights it is meant to grade."""
    height = int(aj.get("shrink_height", 720))
    crf = str(aj.get("shrink_crf", 28))
    if not out.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp4),
             "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", crf, "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(out)],
            capture_output=True,
        )
    if not out.exists():
        return None
    if out.stat().st_size > float(aj.get("max_mb", 90)) * 1024 * 1024:
        log.info("  %s still %.0fMB after shrink - uploading instead of inlining",
                 out.name, out.stat().st_size / 1024 / 1024)
    return out


def judge(mp4: Path, provider) -> dict | None:
    """Send the whole clip (with audio) to the judge role and parse its verdict."""
    return provider.complete_json(PROMPT, video=mp4.read_bytes(), schema=SCHEMA)


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


def parse_verdict(r: dict | None) -> dict | None:
    """Map a judge response onto api_* fields, or None if it isn't a usable verdict.

    The old code read every score with `.get(field, 0)`. On a 0-10 scale 0 is not a
    neutral "unknown" — it is the harshest score there is, so a field the model simply
    never sent became "maximally boring" and was indistinguishable from a real verdict.
    That silently dropped clips (the ent>=6 keep path can never fire at 0) and deflated
    api_rank_score by up to 4 points on clips it did keep, scrambling the countdown.

    Returning None instead routes the clip to local_judge() — the same path used when
    the API is unreachable, which is the honest description of what happened.
    """
    if not r:
        return None
    if any(r.get(k) is None for k in _REQUIRED_SCORES):
        return None
    return {
        "api_focus": str(r.get("clip_focus", "other")).lower(),
        "api_entertainment": int(r["entertainment"]),
        "api_play_quality": int(r["play_quality"]),
        "api_what_happens": str(r.get("what_happens", "")),
        # best_moment_s only positions the replay/Shorts trim; 0 is a fine default and
        # callers already treat it as "no timestamp".
        "api_best_moment_s": int(r.get("best_moment_s") or 0),
    }


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
    if focus in ("gameplay", "reaction") and ent >= min_ent or pq >= aj.get("min_play_quality", 7):
        clip["api_decision"] = "KEEP"
    else:
        clip["api_decision"] = "DROP"
    clip["api_reason"] = f"{focus}_ent{ent}_pq{pq}"


def run(cfg: dict, state, date_label: str) -> Path:
    aj = cfg.get("api_judge", {})
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label

    if not aj.get("enabled", False):
        log.info("api_judge disabled - keeping local selection")
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

    partial_path = work / "api_partial_v3.json"
    cache = (json.loads(partial_path.read_text(encoding="utf-8"))
             if partial_path.exists() else {})

    # No key, no Gemini, misconfigured role — all the same to this stage: clips already
    # in the cache still resolve, and everything else falls back to local scoring. The
    # pipeline is meant to finish without any API access, just less well curated.
    from ..providers import ProviderUnavailable, get_provider
    try:
        provider = get_provider(cfg, "judge")
        provider.require(video=True)
    except ProviderUnavailable as e:
        log.warning("no video judge available (%s) — scoring uncached clips locally", e)
        provider = None

    judged = []
    consecutive_fails = 0
    refused = 0
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
        if provider is None:
            log.warning("%s not in cache and no judge available - scoring locally", c["id"])
            local_judge(c, aj)
            judged.append(c)
            continue
        max_bytes = float(aj.get("max_mb", 90)) * 1024 * 1024
        lq = raw_dir / "lq" / f"{c['id']}.mp4"   # prefilter's low-quality copy
        if lq.exists() and lq.stat().st_size <= max_bytes:
            small = lq                            # already API-sized — skip re-encode
        else:
            small = shrink(mp4, tmp / f"{c['id']}.mp4", aj)
        if small is None:
            log.warning("%s could not be prepared - keeping without API judgment", c["id"])
            local_judge(c, aj)
            judged.append(c)
            continue
        try:
            r = judge(small, provider)
            consecutive_fails = 0
            time.sleep(3)   # stay within free-tier RPM limits
        except Exception as e:
            kind = classify_failure(e)
            if kind == "refusal":
                # The model declined THIS clip's content. Says nothing about the next
                # one, so it must not count toward the stop-calling threshold.
                refused += 1
                log.warning("%s: judge refused this clip (%s) - scoring locally",
                            c["id"], getattr(e, "reason", "") or e)
            elif kind == "dead":
                consecutive_fails = max(consecutive_fails, 2)   # stop immediately
                log.error("judge credentials rejected (%s) — the paid selection pass is "
                          "OFF for this run; remaining clips scored locally", e)
            else:
                consecutive_fails += 1
                if consecutive_fails >= 2:
                    log.warning("API failing repeatedly (%s) — quota likely exhausted; "
                                "keeping remaining clips unjudged (retried next run)", e)
                else:
                    log.warning("API judge failed for %s: %s - keeping clip", c["id"], e)
            r = None
        verdict = parse_verdict(r)
        if verdict is None:  # no answer / incomplete answer — not cached, retried next run
            if r:
                log.warning("%s: judge omitted %s - scoring locally", c["id"],
                            ", ".join(k for k in _REQUIRED_SCORES if r.get(k) is None))
            local_judge(c, aj)
            judged.append(c)
            continue
        c.update(verdict)
        cache[c["id"]] = dict(verdict)
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
    # Degradation has to be LOUD. A dead key or exhausted quota just routes every clip
    # to local_judge(), which keeps publishing a plausible-looking video with no taste
    # pass at all — that ran for 14 consecutive August days (08-05..08-19, every clip,
    # 401s and depleted prepayment credits) and nothing in the log said so at a glance.
    scored_locally = sum(1 for c in judged if c.get("api_focus") == "local")
    if scored_locally:
        say = log.error if scored_locally == len(judged) else log.warning
        say("JUDGE DEGRADED: %d/%d clips scored locally%s — selection quality is not "
            "what it looks like. Check the key/quota above.", scored_locally, len(judged),
            f" ({refused} refused on content)" if refused else "")
    log.info("API judge: %d -> %d kept (%d judged by API, %d local)",
             len(judged), len(kept), len(judged) - scored_locally, scored_locally)
    return work
