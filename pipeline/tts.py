"""Stage 7 — TTS with provider selection.

Providers (config tts.provider):
  edge-tts  (default) — free Microsoft Neural voices, needs internet, no GPU
  kokoro    — local Kokoro TTS (350M params, best free quality); install once:
              pip install kokoro soundfile
              First run downloads ~350MB model from HuggingFace automatically.

synthesize() returns word-level timestamps (used later for captions on derived Shorts).
run() generates one MP3 per clip from commentary.json → data/work/<date>/vo/<clip_id>.mp3
"""
import asyncio
import json
import logging
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

# Kokoro voice IDs  (pip install kokoro)
KOKORO_VOICES = {
    "male-us":   "am_adam",
    "male-us-2": "am_michael",
    "female-us": "af_heart",
    "male-uk":   "bm_daniel",
    "female-uk": "bf_emma",
}


# ── Kokoro provider ───────────────────────────────────────────────────────────

def _synthesize_kokoro(text: str, output_path: Path, voice: str) -> List[Dict]:
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
    except ImportError as e:
        raise RuntimeError(
            f"Kokoro not installed ({e}). Run: pip install kokoro soundfile"
        ) from e

    voice_id = KOKORO_VOICES.get(voice, "am_adam")
    pipeline = KPipeline(lang_code="a")   # 'a' = American English

    chunks = []
    for _, _, audio in pipeline(text, voice=voice_id, speed=1.0):
        chunks.append(audio)

    if not chunks:
        return []

    audio = np.concatenate(chunks)
    duration = len(audio) / 24000

    tmp = Path(tempfile.mktemp(suffix=".wav"))
    sf.write(str(tmp), audio, 24000)
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

def synthesize(text: str, output_path: Path, voice: str = "male-us",
               provider: str = "edge-tts") -> List[Dict]:
    """Synthesize text to MP3; return [{"word", "start", "end"}] in seconds."""
    if provider == "kokoro":
        return _synthesize_kokoro(text, output_path, voice)
    return _synthesize_edge(text, output_path, voice)


def run(cfg: dict, state, date_label: str) -> Path:
    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    tts = cfg.get("tts", {})
    if not tts.get("enabled", True):
        log.info("tts disabled — videos will have no voiceover")
        return work

    provider = tts.get("provider", "edge-tts")
    voice = tts.get("voice", "male-us")
    log.info("TTS provider: %s  voice: %s", provider, voice)

    lines = json.loads((work / "commentary.json").read_text(encoding="utf-8"))
    vo_dir = work / "vo"
    vo_dir.mkdir(exist_ok=True)

    timings = {}
    for item in lines:
        mp3 = vo_dir / f"{item['clip_id']}.mp3"
        words = synthesize(item["text"], mp3, voice, provider=provider)
        timings[item["clip_id"]] = words
        log.info("TTS %s (%.1fs)", item["clip_id"], words[-1]["end"] if words else 0)

    (vo_dir / "timings.json").write_text(
        json.dumps(timings, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return work
