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
from pathlib import Path

import requests

log = logging.getLogger("pipeline.commentary")

STYLES = {
    "hype_caster": ("an esports caster with sharp wit and gen Z energy — energetic, ironic, "
                    "relatable; uses casual phrases like 'bro', 'no way', 'actually insane', "
                    "'ngl', 'cooked', 'W moment' when they fit naturally; never forced or cringe"),
    "analyst": "a calm analytical caster explaining what makes the play good",
    "chill": "a relaxed streamer-style commentator, casual and a bit funny",
    "gen_z_short": ("a gen Z gaming TikTok creator — ONE punchy sentence max 12 words, "
                    "self-aware and ironic; uses 'bro', 'fr', 'ngl', 'W', 'L', 'cooked', "
                    "'not him' naturally; make it quotable or funny"),
}

LINE_PROMPT = """You write short voiceover lines for a daily "League of Legends best moments" YouTube video.
Persona: {style}.

FACTS about the next clip (this is ALL you know — never invent champions, names or numbers):
- Streamer: {streamer}
- What happens: {summary}
- Clip title written by a viewer (may be a joke): "{title}"
- Crowd/streamer audio mood: {mood}

Lines already used in this video (do NOT reuse their structure or opening words):
{previous}

Write AT MOST {max_sentences} sentences of voiceover introducing this clip.
Rules: mention the streamer's name; reference what actually happens; be funny where the
clip allows it — irony and relatable gamer humor land best (if something absurd happens
around the play, like a slap right after a kill, joke about it: that slap was clearly
the reward). Never mean-spirited. No greetings, no hashtags, no "in this clip".
If "What happens" mentions "kills detected", "kill feed", "not detected", or similar
technical phrases — ignore them entirely; write only what a viewer watching the clip sees.
Answer ONLY JSON: {{"line": "..."}}"""

INTRO_PROMPT = """You write the cold-open voiceover for a daily "League of Legends best moments" YouTube video.
Persona: {style}.
Today's video has {n} clips featuring these streamers: {streamers}.
Write 1-2 short sentences welcoming viewers and teasing the content (you may hint at
the best moment: {teaser}). No hashtags, no "subscribe", just a punchy open.
If the teaser mentions "kills detected", "kill feed", "not detected" or similar — ignore those phrases.
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

# Fallbacks (LLM unavailable): rotate templates and attach the judge's grounded
# description when we have one — never the same sentence twice in a row.
FALLBACK_TEMPLATES = [
    "Next up — {name}. {fact}",
    "{name} now, and this one speaks for itself. {fact}",
    "Over to {name}. {fact}",
    "Keep your eyes on {name} for this one. {fact}",
    "{name} had a moment yesterday. See for yourself. {fact}",
    "This is why people clip {name}. {fact}",
]


def _fallback_line(clip: dict, idx: int) -> str:
    name = _ascii_name(clip)
    summary = (clip.get("vlm_summary") or "").strip()
    fact = ""
    if len(summary) > 40 and "no kills detected" not in summary:
        fact = summary.split(". ")[0].rstrip(".") + "."
    return FALLBACK_TEMPLATES[idx % len(FALLBACK_TEMPLATES)].format(
        name=name, fact=fact).strip()


def write_line(clip: dict, cm: dict, writer, previous: list[str]) -> str:
    prompt = LINE_PROMPT.format(
        style=STYLES.get(cm.get("style", "hype_caster"), STYLES["hype_caster"]),
        streamer=_ascii_name(clip),
        summary=clip.get("vlm_summary") or "unknown — describe nothing specific",
        title=clip.get("title", ""),
        mood=_mood(clip.get("audio_score", 0.0)),
        previous="\n".join(f"- {p}" for p in previous) or "- (none yet)",
        max_sentences=cm.get("max_sentences", 2),
    )
    try:
        line = _parse_line(writer(prompt, cm))
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
        line = _parse_line(writer(prompt, cm))
        if line:
            return line
    except Exception as e:
        log.warning("intro commentary failed: %s", e)
    return "Welcome back — these are yesterday's best League of Legends moments."


# ── stage entry ───────────────────────────────────────────────────────────────

def run(cfg: dict, state, date_label: str) -> Path:
    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    src = work / "vlm_filtered.json"
    if not src.exists():
        src = work / "prefiltered.json"
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]

    cm = dict(cfg["commentary"])
    cm.setdefault("gemini_api_key", cfg.get("api_judge", {}).get("api_key", ""))
    writer = make_writer(cm)

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
        text = write_line(c, cm_clip, writer, previous)
        previous.append(text)
        out.append({"clip_id": c["id"], "text": text})
        log.info("%s [%s] -> %s", c["id"][:20], cm_clip.get("style", ""), text[:80])

    (work / "commentary.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return work
