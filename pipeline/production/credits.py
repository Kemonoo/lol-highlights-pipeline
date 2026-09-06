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
#
# Matched against the judge's `what_happens` description, which is far richer than the
# Twitch title it used to rely on — so it is worth listing the things that description
# actually says. Before this list was widened, the generic fallback fired on 15 of 29
# days: over half the uploads carried a thumbnail reading the exact same two words,
# which is the opposite of what a browse-feed thumbnail is for.
_HOOKS = [
    ("penta", "PENTAKILL"), ("quadra", "QUADRA KILL"), ("1v5", "1V5 OUTPLAY"),
    ("1v4", "1V4 CLUTCH"), ("1v3", "1V3 OUTPLAY"), ("triple", "TRIPLE KILL"),
    ("ace", "TEAM ACE"), ("steal", "INSANE STEAL"), ("clutch", "CLUTCH PLAY"),
    ("flash", "FLASH OUTPLAY"), ("outplay", "CRAZY OUTPLAY"),
    ("backdoor", "BACKDOOR!"), ("baron", "BARON STEAL"), ("nexus", "NEXUS RACE"),
    ("comeback", "INSANE COMEBACK"), ("1v9", "1V9 CARRY"), ("solo kill", "SOLO KILL"),
    ("tower dive", "TOWER DIVE"), ("teamfight", "CHAOS TEAMFIGHT"),
    ("team fight", "CHAOS TEAMFIGHT"), ("dragon", "DRAGON FIGHT"),
    ("first blood", "FIRST BLOOD"), ("misclick", "HE MISCLICKED"),
    ("fat-finger", "HE MISCLICKED"), ("flames", "TILTED"), ("rages", "TILTED"),
    ("frustrat", "TILTED"), ("laughs", "HE LOST IT"), ("panic", "PURE PANIC"),
    ("survives", "HOW DID HE LIVE"), ("escapes", "HOW DID HE LIVE"),
    ("1v2", "OUTNUMBERED"), ("1v", "OUTNUMBERED"),
]

# Used only when nothing above matches. A CONSTANT here is the failure mode this
# replaces, so it rotates by date — deterministic, so re-rendering a day is stable.
# Keep these SHORT and free of trailing punctuation: the thumbnail fits the headline to
# the space left of the face, so every extra word shrinks the type, and callers append
# their own "?!".
_GENERIC_HOOKS = ["INSANE PLAYS", "NO WAY", "HE DID THAT",
                  "ACTUALLY INSANE", "WATCH THIS", "UNREAL"]
_EMOJI = ["😱", "🔥", "💀", "😳", "🤯"]

# Clickbait/curiosity hooks (an episode number is appended → "HOOK | 12",
# like the daily-clip channels that number their uploads). The hook is the CLICK
# driver and need not be literally accurate. {hook}=power phrase, {n}=clip count,
# {star}=top streamer (ASCII).
DEFAULT_TITLE_STYLES = [
    "{hook}?! {emoji} League of Legends Best Moments",
    "He Really Did THAT… {emoji} LoL Plays of the Day",
    "Wait Until You See Clip #1 {emoji} LoL Best Moments",
    "You Won't Believe These LoL Plays {emoji} (Top {n})",
    "How Is This Even Possible?! {emoji} LoL Daily Top {n}",
    "{hook} {emoji} The Best League of Legends Moments Today",
    "{star} Went CRAZY… {emoji} LoL Best Moments (Top {n})",
]


def _ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _best(clips: list[dict]) -> dict | None:
    return max(clips, key=lambda c: c.get("api_rank_score", 0), default=None) if clips else None


def _hook(clips: list[dict], date_label: str = "") -> str:
    """Short title hook from the best clip's content.

    `date_label` only picks which generic hook is used when the clip's description
    matches nothing specific; passing it keeps consecutive uploads from sharing a
    thumbnail. Seeded by date rather than random so re-rendering a day is stable.
    """
    best = _best(clips)
    text = ((best.get("vlm_summary", "") + " " + best.get("title", "")).lower()
            if best else "")
    for word, hook in _HOOKS:
        if word in text:
            return hook
    if not date_label:
        return _GENERIC_HOOKS[0]
    seed = int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16)
    return _GENERIC_HOOKS[seed % len(_GENERIC_HOOKS)]


def _star(clips: list[dict]) -> str:
    """ASCII-safe name of the top streamer (empty if only a CJK name is available)."""
    best = _best(clips)
    if not best:
        return ""
    nm = best.get("broadcaster_name", "")
    return nm if nm.isascii() else (best.get("broadcaster_login", "") or "")


def _title(cfg: dict, date_label: str, clips: list[dict], n: int, episode: int) -> str:
    """Clickbait hook + episode series-marker, rotating daily (≤ 100 chars)."""
    up = cfg.get("upload", {})
    seed = int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16)
    ctx = {"hook": _hook(clips, date_label), "n": n or len(clips),
           "star": _star(clips), "emoji": _EMOJI[seed % len(_EMOJI)]}
    styles = up.get("title_styles") or DEFAULT_TITLE_STYLES
    usable = [s for s in styles if not ("{star}" in s and not ctx["star"])] or DEFAULT_TITLE_STYLES[:2]
    head = re.sub(r"\s{2,}", " ", usable[seed % len(usable)].format(**ctx)).strip()
    if up.get("title_date", True):
        return f"{head[:72]} | {episode}".strip()[:100]
    return head[:100]


def build_metadata(cfg: dict, date_label: str, chapters: list[dict],
                   clips: list[dict], episode: int) -> dict:
    emoji = _EMOJI[int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16) % len(_EMOJI)]
    title = _title(cfg, date_label, clips, len(chapters), episode)

    # Lean mode ships no voiceover, so don't advertise commentary that isn't there —
    # this is the description viewers read under a public video.
    blurb = ("with commentary" if cfg.get("commentary", {}).get("enabled", False)
             else "ranked and counted down")

    # CTA + keywords front-loaded (first ~150 chars show in search/feed)
    lines = [f"{emoji} The best League of Legends plays of the day, {blurb}. "
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

    # Riot's "Legal Jibber Jabber" fan-content policy permits this channel's use of their
    # assets (champion splash art via Data Dragon in the brand intro and thumbnails) AND
    # permits ad revenue on the videos — but only on condition that a conspicuous notice
    # in this exact form accompanies the project. Verified against riotgames.com/en/legal
    # on 2026-09-06; the wording is theirs, don't paraphrase it.
    riot = cfg.get("upload", {}).get("riot_fan_notice", True)
    if riot:
        brand = (cfg.get("video", {}).get("brand", {}) or {}).get("name") or "This channel"
        lines += ["", f"{brand} was created under Riot Games' \"Legal Jibber Jabber\" "
                      "policy using assets owned by Riot Games. "
                      "Riot Games does not endorse or sponsor this project."]
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
    episode = state.episode_number(date_label)
    meta = build_metadata(cfg, date_label, chapters, clips, episode)
    out = data / "output" / f"{date_label}.meta.json"
    out.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Metadata: %s", meta["title"])
    return out
