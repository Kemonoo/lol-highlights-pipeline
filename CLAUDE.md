# CLAUDE.md

## Project context

Automated daily pipeline: top League of Legends Twitch clips → filtered by AI →
assembled into a monetizable long-form YouTube video (countdown format, commentary,
TTS voiceover, music, replays) → optional auto-upload. Runs on the owner's Windows
machine (RTX 3050 4GB). Entry point: `python -m pipeline.run_daily [--date YYYY-MM-DD]`.

### Stage chain (pipeline/run_daily.py STAGES, each module has run(cfg, state, date_label))
Code is organized into subpackages: `ingestion/`, `filtering/`, `enrichment/`,
`production/`, `publishing/`, `feedback/`, `tools/` (paths below are relative to `pipeline/`).
0. `feedback/feedback.py` — reads comments on recent uploads (clip-number refs "#3" map to
   chapters), Gemini-classifies sentiment, aggregates (>= feedback.min_agreement
   agreeing comments = ACTIONABLE) → data/feedback/log.jsonl + proposals.md.
   Never blocks the pipeline. Optional auto_update invokes `claude -p` headlessly
   for CONFIG-LEVEL changes only (this file is its context — keep it current).
1. `ingestion/fetch.py` — Twitch Helix top clips (or broadcaster list), METADATA ONLY →
   `data/raw/<date>/clips.json`. Downloads are lazy: prefilter grabs low-quality
   copies (`raw/<date>/lq/`) for scoring; vlm_filter downloads full quality for its
   survivors only; api_judge reuses the LQ copy instead of re-encoding
2. `filtering/prefilter.py` — free local scoring: title keywords / tournament exclude /
   audio-hype / motion (functions from `filtering/scoring.py`) + broadcaster blacklist
   → `work/<date>/prefiltered.json`
3. `filtering/vlm_filter.py` + `filtering/kill_detect.py` — local Ollama VLM, STEPWISE
   (one simple question per image — small models fail multi-question prompts): gameplay
   vote (3 frames), pro-play check, kill-feed/banner/event-log crop analysis. Detection
   cached (`vlm_partial_v3.json`); decisions are PURE CODE recomputed from cache every run
   (`decide()`) so rule tuning costs zero GPU time → `vlm_scored.json`,
   `vlm_filtered.json`, `report.html` (visual audit), `crops/` (region calibration)
4. `filtering/api_judge.py` — Gemini watches survivors as full video (shrunk to 480p,
   inline ≤19MB), scores focus/play_quality/entertainment (cache `api_partial_v2.json`).
   Duration-aware selection: fill toward `video.target_minutes_ideal` with fillers
   (ent≥4), trim at max, order ascending rank = countdown. Writes `api_scored.json`
   (the stage's done-marker) and rewrites vlm_filtered.json (input is always rebuilt
   from vlm_scored.json — idempotent)
5. `enrichment/transcribe.py` — multilingual streamer speech → English (faster-whisper,
   task=translate): detects language, translates, word timestamps → `work/<date>/
   transcripts.json` {clip_id: {lang, text, words}}. Cached per clip; feeds commentary
   (reliable context) + shorts (English captions). `enrichment/match_linker.py`,
   `enrichment/hud_ocr.py` — Phase-2 stubs (Riot API match data; HUD OCR). Docstrings
   contain the implementation plans. Riot API > scraping op.gg/u.gg (no public APIs there)
6. `production/commentary.py` — montage-caster lines (hype the player/moment; never
   invents facts; sees previous lines to avoid repetition) + `_intro` cold-open line.
   Provider auto: Gemini if key, else Ollama → `commentary.json` [{clip_id, text}].
   Treats the per-clip summary as an UNRELIABLE hint, but now ALSO sees the English speech
   transcript (transcripts.json) as a RELIABLE signal — reacts to what the streamer
   actually said; still never invents champions/numbers; English only.
7. `production/tts.py` — Kokoro local (default, voice `af_bella`; the model is built once
   per run as a module-level singleton) / edge-tts fallback. Word timestamps; `_intro`
   handled like any line; per-clip failures are skipped, not fatal; timings flushed
   incrementally. `tts.enabled` config switch → `work/<date>/vo/<clip_id>.mp3`
8. `production/assemble.py` — ffmpeg only (no moviepy): intro card → segments (animated
   PROJECT-style streamer nameplate bottom-left via `production/nameplate.py` when
   `video.nameplate.enabled`, else the plain drawtext lower-third slide-up; both fall back
   gracefully), #N countdown badge, VO ducking, 0.3s fades, 0.5x REPLAY part for clips
   with api_rank_score ≥ replay_min_score using api_best_moment_s) → outro → concat
   demuxer → master (looped music bed, sidechain-ducked under clip audio so music rises
   in quiet gaps + single-pass loudnorm, video stream copied)
9. `production/credits.py` — title hook from best clip, chapters, per-streamer credit
   links, music attribution → `data/output/<date>.meta.json`
10. `production/thumbnail.py` — 1280×720 thumbnail, two providers (`thumbnail.provider`):
    `gemini` (default) builds a viral reaction thumbnail with Gemini 2.5 Flash Image
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
    (client_secret.json in root, token cached at data/yt_token.json)
12. `publishing/shorts.py` — derives vertical Shorts from top clips (facecam detection +
    split-screen layout). Everything is English: speech captions + VO/title use
    `enrichment.transcribe`; the title is generated in English from the summary + speech
    (NEVER the raw, often-native Twitch clip title). Shares the main upload OAuth →
    `work/<date>/shorts/`
13. `publishing/cleanup.py` — prune raw MP4s older than `cleanup.keep_raw_days` to free disk

Support: `config.py` (YAML + ${ENV} expansion + .env loader; picks a random `music_track`
per run + builds its attribution string), `state.py` (data/state.json: processed clip ids,
permissions, uploaded videos), `tools/label_clips.py` (browser labeling UI, stdlib HTTP
server, keys G/O/B → work/<date>/labels.json), `tools/eval_filter.py` (precision/recall vs
labels), `tools/gen_music.py` (numpy-synthesized copyright-free music bed → assets/music/bg.mp3),
`tools/collect_training_data.py` (standalone Twitch fetch + feature extraction →
data/training/dataset_*.json), `tools/review_clips.py` (Tkinter labeling UI for training
data), `tools/train_classifier.py` (logistic regression on labeled clips),
`tools/debug_facecam.py` (facecam-detection debug visualizer),
`tools/gen_sfx.py` (numpy-synthesized nameplate notification SFX → assets/sfx/nameplate.wav).
`production/nameplate.py` renders the per-clip animated streamer card with PIL (write-on
letters + cyan/orange glow, framed Twitch avatar reusing `thumbnail._twitch_pfp`), packs
it to a transparent qtrle .mov (cached in data/cache/nameplates/ keyed by name+avatar+
style), and assemble overlays it + mixes the SFX. NB: chose PIL+ffmpeg over Playwright/
Chromium on purpose — no browser dependency in the daily run.

### Data flow / layout
```
data/raw/<date>/        clips.json + downloaded mp4s
data/work/<date>/       prefiltered.json → vlm_scored/vlm_filtered.json → commentary.json
                        → vo/*.mp3 → segments/*.mp4 → chapters.json; caches; report.html
data/output/            <date>.mp4 + <date>.meta.json
data/training/          dataset_*.json (gitignored) — classifier training data
_archive/               pre-pivot code (shorts app, long-video experiment) — do not touch
```

### Non-obvious decisions (do not re-litigate without instruction)
- **Detection/decision split**: model outputs cached per clip; keep/reject rules
  recompute from cache on every run. When changing DETECTION semantics (prompts,
  regions), bump the cache filename version (`vlm_partial_v3` → v4, `api_partial_v2`
  → v3). When changing only decision rules, never bump.
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
- **ffmpeg drawtext**: any option value containing commas MUST be single-quoted inside
  the filtergraph (`y='h-236+24*(1-...)'`); overlay text passes through `_esc()` which
  strips `\\'%:,[]=;`. Fonts via `_font()` (Windows Arial → DejaVu fallback; CJK
  detection picks msgothic/Noto for JP/KR/CN streamer names).
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
- **Eval loop**: labels via `label_clips.py`, score via `eval_filter.py`. v2→v4 filter
  took precision 0.25→1.0 on 2026-06-09. Filter changes should be re-evaled vs labels.
- **Feedback conservatism**: viewer comments only become actionable on repetition
  (min_agreement). Auto-update (when enabled) may touch config.yaml/blacklist/prompt
  strings ONLY — never code structure. kill_detect short-circuits once kills are
  confirmed; don't remove that without measuring runtime.
- **LEAN MODE (current default)**: chasing the Synapse model (curated English clips +
  transitions + brand, no narration). `commentary.enabled: false`, `tts.enabled: false`,
  `video.music_enabled: false`, `prefilter.include_languages: [en]`. The VO/commentary/
  music machinery is intact behind those switches — don't delete it. Trade-off: dropping
  commentary weakens the YouTube "reused content"/YPP monetization hedge (was the original
  reason commentary existed). Strategy is grow-first; revisit an originality layer (light
  commentary or a niche/heavy editing) before applying for monetization. Per-streamer
  credits are still always generated; uploads private by default.

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
  batch files for user-facing entry points.
- Upload stays `privacy: private` and `enabled: false` by default.
- Keep per-streamer credits/chapters generation intact in credits.py (legal/monetization
  requirement).
