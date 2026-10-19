# SITE.md — state of the project website

The website (`site/`, published to https://kemonoo.github.io/lol-highlights-pipeline/ by
`.github/workflows/pages.yml` on every push to master that touches `site/`) describes the
pipeline. It goes stale whenever the pipeline changes. This file is how the two stay in
sync without re-reading the codebase:

- **Pipeline work (any chat):** if a change alters a fact in the ledger below, add a line
  to **Pending** — date, what changed, which ledger rows it affects. Don't edit the site.
- **Site work:** start here. Work through Pending, update the site and the ledger, move
  the items into the changelog, and bump "Synced with".

**Synced with:** pipeline as of 2026-10-19 (commit "Shorts: upload copy upscaled to
1440x2560"). The page narrates the real run of the night of **2026-10-18** (log
`data/logs/auto_2026-10-19.log`, work files `data/work/2026-10-18/`), the first night with
the Claude pre-screen, trimming and the 120-clip pool.

## Pending (pipeline changes the site doesn't reflect yet)

(none — cleared by the 10-18 export)

## Ledger — every fact the site states, and where it comes from

Numbers of the 10-18 night come from the log; rules and limits come from config/code.

| id | Site says | Source |
|---|---|---|
| H1 | "222 clips in, 29 out." (hero headline) | log "Eligible … 734 of 947", `twitch.fetch_count` 222 + `eligible_only`; log "Assembled … 29 clips" |
| H2 | Runs every night at 03:00, unattended | `setup_schedule.bat` / Task Scheduler |
| H3 | ~10-minute countdown + three Shorts; weekly themed compilation | `video.target_minutes_ideal` 8 / `_max` 11, `shorts.count: 3`, `weekly.enabled` |
| F0 | 222 eligible (947 listed) | log "Eligible (language / blacklist): 734 of 947 fetched", `raw/…/clips.json` |
| F1 | 211 after one per moment (11) | log "Moments: 222 clips -> 211" |
| F2 | 199 not static (motion + tournament) | log "Prefilter … 211 scored -> 199 passed" |
| F3 | 120 best ranked | log "-> 120 kept", overlay `prefilter.max_keep: 120` |
| F4 | 91 gameplay | `vlm_scored.json` KEEP count (sanity mode) |
| F5 | 50 Claude's pick | log "API judge: 50 -> 29", overlay `vlm_filter.max_keep: 50` |
| F6 | 29 after Gemini, 10.8 min | log "Assembled 2026-10-18.mp4 (10.8 min, 29 clips)" |
| S1 | Midnight–midnight Amsterdam; ja/zh skipped except an allow-list; blacklist; read until 222 qualify | `ingestion/fetch.py`, overlay `exclude_languages`, `language_allow_streamers`, `eligible_only` |
| S2 | Same stream < 90 s = one moment, shortest well-viewed clip, no repeats within two weeks; motion; ranked by clip count, self-clip, views, keyword; loudness no longer counts | `filtering/moments.py`, overlay `moment_prefer: short`, `audio_weight: 0`, `quiet_gate: false` |
| S3 | 3-frame gameplay vote only; was 8/8 right in reviews | overlay `vlm_filter.mode: sanity`, `kill_scan: false`; DEVLOG 2026-10-12 |
| S3b | Claude: 8-frame sheet + title, `claude -p` on a subscription, 5 per call, ~4 s a clip, reorders only | `filtering/prescreen.py`, `providers/claude_code.py`, `prescreen.batch` |
| S4 | Fixed JSON incl. facts + action window; hard drops (not League / not playing / pro broadcast); keep ent ≥ 6 (≥ 7 reaction) or play ≥ 7; 20 req/model/day chain; fill toward 8 min, max 11 | `api_judge` SCHEMA v5, `hard_facts`, `min_entertainment*`, `min_play_quality`, `daily_budget`, `select()` |
| S6 | Trim to the action window; 60 fps; 1440p upload copy; transitions; caption rules | `video.trim`, overlay `fps: 60`, `upload_height: 1440`, `production/transitions.py`, `burned_captions.py` |
| S7 | Thumbnail frame vetted (no scoreboard) | overlay `thumbnail.clip_frame_check` |
| S9 | Shorts as before + 1440×2560 upload copy | `shorts.*`, overlay `shorts.upload_height: 2560` |
| S10 | Clip log → weekly theme (longest-unused theme with ≥ 8 strong clips) | `publishing/clip_log.py`, `publishing/weekly.py` |
| D1 | All 947 to Gemini = 48 days of one model's allowance | 947 / 20, rounded up |
| P1 | Cut before the judge: Claude 6+ → 56% kept, < 6 → 28%; no better than signals inside the judged 50 | DEVLOG 2026-10-18 (10-16 sample) |
| P3, P4 | Webcam finder slips; Gemini descriptions 8/20 partly wrong | streamer_cam DEVLOG entries; owner reviews 2026-10-12 |

## Assets (`site/assets/`)

From the 10-18 night unless noted. Clip thumbnails and stills show streamers' footage;
the footer credits them and the video description links each channel.

| file | what | made from |
|---|---|---|
| `hero.mp4` / `hero.jpg` / `keycap.jpg` | 9 s loop / poster / hero tile of countdown #2 | output @ chapter start +1 s / +2 s |
| `clips/NN.jpg` + `funnel.json` | the 120 downloaded clips: tile, verdicts, Claude score, stage reached, rank | `work/<night>/thumbs`, `vlm_scored.json`, `prescreen_v1.json`, `api_partial_v5.json`, `chapters.json` |
| `filmstrip.jpg` | 8 frames of clip #2 | raw clip |
| `sheet.jpg` | the contact sheet Claude scored for clip #2 | `work/<night>/prescreen_sheets/` |
| `caption_en.jpg` / `caption_translate.jpg` | English caption / foreign source caption + ours | output |
| `thumb_1.jpg` | the night's uploaded thumbnail | `work/<night>/thumbnail.jpg` |
| `short_1.jpg`, `short_2.jpg` | sharpest regular Short + the ranking Short | `work/<night>/shorts/*.mp4` @ 6 s |
| `intro.jpg` | brand intro frame (currently unused) | 09-21 output @ 1.2 s |

funnel.json `stage`: 2 = not gameplay, 3 = not picked by Claude, 4 = dropped by Gemini,
5 = in the video.

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
- 2026-10-20 — Exported the 10-18 night (947 listed → 222 eligible → 29 out, 10.8 min) and
  rewrote the funnel and stage texts for the 10-12+ pipeline: 7 steps (one per moment, not
  static, ranked, gameplay, Claude's pick, Gemini), a Claude stage with its contact sheet,
  Gemini's hard facts + action window, trimming, 60 fps / 1440p, weekly compilations.
  Removed the VLM crop and waveform figures (kill scan and loudness are off). Open problems:
  P1 pro-broadcast check (no longer a gate) replaced by the cut before the judge; Gemini
  descriptions added. Exporter: new log lines, prescreen timing inside the judge stage,
  `sheet.jpg`. README shot refreshed.
