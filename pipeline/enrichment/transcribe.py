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

GPU isolation (`Transcriber`, 2026-09-30): on CUDA, whisper runs in a CHILD process.
Some clips crash ctranslate2 natively (exit 127 / 0xC0000094) - deterministic per clip,
~2% of clips, a process kill with no Python exception. Root-caused on 09-29's
ColdAdorableDragon: a clip with no speech, where beam search hallucinates a phrase
("Thank you for watching!") and aligning its word timestamps dies on every CUDA
compute type; CPU is fine. vad_filter / beam_size=1 avoid it but VAD drops the
screams and laughs the captions exist for (NOOOOO 11 words -> 0), so the fix is
containment: the child dies, the pipeline doesn't, that one clip is redone on CPU and
a fresh GPU child takes the rest. Before this every crash killed the run: 10-min bat
retry, then the whole stage on CPU.

Requires: pip install faster-whisper
"""
import json
import logging
import subprocess
import sys
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


class Transcriber:
    """transcribe() with the GPU contained in a child process (see module docstring).

    `with Transcriber(cfg) as t: t(audio, max_s)` - same result dicts as transcribe().
    CPU (resolved device, or `transcribe.gpu_isolation: false`) runs in-process as
    before. A dead child = that clip redone on cpu/int8 via `fallback`, then a fresh
    child; after `max_gpu_crashes` crashes the rest of the run stays on CPU.
    `worker_cmd` / `fallback` are injectable for tests."""

    def __init__(self, cfg: dict, worker_cmd: list | None = None, fallback=None):
        tc = cfg.get("transcribe", {})
        from ..hardware import whisper_device
        self.model = tc.get("model", "small")
        self.device, self.compute = whisper_device(cfg)
        self.isolate = self.device != "cpu" and tc.get("gpu_isolation", True)
        self.max_crashes = int(tc.get("max_gpu_crashes", 3))
        self.crashes = 0
        self.worker_cmd = worker_cmd or [sys.executable, "-m",
                                         "pipeline.enrichment.transcribe", "--worker",
                                         self.model, self.device, self.compute]
        self.fallback = fallback or (lambda audio, max_s: transcribe(
            Path(audio), self.model, max_s, "cpu", "int8"))
        self._proc = None

    @property
    def used(self) -> str:
        return "cpu" if not self.isolate else self.device

    def _start(self):
        from ..config import ROOT
        self._proc = subprocess.Popen(self.worker_cmd, cwd=str(ROOT), text=True,
                                      encoding="utf-8", stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE)

    def _ask(self, audio, max_s):
        """-> ("ok", result) | ("error", msg) | ("crash", exit code)."""
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        try:
            self._proc.stdin.write(json.dumps({"audio": str(audio), "max_s": max_s}) + "\n")
            self._proc.stdin.flush()
            for line in self._proc.stdout:          # skip anything a library printed
                if line.startswith("RESULT "):
                    return "ok", json.loads(line[7:])
                if line.startswith("ERROR "):
                    return "error", line[6:].strip()
        except (BrokenPipeError, OSError):
            pass
        code = self._proc.wait()
        self._proc = None
        return "crash", code

    def __call__(self, audio, max_s=None) -> dict:
        if not self.isolate:
            return transcribe(Path(audio), self.model, max_s, self.device, self.compute)
        kind, val = self._ask(audio, max_s)
        if kind == "ok":
            return val
        if kind == "error":
            raise RuntimeError(val)
        self.crashes += 1
        log.warning("transcribe: GPU worker crashed on %s (exit %s) - redoing that clip "
                    "on cpu/int8%s", Path(audio).name, val,
                    "; rest of the run stays on CPU" if self.crashes >= self.max_crashes
                    else "")
        if self.crashes >= self.max_crashes:
            self.isolate, self.device, self.compute = False, "cpu", "int8"
        return self.fallback(audio, max_s)

    def close(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=30)
            except Exception:
                self._proc.kill()
        self._proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _worker(model_name: str, device: str, compute: str) -> int:
    """Child side: one JSON request per stdin line, one RESULT/ERROR line back."""
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            r = transcribe(Path(req["audio"]), model_name, req.get("max_s"), device, compute)
            print("RESULT " + json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as e:                      # a Python error, not a crash
            print(f"ERROR {type(e).__name__}: {e}", flush=True)
    return 0


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
    max_s = tc.get("max_seconds")
    skip = {l.lower() for l in tc.get("skip_languages", [])}

    t = Transcriber(cfg)
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
                r = t(mp4, max_s)
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
        raise
    finally:
        t.close()

    if not out.exists():                            # ensure the cache file exists
        out.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    # Done-marker, written only after every clip has been attempted. transcripts.json
    # itself cannot serve as one: it is flushed per clip so a crash loses no work, so it
    # exists even when the stage died a third of the way in — and run_daily's "output
    # exists = stage done" check then SKIPPED the rest on the retry. Measured over
    # August: all 7 crash days shipped partial captions (08-31 had 2 of 9 clips), all 22
    # clean days were complete. In lean mode the captions are what carries the video.
    # Same split shorts.py already uses (renders vs. its own done.json).
    (work / "transcripts.done.json").write_text(
        json.dumps({"clips": len(cache), "device": t.used, "gpu_crashes": t.crashes}),
        encoding="utf-8")
    return work


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    sys.exit(_worker(*sys.argv[2:5]))
