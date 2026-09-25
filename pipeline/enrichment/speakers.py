"""Who says each caption phrase? For colour-coded Short captions (owner, 2026-09-25).

A Short with a duo stream shows two webcams side by side; the owner wants each person's
lines in their own colour, inside their own panel, so viewers can tell who is talking.
Voices off camera (a teammate on voice chat) get a neutral colour in the middle.

How: the `speakers` role (default: the judge's Gemini, which already watches whole clips
with sound) gets the clip plus the numbered caption phrases with their times, and the
webcams named by position, and answers one letter per phrase ("A", "B", ... or "other").
It hears the voices AND sees whose mouth moves, which is what links a voice to a webcam —
audio-only speaker separation would still not know which panel a voice belongs to.
Measured 2026-09-25 on a duo clip (09-24, Dantes): the full model and the lite model
disagreed on half the phrases, and whose webcam moves more says nothing (one streamer
gestured the whole time), so the full model is the default and the owner judges by ear.

Any failure (no key, quota, unparsable answer) -> None -> the Short keeps plain captions.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.speakers")

SCHEMA = {"type": "object", "properties": {"speakers": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "who": {"type": "string"}},
    "required": ["i", "who"]}}}, "required": ["speakers"]}


def where(box: tuple, w: int, h: int) -> str:
    """'bottom-left' etc. — how the prompt names a webcam."""
    x, y, bw, bh = box
    cx, cy = (x + bw / 2) / w, (y + bh / 2) / h
    v = "top" if cy < 0.4 else "bottom" if cy > 0.6 else "middle"
    hz = "left" if cx < 0.4 else "right" if cx > 0.6 else "centre"
    return f"{v}-{hz}"


def prompt(cams: list, groups: list, w: int, h: int) -> str:
    letters = [chr(65 + i) for i in range(len(cams))]
    seen = ", ".join(f"{k}: the webcam at the {where(b, w, h)} of the frame"
                     for k, b in zip(letters, cams))
    lines = "\n".join(f'{i}: {g["start"]:.1f}-{g["end"]:.1f}s "{g["text"]}"'
                      for i, g in enumerate(groups))
    return (f"This League of Legends stream clip shows these people's webcams: {seen}. "
            "Other voices (a teammate on voice chat, a video playing) may be heard "
            "without a webcam. Below is the English transcript split into numbered "
            "phrases with their times. For EACH phrase say who speaks it: "
            f"{', '.join(letters)} or \"other\". Use the voices AND whose mouth moves. "
            'Answer ONLY JSON: {"speakers": [{"i": 0, "who": "A"}, ...]}\n'
            f"Phrases:\n{lines}")


def parse(ans, n_groups: int, n_cams: int) -> list[str] | None:
    """-> one label per phrase ("A".. or "other"); None if the answer is unusable.
    Pure (tested)."""
    items = (ans or {}).get("speakers") if isinstance(ans, dict) else None
    if not isinstance(items, list) or not items:
        return None
    valid = {chr(65 + i) for i in range(n_cams)}
    out = ["other"] * n_groups
    got = 0
    for it in items:
        try:
            i, who = int(it["i"]), str(it["who"]).strip().upper()
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n_groups:
            out[i] = who if who in valid else "other"
            got += 1
    return out if got >= max(1, n_groups // 2) else None


def label_groups(cfg: dict, mp4: Path, groups: list, cams: list) -> list[str] | None:
    """Ask the `speakers` role (see module docstring). None on any failure."""
    sh = cfg.get("shorts", {})
    if not groups or not cams:
        return None
    try:
        from ..filtering.api_judge import shrink
        from ..providers import get_provider
        size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "stream=width,height", "-of", "csv=p=0",
                               str(mp4)], capture_output=True, text=True).stdout.strip()
        w, h = (int(v) for v in size.split(",")[:2])
        provider = get_provider(cfg, sh.get("speaker_role", "judge"))
        with tempfile.TemporaryDirectory() as td:
            small = shrink(mp4, Path(td) / "s.mp4", {"shrink_height": 720})
            if small is None:
                return None
            ans = provider.complete_json(prompt(cams, groups, w, h),
                                         video=small.read_bytes(), schema=SCHEMA)
        labels = parse(ans, len(groups), len(cams))
        if labels:
            log.info("  speakers: %s", " ".join(labels))
        return labels
    except Exception as e:
        log.warning("  speakers: labelling failed (%s) - plain captions", e)
        return None
