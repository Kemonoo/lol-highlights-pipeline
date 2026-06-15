# CLAUDE.md

## Project context

Automated daily pipeline: top League of Legends Twitch clips → filtered by AI →
assembled into a monetizable long-form YouTube video (countdown format, commentary,
TTS voiceover, music, replays) → optional auto-upload. Runs on the owner's Windows
machine (RTX 3050 4GB). Entry point: `python -m pipeline.run_daily [--date YYYY-MM-DD]`.

### Stage chain (pipeline/run_daily.py STAGES, each module has run(cfg, state, date_label))
0. `feedback.py` — reads comments on recent uploads (clip-number refs "#3" map to
   chapters), Gemini-classifies sentiment, aggregates (>= feedback.min_agreement
   agreeing comments = ACTIONABLE) → data/feedback/log.jsonl + proposals.md.
   Never blocks the pipeline. Optional auto_update invokes `claude -p` headlessly
   for CONFIG-LEVEL changes only (this file is its context — keep it current).
1. `fetch.py` — Twitch Helix top clips (or broadcaster list), METADATA ONLY →
   `data/raw/<date>/clips.json`. Downloads are lazy: prefilter grabs low-quality
   copies (`raw/<date>/lq/`) for scoring; vlm_filter downloads full quality for its
   survivors only; api_judge reuses the LQ copy instead of re-encoding
2. `prefilter.py` — free local scoring: title keywords / tournament exclude / audio-hype
   / motion (functions from `pipeline/filtering/scoring.py`) + broadcaster blacklist
   → `work/<date>/prefiltered.json`
3. `vlm_filter.py` + `kill_detect.py` — local Ollama VLM, STEPWISE (one simple question
   per image — small models fail multi-question prompts): gameplay vote (3 frames),
   pro-play check, kill-feed/banner/event-log crop analysis. Detection cached
   (`vlm_partial_v3.json`); decisions are PURE CODE recomputed from cache every run
   (`decide()`) so rule tuning costs zero GPU time → `vlm_scored.json`,
   `vlm_filtered.json`, `report.html` (visual audit), `crops/` (region calibration)
4. `api_judge.py` — Gemini watches survivors as full video (shrunk to 480p, inline
   ≤19MB), scores focus/play_quality/entertainment (cache `api_partial_v2.json`).
   Duration-aware selection: fill toward `video.target_minutes_ideal` with fillers
   (ent≥4), trim at max, order ascending rank = countdown. Rewrites vlm_filtered.json
   (input is always rebuilt from vlm_scored.json — idempotent)
5. `match_linker.py`, `hud_ocr.py` — Phase-2 stubs (Riot API match data; HUD OCR).
   Docstrings contain the implementation plans. Riot API > scraping op.gg/u.gg (no
   public APIs there)
6. `commentary.py` — grounded caster lines (funny/ironic, never invents facts; sees
   previous lines to avoid repetition) + `_intro` cold-open line. Provider auto:
   Gemini if key, else Ollama → `commentary.json` [{clip_id, text}]
7. `tts.py` — edge-tts (free), word timestamps; `_intro` handled like any line;
   `tts.enabled` config switch → `work/<date>/vo/<clip_id>.mp3`
8. `assemble.py` — ffmpeg only (no moviepy): intro card → segments (lower-third
   slide-up, #N countdown badge, VO ducking, 0.3s fades, 0.5x REPLAY part for clips
   with api_rank_score ≥ replay_min_score using api_best_moment_s) → outro → concat
   demuxer → master (looped music bed + single-pass loudnorm, video stream copied)
9. `credits.py` — title hook from best clip, chapters, per-streamer credit links,
   music attribution → `data/output/<date>.meta.json`
10. `upload.py` — YouTube Data API v3 resumable upload, OAuth desktop flow
    (client_secret.json in root, token cached at data/yt_token.json), private default

Support: `config.py` (YAML + ${ENV} expansion + .env loader; resolves music_path),
`state.py` (data/state.json: processed clip ids, permissions, uploaded videos),
`tools/label_clips.py` (browser labeling UI, stdlib HTTP server, keys G/O/B →
work/<date>/labels.json), `tools/eval_filter.py` (precision/recall vs labels),
`tools/gen_music.py` (numpy-synthesized copyright-free music bed → assets/music/bg.mp3),
`tools/collect_training_data.py` (standalone Twitch fetch + feature extraction →
data/training/dataset_*.json), `tools/review_clips.py` (Tkinter labeling UI for
training data), `tools/train_classifier.py` (logistic regression on labeled clips).

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
- **Legal posture** (PLAN.md): YouTube "inauthentic content" policy requires real
  per-clip commentary; per-streamer credits always generated; uploads private by
  default; music is generated in-house (no licensing).

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
- Free-by-default: local Ollama for filtering, edge-tts for voice, generated music.
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
