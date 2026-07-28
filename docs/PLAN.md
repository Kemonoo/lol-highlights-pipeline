# Twitch → YouTube Long-Form Pipeline — Plan

> **Current format is LEAN (since 2026-06-19):** no voiceover/commentary/music bed; foreign
> clips get burned English captions instead of narration. The commentary+TTS machinery below
> is intact but switched off. See DEVLOG.md for why, and the monetization caveat below.

## Vision
Fully automated daily pipeline: fetch League of Legends Twitch clips → filter to actual
highlights → (optionally) enrich with real match data and grounded commentary → assemble an
8–12 minute long-form YouTube video with brand intro/outro → upload with credits. Shorts
derived from the same run.

**Format decision (2026-06-10): long-form first.** Long-form unlocks mid-roll ads at 8+ min,
far higher RPM than Shorts, and YouTube's "inauthentic content" policy demands significant
original commentary — which only fits long-form. Shorts are a derived byproduct.

## Monetization & legal constraints (researched 2026-06)
- YouTube's *inauthentic content* policy actively demonetizes raw compilation channels.
  Per-clip, fact-grounded commentary + editing qualifies as "significant original value".
  Commentary must NOT be templated/repetitive.
- Twitch clips: per-streamer credit links generated on every video; permission tracking
  available (`permissions.require_permission` config flag).
- In-stream music can trigger Content ID → music-detection/ducking step planned.
- Riot has historically allowed monetized fan content ("Legal Jibber Jabber") — re-verify
  current policy before launch.

## Pipeline stages

| # | Stage | Module | Status |
|---|---|---|---|
| 0 | Viewer feedback loop (YouTube comments → actionable signals) | `pipeline/feedback/feedback.py` | ✅ working |
| 1 | Fetch clips (Twitch Helix top-clips or challenger broadcaster list) | `pipeline/ingestion/fetch.py` | ✅ working |
| 2 | Pre-filter: broadcaster blacklist + keywords + audio hype + motion | `pipeline/filtering/prefilter.py` | ✅ working |
| 3 | Stepwise local VLM filter: gameplay vote, pro-play, kill-feed detection, HTML report | `pipeline/filtering/vlm_filter.py` + `kill_detect.py` | ✅ tuned (precision 0.25→1.0) |
| 4 | Gemini full-video judge: quality/focus scoring, duration-aware selection | `pipeline/filtering/api_judge.py` | ✅ working, ~1¢/day |
| 5 | Match linker: clip timestamp → real match data (KDA, champion, rank) | `pipeline/enrichment/match_linker.py` | ❌ stub — Phase 2 |
| 6 | HUD OCR: kill feed / scoreboard / multikill banners from frames | `pipeline/enrichment/hud_ocr.py` | ❌ stub — Phase 2 |
| 6.5 | Transcribe: streamer speech → English (faster-whisper `task=translate`, CPU) | `pipeline/enrichment/transcribe.py` | ✅ working — feeds captions |
| 7 | Commentary: montage-caster lines + video intro (Gemini, Ollama fallback) | `pipeline/production/commentary.py` | ⏸ **off in lean mode** (intact) |
| 8 | TTS voiceover + word timestamps (Kokoro local `af_bella` / edge-tts fallback) | `pipeline/production/tts.py` | ⏸ **off in lean mode** (intact) |
| 9 | Assemble: KEMONO brand intro, countdown badges, animated nameplates, English captions on foreign clips, music outro over an NCS drop | `pipeline/production/assemble.py` | ✅ v4 |
| 9.5 | Brand: KEMONO logo (God Fist Lee Sin) + animated intro sting | `pipeline/production/brand.py` | ✅ working |
| 10 | Credits + chapters + description, clickbait title + date series marker | `pipeline/production/credits.py` | ✅ working |
| 11 | Thumbnail: AI reaction composite (Gemini 2.5 Flash Image; PIL fallback) | `pipeline/production/thumbnail.py` | ✅ working, ~4¢/run |
| 12 | Upload to YouTube (resumable, OAuth desktop flow, **idempotent per date**) | `pipeline/publishing/upload.py` | ✅ working |
| 13 | Shorts: face-cam split, English speech captions, no VO/music, English title | `pipeline/publishing/shorts.py` | ⚠️ works; only 1/3 produced in the last run — see DEVLOG open items |
| — | Cleanup: prune raw MP4s older than keep_raw_days | `pipeline/publishing/cleanup.py` | ✅ working |
| — | Orchestrator CLI with stage skip/resume/force | `pipeline/run_daily.py` | ✅ working |

## Roadmap

**Phase 1 — end-to-end automated pipeline** ✅ complete
- Fetch → filter → TTS → assemble → upload working end-to-end
- VLM filter tuned against labeled clips (precision 1.0)
- Commentary grounded, style rotation to avoid monotony
- Shorts with face-cam split layout, Whisper speech captions (auto-translated to English)
- Task Scheduler daily automation (`setup_schedule.bat`)
- Viewer comment feedback loop (stage 0; owner feedback now goes through the dev chat)

**Phase 2 — grounded commentary**
- [ ] Match linker (Riot API): broadcaster login → PUUID → match facts (champion, KDA)
- [ ] HUD OCR: read kill feed / scoreboard / multikill banners as structured events
- [ ] Grounding check: reject commentary sentences not supported by OCR/match facts

**Phase 3 — scale & quality**
- [ ] Fail-clip detection: judge underrates "streamer gets outplayed" clips (known gap)
- [x] Music bed: royalty-free NCS tracks (random per run + attribution) with
      sidechain ducking under clip audio (assemble.master)
- [ ] Content-ID safety: detect copyrighted *in-stream* audio and mute/duck those segments
- [ ] Permission manager: track streamer consent, respect takedowns
- [x] Thumbnail generation (champion splash + text overlay) — A/B title variants still open

## Running
```bash
venv\Scripts\python.exe -m pipeline.run_daily            # full run for yesterday
venv\Scripts\python.exe -m pipeline.run_daily --date 2026-06-09
venv\Scripts\python.exe -m pipeline.run_daily --stop-after vlm_filter
venv\Scripts\python.exe -m pipeline.run_daily --only assemble --force
```
