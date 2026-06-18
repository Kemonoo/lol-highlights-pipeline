"""Stage 7 — TTS with provider selection.

Providers (config tts.provider):
  kokoro    (default) — local Kokoro TTS (82M params, best free quality); install once:
              pip install kokoro soundfile
              First run downloads ~350MB model from HuggingFace automatically.
  edge-tts  — free Microsoft Neural voices, needs internet, no GPU

Voice (config tts.voice): a friendly key from VOICES/KOKORO_VOICES (e.g. "male-us")
or a raw provider voice id (e.g. "am_fenrir", "en-US-GuyNeural") — raw ids pass through.

synthesize() returns word-level timestamps (used later for captions on derived Shorts).
run() generates one MP3 per clip from commentary.json → data/work/<date>/vo/<clip_id>.mp3.
A single clip's failure is logged and skipped (never crashes the stage); KeyboardInterrupt
saves progress so far. Timings are flushed after every clip so a crash loses nothing.
"""
import asyncio
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

log = logging.getLogger("pipeline.tts")

# edge-tts voice IDs
VOICES = {
    "male-us":   "en-US-GuyNeural",
    "female-us": "en-US-AriaNeural",
    "male-uk":   "en-GB-RyanNeural",
    "female-uk": "en-GB-SoniaNeural",
}

# Kokoro voice IDs (pip install kokoro). Grades are Kokoro's own quality ratings —
# am_adam is F+ (avoid); am_michael/am_fenrir/am_puck are the best males (C+);
# af_heart (A) / af_bella (A-) are the best overall.
KOKORO_VOICES = {
    "male-us":   "am_michael",
    "male-us-2": "am_fenrir",
    "female-us": "af_bella",
    "male-uk":   "bm_george",
    "female-uk": "bf_emma",
}
_KOKORO_SR = 24000   # Kokoro's native sample rate


# ── Kokoro provider ───────────────────────────────────────────────────────────

_kokoro_pipeline = None   # module-level singleton: building KPipeline reloads the model


def _kokoro(lang_code: str = "a"):
    """Lazily build and cache the Kokoro pipeline (expensive to construct)."""
    global _kokoro_pipeline
    if _kokoro_pipeline is None:
        try:
            from kokoro import KPipeline
        except ImportError as e:
            raise RuntimeError(
                f"Kokoro not installed ({e}). Run: pip install kokoro soundfile"
            ) from e
        _kokoro_pipeline = KPipeline(lang_code=lang_code)
    return _kokoro_pipeline


def _synthesize_kokoro(text: str, output_path: Path, voice: str,
                       speed: float = 1.0) -> List[Dict]:
    import numpy as np
    import soundfile as sf

    voice_id = KOKORO_VOICES.get(voice, voice)   # unknown -> treat as a raw Kokoro id
    pipeline = _kokoro()

    chunks = [audio for _, _, audio in pipeline(text, voice=voice_id, speed=speed)]
    if not chunks:
        return []

    audio = np.concatenate(chunks)
    duration = len(audio) / _KOKORO_SR

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tmp = Path(tf.name)
    sf.write(str(tmp), audio, _KOKORO_SR)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(tmp), "-b:a", "128k", str(output_path)],
        capture_output=True, check=True,
    )
    tmp.unlink(missing_ok=True)

    return _uniform_distribution(text, duration)


# ── edge-tts provider ─────────────────────────────────────────────────────────

def _synthesize_edge(text: str, output_path: Path, voice: str) -> List[Dict]:
    voice_id = VOICES.get(voice, voice)
    word_events, sent_events, audio_dur = asyncio.run(
        _edge_run(text, output_path, voice_id)
    )
    if word_events:
        return word_events
    if sent_events:
        return _distribute_words(sent_events)
    return _uniform_distribution(text, audio_dur)


async def _edge_run(text: str, output_path: Path, voice_id: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice_id)
    word_events, sent_events, total_bytes = [], [], 0
    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            t = chunk["type"]
            if t == "audio":
                f.write(chunk["data"])
                total_bytes += len(chunk["data"])
            elif t == "WordBoundary":
                word_events.append({
                    "word": chunk["text"],
                    "start": chunk["offset"] / 10_000_000,
                    "end": (chunk["offset"] + chunk["duration"]) / 10_000_000,
                })
            elif t == "SentenceBoundary":
                sent_events.append({
                    "text": chunk["text"],
                    "start": chunk["offset"] / 10_000_000,
                    "end": (chunk["offset"] + chunk["duration"]) / 10_000_000,
                })
    return word_events, sent_events, total_bytes * 8 / 128_000


# ── helpers ───────────────────────────────────────────────────────────────────

def _distribute_words(sentences: List[Dict]) -> List[Dict]:
    words = []
    for sent in sentences:
        raw = sent["text"].split()
        if not raw:
            continue
        dur = sent["end"] - sent["start"]
        chars = [max(len(w), 1) for w in raw]
        total = sum(chars)
        t = sent["start"]
        for word, n in zip(raw, chars):
            wd = dur * n / total
            words.append({"word": word, "start": t, "end": t + wd})
            t += wd
    return words


def _uniform_distribution(text: str, total_duration: float) -> List[Dict]:
    raw = text.split()
    if not raw or total_duration <= 0:
        return []
    dur = total_duration / len(raw)
    return [{"word": w, "start": i * dur, "end": (i + 1) * dur}
            for i, w in enumerate(raw)]


# ── public API ────────────────────────────────────────────────────────────────

# last-line defence: never feed non-Latin text (CJK/Hangul/Cyrillic) to the English
# voices — they mangle it. Commentary already sanitizes; this also covers Shorts.
_NON_LATIN = re.compile(r"[^\u0000-\u024F\u2010-\u201F\s]")


def synthesize(text: str, output_path: Path, voice: str = "male-us",
               provider: str = "kokoro", speed: float = 1.0) -> List[Dict]:
    """Synthesize text to MP3; return [{"word", "start", "end"}] in seconds."""
    text = _NON_LATIN.sub("", text or "").strip()
    if not text:
        return []
    if provider == "edge-tts":
        return _synthesize_edge(text, output_path, voice)
    return _synthesize_kokoro(text, output_path, voice, speed=speed)


def run(cfg: dict, state, date_label: str) -> Path:
    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    tts = cfg.get("tts", {})
    if not tts.get("enabled", True):
        log.info("tts disabled — videos will have no voiceover")
        return work

    provider = tts.get("provider", "kokoro")
    voice = tts.get("voice", "male-us")
    speed = float(tts.get("speed", 1.0))
    log.info("TTS provider: %s  voice: %s  speed: %s", provider, voice, speed)

    lines = json.loads((work / "commentary.json").read_text(encoding="utf-8"))
    vo_dir = work / "vo"
    vo_dir.mkdir(exist_ok=True)
    timings_path = vo_dir / "timings.json"

    timings = {}
    try:
        for item in lines:
            clip_id = item["clip_id"]
            mp3 = vo_dir / f"{clip_id}.mp3"
            try:
                words = synthesize(item["text"], mp3, voice, provider=provider,
                                   speed=speed)
            except Exception as e:   # one bad line must not kill the whole stage
                log.warning("TTS failed for %s: %s", clip_id, e)
                continue
            timings[clip_id] = words
            log.info("TTS %s (%.1fs)", clip_id, words[-1]["end"] if words else 0)
            # flush incrementally so a later crash / interrupt loses nothing
            timings_path.write_text(
                json.dumps(timings, indent=2, ensure_ascii=False), encoding="utf-8")
    except KeyboardInterrupt:
        log.warning("TTS interrupted — keeping %d lines rendered so far", len(timings))
        raise

    return work
