"""Stage 6 — Grounded voiceover commentary, v2.

Providers (config commentary.provider):
  auto    -> gemini if GEMINI_API_KEY is set, else ollama
  gemini  -> Gemini text call (same key as api_judge; ~100 tokens/clip, negligible cost)
  ollama  -> local qwen2.5

Grounding: each line is written ONLY from known facts — the api_judge description
(champion names, what actually happens), title, streamer, audio mood. Previous lines
are passed to the model so openings don't repeat. An intro line for the whole video is
generated too (clip_id "_intro"); tts.py synthesizes it like any other line and
assemble.py plays it over the intro card.

Output: data/work/<date>/commentary.json  [{clip_id, text}, ...]
"""
import json
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger("pipeline.commentary")

# keep Latin scripts + common punctuation; drop CJK / Hangul / Cyrillic / etc. so the
# English TTS never tries to read foreign characters (the "reads Chinese" bug).
_NON_LATIN = re.compile(r"[^\u0000-\u024F\u2010-\u201F\s]")


def _english_only(text: str) -> str:
    """Strip non-Latin characters; return '' if nothing usable remains."""
    if not text:
        return ""
    t = _NON_LATIN.sub("", text)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—,:;\"'").strip()
    # if stripping foreign text left only a stub, treat as empty (caller falls back)
    return t if len(t) >= 8 else ""

# All styles are energetic montage-caster variants — the video should feel like a
# highlight reel narrated by one hype caster, so even when styles rotate the tone stays
# consistent (the owner wants it to sound like commentary, not a calm analyst).
STYLES = {
    "hype_caster": ("a hype highlight-reel caster — punchy, confident, a little ironic; "
                    "uses casual gamer phrases ('no way', 'actually insane', 'cooked', "
                    "'W moment') only when they land naturally"),
    "analyst": ("a hype caster who big-ups how clean a play is — still energetic, just "
                "leaning on respect for the mechanics rather than chaos"),
    "chill": ("a confident, dry-funny montage caster — cocky and quotable, like you've "
              "seen it all and this still got a reaction out of you"),
    "gen_z_short": ("a gen Z gaming TikTok creator — ONE punchy sentence max 12 words, "
                    "self-aware and ironic; uses 'bro', 'fr', 'ngl', 'W', 'L', 'cooked', "
                    "'not him' naturally; make it quotable or funny"),
}

# NOTE: until clip context analysis improves, the per-clip "summary" is often vague or
# wrong, so the prompt treats it as an optional hint and leans on what IS reliable — the
# streamer and the crowd energy. Hype the PLAYER and the MOMENT; never narrate mechanics
# we aren't sure of (a made-up play detail is worse than a clean hype line).
LINE_PROMPT = """You write one short voiceover line for a "League of Legends best moments" highlight montage.
Persona: {style}.

What you know about the next clip:
- Streamer: {streamer}
- Crowd/streamer energy: {mood}
- Clip title a viewer gave it (often a joke, may be misleading): "{title}"
- Rough hint at what happens (UNRELIABLE — may be vague or wrong): {summary}
- What {streamer} actually said in the clip (auto-translated to English; may be empty,
  partial, or just noise): "{speech}"

Lines already used in this video (do NOT reuse their structure or opening words):
{previous}

Write AT MOST {max_sentences} short sentences hyping this moment, montage-trailer style.
Rules:
- Build up the STREAMER and the moment — make the viewer want to watch ("this is why
  {streamer} is feared", "{streamer} doesn't miss", "watch this fall apart for them").
- If the streamer's own words are given, you may react to their energy or play off what
  they said — that part is reliable. Never quote a foreign language; English only.
- Lean on the energy/title vibe. Only mention a specific play detail if the hint clearly
  supports it — when in doubt, hype the player generally instead of narrating mechanics.
- NEVER invent champions, names, numbers, or events. A vague hype line beats a wrong one.
- Be confident and a bit funny; never mean-spirited. No greetings, hashtags, or "in this clip".
- Ignore any technical phrasing in the hint ("kills detected", "kill feed", "not detected").
Answer ONLY JSON: {{"line": "..."}}"""

INTRO_PROMPT = """You write the cold-open voiceover for a "League of Legends best moments" highlight montage.
Persona: {style}.
Today's reel has {n} clips featuring: {streamers}.
Write 1-2 short, punchy sentences that hype the reel and make viewers stay — montage-trailer
energy. You may tease the vibe loosely ({teaser}) but do NOT state specific plays as fact.
No hashtags, no "subscribe", no greetings beyond a quick hook.
Ignore any technical phrasing in the teaser ("kills detected", "kill feed", "not detected").
Answer ONLY JSON: {{"line": "..."}}"""


def _ascii_name(c: dict) -> str:
    """Use broadcaster_login (always ASCII) when display name contains CJK chars."""
    name = c.get("broadcaster_name", "")
    if name.isascii():
        return name or "the streamer"
    return c.get("broadcaster_login", "") or "the streamer"


def _mood(audio: float) -> str:
    if audio >= 0.55:
        return "loud / excited"
    if audio >= 0.35:
        return "lively"
    return "calm"


def _parse_line(d: dict | None) -> str:
    if d and isinstance(d.get("line"), str):
        return d["line"].strip().strip('"')
    return ""


# ── providers ─────────────────────────────────────────────────────────────────

def _gemini_text(prompt: str, cm: dict) -> dict | None:
    """Gemini text call with retry/backoff — free-tier rate limits (429) are common
    when writing 20+ lines right after the api_judge calls."""
    import time
    from ..filtering.vlm_filter import _parse_json
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{cm.get('gemini_model', 'gemini-2.5-flash')}:generateContent"
           f"?key={cm['gemini_api_key']}")
    last = None
    for attempt in range(2):   # short: a dead daily quota won't recover, fallback will
        try:
            resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]},
                                 timeout=60)
            if resp.status_code in (429, 500, 503):
                wait = 15 * (attempt + 1)
                log.info("  Gemini %s — waiting %ss (attempt %d/2)",
                         resp.status_code, wait, attempt + 1)
                time.sleep(wait)
                last = RuntimeError(f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            return _parse_json(
                resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        except requests.RequestException as e:
            last = e
            time.sleep(8)
    raise last


def _ollama_text(prompt: str, cm: dict) -> dict | None:
    from ..filtering.vlm_filter import _parse_json
    resp = requests.post(
        f"{cm['ollama_url']}/api/generate",
        json={"model": cm["model"], "prompt": prompt, "stream": False,
              "options": {"temperature": 0.7}},
        timeout=120,
    )
    resp.raise_for_status()
    return _parse_json(resp.json().get("response", ""))


def _ollama_or_none(prompt: str, cm: dict) -> dict | None:
    try:
        return _ollama_text(prompt, cm)
    except Exception as e:
        log.warning("ollama fallback failed: %s", e)
        return None  # caller emits the template line


def make_writer(cm: dict):
    provider = cm.get("provider", "auto")
    if provider == "auto":
        provider = "gemini" if cm.get("gemini_api_key") else "ollama"
    log.info("Commentary provider: %s", provider)
    if provider != "gemini":
        return _ollama_text

    # Gemini primary with sticky local fallback: after 2 consecutive failures
    # (= daily quota exhausted, retrying is pointless) switch to Ollama for the rest.
    state = {"fails": 0}

    def writer(prompt: str, cm_: dict) -> dict | None:
        if state["fails"] >= 2:
            return _ollama_or_none(prompt, cm_)
        try:
            r = _gemini_text(prompt, cm_)
            state["fails"] = 0
            return r
        except Exception as e:
            state["fails"] += 1
            if state["fails"] >= 2:
                log.warning("Gemini failing repeatedly (%s) — switching to local "
                            "Ollama for the remaining lines", e)
            return _ollama_or_none(prompt, cm_)
    return writer


# ── line generation ───────────────────────────────────────────────────────────

# Fallbacks (LLM unavailable): pure montage-hype lines that need NO clip facts, so they
# stay clean even when context is unknown. Rotate so we never repeat back-to-back.
FALLBACK_TEMPLATES = [
    "Next up — {name}, and you'll see why this one made the cut.",
    "This is exactly why people clip {name}.",
    "Keep your eyes on {name} for this one.",
    "{name} up next — watch this one closely.",
    "{name} doesn't miss. See for yourself.",
    "Over to {name} — this is the good stuff.",
]


def _fallback_line(clip: dict, idx: int) -> str:
    return FALLBACK_TEMPLATES[idx % len(FALLBACK_TEMPLATES)].format(
        name=_ascii_name(clip)).strip()


def write_line(clip: dict, cm: dict, writer, previous: list[str],
               speech: str = "") -> str:
    speech = re.sub(r'\s+', ' ', (speech or "").replace('"', "")).strip()[:220]
    prompt = LINE_PROMPT.format(
        style=STYLES.get(cm.get("style", "hype_caster"), STYLES["hype_caster"]),
        streamer=_ascii_name(clip),
        summary=clip.get("vlm_summary") or "unknown — describe nothing specific",
        title=clip.get("title", ""),
        mood=_mood(clip.get("audio_score", 0.0)),
        speech=speech or "(nothing intelligible)",
        previous="\n".join(f"- {p}" for p in previous) or "- (none yet)",
        max_sentences=cm.get("max_sentences", 2),
    )
    try:
        line = _english_only(_parse_line(writer(prompt, cm)))
        if line:
            return line
    except Exception as e:
        log.warning("commentary failed for %s: %s", clip.get("id"), e)
    return _fallback_line(clip, len(previous))


def write_intro(clips: list[dict], cm: dict, writer) -> str:
    streamers = ", ".join(dict.fromkeys(_ascii_name(c) for c in clips))
    teaser = (clips[0].get("vlm_summary", "")[:120] if clips else "")
    prompt = INTRO_PROMPT.format(
        style=STYLES.get(cm.get("style", "hype_caster"), STYLES["hype_caster"]),
        n=len(clips), streamers=streamers, teaser=teaser,
    )
    try:
        line = _english_only(_parse_line(writer(prompt, cm)))
        if line:
            return line
    except Exception as e:
        log.warning("intro commentary failed: %s", e)
    return "Welcome back — these are yesterday's best League of Legends moments."


# ── stage entry ───────────────────────────────────────────────────────────────

def run(cfg: dict, state, date_label: str) -> Path:
    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    if not cfg.get("commentary", {}).get("enabled", True):
        log.info("commentary disabled (lean mode) — no voiceover script")
        return work
    src = work / "vlm_filtered.json"
    if not src.exists():
        src = work / "prefiltered.json"
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]

    cm = dict(cfg["commentary"])
    cm.setdefault("gemini_api_key", cfg.get("api_judge", {}).get("api_key", ""))
    writer = make_writer(cm)

    from ..enrichment.transcribe import load as _load_transcripts
    transcripts = _load_transcripts(work)   # clip_id -> {lang, text, words}

    out = [{"clip_id": "_intro", "text": write_intro(clips, cm, writer)}]
    log.info("_intro -> %s", out[0]["text"][:80])

    # Cycle styles across clips so the video doesn't feel monotonous.
    # hype_caster appears twice to stay dominant; analyst and chill add variety.
    _style_cycle = ["hype_caster", "chill", "hype_caster", "analyst"]
    vary = cm.get("vary_style", True)

    previous: list[str] = []
    for i, c in enumerate(clips):
        cm_clip = dict(cm)
        if vary:
            cm_clip["style"] = _style_cycle[i % len(_style_cycle)]
        speech = transcripts.get(c["id"], {}).get("text", "")
        text = write_line(c, cm_clip, writer, previous, speech=speech)
        previous.append(text)
        out.append({"clip_id": c["id"], "text": text})
        log.info("%s [%s] -> %s", c["id"][:20], cm_clip.get("style", ""), text[:80])

    (work / "commentary.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return work
