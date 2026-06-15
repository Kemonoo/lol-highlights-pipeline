"""Stage 0 — Viewer feedback loop (runs at the start of each daily run).

Reads comments on recent uploads, maps clip-number mentions ("#3", "number 3",
"clip 3") back to actual clips via that day's chapters, classifies sentiment with
Gemini flash-lite, and aggregates. Deliberately conservative:

  - a signal becomes ACTIONABLE only when >= feedback.min_agreement comments agree
    (one person's opinion is noise; repetition is signal)
  - this stage NEVER edits the pipeline itself — it appends to a cumulative log
    (data/feedback/log.jsonl) and writes human-readable proposals
    (data/feedback/proposals.md) with suggested, config-level changes

Optional last mile (config auto_update.enabled): invoke the Claude Code CLI
headlessly on the proposals. Claude reads CLAUDE.md (the project's distilled context)
fresh each run and is instructed to apply ONLY config-level changes (blacklist,
thresholds, prompt wording) — never code — and leave a note in the proposals file.
Disabled by default; requires `claude` CLI installed and authenticated.

Never crashes the pipeline: all errors are logged and swallowed (videos must ship
even when feedback reading fails).
"""
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pipeline.feedback")

REF_RE = re.compile(r"(?:#|number\s+|clip\s+|no\.?\s*)(\d{1,2})\b", re.I)

CLASSIFY_PROMPT = """These are YouTube comments on a League of Legends highlights video.
Some reference specific clips by number. For each comment, answer what it says about the
video or a clip. Be literal — do not infer beyond the text.

Comments (JSON): {comments}

Answer ONLY JSON, one entry per comment, same order:
{{"results": [{{"clip_ref": 3 | null, "sentiment": "positive"|"negative"|"neutral",
   "topic": "clip_quality"|"commentary"|"music"|"editing"|"other",
   "summary": "<=10 words"}}]}}"""


def _classify(comments: list[str], cfg: dict) -> list[dict]:
    """One batched Gemini call; [] on any failure."""
    from .commentary import _gemini_text
    cm = {"gemini_model": cfg.get("commentary", {}).get("gemini_model", "gemini-2.0-flash-lite"),
          "gemini_api_key": cfg.get("api_judge", {}).get("api_key", "")}
    if not cm["gemini_api_key"]:
        return []
    try:
        r = _gemini_text(CLASSIFY_PROMPT.format(
            comments=json.dumps(comments[:60], ensure_ascii=False)), cm)
        if r and isinstance(r.get("results"), list):
            return r["results"]
    except Exception as e:
        log.warning("comment classification failed: %s", e)
    return []


def _fetch_comments(yt, video_id: str, limit: int = 100) -> list[str]:
    out = []
    try:
        resp = yt.commentThreads().list(
            part="snippet", videoId=video_id, maxResults=min(limit, 100),
            textFormat="plainText", order="relevance").execute()
        for item in resp.get("items", []):
            s = item["snippet"]["topLevelComment"]["snippet"]
            out.append(s.get("textDisplay", ""))
    except Exception as e:
        log.info("no comments readable for %s (%s)", video_id, e)
    return [c for c in out if c.strip()]


def _load_owner_notes(fb_dir: Path, seen: set) -> list[dict]:
    """Return unprocessed entries from owner_notes.jsonl (written by owner_feedback UI)."""
    f = fb_dir / "owner_notes.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            n = json.loads(line)
            if n.get("note") and n["note"][:300] not in seen:
                out.append(n)
        except Exception:
            pass
    return out


def _chapters_for(data: Path, date: str) -> dict[int, dict]:
    f = data / "work" / date / "chapters.json"
    if not f.exists():
        return {}
    chapters = json.loads(f.read_text(encoding="utf-8").rstrip("\x00"))
    return {ch["rank"]: ch for ch in chapters if ch.get("rank")}


def run(cfg: dict, state, date_label: str) -> None:
    fb = cfg.get("feedback", {})
    if not fb.get("enabled", True):
        log.info("feedback disabled")
        return
    try:
        _run(cfg, state, fb)
    except Exception as e:                          # never block the daily video
        log.warning("feedback stage failed (continuing pipeline): %s", e)


def _run(cfg: dict, state, fb: dict) -> None:
    from .config import ROOT
    data = Path(cfg["paths"]["data_abs"])
    fb_dir = data / "feedback"
    fb_dir.mkdir(exist_ok=True)
    log_f = fb_dir / "log.jsonl"

    seen: set[str] = set()
    if log_f.exists():
        for line in log_f.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["comment"])
            except Exception:
                pass

    signals: list[dict] = []

    # ── YouTube comments ──────────────────────────────────────────────────────
    videos = [v for v in state._d.get("videos", []) if v.get("youtube_id")]
    if not videos:
        log.info("feedback: no uploaded videos yet")
    else:
        try:
            from .upload import _credentials
            from googleapiclient.discovery import build
            yt = build("youtube", "v3", credentials=_credentials(ROOT, data))
            for v in videos[-fb.get("videos_to_check", 5):]:
                comments = [c for c in _fetch_comments(yt, v["youtube_id"])
                            if c not in seen]
                if not comments:
                    continue
                ranks = _chapters_for(data, v.get("date", ""))
                results = _classify(comments, cfg)
                for comment, res in zip(comments, results):
                    ref = res.get("clip_ref")
                    ch = ranks.get(int(ref)) if isinstance(ref, (int, float)) and ref else None
                    rec = {
                        "when": datetime.now(timezone.utc).isoformat(),
                        "video": v["youtube_id"], "video_date": v.get("date"),
                        "comment": comment[:300],
                        "clip_ref": ref,
                        "broadcaster": (ch or {}).get("broadcaster"),
                        "clip_id": (ch or {}).get("clip_id"),
                        "sentiment": res.get("sentiment"),
                        "topic": res.get("topic"),
                        "summary": res.get("summary"),
                    }
                    with open(log_f, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    signals.append(rec)
                    seen.add(comment)
        except Exception as e:
            log.warning("YouTube comment fetch failed: %s", e)

    # ── Owner notes (owner_feedback UI → always ACTIONABLE) ──────────────────
    owner_notes = _load_owner_notes(fb_dir, seen)
    if owner_notes:
        classified = _classify([n["note"] for n in owner_notes], cfg)
        if not classified:
            classified = [{}] * len(owner_notes)
        for note, res in zip(owner_notes, classified):
            rec = {
                "when": note["when"],
                "video": note.get("youtube_id", ""),
                "video_date": note.get("video_date", ""),
                "comment": note["note"][:300],
                "clip_ref": res.get("clip_ref"),
                "broadcaster": None, "clip_id": None,
                "sentiment": res.get("sentiment", "negative"),
                "topic": res.get("topic", "other"),
                "summary": res.get("summary", "owner note"),
                "source": "owner",
            }
            with open(log_f, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            signals.append(rec)
            seen.add(note["note"][:300])
        log.info("feedback: %d owner note(s) loaded", len(owner_notes))

    if not signals:
        log.info("feedback: no new comments or owner notes")
        return

    # ── Aggregate ─────────────────────────────────────────────────────────────
    min_agree = fb.get("min_agreement", 2)
    buckets: dict[str, list[dict]] = {}
    for s in signals:
        if s.get("sentiment") not in ("positive", "negative"):
            continue
        key = f"{s['sentiment']}|{s['topic']}|{s.get('broadcaster') or 'video-wide'}"
        buckets.setdefault(key, []).append(s)

    lines = [f"# Feedback proposals — {datetime.now().date()}",
             f"\n{len(signals)} signal(s) analyzed. Owner notes are always ACTIONABLE; "
             f"viewer comments require >= {min_agree} agreeing.\n"]
    actionable = 0
    for key, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        sentiment, topic, target = key.split("|")
        has_owner = any(s.get("source") == "owner" for s in group)
        tag = "ACTIONABLE" if len(group) >= min_agree or has_owner else "noted"
        if tag == "ACTIONABLE":
            actionable += 1
        source_note = " [owner]" if has_owner else ""
        lines.append(f"## [{tag}]{source_note} {sentiment} about {topic} ({target}) — "
                     f"{len(group)} signal(s)")
        for s in group[:5]:
            lines.append(f"- \"{s['comment'][:120]}\" "
                         f"(clip #{s.get('clip_ref')}, {s.get('summary')})")
        if tag == "ACTIONABLE" and sentiment == "negative" and topic == "clip_quality" \
                and target != "video-wide":
            lines.append(f"  -> suggestion: review clips from {target}; consider "
                         f"blacklist or a stricter threshold for this pattern")
        lines.append("")
    (fb_dir / "proposals.md").write_text("\n".join(lines), encoding="utf-8")
    log.info("feedback: %d signal(s), %d actionable -> %s",
             len(signals), actionable, fb_dir / "proposals.md")

    # optional: hand proposals to Claude Code for config-level updates
    au = cfg.get("auto_update", {})
    if actionable and au.get("enabled", False):
        prompt = (
            "Read data/feedback/proposals.md and CLAUDE.md. For ACTIONABLE signals "
            "only, apply conservative CONFIG-LEVEL changes (pipeline/config.yaml: "
            "blacklist entries, thresholds, prompt wording in pipeline modules' "
            "PROMPT strings). Do NOT restructure code. Append a '## Applied' section "
            "to data/feedback/proposals.md describing what you changed and why, or "
            "'## Applied: nothing' if no change is justified.")
        try:
            log.info("auto_update: invoking Claude Code on proposals…")
            subprocess.run([au.get("command", "claude"), "-p", prompt,
                            "--permission-mode", "acceptEdits"],
                           cwd=ROOT, timeout=900, capture_output=True)
        except Exception as e:
            log.warning("auto_update failed: %s", e)
