"""Enrichment — multilingual speech → English (faster-whisper).

Detects the spoken language and translates it to English in a single pass
(task="translate"), with word-level timestamps. Everything we publish is English;
this is the component that gets us there when the streamer speaks another language.

Output feeds two consumers:
  - commentary.py — reliable context: what the streamer ACTUALLY said (not the
    judge's guess), so the English voiceover can react to the real moment
  - shorts.py     — English captions (burned word-by-word, TikTok style)

Stage cache: data/work/<date>/transcripts.json
  { clip_id: {"lang": "de", "lang_prob": 0.98, "text": "...",
              "words": [{"word","start","end"}, ...]} }
A result is cached even when empty (no speech) so reruns skip it; only exceptions
are left uncached for retry. Never blocks the pipeline.

Done-marker: data/work/<date>/transcripts.done.json, written only once every clip has
been attempted. The cache above is flushed per clip and so cannot double as one.

Requires: pip install faster-whisper
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("pipeline.transcribe")

_model = None
_model_key = None


def _get_model(name: str, device: str = "cpu", compute_type: str = "int8"):
    """Build + cache the faster-whisper model once.

    Callers pass the device resolved by hardware.whisper_device() (`transcribe.device`,
    default `auto`). This try/except only covers CONSTRUCTION failures — a missing cuDNN
    raises here and falls back cleanly. It does not cover a GPU that dies during
    inference, which is a process kill, not an exception; run() handles that case by
    remembering the crash and choosing CPU on the next attempt."""
    global _model, _model_key
    key = (name, device, compute_type)
    if _model is not None and _model_key == key:
        return _model
    from faster_whisper import WhisperModel
    try:
        _model = WhisperModel(name, device=device, compute_type=compute_type)
    except Exception as e:
        log.warning("whisper %s on %s/%s failed (%s) — falling back to cpu/int8",
                    name, device, compute_type, e)
        _model = WhisperModel(name, device="cpu", compute_type="int8")
    _model_key = key
    return _model


def transcribe(audio: Path, model_name: str = "small", max_s: float | None = None,
               device: str = "cpu", compute_type: str = "int8") -> dict:
    """Translate any-language speech in `audio` to English with word timestamps.

    Returns {"lang", "lang_prob", "text", "words": [{word,start,end}, ...]}.
    """
    model = _get_model(model_name, device, compute_type)
    segments, info = model.transcribe(str(audio), task="translate",
                                      word_timestamps=True)
    text_parts, words = [], []
    for seg in segments:                       # generator → drives the transcription
        if max_s is not None and seg.start > max_s + 0.5:
            break
        if seg.text:
            text_parts.append(seg.text.strip())
        for w in (seg.words or []):
            if max_s is not None and w.start > max_s + 0.5:
                break
            token = (w.word or "").strip()
            if token:
                words.append({"word": token, "start": float(w.start),
                              "end": float(w.end)})
    return {"lang": info.language,
            "lang_prob": round(float(info.language_probability), 2),
            "text": " ".join(text_parts).strip(), "words": words}


def _resolve_mp4(clip: dict, raw_dir: Path) -> Path | None:
    p = raw_dir / f"{clip.get('id', '')}.mp4"
    if p.exists():
        return p
    lp = clip.get("local_path") or ""
    return Path(lp) if lp and Path(lp).exists() else None


def load(work: Path) -> dict:
    """Read transcripts.json (clip_id → result) for downstream stages; {} if absent."""
    f = work / "transcripts.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8").rstrip("\x00"))
    except Exception:
        return {}


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    tc = cfg.get("transcribe", {})
    if not tc.get("enabled", True):
        log.info("transcribe disabled")
        return work

    src = work / "vlm_filtered.json"
    if not src.exists():
        log.info("transcribe: no vlm_filtered.json - skip")
        return work
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    raw_dir = data / "raw" / date_label

    out = work / "transcripts.json"
    cache = load(work)
    model_name = tc.get("model", "small")
    max_s = tc.get("max_seconds")
    # `auto` verifies ctranslate2 + cuDNN 9 are INSTALLED, which is a load-time check —
    # it cannot prevent the GPU dying mid-inference. Measured: 7 of 32 August runs were
    # killed inside model.transcribe() on cuda/int8_float16 (4GB card), always right
    # after language detection, with no Python exception to catch — the interpreter is
    # gone, so no try/except here can help. What we CAN do is not repeat it: leave a
    # breadcrumb before touching the GPU, and if it is still there next run, the last
    # attempt died on the GPU, so use the CPU path that has never crashed.
    from ..hardware import whisper_device
    device, compute = whisper_device(cfg)
    guard = work / ".transcribe_gpu_crash"
    if device != "cpu" and tc.get("cpu_fallback_after_crash", True):
        if guard.exists():
            log.warning("transcribe: previous attempt was killed on %s - "
                        "using cpu/int8 this run (slower, but it finishes)", device)
            device, compute = "cpu", "int8"
        else:
            guard.write_text(device, encoding="utf-8")
    skip = {l.lower() for l in tc.get("skip_languages", [])}

    try:
        for c in clips:
            cid = c["id"]
            if cid in cache:
                continue
            if (c.get("language") or "").lower() in skip:   # already English → no translation
                continue
            mp4 = _resolve_mp4(c, raw_dir)
            if not mp4:
                continue
            try:
                r = transcribe(mp4, model_name, max_s, device, compute)
            except KeyboardInterrupt:
                raise
            except Exception as e:                 # one clip's failure isn't fatal
                log.warning("transcribe failed for %s: %s", cid, e)
                continue
            cache[cid] = r
            out.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                           encoding="utf-8")     # flush per clip so a crash loses nothing
            log.info("transcribe %s [%s %.2f] %d words: %s", cid[:18], r["lang"],
                     r["lang_prob"], len(r["words"]), r["text"][:60] or "(no speech)")
    except KeyboardInterrupt:
        log.warning("transcribe interrupted - %d clip(s) cached", len(cache))
        guard.unlink(missing_ok=True)   # a deliberate stop is not a GPU crash
        raise

    if not out.exists():                            # ensure the cache file exists
        out.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    guard.unlink(missing_ok=True)
    # Done-marker, written only after every clip has been attempted. transcripts.json
    # itself cannot serve as one: it is flushed per clip so a crash loses no work, so it
    # exists even when the stage died a third of the way in — and run_daily's "output
    # exists = stage done" check then SKIPPED the rest on the retry. Measured over
    # August: all 7 crash days shipped partial captions (08-31 had 2 of 9 clips), all 22
    # clean days were complete. In lean mode the captions are what carries the video.
    # Same split shorts.py already uses (renders vs. its own done.json).
    (work / "transcripts.done.json").write_text(
        json.dumps({"clips": len(cache), "device": device}), encoding="utf-8")
    return work
