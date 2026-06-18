"""Stage 9 — YouTube title, description with credits + chapters, v2.

Title hook is derived from the best clip's API judgment (e.g. a penta -> "PENTAKILL"),
falling back to a generic hook. Music attribution from config is appended when set.

Output: data/output/<date>.meta.json  {title, description, tags}
"""
import hashlib
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("pipeline.credits")

# strongest specific moment -> punchy hook word (checked in order, most impressive first)
_HOOKS = [
    ("penta", "PENTAKILL"), ("quadra", "QUADRA KILL"), ("1v5", "1V5 OUTPLAY"),
    ("1v4", "1V4 CLUTCH"), ("1v3", "1V3 OUTPLAY"), ("triple", "TRIPLE KILL"),
    ("ace", "TEAM ACE"), ("steal", "INSANE STEAL"), ("clutch", "CLUTCH PLAY"),
    ("flash", "FLASH OUTPLAY"), ("outplay", "CRAZY OUTPLAY"), ("1v", "OUTNUMBERED"),
]
_EMOJI = ["😱", "🔥", "💀", "😳", "🤯"]

# curiosity-first title templates (no date — dates make a video look stale and kill
# evergreen CTR). {hook}=power phrase, {n}=clip count, {star}=top streamer (ASCII).
DEFAULT_TITLE_STYLES = [
    "{hook}?! {emoji} League of Legends Best Moments",
    "The Most INSANE LoL Plays of the Day {emoji} (Top {n})",
    "{star} Went CRAZY… {emoji} Best LoL Moments (Top {n})",
    "You Won't Believe Clip #1 {emoji} LoL Daily Best Moments",
    "{hook} {emoji} Top {n} League of Legends Plays Today",
]


def _ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _best(clips: list[dict]) -> dict | None:
    return max(clips, key=lambda c: c.get("api_rank_score", 0), default=None) if clips else None


def _hook(clips: list[dict]) -> str:
    """Short title hook from the best clip's content."""
    best = _best(clips)
    text = ((best.get("vlm_summary", "") + " " + best.get("title", "")).lower()
            if best else "")
    for word, hook in _HOOKS:
        if word in text:
            return hook
    return "INSANE PLAYS"


def _star(clips: list[dict]) -> str:
    """ASCII-safe name of the top streamer (empty if only a CJK name is available)."""
    best = _best(clips)
    if not best:
        return ""
    nm = best.get("broadcaster_name", "")
    return nm if nm.isascii() else (best.get("broadcaster_login", "") or "")


def _title(cfg: dict, date_label: str, clips: list[dict], n: int) -> str:
    """Build a curiosity-driven, dateless, rotating title (≤ ~90 chars)."""
    seed = int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16)
    ctx = {"hook": _hook(clips), "n": n or len(clips),
           "star": _star(clips), "emoji": _EMOJI[seed % len(_EMOJI)]}
    styles = cfg.get("upload", {}).get("title_styles") or DEFAULT_TITLE_STYLES
    usable = [s for s in styles if not ("{star}" in s and not ctx["star"])] or DEFAULT_TITLE_STYLES[:2]
    title = usable[seed % len(usable)].format(**ctx)
    return re.sub(r"\s{2,}", " ", title).strip()[:90]


def build_metadata(cfg: dict, date_label: str, chapters: list[dict],
                   clips: list[dict]) -> dict:
    emoji = _EMOJI[int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16) % len(_EMOJI)]
    title = _title(cfg, date_label, clips, len(chapters))

    # CTA + keywords front-loaded (first ~150 chars show in search/feed)
    lines = [f"{emoji} The best League of Legends plays of the day, with commentary. "
             f"Which clip was your favorite? Drop the number in the comments 👇 — and "
             f"SUBSCRIBE for daily LoL highlights!",
             "", "⏱ Chapters & streamers (go follow them):"]
    for ch in chapters:
        n = f"#{ch['rank']} " if ch.get("rank") else ""
        lines.append(f"{_ts(ch['start'])} {n}{ch['broadcaster']} — {ch['broadcaster_url']}")
    lines += ["", "All clips credited to their creators on Twitch. "
              "Contact us for credit changes or removal requests."]
    attribution = cfg.get("video", {}).get("music_attribution", "")
    if attribution:
        lines += ["", attribution]
    lines += ["", "#LeagueOfLegends #LoL #lolhighlights #lolbestmoments"]

    streamers = sorted({ch["broadcaster"] for ch in chapters if ch["broadcaster"]})
    tags = ["league of legends", "lol", "lol highlights", "lol best moments",
            "league of legends highlights", "lol plays", "outplays", "lol montage",
            "twitch highlights", "lol funny moments", *[s.lower() for s in streamers[:15]]]
    return {"title": title, "description": "\n".join(lines), "tags": tags[:30]}


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    chapters = json.loads((work / "chapters.json").read_text(encoding="utf-8")
                          .rstrip("\x00"))
    src = work / "vlm_filtered.json"
    clips = (json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
             if src.exists() else [])
    meta = build_metadata(cfg, date_label, chapters, clips)
    out = data / "output" / f"{date_label}.meta.json"
    out.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Metadata: %s", meta["title"])
    return out
