# Twitch → YouTube Long-Form Pipeline — Plan

## Vision
Fully automated daily pipeline: fetch League of Legends Twitch clips → filter to actual
highlights → enrich with real match data → write grounded voiceover commentary → TTS →
assemble an 8–12 minute long-form YouTube video → upload with credits.

**Format decision (2026-06-10): long-form first.** Long-form unlocks mid-roll ads at 8+ min,
far higher RPM than Shorts, and YPP's "inauthentic content" policy demands significant
original commentary — which only fits long-form. Shorts become a derived byproduct later
(caption/composer code for that is preserved in `_archive/clip_intel/export/`).

## Monetization & legal constraints (from research, 2026-06)
- YouTube's *inauthentic content* policy (renamed July 2025) actively demonetizes raw
  compilation channels. Per-clip, fact-grounded commentary + editing is what qualifies as
  "significant original value". Commentary must NOT be templated/repetitive.
- Twitch clips: streamer permission is the safe route (track it; auto-credit always).
  In-stream music can trigger Content ID → music-detection/ducking step planned.
- Riot has historically allowed monetized fan content ("Legal Jibber Jabber") — re-verify
  current policy before launch.

## Repo layout
```
Clips/
  PLAN.md
  requirements.txt
  .env                  TWITCH_CLIENT_ID/SECRET (+ later RIOT_API_KEY, GEMINI_API_KEY)
  pipeline/             NEW — the automation (see module map)
  twitch_clips/         Existing fetcher + audio/motion classifier + labeled datasets (kept)
  data/                 Runtime data (created on first run): raw/ work/ output/ state.json
  _archive/             Old shorts app + long-video experiment (see _archive/README.md)
  venv/
```

## Pipeline stages & module map

| # | Stage | Module | Status |
|---|---|---|---|
| 1 | Fetch clips (top-of-game or challenger broadcaster list) | `pipeline/fetch.py` | ✅ working |
| 2 | Pre-filter: broadcaster blacklist + keywords + audio hype + motion (cheap, local) | `pipeline/prefilter.py` | ✅ working (reuses twitch_clips scoring) |
| 3 | Stepwise local VLM filter: gameplay vote, pro-play, kill-feed/event-log detection, rule decisions, HTML report | `pipeline/vlm_filter.py` + `kill_detect.py` | ✅ tuned vs labels (precision 0.25→1.0 on 06-09 w/ stage 3.5) |
| 3.5 | Gemini full-video judge re-ranks local survivors (quality/focus) | `pipeline/api_judge.py` | ✅ working, ~1¢/day |
| 4 | Match linker: clip timestamp+streamer → real match data | `pipeline/match_linker.py` | ❌ stub — **the key missing tool** |
| 5 | HUD OCR: kill feed / scoreboard events from frames | `pipeline/hud_ocr.py` | ❌ stub |
| 6 | Commentary: grounded lines + video intro (Gemini, Ollama fallback) | `pipeline/commentary.py` | ✅ v2 |
| 7 | TTS voiceover + word timestamps | `pipeline/tts.py` | ✅ working (ported from clip_intel) |
| 8 | Assemble: intro card, animated lower-thirds, fades, VO ducking, music bed, loudnorm | `pipeline/assemble.py` | ✅ v2, sample verified |
| 9 | Credits + chapters + description | `pipeline/credits.py` | ✅ working |
| 10 | Upload to YouTube | `pipeline/upload.py` | ❌ stub (OAuth setup documented inside) |
|   | Orchestrator CLI | `pipeline/run_daily.py` | ✅ working, skips unimplemented stages gracefully |
|   | Config / state | `pipeline/config.py`, `state.py`, `config.yaml` | ✅ working |

## Missing tools (build order)
1. **Clip→match linker** (`match_linker.py`) — clip has `broadcaster_name` + `created_at`.
   Map broadcaster → known summoner name(s) (manual table in config to start) → Riot
   match-v5: matches by PUUID in time window → match detail (champion, KDA, gold, rank).
   No public tool does this. Riot API is free & reliable; u.gg/op.gg have no public API
   (scraping is brittle + ToS risk) — provider interface supports both, Riot first.
2. **HUD OCR event extractor** (`hud_ocr.py`) — VLM (Qwen3-VL / Gemini) reads kill feed,
   champion names, scoreboard from sampled frames → structured events. Enables outplay
   detection (multikills, 1vX) + grounded commentary without match data.
3. **Grounding check** — reject commentary sentences containing claims not present in
   OCR/match facts (simple LLM-as-judge or rule check). Add inside `commentary.py`.
4. **Music detection/ducking** — detect copyrighted in-stream music segments, duck or mute.
5. **Permission manager** — track streamer outreach/consent in `state.json`; only use
   clips from approved broadcasters when `require_permission: true`.
6. **Uploader** — YouTube Data API v3, OAuth desktop flow. NB: default quota = 10k units/day,
   one upload = 1600 units.

## Costs (researched 2026-06)
- VLM filtering: Gemini Flash class, video ≈ 100–300 tokens/sec → ~30s clip ≈ cents/day for 200 clips.
- TTS: edge-tts free (current); upgrade path: Chatterbox (open source) or Fish Audio/Inworld (~$15/1M chars).
- No video generation costs — clips are the content.

## Roadmap
**Phase 1 — end-to-end manual-trigger video (current)**
- [x] Fetch + prefilter + TTS + credits + orchestrator skeleton
- [x] Test VLM filter on a real day of clips, tune threshold (labeling tool + eval harness)
- [x] Polish assemble.py (transitions, streamer-name overlay, loudness normalization)
- [x] First sample video produced (data/output/2026-06-09_sample_noVO.mp4); full VO version = one command (see MORNING_NOTES.md)

**Phase 2 — grounded commentary**
- [ ] Match linker (Riot API) + broadcaster→summoner table
- [ ] HUD OCR events
- [ ] Grounding check; commentary quality pass

**Phase 3 — publish & automate**
- [x] YouTube uploader + metadata generator (OAuth setup steps in pipeline/upload.py)
- [ ] Permission manager + music ducking
- [ ] Schedule daily run (Windows Task Scheduler → run_days.bat pattern); human approval optional

**Phase 4 — scale**
- [ ] Shorts derived from best clip of each video (port _archive captions/composer)
- [ ] Thumbnail generation; A/B titles

## Running
```bash
venv\Scripts\python.exe -m pipeline.run_daily            # full run for yesterday
venv\Scripts\python.exe -m pipeline.run_daily --date 2026-06-09 --stop-after prefilter
venv\Scripts\python.exe -m pipeline.run_daily --skip vlm_filter,upload
```
