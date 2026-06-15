"""Stage 4 — Match linker (STUB, Phase 2). THE key missing tool.

Goal: a Twitch clip gives us `broadcaster_name` + `created_at` (UTC). Link it to the
actual LoL match being played so commentary can cite real facts (champion, KDA, gold,
rank, objectives) instead of guessing.

Planned Riot flow (provider="riot"):
  1. config match_linker.summoner_map: twitch login -> ["GameName#TAG", ...]
     (manual table to start; Challenger players often have known accounts)
  2. account-v1: GameName#TAG -> puuid (cache in state.json)
  3. match-v5: /matches/by-puuid/{puuid}/ids?startTime=...&endTime=...
     window = clip.created_at ± 45 min
  4. match-v5: /matches/{id} -> pick the match whose gameStart < clip_time < gameEnd
  5. Extract participant facts for the streamer's puuid:
     champion, kills/deaths/assists, gold, items, multikills, rank tier (league-v4)
  6. Attach as clip["match_facts"] for commentary.py

Notes:
  - Riot dev API key is free (rate-limited 100 req/2 min) — enough for ~12 clips/day.
  - u.gg / op.gg ("opgg"/"ugg" providers): no public APIs; they are themselves Riot API
    consumers. Scraping is brittle + ToS risk. Keep the provider interface, prefer Riot.
  - Clips are created with a delay vs. live play (stream delay + clip window), so match by
    overlap of game duration window, not exact timestamp.
"""
import logging
from pathlib import Path

log = logging.getLogger("pipeline.match_linker")


def link_clip(clip: dict, ml_cfg: dict, state) -> dict | None:
    """Return match_facts dict for a clip, or None if unlinkable. NOT IMPLEMENTED."""
    raise NotImplementedError("Phase 2 — see module docstring for the implementation plan.")


def run(cfg: dict, state, date_label: str) -> Path:
    work = Path(cfg["paths"]["data_abs"]) / "work" / date_label
    if not cfg["match_linker"]["enabled"]:
        log.info("match_linker disabled — skipping (commentary will use VLM summary only)")
        return work
    raise NotImplementedError("Phase 2")
