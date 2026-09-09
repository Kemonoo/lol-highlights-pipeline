"""Detect speech captions the STREAMER already burned into the source clip.

Why this exists: a growing number of Twitch streamers run a live-caption widget
(Streamlabs and friends), which bakes a word-highlighted subtitle into the video at
roughly y=0.90. We cannot remove it — it is pixels in the source. So the daily video
was showing two transcriptions of the same speech at once: theirs near the bottom
edge, ours at `video.caption_y_frac` just above it. On 2026-09-08 this affected 8 of
the 17 clips that had any speech at all.

The owner's rule, which this implements together with assemble._caption_dt:

  * source caption present AND the streamer is speaking English -> we add NOTHING.
    The viewer can already read it, and our line is redundant clutter.
  * source caption present but the speech is NOT English -> keep ours. Their caption
    is (necessarily) in their own language, so an English line above it is the
    translation, and two lines is the acceptable cost of not being able to erase one.
  * no source caption -> ours, as before.

The language of the burned caption is never read off the screen: a live-caption widget
transcribes its own streamer, so the spoken language whisper already detected
(transcripts.json) is the same language, and is free.

### How the detection works, and why it is not a model

A caption widget draws **only while someone is talking**. Everything else in that band —
HUD, minimap, chat, sub goals, sponsor logos, now-playing widgets — is there whether or
not the streamer is speaking. That difference is the entire signal, and whisper already
told us exactly when speech happens, so it can be measured directly:

    score(rows during speech) - score(rows during silence)

where `score` counts outlined-text strokes (a bright pixel with a dark pixel a few px
away — captions are always drawn with a heavy outline or a box, precisely so they read
over video). Anything permanent cancels out in the subtraction; only text that comes and
goes with speech survives it. Gameplay motion is broadband and does not concentrate in
one row, so the statistic is a MAX OVER ROWS of the difference.

This replaced a version that asked the local VLM "are there subtitles here?" over
evenly-spaced frames. That scored 2/4 on known clips and added ~10 min to the nightly
run: a widget draws nothing during silence, so most evenly-spaced frames showed no
caption *even on clips that had one*, and a 4B model happily called HUD text a subtitle.
The pixel test needs no model, no GPU and no network, runs in ~2s per clip, and is
calibrated against hand-labelled clips in tests/test_captions.py.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.burned_captions")

CACHE = "burned_captions.json"


def speech_windows(words: list | None) -> list[tuple[float, float]]:
    """Merge word timings into [start, end] runs of continuous speech."""
    spans = []
    for w in words or []:
        if not (w.get("word") or "").strip():
            continue
        try:
            t0, t1 = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if spans and t0 - spans[-1][1] < 0.45:
            spans[-1][1] = max(spans[-1][1], t1)
        else:
            spans.append([t0, max(t0, t1)])
    return [(a, b) for a, b in spans]


def sample_times(words: list | None, duration: float, n: int) -> tuple[list, list]:
    """(speech_times, silence_times) — moments to compare against each other.

    Speech times sit inside a run at least 0.5s long (a caption is on screen by then).
    Silence times sit at least 0.8s clear of every run, so a caption that lingers a
    little after the last word does not leak into the baseline.
    """
    if duration <= 0:
        return [], []
    runs = [(a, b) for a, b in speech_windows(words) if b - a >= 0.5]
    if not runs:
        return [], []

    # Step THROUGH each run rather than taking one point per run: a clip is often a
    # single unbroken 12s sentence, and one sample per run left such clips with too few
    # frames to compare, which read as "no caption" on clips that plainly had one.
    # The 0.4s offset lets the widget catch up with the first word.
    speech = []
    for a, b in runs:
        t = a + 0.4
        while t <= b:
            speech.append(t)
            t += 0.7
    speech = _spread(speech, n)

    silence = []
    t = 0.4
    while t < duration - 0.3:
        if all(t < a - 0.8 or t > b + 0.8 for a, b in runs):
            silence.append(t)
        t += 0.35
    return speech, _spread(silence, n)


def _spread(times: list, n: int) -> list:
    if len(times) <= n:
        return times
    step = len(times) / n
    return [times[int(i * step)] for i in range(n)]


def _band(mp4: Path, times: list, y_frac: float):
    """Grayscale arrays of the strip between our caption line and the frame bottom."""
    import numpy as np
    from PIL import Image

    top = min(max(y_frac + 0.02, 0.05), 0.95)
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            p = Path(td) / f"b{i}.png"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1",
                 "-vf", f"crop=iw:ih*{1 - top:.3f}:0:ih*{top:.3f},scale=640:-1",
                 str(p)], capture_output=True)
            if p.exists():
                out.append(np.asarray(Image.open(p).convert("L")).astype(np.int16))
    if not out:
        return None
    h = min(a.shape[0] for a in out)
    return np.stack([a[:h] for a in out])


def _stroke_rows(frames):
    """Per-row density of outlined-text strokes, averaged over `frames`.

    A caption stroke is a bright pixel with a dark pixel a few px to its left or right —
    white-on-black-outline, or white-on-dark-box. Ordinary bright gameplay (a lit lane, a
    health bar) has no such neighbour and scores ~0, which is what keeps this from simply
    measuring brightness.
    """
    import numpy as np
    bright = frames > 195
    dark = frames < 90
    near = np.zeros_like(dark)
    for s in (2, 3, 4, 5):
        near[:, :, s:] |= dark[:, :, :-s]
        near[:, :, :-s] |= dark[:, :, s:]
    return (bright & near).mean(axis=2).mean(axis=0)     # -> (rows,)


def score(mp4: Path, words: list | None, y_frac: float, samples: int) -> float:
    """How much more outlined text the caption band carries during speech than silence.

    ~0 for a clip with no caption widget (everything in the band is permanent, so it
    cancels); clearly positive for one with a widget. Negative values are noise.
    """
    import numpy as np

    from ..filtering.kill_detect import probe_duration

    sp_t, si_t = sample_times(words, probe_duration(mp4), samples)
    if len(sp_t) < 2 or len(si_t) < 2:
        return 0.0
    sp, si = _band(mp4, sp_t, y_frac), _band(mp4, si_t, y_frac)
    if sp is None or si is None:
        return 0.0
    h = min(sp.shape[1], si.shape[1])
    diff = _stroke_rows(sp[:, :h]) - _stroke_rows(si[:, :h])
    if diff.size < 5:
        return 0.0
    # a caption is several rows tall, so smooth before taking the max — a single hot row
    # is noise, a run of them is a line of text
    k = np.ones(5) / 5
    return float(np.convolve(diff, k, mode="valid").max())


def detect(cfg: dict, date_label: str, clips: list[dict],
           transcripts: dict | None = None) -> dict:
    """{clip_id: bool} — does the source already carry burned-in speech captions?

    Cached in work/<date>/burned_captions.json; only clips missing from the cache are
    measured. Never raises: any failure degrades to "no caption", i.e. we caption the
    clip exactly as before.
    """
    v = cfg.get("video", {})
    if not v.get("skip_caption_when_source_has_one", True):
        return {}
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    raw = data / "raw" / date_label
    cache_path = work / CACHE
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8").rstrip("\x00"))
        except (ValueError, OSError):
            cache = {}

    todo = [c for c in clips if c.get("id") and c["id"] not in cache]
    if todo:
        threshold = float(v.get("source_caption_threshold", 0.010))
        samples = int(v.get("source_caption_samples", 6))
        y_frac = float(v.get("caption_y_frac", 0.70))
        for c in todo:
            mp4 = raw / f"{c['id']}.mp4"
            if not mp4.exists():
                continue
            try:
                s = score(mp4, (transcripts or {}).get(c["id"], {}).get("words"),
                          y_frac, samples)
            except Exception as e:                   # never let this break a render
                log.debug("burned-caption score failed for %s: %s", c["id"], e)
                continue
            cache[c["id"]] = s >= threshold
            log.debug("burned-caption score %.4f (%s) %s", s,
                      "yes" if s >= threshold else "no", c["id"])
        try:
            work.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except OSError as e:
            log.debug("could not cache burned-caption results: %s", e)
    n = sum(1 for x in cache.values() if x)
    if n:
        log.info("source already carries captions on %d/%d clip(s) — skipping ours "
                 "on the English ones", n, len(cache))
    return cache
