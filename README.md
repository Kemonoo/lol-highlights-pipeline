# Twitch → YouTube highlights pipeline

Turns each day's top Twitch clips into a finished, uploadable YouTube video: fetch,
filter with AI, edit, thumbnail, publish — plus vertical Shorts. One command in, one
~9-minute video out.

# Video example
https://youtu.be/OhYVCmSULfs?si=Ss4IasyotYa7DGUl

```bash
python -m pipeline.doctor        # check your machine and config
python -m pipeline.run_daily     # yesterday's clips -> data/output/<date>.mp4
```

Built for League of Legends, runs unattended on a single Windows PC with a consumer
GPU, and costs about **1¢ a day** in API calls — because ~90% of clips are discarded by
free local models before anything paid ever sees them.

<!--
  TODO(maintainer): add visuals here — this is the biggest single improvement left.
    docs/images/output.gif      a 5-10s grab of a finished video (intro + a clip + badge)
    docs/images/report.png      a screenshot of data/work/<date>/report.html
    docs/images/thumbnail.jpg   one generated thumbnail
  Then reference them below. A video project with no visible output undersells itself.
-->

---

## What it actually does

| | |
|---|---|
| **Selects** | Pulls the day's top clips, then filters them through three escalating layers — free local scoring, a local vision model, and a video-native LLM that watches each survivor and grades the play. |
| **Edits** | FFmpeg-only: branded intro, countdown badges, animated streamer nameplates, burned English speech captions (any language, translated), optional slow-mo replays, music outro, loudness mastering. |
| **Publishes** | Clickbait title, chapters, per-streamer credits, an AI or locally-composited thumbnail, resumable YouTube upload, and 3 derived vertical Shorts. |

Every stage is cached and resumable. Interrupt it at any point and re-run — it picks up
where it stopped. Every run writes `data/work/<date>/report.html`: a visual audit of
every keep/reject decision with thumbnails, scores, and reasons.

A full run takes a while, so progress is readable from another terminal:

```bash
python -m pipeline.progress --watch
```

```
  [x] fetch        219 clips
  [x] prefilter    40 kept
  [>] vlm_filter   [##################....] 32/40
  [ ] api_judge    [......................] 0/18
```

It reads the caches the run already writes rather than anything instrumented, so it
works against a run started by cron, the .bat, or another terminal — and tells you if
nothing has been written for a while.

## Quickstart

```bash
git clone https://github.com/Kemonoo/lol-highlights-pipeline
cd lol-highlights-pipeline

python -m venv venv
venv\Scripts\pip install -e .          # Windows
# python3 -m venv venv && venv/bin/pip install -e .    # macOS / Linux

cp .env.example .env                    # then fill in your Twitch keys
python -m pipeline.doctor               # tells you exactly what's still missing
```

`doctor` checks FFmpeg, your GPU and encoders, every configured model, disk space, and
credentials — and prints the fix command for anything that isn't ready. Then:

```bash
python -m pipeline.run_daily --date 2026-07-27
```

Nothing is uploaded. `upload.enabled` is `false` and privacy is `private` by default —
publishing is always an explicit decision you make after watching a few outputs.

To run it every day unattended, `setup_schedule.bat` (Windows) registers a Task
Scheduler job — 03:00 by default, and allowed to **wake the PC** to do it. It can also
**put the PC back to sleep** when it finishes, behind a countdown window you can cancel;
that part is off unless you turn it on. Both the schedule and the sleep behaviour are
worth reading before you enable them — see
**[Running it daily](docs/setup.md#7-running-it-daily)**.

Full walkthroughs for the two fiddly bits — registering a Twitch app and setting up
YouTube OAuth — are in **[docs/setup.md](docs/setup.md)**.

## What you need

| | Required? | Notes |
|---|---|---|
| Python 3.10+ | **yes** | |
| FFmpeg + ffprobe on PATH | **yes** | all rendering; `winget install Gyan.FFmpeg` |
| [Twitch API keys](https://dev.twitch.tv/console/apps) | **yes** | free; this is where clips come from |
| [Ollama](https://ollama.com) + a vision model | recommended | free, local. `ollama pull qwen3-vl:4b` (~3GB, fits 4GB VRAM) |
| [Gemini API key](https://aistudio.google.com/apikey) | optional | ~1¢/day. Much better clip selection |
| NVIDIA / Intel / Apple GPU | optional | hardware video encoding + faster transcription |
| YouTube OAuth | only to upload | see docs/setup.md |

**It degrades instead of failing.** That's a design property, not an accident:

| Missing | What happens |
|---|---|
| Gemini key | Clips are scored from local signals instead. You get a video, less well curated. |
| Billing on Gemini | Thumbnails use the free PIL design (`thumbnail.provider: local`, the default). |
| GPU | Everything runs on CPU. Slower, identical output. |
| Ollama | The local vision filter can't run — this is the one hard requirement beyond FFmpeg. |
| YouTube OAuth | Renders normally, just doesn't upload. |

## Choosing your own models

Model choice is configuration, not code. Every model call goes through one of five
**roles**, and each role can point at any backend you have access to:

```yaml
llm:
  roles:
    judge:                       # watches whole clips - needs native video
      provider: gemini
      model: gemini-3.6-flash
      api_key: ${GEMINI_API_KEY}
      thinking: low

    vlm:                         # per-frame filtering - keep this free and local
      provider: ollama
      model: auto                # picks the best vision model you have installed
      base_url: http://localhost:11434

    commentary:                  # anything OpenAI-compatible
      provider: openai
      model: meta-llama/llama-3.3-70b-instruct
      base_url: https://openrouter.ai/api/v1
      api_key: ${OPENROUTER_API_KEY}
```

Supported providers: **`gemini`**, **`ollama`**, **`anthropic`**, and **`openai`** —
where `openai` means any OpenAI-compatible endpoint, which covers OpenRouter, Groq,
Together, DeepSeek, Fireworks, LM Studio, vLLM, llama.cpp's server, and Ollama's own
`/v1`. Point `base_url` at a box on your LAN and the pipeline runs against your own
hardware.

Two constraints worth knowing: the `judge` role needs a provider that ingests **video**
(Gemini, today) or it falls back to local scoring; and keeping `vlm` local is what makes
the whole thing cost pennies, since it sees roughly ten times more clips than the judge.

Don't fork `config.yaml` — write an **overlay** with just your changes:

```bash
python -m pipeline.run_daily --config config.kemono.yaml
```

See [`pipeline/config.kemono.yaml`](pipeline/config.kemono.yaml) for a real one.

## Using your hardware

`video.encoder: auto` probes FFmpeg and picks **NVENC / QSV / VideoToolbox / AMF** when
available, falling back to libx264. Assembly is the slowest stage on a CPU-only machine,
so this is the single biggest win from better hardware. `transcribe.device: auto` does
the same for CUDA, and verifies the whole stack (including cuDNN 9) before selecting the
GPU path rather than crashing mid-run. `python -m pipeline.doctor` reports what it found.

## How it works

| Stage | What it does | Cost |
|---|---|---|
| `fetch` | Top clips for the day via the Twitch Helix API (metadata; downloads are lazy) | free |
| `prefilter` | Local scoring: title keywords, audio hype, motion, language, blacklist | free |
| `vlm_filter` | Local vision model, one simple question per crop: real gameplay? esports? kills in the feed? | free |
| `api_judge` | An LLM watches each survivor *as video* and grades play quality + entertainment | ~1¢/day |
| `transcribe` | faster-whisper detects the language and translates speech → English | free |
| `commentary` · `tts` | *(optional)* grounded caster lines + neural voiceover | ~free |
| `assemble` | FFmpeg: intro, countdown badges, nameplates, captions, replays, outro, mastering | free |
| `credits` | Title, chapters, per-streamer credit links, music attribution | free |
| `thumbnail` | AI reaction composite, or the free local design | 0–4¢ |
| `upload` · `shorts` | Resumable YouTube upload + 3 vertical Shorts | free |

Two modes, both config toggles: **lean** (default — curated clips, transitions, brand,
no narration) and **produced** (AI commentary + voiceover + music bed).

## Design notes

**The cost cascade.** Free local checks discard ~90% of clips; the paid video judge only
ever sees the survivors. Everything about the architecture protects that ratio.

**Detection and decision are separate.** Model outputs are cached per clip; the
keep/reject *rules* are pure functions recomputed from that cache on every run. Retuning
the filter costs zero GPU time and zero API calls — and it makes the rules directly
unit-testable, which is what `tests/test_filter_decisions.py` exercises.

**Small models get decomposed questions.** A 4B local model fails at "is this a good
highlight?" but is reliable at "count the kill banners in this crop." Taste is delegated
to the video judge; perception is done locally, one narrow question at a time.

More detail in [docs/architecture.md](docs/architecture.md).

## Repo layout

```
pipeline/
  ingestion/ filtering/ enrichment/ production/ publishing/ feedback/
  providers/        model backends (gemini, ollama, openai-compatible, anthropic)
  tools/            labelling UI, filter eval, training-data collection
  config.yaml       the single tuning surface
  doctor.py         environment check
data/               clips, work files, renders (gitignored)
docs/               setup, architecture, devlog
tests/              decision rules + config resolution (no network, no GPU)
```

## Legal

This software is MIT-licensed. **The clips are not.** They belong to the streamers who
made them, and running this makes you responsible for complying with Twitch's and
YouTube's terms, obtaining whatever permission republishing requires, and honouring
takedowns. `permissions.require_permission: true` restricts the pipeline to broadcasters
you've explicitly approved. Per-streamer credits and chapters are always generated —
please don't remove them. See [LICENSE](LICENSE) for the full notice.

**On monetization:** lean mode is a thin transformation of reused content, which is a
real risk for YouTube Partner Program eligibility. Add an originality layer — the
produced-mode commentary, a sharper niche, per-language editions — before depending on
ad revenue.

League of Legends is a trademark of Riot Games, Inc. This project is not endorsed by,
affiliated with, or sponsored by Riot Games.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). `pytest -q` and `ruff check pipeline tests` both
need to pass; neither needs a GPU, an API key, or FFmpeg.

## Credits

Architecture, code, and iteration loops developed with [Claude](https://claude.com)
under human creative direction. Music by [NoCopyrightSounds](https://ncs.io) (not
redistributed here — see `assets/music/README.md`). Champion art via Riot's Data Dragon.
