# CLAUDE.md

## Docs map — read these before changing direction
- **This file** = current architecture + rules. Always loaded.
- **DEVLOG.md** = chronological context: why things are this way, what was tried and
  **reversed**, production incidents + root causes, environment traps that cost hours,
  open items. **Read it at the start of any non-trivial task**, and append a dated entry
  when you change direction, fix a production bug, or shelve an idea.
- **VIRAL_STRATEGY.md** = ranked growth roadmap (CTR/AVD levers) + what's shipped.
- **FUTURE_WORK.md** = parked ideas incl. the shelved funny-outro findings.
- **PLAN.md** = stage status table + monetization/legal constraints. README.md = public.
- **SITE.md** = the project website (`site/`, GitHub Pages): a ledger of every fact the
  site states and its source. Pipeline changes that alter a ledger fact get a line under
  its **Pending** heading (don't edit `site/` from pipeline work); site work starts there.

## Project context

Automated daily channel **KEMONO**: top League of Legends Twitch clips → filtered by AI →
assembled into a daily YouTube video (countdown format, brand intro, AI thumbnail) +
derived Shorts → auto-uploaded. Currently **LEAN MODE** (no voiceover/commentary/music
bed — see the lean-mode note below). Runs unattended at 03:00 on the owner's Windows
machine (RTX 3050, 8GB VRAM) via `run_daily_auto.bat` (Task Scheduler; `setup_schedule.bat`
registers it with `WakeToRun`). The bat wraps the run in `scripts/keep_awake.ps1`
(a wake-timer wake is an *unattended* wake — Windows re-sleeps it after ~2 min) and,
when `SLEEP_AFTER=1`, suspends afterwards via `scripts/sleep_prompt.ps1` (skips its
cancel window when the session is already idle; every ambiguous case = stay awake).
Per-machine settings live in the gitignored `auto_run.local.cmd` (`CONFIG`,
`SLEEP_AFTER`, ...) so the committed bat keeps shipping harmless defaults — same split
as config.yaml vs. overlays.
Entry points: `python -m pipeline.run_daily [--date YYYY-MM-DD] [--config <overlay.yaml>]
[--skip-doctor]` and `python -m pipeline.doctor` (environment check; also runs as a fast
preflight at the top of every run).

**Debugging a bad/missing upload starts at `data/logs/auto_<date>.log`.** The bat retries
the whole run on a non-zero exit, so every side-effecting stage must stay idempotent.

### Stage chain (pipeline/run_daily.py STAGES, each module has run(cfg, state, date_label))
Code is organized into subpackages: `ingestion/`, `filtering/`, `enrichment/`,
`production/`, `publishing/`, `feedback/`, `providers/`, `tools/` (paths below are
relative to `pipeline/`).
0. `feedback/feedback.py` — reads comments on recent uploads (clip-number refs "#3" map to
   chapters), Gemini-classifies sentiment, aggregates (>= feedback.min_agreement
   agreeing comments = ACTIONABLE) → data/feedback/log.jsonl + proposals.md.
   Never blocks the pipeline. Optional auto_update invokes `claude -p` headlessly
   for CONFIG-LEVEL changes only (this file is its context — keep it current).
1. `ingestion/fetch.py` — Twitch Helix top clips (or broadcaster list), METADATA ONLY →
   `data/raw/<date>/clips.json`. Downloads are lazy: prefilter grabs low-quality
   copies (`raw/<date>/lq/`) for scoring; vlm_filter downloads full quality for its
   survivors only; api_judge shrinks that to 720p (the LQ copy is now portrait-360, unusable)
2. `filtering/prefilter.py` — free local scoring: title keywords / tournament exclude /
   audio-hype / motion (functions from `filtering/scoring.py`) + broadcaster blacklist
   → `work/<date>/prefiltered.json`
3. `filtering/vlm_filter.py` + `filtering/kill_detect.py` — local VLM via the `vlm`
   role, STEPWISE
   (one simple question per image — small models fail multi-question prompts): gameplay
   vote (3 frames), pro-play check, kill-feed/banner/event-log crop analysis. Detection
   cached (`vlm_partial_v4.json`); decisions are PURE CODE recomputed from cache every run
   (`decide()`) so rule tuning costs zero GPU time → `vlm_scored.json`,
   `vlm_filtered.json`, `report.html` (visual audit), `crops/` (region calibration)
4. `filtering/api_judge.py` — the `judge` role watches survivors as full video (shrunk
   to `shrink_height`, default 720p; inline ≤`max_mb`, default 90MB — larger goes via the
   provider's Files API), scores focus/play_quality/entertainment against a JSON schema
   (cache `api_partial_v4.json` — v4 since 2026-09-24: the judge
   used to watch Twitch's portrait-360 LQ copy; it now only reuses the LQ copy when it is
   landscape and ≥ `shrink_height`, else shrinks the full-quality file). No judge available → `local_judge()`, never a hard fail.
   Spend is capped by `api_judge.daily_budget` (free tier = 20 req/day/model, counted per
   PACIFIC day in state.json); clips are judged best-first so the budget buys the top.
   The quota is metered PerProjectPerModel, so `llm.roles.judge.overflow_models` lists
   further ids that each carry their OWN allowance on the same key — the stage walks the
   chain and re-sends the clip, turning 20/day into 20 x len(chain).
   Selection (`api_judge.select`, pure): `video.target_clips` > 0 = exactly that many clips
   (owner overlay: 20); 0 = duration rule — fill toward `video.target_minutes_ideal` with fillers
   (ent≥4), trim at max, order ascending rank = countdown. Writes `api_scored.json`
   (the stage's done-marker) and rewrites vlm_filtered.json (input is always rebuilt
   from vlm_scored.json — idempotent)
5. `enrichment/transcribe.py` — multilingual streamer speech → English (faster-whisper,
   task=translate; `transcribe.device: auto` resolves via `hardware.whisper_device()`,
   which picks CUDA only when ctranslate2 AND cuDNN 9 are both present — the old unattended
   crash was a missing cuDNN, fixed by `pip install nvidia-cudnn-cu12`): detects language,
   translates, word timestamps → `work/<date>/transcripts.json`
   {clip_id: {lang, text, words}}. `transcribe.skip_languages` is now `[]` — EVERY clip is
   transcribed and captioned (the list filters on Twitch's declared CHANNEL language, which
   is often wrong, so `[en]` silently dropped captions from bilingual streamers).
   Cached per clip; feeds assemble (burns English captions on every clip), commentary
   (reliable context, produced mode) + shorts (English captions). `enrichment/match_linker.py`,
   `enrichment/hud_ocr.py` — Phase-2 stubs (Riot API match data; HUD OCR). Docstrings
   contain the implementation plans. Riot API > scraping op.gg/u.gg (no public APIs there)
6. `production/commentary.py` — montage-caster lines (hype the player/moment; never
   invents facts; sees previous lines to avoid repetition) + `_intro` cold-open line.
   Uses the `commentary` role with a STICKY runtime fallback: after 2 consecutive primary
   failures (= quota gone, retrying is pointless) it switches to the role's `fallback`
   block for the rest of the run → `commentary.json` [{clip_id, text}].
   Treats the per-clip summary as an UNRELIABLE hint, but now ALSO sees the English speech
   transcript (transcripts.json) as a RELIABLE signal — reacts to what the streamer
   actually said; still never invents champions/numbers; English only.
7. `production/tts.py` — Kokoro local (default, voice `af_bella`; the model is built once
   per run as a module-level singleton) / edge-tts fallback. Word timestamps; `_intro`
   handled like any line; per-clip failures are skipped, not fatal; timings flushed
   incrementally. `tts.enabled` config switch → `work/<date>/vo/<clip_id>.mp3`
8. `production/assemble.py` — ffmpeg only (no moviepy). REFUSES to build below
   `video.min_clips` (default 3), and upload re-checks it: a broken yt-dlp extractor
   once produced 0 clips and the pipeline published a 15s intro+outro publicly.
   KEMONO brand intro (or text card)
   → segments (animated PROJECT-style streamer nameplate bottom-left via
   `production/nameplate.py` when `video.nameplate.enabled`, else the plain drawtext
   lower-third; both fall back gracefully), #N countdown badge, drawtext English captions
   grouped into 1-3 word phrases with DISJOINT windows (`_caption_groups`; one word at a
   time both overlapped and ran ~7 words/s — see DEVLOG 2026-09-09), skipped entirely when
   `enrichment/burned_captions.py` finds the streamer's OWN live-caption widget baked into
   the source AND the speech is already English, VO
   ducking, 0.3s fades, 0.5x REPLAY part for clips with api_rank_score ≥ replay_min_score
   using api_best_moment_s) → outro (when `video.outro_music`: KEMONO logo over the
   energy-detected drop of an NCS track, `_find_drop`; else text card) → concat
   demuxer → master (looped music bed, sidechain-ducked under clip audio so music rises
   in quiet gaps + single-pass loudnorm, video stream copied)
9. `production/credits.py` — title hook from best clip, chapters, per-streamer credit
   links, music attribution → `data/output/<date>.meta.json`
10. `production/thumbnail.py` — 1280×720 thumbnail, two providers (`thumbnail.provider`,
    now defaulting to `local` so a fresh clone needs no billing):
    `gemini` builds a viral reaction thumbnail via the `thumbnail_image` role
    ("Nano Banana", paid ~$0.04/img): picks the highest-ranked clip WITH a detectable
    facecam, has the model enhance that facecam into an over-the-top excited/shocked
    reaction on a green screen (`_CUT_PROMPT`), chroma-keys it out (`_green_key`),
    composites it over the REAL clip gameplay frame (slightly blurred, `_gameplay_bg`),
    and overlays a centred metallic gold announcement (PENTAKILL/QUADRA in Montserrat
    Bold, per-line gradient; fonts in assets/fonts/), a highlighted streamer name badge,
    and a radiating red border (`_red_border`). `local` (fallback) is the free PIL design:
    champion splash from Data Dragon + reaction face + hook text. Writes
    `work/<date>/thumbnail.jpg`; splash/pfp cached in data/cache/
11. `publishing/upload.py` — YouTube Data API v3 resumable upload, OAuth desktop flow
    (client_secret.json in root, token cached at data/yt_token.json). IDEMPOTENT: skips a
    date already in state (`state.uploaded_id`) so the bat's crash-retry can't double-upload
    (`upload.allow_reupload` to force)
12. `publishing/shorts.py` — derives vertical Shorts from top clips (facecam detection +
    split-screen layout, punchy `target_seconds` trim). NO voiceover/music: just the clip
    audio + English speech captions (drawtext, `_caption_filters`, layout-aware height via
    `caption_split_gap`/`caption_margin_v` — fontfile, no fontconfig). English title from
    the summary + speech (NEVER the raw native Twitch title). Shares the main upload OAuth →
    `work/<date>/shorts/`
13. `publishing/cleanup.py` — prune raw MP4s older than `cleanup.keep_raw_days`, and
    published masters older than `cleanup.keep_output_days` (0 = keep forever, the
    default; a master is only ever deleted when state.json holds a confirmed YouTube
    id for that date, so an un-uploaded video is never touched. meta.json always stays)

Support: `config.py` (YAML + ${ENV} expansion + .env loader + `--config` OVERLAY deep-merge
+ legacy-key migration; picks a random `music_track` per run + builds its attribution
string), `providers/` (role→backend abstraction: `base.py` interface + retry + schema
handling, `gemini.py` (text/image/video, Files API, thinking levels), `ollama.py`,
`openai_compat.py` (any OpenAI-compatible endpoint), `anthropic_api.py` (official SDK,
optional extra)), `hardware.py` (ffmpeg encoder + GPU/VRAM + cuDNN detection),
`doctor.py` (environment report + preflight), `state.py` (data/state.json: processed
clip ids, permissions, uploaded videos), `tools/label_clips.py` (browser labeling UI, stdlib HTTP
server, keys G/O/B → work/<date>/labels.json), `tools/eval_filter.py` (precision/recall vs
labels), `tools/gen_music.py` (numpy-synthesized copyright-free music bed → assets/music/bg.mp3),
`tools/collect_training_data.py` (standalone Twitch fetch + feature extraction →
data/training/dataset_*.json), `tools/review_clips.py` (Tkinter labeling UI for training
data), `tools/train_classifier.py` (logistic regression on labeled clips — ADVISORY: prints
feature weights + threshold suggestions; the pipeline never loads its model.json),
`tools/debug_facecam.py` (facecam-detection debug visualizer),
`tools/gen_sfx.py` (numpy-synthesized nameplate notification SFX → assets/sfx/nameplate.wav).
`production/nameplate.py` renders the per-clip animated streamer card with PIL (write-on
letters + cyan/orange glow, framed Twitch avatar reusing `thumbnail._twitch_pfp`), packs
it to a transparent qtrle .mov (cached in data/cache/nameplates/ keyed by name+avatar+
style), and assemble overlays it + mixes the SFX. NB: chose PIL+ffmpeg over Playwright/
Chromium on purpose — no browser dependency in the daily run.

### Data flow / layout
```
tests/                  decision rules + config resolution (no network/GPU/ffmpeg)
docs/                   setup.md, architecture.md + internal notes (DEVLOG, PLAN, ...)
data/raw/<date>/        clips.json + downloaded mp4s
data/work/<date>/       prefiltered.json → vlm_scored/vlm_filtered.json → commentary.json
                        → vo/*.mp3 → segments/*.mp4 → chapters.json; caches; report.html
data/output/            <date>.mp4 + <date>.meta.json
data/training/          dataset_*.json (gitignored) — classifier training data
_archive/               pre-pivot code (shorts app, long-video experiment) — do not touch
```

### Non-obvious decisions (do not re-litigate without instruction)
- **Provider abstraction**: stages ask for a ROLE (`judge`/`vlm`/`commentary`/`feedback`/
  `thumbnail_image`) via `providers.get_provider(cfg, role)`; `llm.roles.*` in config.yaml
  decides the backend. NEVER hardcode a model id in a stage — every id lives in
  `llm.roles` so a vendor retirement is a one-line fix (this already bit us twice:
  gemini-2.0-flash, then the whole 2.5 family). Capabilities are declared
  (`supports_video`/`supports_images`/`supports_image_generation`) and checked with
  `provider.require(...)` so a misconfigured role fails clearly at startup.
- **Degradation is a tested property, not an accident**: no key → `local_judge()`;
  quota dead mid-run → sticky commentary fallback; no billing → PIL thumbnail; no GPU →
  CPU. Only ffmpeg and (for vlm_filter) Ollama are hard requirements. Don't "simplify"
  a fallback path away.
- **Detection/decision split**: model outputs cached per clip; keep/reject rules
  recompute from cache on every run. When changing DETECTION semantics (prompts,
  regions), bump the cache filename version (`vlm_partial_v4` → v5, `api_partial_v4`
  → v5). When changing only decision rules, never bump.
- **Cost cascade**: free local checks discard ~90%; paid Gemini only sees survivors
  (~1¢/day). Keep it that way.
- **Gemini image thumbnail (Nano Banana) guardrails**: image generation needs a
  BILLED API key (free tier limit=0). Having the model build the whole scene is
  unreliable — naming IP ("League of Legends" / a champion) or feeding copyrighted Riot
  art trips the recitation filter (`finishReason: IMAGE_OTHER`, no image), and even a
  generic full composite flakes. So we only ask it to ENHANCE the real facecam into an
  excited reaction on a green screen (reliable), key that out, and build the rest
  ourselves (real blurred gameplay frame + PIL Cinzel announcement). Don't push scene
  generation / IP names back onto the model.
- **Stage skip/resume**: a stage is "done" if its output file exists (`_stage_outputs`);
  `--force` redoes. Slow stages also have per-clip caches; failures are never cached
  (auto-retry next run). KeyboardInterrupt anywhere = progress saved.
- **Expansion loop** (`run_daily._expand_selection_if_short`): if selection <
  target_minutes_ideal, fetch_count += expand_step (also raises prefilter/vlm keep
  caps), re-runs stages 1–4 incrementally until ideal or twitch.max_fetch.
- **Segment VO staleness**: `<segment>.vo` sidecar marks whether VO existed at render;
  assemble re-renders segments when VO appears later.
- **mp4 paths are derived** (`data/raw/<date>/<id>.mp4`), stored `local_path` is only
  a fallback — code must work when the repo moves between machines.
- **Read tolerance**: JSON reads use `.rstrip("\x00")` — work files can carry NUL
  padding from a filesystem-sync quirk. Keep this on new readers of work/ files.
- **Encoding goes through `assemble.enc(v)` / `hardware.encoder_args()`** — `video.encoder:
  auto` probes for NVENC/QSV/VideoToolbox/AMF. EVERY segment (including brand.py's intro)
  must use the same helper or the concat demuxer breaks. Presets are per-encoder: an x264
  preset name is rejected by NVENC.
- **ffmpeg drawtext**: any option value containing commas MUST be single-quoted inside
  the filtergraph (`y='h-236+24*(1-...)'`); overlay text passes through `_esc()` which
  strips `\\'%:,[]=;`. Fonts via `_font()` (Windows Arial → DejaVu fallback; CJK
  detection picks msgothic/Noto for JP/KR/CN streamer names).
- **Burn text with drawtext + an explicit `fontfile`, never ASS/libass**: this dev shell's
  ffmpeg can't font-match ASS, so ASS captions render BLANK in preview frames (that burned
  several rounds of blind repositioning). drawtext is also pixel-precise, which is what
  caption/HUD-clearance geometry needs. A blank preview = suspect fonts, not logic.
- **Raw MP4s vanish fast** (`cleanup.keep_raw_days: 1`): re-rendering or re-analyzing an
  older date usually isn't possible — test on the newest date that still has MP4s.
- **JP-title rule**: kana in title → clip can't pass on audio hype alone (owner finding:
  JP clips are usually talk-context). CJK ideographs alone ≠ Japanese.
- **Blacklist** (config) was seeded from owner labels: JP event/custom-tournament
  channels (k4sen orbit). Owner curates; don't auto-edit.
- **Filler clips**: reaction-focus fillers (ent≥4) are allowed by explicit owner
  decision (the "slap" clip reversal). Reaction clips need ent≥7 to keep outright.
- **Commentary grounding**: writer only sees facts from the judge's description.
  Tone: funny/ironic/relatable, never mean-spirited. Never let it invent champions,
  names, numbers.
- **Known open disagreement**: judge underrates "streamer gets outplayed" fail clips
  (labeled good, ent2). Candidate future signal; don't silently "fix".
- **Review queue** (`tools/review_queue.py` + `review_ui.html`, `review.bat`): the ongoing
  ground-truth loop. Samples `review.per_gate` clips from EVERY gate per finished night plus
  `review.random`, traces why each stopped (`trace()`, rebuilt from work files — nothing
  extra runs at night), and records two answers: was the gate's stated reason TRUE (detector
  accuracy) and how GOOD is the clip (rule accuracy). Plays the full-quality file while it
  exists, else the Twitch embed. Append-only `data/reviews/reviews.jsonl` with a snapshot
  of the pipeline's facts per clip (newest line per clip wins); `--stats` = per-gate table.
- **Eval loop (older)**: labels via `label_clips.py`, score via `eval_filter.py`. v2→v4 filter
  took precision 0.25→1.0 on 2026-06-09 — but on a tiny set (25 labels, of which only
  ~9 reached the VLM stage, 1-3 of them good), and it does not reproduce: today's rules
  on the same saved day score 0.33 on 9 clips. Treat it as "tuning helped", not a
  benchmark. Filter changes should be re-evaled vs labels.
- **Feedback conservatism**: viewer comments only become actionable on repetition
  (min_agreement). Auto-update (when enabled) may touch config.yaml/blacklist/prompt
  strings ONLY — never code structure. kill_detect short-circuits once kills are
  confirmed; don't remove that without measuring runtime.
- **LEAN MODE (current default)**: chasing the Synapse model (curated clips + transitions
  + KEMONO brand, no narration). `commentary.enabled: false`, `tts.enabled: false`,
  `video.music_enabled: false` (background bed). The VO/commentary/music-bed machinery is
  intact behind those switches — don't delete it. NB: clips are NO LONGER English-only —
  `prefilter.include_languages` includes EU langs + ko, and EVERY clip gets burned English
  captions (transcribe → assemble) so the video carries without narration and works muted.
  Caption height is `video.caption_y_frac` (0.70). ~Half of all clips carry the streamer's
  OWN live-caption widget burned into the source (verified 2026-09-09: 8 of 17), which we
  cannot remove — so where one exists and the speech is English we draw NOTHING, and where
  the speech is foreign ours sits above theirs as the translation. The OUTRO still uses music
  (`video.outro_music`: KEMONO logo over an NCS drop) even though the bed is off. Trade-off:
  no commentary weakens the YouTube "reused content"/YPP hedge — grow-first, revisit an
  originality layer before monetizing. Per-streamer credits always generated.

## Token efficiency
- Read only files relevant to the task. Module docstrings are accurate and current —
  trust them before opening bodies. Never re-read the whole codebase.
- Everything above is established context — don't re-summarize it in responses or
  re-derive it from source.
- Don't explain changes back unless asked. Respond in concise commit-style language
  ("fix segment cache invalidation when VO appears; add test").
- Don't regenerate report.html/MORNING_NOTES-style artifacts unless asked.

## Model routing
Use the cheapest model that can handle the task reliably:
- **claude-haiku-4-5**: single-function fixes, renaming, comments, dependency bumps,
  small config changes
- **claude-sonnet-4-6 (default)**: feature implementation, refactoring, debugging,
  writing tests, multi-file edits
- **claude-opus-4-8**: architecture decisions, complex multi-file refactors, anything
  where a wrong decision is expensive to undo
- **claude-fable-5**: only when opus-4-8 fails on consecutive reasoning turns, or for
  tasks requiring days-long autonomous work across the whole codebase. High latency,
  high cost — not for interactive sessions.

## Constraints (never change without explicit instruction)
- Stage contract: every pipeline module exposes `run(cfg, state, date_label)`; stages
  stay independently runnable/resumable via run_daily flags. No new orchestration layer.
- ffmpeg/ffprobe via subprocess only — no moviepy/opencv for rendering (opencv is
  allowed in prefilter scoring only).
- Free-by-default: local Ollama for filtering, local Kokoro (edge-tts fallback) for
  voice, royalty-free NCS music tracks (attribution generated) or the in-house bed.
  Paid APIs only where already wired (Gemini judge + commentary) at comparable cost.
- Keys live in `.env` (TWITCH_CLIENT_ID/SECRET, GEMINI_API_KEY) expanded via
  `${VAR}` in config.yaml. Never hardcode keys; never commit .env / client_secret.json
  (.gitignore covers them).
- `pipeline/config.yaml` is the single tuning surface — new behavior gets a config key
  with a comment, defaults preserving current behavior.
- Don't touch `_archive/` (reference only). Scoring functions live in
  `pipeline/filtering/scoring.py` — prefilter depends on their signatures.
- Windows is the production target: paths must work on Windows; fonts via _font();
  batch files for user-facing entry points. POSIX is supported-but-untested — keep the
  `.sh` counterparts and macOS/Linux font candidates working, don't add Windows-only
  syntax to shared code paths.
- Upload stays `privacy: private` and `enabled: false` by default (also `shorts.upload`
  and `shorts.privacy`). A fresh clone must never publish to someone's channel.
- Keep per-streamer credits/chapters generation intact in credits.py (legal/monetization
  requirement).
- Model ids ONLY in `llm.roles.*`. Stages reference roles, never vendors or model names.
- `tests/` must stay runnable with no network, no GPU, no ffmpeg and no API keys — that
  is what makes CI possible. Test the pure decision layer; don't mock ffmpeg.
- Public-repo hygiene: this is a published GitHub project. LICENSE, README.md,
  docs/setup.md, CONTRIBUTING.md and .env.example are user-facing — keep them accurate
  when behavior changes. The owner's own settings live in `config.kemono.yaml` (an
  overlay), NOT in config.yaml defaults.
