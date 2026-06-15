"""Stage 5 — HUD OCR event extractor (STUB, Phase 2).

Goal: read structured events from the LoL HUD in sampled frames so commentary is
grounded even without match data, and so "outplay" detection becomes a rule, not a vibe.

Targets (per frame, then merged across frames into clip["hud_events"]):
  - kill feed (top right): killer champion -> victim champion, multikill banners
  - scoreboard (top): kills per team, gold, game timer
  - champion + level (bottom center), health/mana state
  - shutdown / "Ace" / "Penta Kill" banners

Planned approach:
  1. Reuse vlm_filter.sample_frames() at higher density (1 fps).
  2. Send frames to a strong-OCR VLM (Qwen3-VL local, or Gemini) with a JSON schema
     prompt per HUD region; optionally crop regions first with ffmpeg for accuracy.
  3. Merge frame-level reads into events with timestamps (frame index / fps).
  4. Derive flags: multikill, 1vX (enemies visible vs allies), low-hp escape.

Output feeds commentary.py and could later replace the audio-hype prefilter as the
main highlight detector.
"""
import logging
from pathlib import Path

log = logging.getLogger("pipeline.hud_ocr")


def extract_events(clip: dict, cfg: dict) -> list[dict]:
    """Return list of {t, event, detail} for a clip. NOT IMPLEMENTED."""
    raise NotImplementedError("Phase 2 — see module docstring.")


def run(cfg: dict, state, date_label: str) -> Path:
    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    log.info("hud_ocr not implemented — skipping")
    return work
