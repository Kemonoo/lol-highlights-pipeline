# LoL Daily Highlights — automated Twitch → YouTube pipeline

A fully automated pipeline that turns each day's top League of Legends Twitch clips into
a polished, monetizable long-form YouTube video — with AI clip selection, generated
commentary, text-to-speech voiceover, countdown editing, slow-mo replays, music, and
auto-upload.

One command in, one ~8-minute video out:

```
venv\Scripts\python.exe -m pipeline.run_daily
```

## How it works

| Stage | What it does | Cost |
|---|---|---|
| 1. fetch | Pulls the day's top LoL clips from the Twitch Helix API, downloads MP4s | free |
| 2. prefilter | Cheap local scoring: title keywords, audio-hype, motion, broadcaster blacklist | free |
| 3. vlm_filter | Local vision model (Ollama) judges each clip stepwise: real gameplay? esports broadcast? kills in the kill feed / event log? Rule-based keep/reject | free (your GPU) |
| 3.5 api_judge | Gemini watches each surviving clip *as video* and scores play quality + entertainment; selection fills toward the target length and orders clips as a countdown | ~1¢/day |
| 4. commentary | LLM writes grounded, ironic caster lines per clip + a cold open (only from verified facts — no hallucinated plays) | ~0¢ |
| 5. tts | Free neural voiceover (edge-tts) with word timestamps | free |
| 6. assemble | FFmpeg: intro card, #N countdown badges, animated streamer lower-thirds, voiceover ducking, slow-mo REPLAY of the best moment, outro, music bed, loudness mastering | free |
| 7. credits | YouTube title with a content hook, chapters, per-streamer credit links | free |
| 8. upload | Resumable upload via YouTube Data API (private by default) | free |

Every stage is cached and resumable — interrupt anything, re-run, it continues.
If a day's selection is shorter than `target_minutes_ideal`, the pipeline automatically
fetches deeper into that day's clips until the video is long enough.

## Setup

1. **Python**: `python -m venv venv && venv\Scripts\pip install -r requirements.txt`
2. **FFmpeg**: install and put `ffmpeg`/`ffprobe` on PATH
3. **Ollama** (free local vision filtering): install from ollama.com, then
   `ollama pull qwen3-vl:4b` (4GB VRAM) or a larger vision model if you have one
4. **Keys** — create `.env` in the repo root:
   ```
   TWITCH_CLIENT_ID=...        # dev.twitch.tv -> register an app
   TWITCH_CLIENT_SECRET=...
   GEMINI_API_KEY=...          # aistudio.google.com (free tier is plenty)
   ```
5. **Music**: `python -m pipeline.tools.gen_music` generates a copyright-free bed, or drop any
   licensed track at `assets/music/bg.mp3` (see `assets/music/README.md`)
6. **YouTube upload** (optional): follow the 5-step OAuth setup in `pipeline/upload.py`,
   then set `upload.enabled: true` in `pipeline/config.yaml`

## Usage

```bash
python -m pipeline.run_daily                      # full run for yesterday
python -m pipeline.run_daily --date 2026-06-08    # specific day
python -m pipeline.run_daily --stop-after vlm_filter   # partial run
python -m pipeline.run_daily --only assemble --force   # redo one stage
```

Review tools:

```bash
python -m pipeline.tools.label_clips    # browser UI: label clips good/ok/bad (G/O/B keys)
python -m pipeline.tools.eval_filter    # precision/recall of the filter vs your labels
```

Every run writes `data/work/<date>/report.html` — a visual breakdown of every keep/reject
decision with thumbnails, scores, and reasons.

## Tuning (pipeline/config.yaml)

The interesting knobs: `video.target_minutes_ideal` (default 8), `blacklist.broadcasters`
(channels to always skip), `api_judge.min_entertainment` (quality bar), `commentary.style`
(hype_caster / analyst / chill), `tts.voice` and `tts.enabled`, `video.countdown_enabled`,
`video.replay_min_score`, `video.music_volume_db`. Decision rules recompute from cache,
so re-tuning costs seconds, not GPU time.

## Design notes

The filter is a cost cascade: free local checks discard ~90% of clips, the paid video-AI
judge only ever sees the handful of survivors. Local models get *decomposed* questions
(one simple question per cropped image — kill feed, announcement banner, event log)
because small VLMs fail at open-ended judgment but are reliable at "count the kill
banners in this crop". Taste — "is this play actually good?" — is delegated to Gemini
watching the full clip. Commentary is grounded: the writer model only sees facts the
judge verified on screen, which keeps the voiceover accurate (a YouTube monetization
requirement: significant original commentary, not templated filler).

Per-streamer credit links and chapters are generated for every video. Clips remain the
property of their creators; use `permissions.require_permission` to restrict the pipeline
to broadcasters who have approved use, and respect takedown requests.

## Credits

This project was vibe-coded with [Claude](https://claude.com) (Anthropic) — architecture,
research, code, and iteration loops were AI-driven with human creative direction and
quality labeling.
