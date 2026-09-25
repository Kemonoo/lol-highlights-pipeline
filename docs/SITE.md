# SITE.md — state of the project website

The website (`site/`, published to https://kemonoo.github.io/lol-highlights-pipeline/ by
`.github/workflows/pages.yml` on every push to master that touches `site/`) describes the
pipeline. It goes stale whenever the pipeline changes. This file is how the two stay in
sync without re-reading the codebase:

- **Pipeline work (any chat):** if a change alters a fact in the ledger below, add a line
  to **Pending** — date, what changed, which ledger rows it affects. Don't edit the site.
- **Site work:** start here. Work through Pending, update the site and the ledger, move
  the items into the changelog, and bump "Synced with".

**Synced with:** pipeline as of 2026-09-24 (commit "Thumbnails: restore the previous
avatar sizes"). The page narrates the real run of the night of **2026-09-23** (log
`data/logs/auto_2026-09-24.log`, work files `data/work/2026-09-23/`), the first night
with the owner overlay's 222/22 settings and the v4 crop regions.

## Pending (pipeline changes the site doesn't reflect yet)

- 2026-09-25 — Video: the brand intro is gone (owner overlay), clips are joined by
  glitch / whip / pixelate cuts. The assemble stage text ("renders the intro, then...")
  should say so.
- 2026-09-25 — Shorts: speaker-coloured captions (duo lines inside each person's panel),
  2D avatars/VTubers count as the streamer. Shorts stage text could mention both.
- 2026-09-25 — one of the 3 daily Shorts is now a TOP 5 ranking Short (publishing/
  ranking_shorts.py). Rows H3/S9 ("three Shorts" from the top clips) need a line.
- 2026-09-25 — duo streams: Shorts show up to two webcams side by side; webcam boxes
  are snapped to the overlay's edges.
- 2026-09-25 — Shorts/thumbnails now locate the streamer with the local vision model
  (webcam OR VTuber/avatar), Haar as a model-confirmed fallback. Shorts stage text
  ("face moves between frames and shows skin tones") and open problem P3 are out of date.
- 2026-09-24 — the judge now watches 720p (open problem P2 is fixed: remove it); judge
  cache is `api_partial_v4.json` (exporter handles both).
- 2026-09-25 — new stage `clip_log` between shorts and cleanup (data/clip_log.jsonl,
  every published clip kept forever); affects any stage list / stage count on the site.
- Refresh `docs/images/site.jpg` (README hero screenshot of the live page) after each
  export — it currently shows "219 clips in".
- 2026-09-24 — fetch now returns exactly `fetch_count` (222) instead of ~3 fewer.
  The next export should read "222 clips in, 22 out" (H1, F0, D1). Nothing to edit by
  hand: the exporter takes the count from clips.json.

## Ledger — every fact the site states, and where it comes from

Numbers of the 09-23 night come from the log; rules and limits come from config/code.

| id | Site says | Source |
|---|---|---|
| H1 | "219 clips in, 22 out." (hero headline) | `raw/…/clips.json` 219 (Twitch returned 219 of the 222 asked); `target_clips: 22` |
| H2 | Runs every night at 03:00, unattended | `setup_schedule.bat` / Task Scheduler |
| H3 | Three Shorts per night | `shorts.count: 3` |
| F0 | 219 fetched | `raw/2026-09-23/clips.json` (219 of `fetch_count` 222) |
| F1 | 190 after language (29 dropped) | log "Prefilter … 190 scored" |
| F2 | 141 after sound & motion (49 dropped) | log "190 scored -> 141 passed -> 52 kept" |
| F3 | 52 best ranked (89 dropped) | `prefilter.max_keep: 52` |
| F4 | 35 after local vision model (17 dropped) | overlay `vlm_filter.max_keep: 35`; log "Filter: 52 -> 35 kept" |
| F5 | 22 after Gemini (13 dropped) | log "API judge: 35 -> 22 kept"; `chapters.json` |
| S0 | Self-check, then comment feedback at 03:00:00 | `doctor.py`, `feedback/feedback.py` |
| S1 | Midnight–midnight Amsterdam, metadata only | `ingestion/fetch.py` |
| S2 | audio < 0.22 or motion < 0.015 dropped; title keyword skips the audio test; rank = keyword, loudness, views | `prefilter.audio_exclude`, `motion_exclude`, `filtering/prefilter.py` |
| S2-lr | Logistic regression exists but isn't loaded | `tools/train_classifier.py` docstring |
| S3 | Qwen3-VL 4B via Ollama; 3-frame gameplay vote, 1-frame pro check, crops every 5 s | `llm.roles.vlm`, `filtering/vlm_filter.py`, `kill_detect.py` |
| S3-rules | The 8-line decide() order, audio ≥ 0.30 / ≥ 0.55, not for Japanese titles | `vlm_filter.decide()` |
| S3-keep | 35 kept | overlay `vlm_filter.max_keep: 35` (default 24) |
| S4 | Gemini watches video+sound, fixed JSON schema, missing field = no answer | `filtering/api_judge.py` (`parse_verdict`) |
| S4-rule | keep: entertainment ≥ 6 (≥ 7 reactions) or play quality ≥ 7 | `api_judge` keep rule |
| S4-budget | 20 requests/model/day, walks a model chain; switched once on 09-23 | `api_judge.daily_budget`, `llm.roles.judge.overflow_models`, log |
| S4-selection | "ordered into a countdown: 22 clips, 12.9 minutes of video" | log "Assembled … (12.9 min, 22 clips)"; `video.target_clips` |
| S5 | Whisper translates to English with word timestamps, 1 min | `transcribe`, log times |
| S6 | Captions 1–3 words, never overlap; source-caption detection by speech-vs-silence difference | `assemble._caption_groups`, `enrichment/burned_captions.py` |
| S6-replay | Slow-mo replay for top-rated clips | `video.replay_min_score: 7` |
| S7 | Chapters + per-streamer links; PIL thumbnails in three colourways (sharp champion splash) | `production/credits.py`, `thumbnail.provider: local` |
| S8 | Resumable upload, can't upload a day twice | `publishing/upload.py` (`state.uploaded_id`) |
| S9 | Three best clips, ~30 s, face below / centred + slightly zoomed when none; live-face webcam check; no captions over English source captions | `shorts.count`, `shorts.target_seconds: 32` |
| S10 | Raw downloads deleted after a day; masters only after YouTube confirms | `cleanup.keep_raw_days: 1`, `cleanup.py` |
| T | Stage start times (first = first log line, 03:00:06) … finish 05:52:11, durations rounded to minutes | `[stage] starting` lines in the log |
| D1 | All 219 to Gemini = 11 days of one model's allowance | 219 / 20 |
| P1–P4 | Open problems: pro-broadcast check ~2/7 right; judge sees 360p copy; webcams: 2 wrong / ~7 missed of 52 on a held-out night; filters tuned on small samples | DEVLOG 2026-09-13 (P1, P2), 2026-09-24 facecam entry (P3), CLAUDE.md eval loop + review queue (P4); remove each when fixed |

## Assets (`site/assets/`)

From the 09-23 night unless noted. Clip thumbnails and stills show streamers' footage;
the footer credits them and the video description links each channel.

| file | what | made from |
|---|---|---|
| `hero.mp4` / `hero.jpg` / `keycap.jpg` | 9 s loop / poster / hero tile of countdown #2 (BigDog_Q) | output @ chapter start +1 s / +2 s |
| `clips/NN.jpg` + `funnel.json` | 52 prefilter survivors: tile, verdicts, scores, stage reached, rank | `work/<night>/thumbs`, `vlm_scored.json`, `api_partial_v3.json`, `chapters.json` |
| `filmstrip.jpg`, `wave.png` | 8 frames + waveform of clip #2 | raw clip |
| `crop_banner.jpg`, `crop_killfeed.jpg` | VLM crops of #1 at 22 s ("YoungGoobyV2 is godlike!") — v4 regions | `work/2026-09-23/crops/` |
| `caption_en.jpg` | #19 English caption | output @ chapter start + first 3-word phrase |
| `caption_translate.jpg` | **09-21** #12 Polish source caption + ours (09-23 had no such clip; the exporter keeps the old still and its text) | 09-21 output |
| `thumb_1..3.jpg` | 09-23 thumbnails **re-rendered with the restored avatar sizes** (the uploaded ones used the one-night 0.95 circle) | `thumbnail.generate_variants` |
| `short_1.jpg`, `short_2.jpg` | 09-23 Shorts (BigDog_Q split, YoungGooby zoomed) **re-rendered with the 2026-09-24 code** (live-face check, zoom, no double captions); the uploaded ones predate it | `shorts._render_short` @ 6 s |
| `intro.jpg` | brand intro frame (currently unused) | 09-21 output @ 1.2 s |

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
- 2026-09-24 — Open problems updated: Shorts double captions removed (fixed), webcam
  detection reworded to measured numbers, "filters tuned on small samples" added. Shorts
  stage text mentions the zoom, the live-face check and the caption rule.
- 2026-09-24 — re-exported from the 09-23 night (first 222/22 night: 219 in, 22 out).
  Exporter: crops now come from the best-ranked clip whose announcement the model read,
  and the model's reading is only quoted when unambiguous; missing caption/translation
  matches keep the old still AND its text. Cleared Pending: target_clips, 222/22, v4
  crop regions, thumbnail layout.
- 2026-09-24 — `scripts/export_site.py`: the whole night is exported by one command;
  night-specific text is `data-n`-bound. Re-exported 09-21 (same numbers; new picks:
  caption still #16, sharper kill-feed crop, prefilter "7 min").
- 2026-09-26 — Impeccable audit (`npx impeccable detect site/`, 37 -> 18 findings, the rest
  false positives): --dim/--muted lifted to WCAG AA, 4-step type scale (--fs-sm/base/lg/xl),
  glows kept only on the surviving hero tiles, no gradient headline text.
