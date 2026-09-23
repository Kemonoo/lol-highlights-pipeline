# SITE.md — state of the project website

The website (`site/`, published to https://kemonoo.github.io/lol-highlights-pipeline/ by
`.github/workflows/pages.yml` on every push to master that touches `site/`) describes the
pipeline. It goes stale whenever the pipeline changes. This file is how the two stay in
sync without re-reading the codebase:

- **Pipeline work (any chat):** if a change alters a fact in the ledger below, add a line
  to **Pending** — date, what changed, which ledger rows it affects. Don't edit the site.
- **Site work:** start here. Work through Pending, update the site and the ledger, move
  the items into the changelog, and bump "Synced with".

**Synced with:** pipeline as of 2026-09-24 (the commit that added this file). The page
narrates the real run of the night of **2026-09-21** (log `data/logs/auto_2026-09-22.log`,
work files `data/work/2026-09-21/`), which still used the old selection rule (~8 minutes
of video, 17 clips).

## Pending (pipeline changes the site doesn't reflect yet)

- 2026-09-23 — `video.target_clips` added; the owner overlay makes the video a fixed
  number of clips instead of ~8 minutes. Affects rows F5, S4-selection.
- 2026-09-24 — owner overlay: `twitch.fetch_count`/`max_fetch` 222, `target_clips` 22,
  `vlm_filter.max_keep` 35 ("222 clips in, 22 out"). Affects the hero headline, F0–F5,
  S3-keep, S4-selection, the hero-grid survivors, and the whole funnel data. Needs a
  real night run with the new settings, then a re-export (see "Regenerating assets").
  Note: Twitch can return a clip or two fewer than asked (219 of 220 on 09-21), so check
  the real count before writing "222" anywhere.
- 2026-09-23 — `tools/review_queue` (manual review of filter decisions) exists; the site
  doesn't mention it. Optional.

## Ledger — every fact the site states, and where it comes from

Numbers of the 09-21 night come from the log; rules and limits come from config/code.

| id | Site says | Source |
|---|---|---|
| H1 | "219 clips in, 17 out." (hero headline) | log: fetched 219; chapters.json 17 |
| H2 | Runs every night at 03:00, unattended | `setup_schedule.bat` / Task Scheduler |
| H3 | Three Shorts per night | `shorts.count: 3` |
| F0 | 219 fetched | `raw/2026-09-21/clips.json` (219 of `fetch_count` 220) |
| F1 | 162 after language (57 dropped) | log "Language filter … removed 57 clips" |
| F2 | 124 after sound & motion (38 dropped) | log "162 scored -> 124 passed -> 52 kept" |
| F3 | 52 best ranked (72 dropped) | `prefilter.max_keep: 52` |
| F4 | 24 after local vision model (28 dropped) | `vlm_filter.max_keep: 24`; `api_partial_v3.json` |
| F5 | 17 after Gemini (7 dropped) | log "API judge: 24 -> 17 kept"; `chapters.json` |
| S0 | Self-check, then comment feedback at 03:00:00 | `doctor.py`, `feedback/feedback.py` |
| S1 | Midnight–midnight Amsterdam, metadata only | `ingestion/fetch.py` |
| S2 | audio < 0.22 or motion < 0.015 dropped; title keyword skips the audio test; rank = keyword, loudness, views | `prefilter.audio_exclude`, `motion_exclude`, `filtering/prefilter.py` |
| S2-lr | Logistic regression exists but isn't loaded | `tools/train_classifier.py` docstring |
| S3 | Qwen3-VL 4B via Ollama; 3-frame gameplay vote, 1-frame pro check, crops every 5 s | `llm.roles.vlm`, `filtering/vlm_filter.py`, `kill_detect.py` |
| S3-rules | The 8-line decide() order, audio ≥ 0.30 / ≥ 0.55, not for Japanese titles | `vlm_filter.decide()` |
| S3-keep | 24 kept | `vlm_filter.max_keep` (default 24; overlay now 35) |
| S4 | Gemini watches video+sound, fixed JSON schema, missing field = no answer | `filtering/api_judge.py` (`parse_verdict`) |
| S4-rule | keep: entertainment ≥ 6 (≥ 7 reactions) or play quality ≥ 7 | `api_judge` keep rule |
| S4-budget | 20 requests/model/day, walks a model chain; switched once on 09-21 | `api_judge.daily_budget`, `llm.roles.judge.overflow_models`, log |
| S4-selection | "ordered into a countdown: 17 clips, 8.8 minutes of video" (no rule named, so it holds under target_clips too) | log "Selection: 17 clips, 8.7 min" / "Assembled … 8.8 min" (now `target_clips`) |
| S5 | Whisper translates to English with word timestamps, 1 min | `transcribe`, log times |
| S6 | Captions 1–3 words, never overlap; source-caption detection by speech-vs-silence difference | `assemble._caption_groups`, `enrichment/burned_captions.py` |
| S6-replay | Slow-mo replay for top-rated clips | `video.replay_min_score: 7` |
| S7 | Chapters + per-streamer links; PIL thumbnails in three colourways | `production/credits.py`, `thumbnail.provider: local` |
| S8 | Resumable upload, can't upload a day twice | `publishing/upload.py` (`state.uploaded_id`) |
| S9 | Three best clips, ~30 s, face below / centred when none | `shorts.count`, `shorts.target_seconds: 32` |
| S10 | Raw downloads deleted after a day; masters only after YouTube confirms | `cleanup.keep_raw_days: 1`, `cleanup.py` |
| T | Stage start times (first = first log line, 03:00:13) … 05:06:30, durations rounded to minutes | `[stage] starting` lines in the log |
| D1 | All 219 to Gemini = 11 days of one model's allowance | 219 / 20 |
| P1–P4 | Open problems: pro-broadcast check ~2/7 right; judge sees 360p copy; Shorts face miss ~50%; Shorts double captions | audits in this repo's chats (DEVLOG 2026-09-13 area); remove each when fixed |

## Assets (`site/assets/`)

All from the 09-21 night. Clip thumbnails and stills show streamers' footage; the footer
credits them and the video description links each channel.

| file | what | made from |
|---|---|---|
| `hero.mp4` / `hero.jpg` / `keycap.jpg` | 9 s loop / poster / hero tile of countdown #2 (lol_Ethereal pentakill) | output @ chapter start +1 s / +2 s |
| `clips/NN.jpg` + `funnel.json` | 52 prefilter survivors: tile, verdicts, scores, stage reached, rank | `work/2026-09-21/thumbs`, `vlm_scored.json`, `api_partial_v3.json`, `chapters.json` |
| `filmstrip.jpg`, `wave.png` | 8 frames + waveform of clip #2 | raw clip `PlumpSingleQuail…mp4` |
| `crop_banner.jpg`, `crop_killfeed.jpg` | VLM crops at 22 s | `work/2026-09-21/crops/` |
| `caption_en.jpg` / `caption_translate.jpg` | #16 English caption / #12 Polish source caption + ours | output @ chapter start + first 3-word phrase |
| `thumb_1..3.jpg` | the night's thumbnails | `work/2026-09-21/thumbnail*.jpg` |
| `short_1.jpg`, `short_2.jpg` | split layout / centred layout | `work/2026-09-22/shorts/` @ 6 s |
| `intro.jpg` | brand intro frame (currently unused) | output @ 1.2 s |

funnel.json `stage`: 2 = dropped by the VLM,
3 = dropped by Gemini, 4 = in the video.

### Regenerating assets for a new night

```
./venv/Scripts/python.exe scripts/export_site.py --date <night> --dry-run   # check numbers
./venv/Scripts/python.exe scripts/export_site.py --date <night>
```

Run it the morning after the night (raw MP4s are deleted after a day; the filmstrip and
waveform need one). It reads the night's log and work files and rewrites: every element
in `index.html` marked `data-n="key"` (numbers, times, durations, dates, the judge's JSON,
figure captions), `assets/funnel.json` (`{night, clips}`, which funnel.js reads for its
counts and notes), the clip tiles, `keycap.jpg`, the hero loop/poster, caption stills,
crops, thumbnails and Short stills. Missing sources are skipped with a warning and the
old asset stays. Rerunning is idempotent. The script's docstring lists the rules that
replace hand-picking (hero = countdown #2 via `--hero-rank`, `--crop-time`, …).

Not automated: prose that isn't a number (e.g. "a silent pentakill once…"), the ledger
and changelog below, and a look at the result. New night-specific text in the page
should get a `data-n` key and a value in the script's `values` dict.

## Design (keep consistent)

Direction chosen 2026-09-23 after the owner rejected a light editorial first draft and
pointed at dovetail.com: near-black, faint square grid, Geist + Doto (dot-matrix for
numbers and the "17 out." line), Twitch purple `#a970ff` as the one accent, cyan for
the home GPU, pill-shaped fixed nav, thin-bordered panels. One orchestrated motion:
the hero grid lights the examined clips, then only the survivors stay.
Built with Anthropic's `frontend-design` plugin guidance.

## Working on the site

- Preview: `.claude/launch.json` → "site" (`python -m http.server 8765 --directory site`).
  The funnel fetches JSON, so `file://` won't work.
- Screenshots: the in-app browser pane often stalls after scrolling; headless Edge is
  reliable: `msedge --headless=new --hide-scrollbars --virtual-time-budget=6000
  --window-size=1440,7400 --screenshot=out.png http://localhost:8765/`
  (it can't go narrower than ~500 px; check phones in the pane's mobile preset).
- No build step, no framework: `index.html`, `style.css`, `funnel.js`.

## Changelog

- 2026-09-23 — v1: light editorial page, interactive funnel of the 09-21 night.
- 2026-09-24 — v2: Dovetail-style dark redesign, renamed "Twitch Highlights Pipeline",
  hero video loop, animated hero grid, full HH:MM:SS stage times.
- 2026-09-24 — `scripts/export_site.py`: the whole night is exported by one command;
  night-specific text is `data-n`-bound. Re-exported 09-21 (same numbers; new picks:
  caption still #16, sharper kill-feed crop, prefilter "7 min").
