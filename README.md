# KEMONO — automated LoL Twitch → YouTube pipeline

A fully automated pipeline that turns each day's top League of Legends Twitch clips into a
polished daily YouTube video — AI clip selection, an animated brand intro, countdown
editing, an AI-composited thumbnail, and auto-upload — plus derived vertical Shorts.

One command in, one ~9-minute video out:

```
venv\Scripts\python.exe -m pipeline.run_daily
```

## Two modes

- **Lean (default)** — Synapse-style: curated **English** clips + transitions + the KEMONO
  brand intro, the clips' own audio, no voiceover/music. Clean and fast.
- **Produced** — flip on AI commentary + neural voiceover + a music bed (all config
  toggles; the machinery is intact). Useful if you want a narrated, more "transformed"
  cut for monetization.

## How it works

| Stage | What it does | Cost |
|---|---|---|
| fetch | Pulls the day's top LoL clips from the Twitch Helix API (metadata; lazy MP4 downloads) | free |
| prefilter | Cheap local scoring: title keywords, audio-hype, motion, blacklist, language allowlist | free |
| vlm_filter | Local vision model (Ollama) judges each clip stepwise: real gameplay? esports? kills in the feed? | free (GPU) |
| api_judge | Gemini watches each survivor *as video*, scores play/entertainment, orders a countdown | ~1¢/day |
| transcribe | faster-whisper detects the spoken language and translates speech → English (off in lean mode) | free (GPU) |
| commentary · tts | *(produced mode only)* grounded caster lines + neural voiceover | ~free |
| assemble | FFmpeg: KEMONO brand intro, #N countdown badges, animated streamer nameplates, fades, optional slow-mo replay, outro, loudness mastering | free |
| credits | Clickbait title + date marker, chapters, per-streamer credit links | free |
| thumbnail | Gemini 2.5 Flash Image ("Nano Banana") composite: real reaction face + gameplay + gold announcement (local PIL fallback) | ~4¢/img |
| upload | Resumable upload via YouTube Data API (private by default) | free |
| shorts | Vertical Shorts from the top clips: facecam split, English captions, top hook text | free |

Every stage is cached and resumable. If a day's selection is shorter than
`target_minutes_ideal`, the pipeline fetches deeper into that day's clips until it's long
enough.

## Setup

1. **Python**: `python -m venv venv && venv\Scripts\pip install -r requirements.txt`
2. **FFmpeg**: install and put `ffmpeg`/`ffprobe` on PATH
3. **Ollama** (free local vision filtering): install from ollama.com, then
   `ollama pull qwen3-vl:4b` (4GB VRAM) or a larger vision model
4. **Keys** — create `.env` in the repo root:
   ```
   TWITCH_CLIENT_ID=...        # dev.twitch.tv -> register an app
   TWITCH_CLIENT_SECRET=...
   GEMINI_API_KEY=...          # aistudio.google.com — needs BILLING for AI thumbnails
   ```
   The AI thumbnail model (`gemini-2.5-flash-image`) is **not** on the free tier; enable
   pay-as-you-go billing on the API project, or set `thumbnail.provider: local`.
5. **Music** *(produced mode)*: see `assets/music/README.md` (tracks are local-only).
6. **YouTube upload** (optional): follow the OAuth setup in `pipeline/publishing/upload.py`,
   then set `upload.enabled: true` in `pipeline/config.yaml`.

## Usage

```bash
python -m pipeline.run_daily                      # full run for yesterday
python -m pipeline.run_daily --date 2026-06-08    # specific day
python -m pipeline.run_daily --stop-after vlm_filter   # partial run
python -m pipeline.run_daily --only assemble --force   # redo one stage
```

Every run writes `data/work/<date>/report.html` — a visual breakdown of every keep/reject
decision with thumbnails, scores, and reasons.

## Tuning (pipeline/config.yaml)

Lean/produced: `commentary.enabled`, `tts.enabled`, `video.music_enabled`,
`transcribe.enabled`. Selection: `prefilter.include_languages` (`[en]`),
`video.target_minutes_ideal`, `blacklist.broadcasters`, `api_judge.min_entertainment`.
Packaging: `upload.title_styles` + `title_date`, `thumbnail.provider` (gemini/local),
`video.brand` (KEMONO intro / God Fist Lee Sin splash). Shorts: `shorts.target_seconds`,
`shorts.pre_roll_s`. Decision rules recompute from cache, so re-tuning costs seconds.

## Design notes

The filter is a **cost cascade**: free local checks discard ~90% of clips; the paid video
judge only sees the survivors. Small local VLMs get *decomposed* questions (one cropped
image — kill feed, banner, event log) because they fail at open-ended judgment but are
reliable at "count the kill banners here"; taste ("is this play actually good?") is
delegated to Gemini watching the full clip. Multilingual handling: faster-whisper
translates streamer speech to English so nothing we publish is in another language.

**Monetization note:** lean mode is a thin "transformation" of reused content, which is a
risk for YouTube Partner Program eligibility. Grow first; add an originality layer (the
produced-mode commentary, a sharper niche, or per-language channels — the real automation
moat) before relying on ad revenue. Per-streamer credit links + chapters are always
generated; clips remain their creators' property (`permissions.require_permission` gates
to approved broadcasters; respect takedowns).

## Credits

Vibe-coded with [Claude](https://claude.com) (Anthropic) — architecture, research, code,
and iteration loops AI-driven with human creative direction.
