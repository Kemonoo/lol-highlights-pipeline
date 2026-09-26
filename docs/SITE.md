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

(none — cleared by the 09-25 export)

## Ledger — every fact the site states, and where it comes from

Numbers of the 09-25 night come from the log; rules and limits come from config/code.

| id | Site says | Source |
|---|---|---|
| H1 | "222 clips in, 22 out." (hero headline) | `raw/…/clips.json` 222 (`fetch_count` 222, exact since fetch_slack); `target_clips: 22` |
| H2 | Runs every night at 03:00, unattended | `setup_schedule.bat` / Task Scheduler |
| H3 | Three Shorts per night (one is a ranking Short) | `shorts.count: 3`, `ranking_shorts.replace_slots: 1` |
| F0 | 222 fetched | `raw/2026-09-25/clips.json` |
| F1 | 171 after language (51 dropped) | log "Prefilter … 171 scored" |
| F2 | 126 after sound & motion (45 dropped) | log "171 scored -> 126 passed -> 52 kept" |
| F3 | 52 best ranked (74 dropped) | `prefilter.max_keep: 52` |
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
| S4-selection | "ordered into a countdown: 22 clips, 11.0 minutes of video" | log "Assembled … (11.0 min, 22 clips)"; `video.target_clips` |
| S5 | Whisper translates to English with word timestamps, 1 min | `transcribe`, log times |
| S6 | Captions 1–3 words, never overlap; source-caption detection by speech-vs-silence difference | `assemble._caption_groups`, `enrichment/burned_captions.py` |
| S6-cut | No intro; glitch / whip / pixelate cuts, random, never twice in a row (the slow-mo replay line was removed: `replay_enabled: false`) | overlay `intro_enabled: false`, `video.transitions`, `production/transitions.py` |
| S7 | Chapters + per-streamer links; Gemini writes the hook + thumbnail words; thumbnail = best-moment frame + webcam picture-in-picture, original webcam blurred | `production/credits.py`, overlay `thumbnail.provider: clip`, `upload.title_mode: hook` |
| S8 | Resumable upload, can't upload a day twice | `publishing/upload.py` (`state.uploaded_id`) |
| S9 | Best clips ~30 s; local vision model finds webcam / VTuber, snapped to edges; duo = two panels; speaker-coloured captions (Gemini); zoomed when nobody; no captions over English source captions; one ranking Short (top 5 of a theme, all published clips, never reused) | `shorts.*` (overlay `facecam_method: vlm`, `speaker_colors`), `enrichment/streamer_cam.py`, `enrichment/speakers.py`, `publishing/ranking_shorts.py` |
| S10 | Every published clip logged forever (feeds ranking Shorts); raw downloads deleted after a day; masters only after YouTube confirms | `publishing/clip_log.py`, `cleanup.keep_raw_days: 1`, `cleanup.py` |
| T | Stage start times (first = first log line, 03:00:06) … finish 05:53:09, durations rounded to minutes | `[stage] starting` lines in the log |
| D1 | All 222 to Gemini = twelve days of one model's allowance | 222 / 20, rounded up |
| P1, P3, P4 | Open problems: pro-broadcast check ~2/7 right; webcam finder can still frame a sticker / crop tight; filters tuned on small samples. (P2, judge on 360p, removed: fixed 2026-09-24) | DEVLOG 2026-09-13 (P1), 2026-09-25 streamer_cam entries (P3), CLAUDE.md eval loop + review queue (P4); remove each when fixed |

## Assets (`site/assets/`)

From the 09-25 night unless noted. Clip thumbnails and stills show streamers' footage;
the footer credits them and the video description links each channel.

| file | what | made from |
|---|---|---|
| `hero.mp4` / `hero.jpg` / `keycap.jpg` | 9 s loop / poster / hero tile of countdown #2 (BROHAN) | output @ chapter start +1 s / +2 s |
| `clips/NN.jpg` + `funnel.json` | 52 prefilter survivors: tile, verdicts, scores, stage reached, rank | `work/<night>/thumbs`, `vlm_scored.json`, `api_partial_v3.json`, `chapters.json` |
| `filmstrip.jpg`, `wave.png` | 8 frames + waveform of clip #2 | raw clip |
| `crop_banner.jpg`, `crop_killfeed.jpg` | VLM crops of #2 at 22 s — v4 regions (exporter rule: best-ranked clip with read announcements) | `work/2026-09-25/crops/` |
| `caption_en.jpg` | #18 English caption | output @ chapter start + first 3-word phrase |
| `caption_translate.jpg` | #19 French source caption + our English above it | output |
| `thumb_1.jpg` | the night's uploaded thumbnail (style A); thumb_2/3 deleted with the colourways | `work/<night>/thumbnail.jpg` |
| `short_1.jpg`, `short_2.jpg` | sharpest regular Short + the ranking Short, as uploaded | `work/<night>/shorts/*.mp4` @ 6 s (exporter picks by edge detail) |
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
- 2026-09-26 — Exported the 09-25 night (222 → 22, 11.0 min). Stage texts: no intro +
  transitions, no slow-mo replay (off in config), style A thumbnail + Gemini hook, Shorts via
  the vision model / duo panels / speaker colours / ranking Short, clip log. Open problem P2
  removed, P3 rewritten. Exporter: Short stills = sharpest regular + ranking. README shot refreshed.
