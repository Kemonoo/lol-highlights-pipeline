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
    # Hook TRAILING rather than leading. Without one of these, a day whose opening word
    # is blocked (because yesterday used it) loses the hook entirely — and a pentakill
    # two days running is real information worth keeping, just not worth repeating as
    # the first word. Appositive form, so it stays grammatical for every hook in _HOOKS
    # ("PENTAKILL", "TILTED", "HOW DID HE LIVE" all read fine after a dash).
    "{emoji} LoL Daily Top {n} — {hook}",
    "LoL Best Moments of the Day {emoji} {hook}",
]


# ── "hook" title mode: "<day-specific hook>... <series> #<episode>" ───────────────
#
# Owner decision 2026-09-25, modelled on the daily-clip channels that rank for "lol
# moments" ("AFK Bait That Always Works...LoL Daily Moments Ep 3482"): the title says
# something SPECIFIC about the day's best clip, then a constant series name + episode.
# The clip count ("Top 22") is dropped — nobody chooses a video by it. The hook is
# written by the `commentary` role from the judge's descriptions of the top clips;
# the same call writes the 1-3 word thumbnail text. Cached per date (title_hook.json)
# so a re-run keeps the same title and spends no second request.

HOOK_CACHE = "title_hook.json"

_HOOK_SCHEMA = {"type": "object",
                "properties": {"hook": {"type": "string"}, "thumb": {"type": "string"}},
                "required": ["hook", "thumb"]}

_HOOK_PROMPT = """You write YouTube titles for a daily League of Legends Twitch-clips compilation.
Write the HOOK: the part before "... {series} #N". It sells the day's best moment.

Look across ALL the clips below for the best STORY, not just the biggest number: a twist
(a stolen penta, a predicted penta, a fail right after a win), a streamer losing it, an
absurd outplay. Multikills are common in this series, so "X gets a pentakill" alone is weak.

Rules:
- 3-7 words, English, max 45 characters. Title Case, with ONE word in CAPS for punch.
- Curiosity or emotion; make people want to see it. Good shapes (other days' clips):
  "He Called the Penta... Then DID IT", "Nobody Expected This Thresh HOOK",
  "The Most DISRESPECTFUL Flash", "Teemo Should NOT Win This".
- Never use flat verbs: secures, achieves, gets, performs, executes, demonstrates.
- Only facts stated in the descriptions (champion names, multikills, what happened).
  Never invent names, numbers or events. No streamer names, no emojis, no hashtags,
  no trailing punctuation.
{avoid}
Also write THUMB: 1-3 word ALL-CAPS thumbnail text for the same moment, clickbait allowed,
usually ending "?!" (e.g. "PENTA STOLEN?!", "200 IQ", "ONE SHOT", "1V5?!", "HE CALLED IT").
Don't just repeat the hook's CAPS word.

Top clips (best first):
{clips}"""


def _top_clips(clips: list[dict], n: int = 3) -> list[dict]:
    return sorted(clips, key=lambda c: -(c.get("api_rank_score") or 0))[:n]


def clean_hook(text: str) -> str:
    """Model output -> a safe title fragment ('' if unusable)."""
    t = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip().strip("\"'")
    t = re.sub(r"\s+", " ", re.sub(r"[#@|]", "", t)).strip()
    t = re.sub(r"(\.{2,}|…|[.!?,:;-])+$", "", t).strip()    # we append the "..."
    if not t or len(t) > 60 or len(t.split()) > 9:
        return ""
    return t


def clean_thumb(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip().strip("\"'").upper()
    t = re.sub(r"[^A-Z0-9 ?!'.-]", "", t).strip()
    if not t or len(t.split()) > 3 or len(t) > 20:
        return ""
    return t


def write_hook(cfg: dict, clips: list[dict], series: str,
               recent_titles: list | None = None, recent_thumb: str = "") -> dict:
    """{'hook','thumb'} from the `commentary` role; {} on any failure (never raises)."""
    from ..providers import ProviderUnavailable, get_provider
    top = _top_clips(clips)
    if not top:
        return {}
    lines = []
    for i, c in enumerate(top, 1):
        desc = c.get("api_what_happens") or c.get("vlm_summary") or ""
        lines.append(f"{i}. Twitch title: {c.get('title', '')!r}. What happens: {desc}")
    avoid = ""
    if recent_titles:
        avoid = f"- Yesterday's title was {recent_titles[0]!r}: open with a different word."
    if recent_thumb:
        avoid += f"\n- Yesterday's THUMB was {recent_thumb!r}: use different words."
    prompt = _HOOK_PROMPT.format(series=series, avoid=avoid, clips="\n".join(lines))
    try:
        r = get_provider(cfg, "commentary").complete_json(prompt, schema=_HOOK_SCHEMA) or {}
    except ProviderUnavailable as e:
        log.info("  no title-hook provider (%s) - keyword hook", e)
        return {}
    except Exception as e:
        log.warning("  title hook failed (%s) - keyword hook", e)
        return {}
    out = {"hook": clean_hook(r.get("hook", "")), "thumb": clean_thumb(r.get("thumb", ""))}
    return {k: v for k, v in out.items() if v}


def _previous_thumb(work: Path) -> str:
    """Thumbnail text of the latest earlier date that has one (so days don't repeat it)."""
    try:
        for d in sorted((x for x in work.parent.iterdir() if x.name < work.name),
                        reverse=True)[:7]:
            f = d / HOOK_CACHE
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8").rstrip("\x00")).get("thumb", "")
    except (OSError, ValueError):
        pass
    return ""


def hook_title(hook: str, series: str, episode: int) -> str:
    # the model sometimes writes the whole title ("... LoL Daily Clips 29") into the hook
    i = hook.lower().find(series.lower())
    if i >= 0:
        hook = clean_hook(hook[:i]) or hook[:i].strip()
    tail = f"... {series} #{episode}"
    return (hook[:100 - len(tail)].rstrip() + tail).strip()


def day_hook(cfg: dict, work: Path, clips: list[dict], date_label: str,
             recent_titles: list | None = None) -> dict:
    """The cached {'hook','thumb'} for this date, writing it on first use.

    Falls back to the keyword hook ("PENTAKILL?!") when the model is unavailable; that
    fallback is NOT cached, so the next run tries the model again."""
    series = cfg.get("upload", {}).get("title_series", "LoL Daily Clips")
    p = work / HOOK_CACHE
    try:
        cached = json.loads(p.read_text(encoding="utf-8").rstrip("\x00"))
        if cached.get("hook"):
            return cached
    except (OSError, ValueError):
        pass
    got = write_hook(cfg, clips, series, recent_titles, _previous_thumb(work))
    if got.get("hook"):
        got.setdefault("thumb", "")
        try:
            p.write_text(json.dumps(got, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return got
    kw = _hook(clips, date_label)
    return {"hook": kw.title() if len(kw.split()) > 1 else kw, "thumb": "", "fallback": True}


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


def _opening_word(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (title or "").split(" ")[0]).lower()


def _title(cfg: dict, date_label: str, clips: list[dict], n: int, episode: int,
           recent_titles: list | None = None) -> str:
    """Clickbait hook + episode series-marker, rotating daily (≤ 100 chars).

    `recent_titles` stops consecutive uploads opening on the same word. The hook itself
    is left alone — a pentakill really is the best thing that happened, and two in a row
    is a fact about the clips, not a bug — but two videos both LEADING with "PENTAKILL"
    look like a duplicate in the subscriptions feed. So the hook stays and the sentence
    around it changes: pick a style that opens differently.
    """
    up = cfg.get("upload", {})
    seed = int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16)
    ctx = {"hook": _hook(clips, date_label), "n": n or len(clips),
           "star": _star(clips), "emoji": _EMOJI[seed % len(_EMOJI)]}
    styles = up.get("title_styles") or DEFAULT_TITLE_STYLES
    usable = [s for s in styles if not ("{star}" in s and not ctx["star"])] or DEFAULT_TITLE_STYLES[:2]

    def render(style: str) -> str:
        return re.sub(r"\s{2,}", " ", style.format(**ctx)).strip()

    order = [usable[(seed + i) % len(usable)] for i in range(len(usable))]
    head = render(order[0])
    avoid = {_opening_word(t) for t in (recent_titles or [])[:1] if t}
    if avoid:
        for style in order:
            cand = render(style)
            if _opening_word(cand) not in avoid:
                head = cand
                break
    if up.get("title_date", True):
        return f"{head[:72]} | {episode}".strip()[:100]
    return head[:100]


def build_metadata(cfg: dict, date_label: str, chapters: list[dict],
                   clips: list[dict], episode: int,
                   recent_titles: list | None = None, work: Path | None = None) -> dict:
    emoji = _EMOJI[int(hashlib.md5(date_label.encode("utf-8")).hexdigest(), 16) % len(_EMOJI)]
    up = cfg.get("upload", {})
    if up.get("title_mode", "styles") == "hook" and work is not None:
        h = day_hook(cfg, work, clips, date_label, recent_titles)
        title = hook_title(h["hook"], up.get("title_series", "LoL Daily Clips"), episode)
    else:
        title = _title(cfg, date_label, clips, len(chapters), episode,
                       recent_titles=recent_titles)

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
    meta = build_metadata(cfg, date_label, chapters, clips, episode,
                          recent_titles=state.recent_titles() if state else None,
                          work=work)
    out = data / "output" / f"{date_label}.meta.json"
    out.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Metadata: %s", meta["title"])
    return out
