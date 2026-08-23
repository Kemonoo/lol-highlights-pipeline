# DEVLOG — how this project got here

Chronological context for a session picking this up cold. **CLAUDE.md = what the system is
now. This file = how/why it got that way, what was tried and rejected, what broke in
production, and which traps cost hours.** Read both before proposing changes; most obvious
"improvements" here have already been tried and reversed for a reason.

Dates are **git commit dates** (hard evidence). The shell clock and commit dates have
disagreed in this environment — don't trust a single date source; `git log --date=short`
is the anchor. Last entry: commit `e90ae5f`.

**Append an entry** when you change direction, fix a production bug, or shelve an idea.
Record the decision and its cause, not a code diff — git already has the diff.

---

## Snapshot

Automated daily channel: **KEMONO** (`Kemonoo/lol-highlights-pipeline`, branch `master`).
Top LoL Twitch clips → AI-filtered → countdown video + AI thumbnail + derived Shorts →
auto-uploaded public, unattended at 03:00 via Task Scheduler (`run_daily_auto.bat`).

Current format is **LEAN** (Synapse-style): curated clips + transitions + brand, **no AI
voiceover, no commentary script, no background music bed**. Foreign-language clips are
included and get **burned English captions** from translated speech. Outro = KEMONO logo
over an NCS track's drop. ~12 videos uploaded as of `e90ae5f`; a handful of viewer comments
so far (see *Viewer feedback*).

---

## Timeline

### Pre-repo (~2026-06-09) — filtering era
Cost-cascade filter built and tuned against hand labels (`tools/label_clips.py` +
`eval_filter.py`): v2→v4 took precision 0.25→1.0. Blacklist seeded from owner labels (JP
event/custom-tournament channels). This is why the filter is a *decision/detection split*
with per-clip caches — retuning rules costs seconds, not GPU time. Don't collapse it.

### 2026-06-13 → 06-15 — repo + structure (`7e15b5c`…`92e4b3b`)
Initial commit; Shorts pipeline; owner-feedback UI; `pipeline/` reorganized into
`ingestion/filtering/enrichment/production/publishing/feedback/tools`; first cinematic
thumbnail compositor. Pre-pivot experiments (shorts app, clip_intel narration platform)
were moved to `_archive/` — reference only, do not touch.

### 2026-06-15 → 06-18 — quality pass + viral research (landed in `6535994`)
- **Voice/commentary rework.** Owner: the TTS "sounds like a robot", commentary read like a
  talk-show. Rewrote prompts to montage-caster style, switched voice to Kokoro `af_bella`,
  made the Kokoro pipeline a module-level singleton (it was rebuilding the model per clip),
  added a non-Latin sanitizer so English voices never try to read CJK/Cyrillic.
- **Animated PROJECT-style nameplate** (`production/nameplate.py`): PIL frames → transparent
  qtrle .mov, overlaid by assemble + a synthesized SFX (`tools/gen_sfx.py`). Chose
  PIL+ffmpeg over Playwright/Chromium **on purpose** — no browser in the unattended run.
- **api_judge fail-open bug.** On quota-dead days unjudged clips were *kept* at ent6/pq5, so
  garbage shipped. Added `local_judge()` (kills/multikill/audio/motion) — verified it turned
  keep-all into KEEP 14 / DROP 10 on a 24-clip day.
- **Language exclusion.** Owner watched a Chinese singing clip with a VO reading "Chinese
  letter. Chinese letter." → excluded `ja`/`zh`, English-only VO.
- **Viral research → `VIRAL_STRATEGY.md`.** MrBeast's leaked production doc + ViewStats +
  2026 thumbnail/retention guides all reduce to: **CTR** (idea+thumbnail+title packaged
  together, A/B tested) and **AVD** (won or lost in the first minute). Produced a ranked
  roadmap; titles and thumbnails were done first as the highest-leverage items.
- **Titles** rewritten to curiosity-first, rotating daily (`credits.py` `_title`).
- **AI thumbnails** (see *Thumbnail evolution* below).
- **Owner-feedback pipeline deleted.** Owner: "my feedback will just go through this chat."
  The `owner_feedback.py` UI/bat are gone; the **viewer-comment loop stays** (`feedback.py`).

### 2026-06-19 — the Synapse pivot (`415f389`, `af2fd42`)
Owner looked at **Synapse** (`@Synapse1`, ~700–800k subs, running since ~2015, episodes
literally numbered "Best of LoL Streams #1253") and found it does *less*: English clips,
simple transitions, brand intro, funny outro clips, **no commentary, no captions, no music**.
Conclusion: their moat is **curation + brand + consistency**, not production layers.
→ Went **lean**: `commentary.enabled: false`, `tts.enabled: false`,
`video.music_enabled: false`. Machinery kept behind the switches — do not delete it.
→ Built the **KEMONO brand** (`production/brand.py`): God Fist Lee Sin splash (Data Dragon
`LeeSin_11`, overridable with an edited image via `video.brand.splash_path`) + gold metallic
wordmark reusing the thumbnail's treatment, animated as the intro sting.
→ **Multilingual transcription** (`enrichment/transcribe.py`, faster-whisper
`task=translate`) so nothing published is in another language.
→ Monetization trade-off accepted knowingly: commentary was the "reused content"/YPP hedge.
Owner chose **grow first, monetize later** ("why would anyone switch to my copy of a YT
channel?" is still an open strategic question — see *Open items*).

### 2026-06-19 — funny outro: tried, shelved (`6d3732f`)
Prototyped a Gemini funny-reaction finder. **Detection worked** (9/10 on a scatting/singing
bit; correctly gave "calmly takes a drink" 0/10) but it **conflated hype with comedy**
(pentakill screams scored high) and **could not localize the window** (timestamps came back
degenerate, `0.2-0.2s`). Owner reviewed 4 previews: only 1 was actually funny → "if that was
the best you can do, let's give up on that part for now." Tool deleted; the full findings and
the right approach (funny-title prefilter over a *broader* fetch, comedy-specific prompt,
audio/whole-clip windowing, rolling dedup pool) are in **FUTURE_WORK.md**. Read that before
re-attempting.

### 2026-06-19 — production incidents (`6d3732f`, `7be80c5`, `ca6f0e6`)
Three real bugs, all found by reading `data/logs/auto_*.log` (always start there):
1. **Retired model.** `gemini-2.0-flash` began 404-ing on `generateContent` → the video judge
   had been silently falling back to `local_judge` for days, i.e. clip selection lost its
   "taste" pass with no error. Now `gemini-2.5-flash` (+ commentary on `2.5-flash-lite`).
   Lesson: a graceful fallback can hide a dead dependency — check the logs for how often the
   fallback fires.
2. **Duplicate upload.** `run_daily_auto.bat` retries the whole run on non-zero exit. Shorts
   crashed *after* the main upload succeeded → retry → **second YouTube video** for
   2026-06-18 (`BE_-ut285OQ` + `OhYVCmSULfs`, both still in `state.json`). Fixed with an
   idempotency guard (`state.uploaded_id()`, `upload.allow_reupload` to force).
   **Lesson: with a whole-run retry, every side-effecting stage must be idempotent.**
   Shorts have their own `done.json` guard; anything new that posts/writes externally needs
   the same treatment.
3. **cuDNN crash.** faster-whisper on CUDA died in the *scheduled* session
   ("Could not load symbol cudnnGetLibConfig", err 127) — the interactive shell was fine.
   `transcribe.device` now defaults to **cpu/int8**. Only flip to cuda if cuDNN is fixed.
- Also: Shorts **voiceover and the AI meme-caption overlay removed** (owner: only translate
  speech + caption it), music bed lowered then disabled, captions raised.

### 2026-06-20 — captions, languages, outro (`e90ae5f`)
- **Captions ASS → drawtext.** Repeated attempts to move Shorts captions failed to verify
  because **this dev shell's ffmpeg cannot rasterize ASS** (no fontconfig font match) — every
  preview frame came back blank, so positioning was blind guesswork for several rounds.
  Switching to `drawtext` with an explicit `fontfile` made it render everywhere, pixel-precise
  and previewable. Position is now **layout-aware**: split layout → above the facecam *and*
  the in-game HUD (`shorts.caption_split_gap`), blur-bg → high lower-third
  (`caption_margin_v`). **If burned text ever "doesn't render", suspect fonts, not logic.**
- **European + Korean clips re-included** (`prefilter.include_languages` = en, fr, de, es,
  ru, pt, it, pl, cs, sk, ko; `ja`/`zh` stay out). The transcribe stage runs again, skipping
  `skip_languages: [en]`, and **assemble burns English captions onto foreign-speech
  segments** so they work without narration. Verified on a Korean clip → "ULTRA GUNFARE!".
- **Outro** = KEMONO logo over an NCS track's **energy-detected drop** (`_find_drop`,
  librosa RMS, fraction fallback). `config.py` picks an outro track independently of the
  (disabled) bed and still credits it.

---

## Settled decisions — do not re-litigate without instruction

| Decision | Why | Reversal history |
|---|---|---|
| No AI voiceover / no commentary script | Owner hated the robotic TTS; a viewer commented "**the AI is disgusting**"; Synapse proves it's unnecessary | Built (af_bella + montage prompts) → **switched off** |
| No background music bed | Viewer: "**music is too loud**"; clip audio carries the energy | −14 dB → −20 dB → **disabled**; outro music kept |
| Multi-language clips + burned English captions | Owner wants EU/KR clips; captions make them watchable without narration | all langs → **English-only** (lean) → **EU+ko re-included with captions** |
| Title = clickbait hook + `\| Month Day, Year` | Competitor daily channels suffix an episode number; owner wants the date as the series marker. Hook need not be literally accurate | dated ISO title → **date removed** (looked stale) → **date re-added as series marker** |
| Thumbnail: Gemini enhances the *face only*, we composite the rest | Full-scene generation is unreliable (see gotchas) | PIL-only → full-scene Gemini → **face-only + PIL** |
| Montserrat Bold, centred, no underline, radiating red border w/ slightly rounded corners, white sticker outline on the face | Owner's explicit visual direction, iterated over several rounds | Arial Black → Cinzel (rejected) → **Montserrat** |
| Shorts: clip audio + English speech captions only | Owner: "do not do any voice overs… translate the speech, make the captions, and that's it" | VO + AI meme overlay → **both removed** |
| Owner feedback via chat, not a pipeline stage | Owner's call | UI built → **deleted**; viewer-comment loop kept |
| Funny outro shelved | Detection works, comedy≠hype, windowing failed | see FUTURE_WORK.md |
| Grow first, monetize later | Lean mode weakens the YPP "reused content" hedge; accepted deliberately | — |

---

## Environment traps (each of these cost real time)

- **This dev shell can't render ASS subtitles** (blank frames) — use `drawtext` + explicit
  `fontfile` for anything you need to *see*. Production Windows renders ASS fine, so a blank
  preview is not proof of a production bug.
- **Gemini image generation needs a *billed* API key** — free tier is `limit: 0`, and the
  error is a 429 that looks like ordinary rate limiting. Billing is enabled now.
- **Nano Banana guardrails:** naming the IP ("League of Legends" / a champion) **or** sending
  3+ copyrighted reference images returns `finishReason: IMAGE_OTHER` with **no image and no
  text**. Keep the prompt generic and send ≤2 images.
- **Gemini model IDs get retired** and `ListModels` may still list them; the failure surfaces
  as a silent fallback, not a crash.
- **`keep_raw_days: 1`** — raw MP4s are pruned fast, so you often *cannot* re-render or
  re-analyze an older date (bit me on 06-15 and 06-17). Use the newest date with MP4s
  present, or re-download.
- **Whole-run retry in the bat** amplifies any late-stage crash into duplicated side effects.
- Long-form segments cache; `<segment>.vo` sidecars mark whether VO existed at render time,
  so segments re-render when VO appears. Delete `segments/` when changing render code.
- JSON in `work/` may carry NUL padding — keep the `.rstrip("\x00")` on new readers.

## Verified in production (evidence, not assumption)

- Last two unattended runs (`data/logs/auto_2026-06-20.log`, `auto_2026-06-21.log`):
  **0 retries, 0 cuDNN errors, exactly 1 main upload each** → the idempotency + CPU
  transcription fixes hold. 2026-06-19's log still shows 1 retry / 2 uploads (pre-fix).
- Newest run logged `captions: 88 words @ 958px from bottom` on a **French** clip → the new
  caption geometry and the EU-language path are both live.
- Transcription across a real day: fr/de/es/ru/pl/ko all translated to English; silent clips
  correctly return empty; English clips skipped.

## Viewer feedback so far (real signals — few but acted on)

1. "**The AI is disgusting**" (on a pre-lean video with TTS) → voiceover dropped everywhere.
2. "**The music is too loud**" → bed lowered, then disabled.
Early comments are treated as signal, not noise, because volume is low and there's no
incentive to troll yet. The automated loop still requires `min_agreement` before flagging.

## Open items / next up

- **Cold-open hook** — the top *retention* item in VIRAL_STRATEGY.md and still not done:
  front-load the #1 moment, kill the slow intro, keep pre-content under ~8s.
- **Only 1 of 3 Shorts produced** in the newest run ("shorts: 1 processed", `count: 3`).
  Unclear why — suspect missing full-quality MP4s for candidates 2–3, or per-clip skips.
  Worth diagnosing from the log; note the log file has binary noise that breaks naive grep.
- **Facecam misdetection**: a *minimap* was detected as a face (split layout showed the
  minimap in the bottom panel). Haar cascade limitation — YOLOv8-face upgrade path is in
  FUTURE_WORK.md; `_detect_facecam` is the single place to change.
- **Duplicate 2026-06-18 upload** (`OhYVCmSULfs`) may still need manual deletion in YouTube
  Studio; both IDs remain in `state.json`.
- **Differentiation vs Synapse is unresolved** — a lean English clone competes head-on with
  an established brand. The automation-native answer nobody can hand-do: **per-language
  channels** (the translation stack already exists). Parked, not decided.
- **Thumbnail A/B**: YouTube's "Test & Compare" has no public API, so only the primary
  thumbnail is uploaded; variants must be added by hand in Studio, or learned later via the
  analytics loop.
- **CPU transcription lengthens the unattended run** — fine at 03:00, revisit if it bites.

## Working agreements with the owner

- **Show, don't claim.** Render a frame/sample and display it; the owner iterates visually
  and will reject descriptions of work they can't see.
- Feedback arrives as several items in one message → handle them **one at a time**, in order.
- Commit + push to GitHub at milestones; the owner asks for this explicitly.
- Owner runs the scheduled job on their own Windows machine (RTX 3050 4GB) — favour stability
  over speed for anything in the unattended path.
- Don't over-explain finished work; commit-style summaries are preferred.

---

## 2026-07-28 — Open-source pass: provider abstraction, model migration, doctor

Goal: make the repo something a stranger can clone and run with **their** keys and
hardware, without weakening what's here. Five things came out of it.

**1. The paid layer had ~5 weeks to live.** Config pinned `gemini-2.5-flash` (judge),
`gemini-2.5-flash-lite` (commentary/feedback) and `gemini-2.5-flash-image` (thumbnail).
All three were deprecated: the image model shuts down **2026-10-02**, the other two
**2026-10-16**. Verified against the live ListModels endpoint rather than the docs, and
moved to `gemini-3.6-flash` / `gemini-3.1-flash-lite` / `gemini-3.1-flash-image` (all
confirmed reachable on this key). This is the *second* time a retirement broke the
pipeline — `gemini-2.0-flash` was the first. Hence #2.

**2. Model ids now live in exactly one place.** New `pipeline/providers/` package: stages
ask for a ROLE (`judge`/`vlm`/`commentary`/`feedback`/`thumbnail_image`) and
`llm.roles.*` in config.yaml decides the backend. Four adapters — gemini, ollama,
openai-compatible (which covers OpenRouter/Groq/Together/LM Studio/vLLM/llama.cpp and
Ollama's own `/v1`), anthropic. The next retirement is a one-line config edit, and
someone with their own GPU box can point `base_url` at it.

Replaced four hand-rolled `requests.post` call sites that each had their own retry and
brace-scraping JSON parse (only api_judge retried at all). All four now use native
structured output — `responseSchema` / `format` / `json_schema`. That matters most for
the 4B local model, which wrapped JSON in prose often enough to skew the filter.

**3. Judge quality was being throttled by an obsolete limit.** `max_mb: 19` + 480p/CRF-30
existed to fit Google's 20MB inline cap. That cap became **100MB in Jan 2026**. The judge
had been grading fast teamfights off a 480p smear for no reason. Now 720p/CRF-28 at 90MB,
with the Files API for anything larger. Also set `thinking: low` on the judge role —
measured 409 → 92 tokens and 4.2s → 2.3s per call with identical scores.

**4. Dangerous defaults.** The committed config had `upload.enabled: true`,
`privacy: public`, and the same for Shorts. Anyone who cloned this, completed OAuth and
ran it would have auto-published to their own channel. Now false/private across the
board — and this contradicted CLAUDE.md's own stated constraint, which is worth
remembering as a class of bug: the constraint was written down and the config drifted
past it anyway.

**5. Hardware was being ignored.** Everything encoded with libx264 on CPU. `video.encoder:
auto` now probes ffmpeg and *test-encodes a frame* (NVENC is listed on machines with no
driver loaded) — this box picks `h264_nvenc`. Verified NVENC segments still concat with
stream copy, which was the real risk. Note presets are not portable: an x264 preset name
is rejected by NVENC, so `hardware.args_for_encoder()` maps per family.

Also: `transcribe.device: auto`. The old CPU pin was working around an unattended crash
in cuDNN — root cause was simply **missing cuDNN 9** (`pip install nvidia-cudnn-cu12`).
`auto` verifies ctranslate2 *and* cuDNN before selecting CUDA, so it can't crash mid-run
the way it did.

**`python -m pipeline.doctor`** reports all of the above and runs as a preflight on every
run. First cut of the preflight was wrong in an instructive way: it skipped the actual
role health check for speed, which meant the one question it existed to answer — "is
Ollama running?" — was the one it couldn't answer. Now it really checks, scoped to the
roles the planned stages need (2.4s for a full run, 0s for `--only assemble`).

**Two real bugs found by turning the linter on**, both pre-existing:
- `tools/review_clips.py` — `seen, files = set(), [... if f in seen ...]` raises
  NameError every time (tuple RHS evaluates before the binding). The labelling UI had
  been broken on this path.
- `tools/collect_training_data.py` — imported `.scoring`, which moved to
  `filtering/scoring.py` in the subpackage reorg. Module hadn't imported since.

Kept deliberately: the cost cascade, the detection/decision split, cache versioning,
FFmpeg-only rendering, the stage contract. Lint is scoped to E/F/W/I/UP — B and SIM are
refactor opinions and applying them to working render code is churn.

Repo hygiene: MIT LICENSE (with an explicit notice that the *clips* aren't covered),
README rewrite, `docs/setup.md` (the Twitch app and YouTube OAuth walkthroughs, including
the test-user step everyone misses), `docs/architecture.md`, CONTRIBUTING, pyproject with
extras so a curious visitor installs ~50MB instead of 4GB, 43 tests over the pure
decision rules, and CI on Windows + Linux. Brand defaults are neutral now; the KEMONO
setup lives in `config.kemono.yaml` as an overlay — which is also the documented way for
anyone to keep their own settings without forking config.yaml.

**Still open:** the README has no screenshots. For a *video* project that is the biggest
remaining gap, and it needs a human with a finished render.

---

## 2026-07-28 (later) — First public run on the new stack, and three bugs it found

Ran the whole pipeline end to end with the refactor in place: 219 clips → 40 → 11,
uploaded public as `WlnEF80YafU` plus 3 Shorts, ~2h10m at 70% CPU. What broke on the way
is the useful part.

**1. Ollama structured output came back empty — a regression I introduced.** The new
provider layer called the legacy `/api/generate`, which on Ollama 0.32.1 returns `''`
whenever `format` is combined with images. Every frame vote was empty, every clip scored
`0of3`, and **the rejections were cached** — clips literally titled "Quinn Penta!" and
"Instant Quadrakill" were thrown away. Fixed by moving to `/api/chat`.

Two lessons worth keeping. First, the fix I reached for *next* was wrong: making an empty
response raise `_Retryable` turned the kill-feed crops — where empty legitimately means
"no kills here" — into 20s/40s/60s backoff storms. Empty is now a valid answer at the
provider, and the *consumer* decides whether it's suspicious. Second, `detect()` now
refuses to cache a verdict when **no** frame produced a usable answer:

    if not answered:
        raise RuntimeError("no usable answer from the vlm provider ...")

A broken provider used to be indistinguishable from a confident REJECT, and the damage
outlived the outage because it went in the cache. Distinguish "said no" from "said
nothing" anywhere a model's silence gets persisted.

**2. `pipeline.progress` reported on the watcher's config, not the run's.** Owner noticed
`[-] upload  disabled` while an upload was very much about to happen — the monitor loads
config itself, and it hadn't been passed the same `--config` overlay as the run. I had
claimed in the docstring that it "can't drift out of sync with what the pipeline did",
which was true of the per-clip caches and false of everything config-derived. Runs now
drop `work/<date>/run.json` at startup recording what they are configured to do, and
progress prefers it, falling back to config only for a date that never ran.

Second, smaller one from the same report: `upload` is the only stage with no output file,
so it sat at PENDING forever and `--watch` could never print "Run complete". It now reads
the YouTube id back out of `state.json`.

**3. The video description advertised "with commentary".** Hardcoded in `credits.py`
since before lean mode; it went out on a public video. Now conditional on
`commentary.enabled`.

**Degradation held up.** The Gemini thumbnail tripped the recitation filter
(`IMAGE_OTHER`) and fell back to the PIL design without stopping the run — though it
landed on the weakest variant (`champion=none, face=pfp`), which is worth revisiting.

**Unattended runs now manage power.** `run_daily_auto.bat` wraps the run in
`scripts/keep_awake.ps1` — a PC woken by a wake timer is in an *unattended* wake and
Windows re-sleeps it after ~2 minutes, which would have killed every scheduled run before
it finished. `SLEEP_AFTER=1` then suspends the machine via `scripts/sleep_prompt.ps1`,
which skips its countdown window when the session is already idle (nobody to ask at
03:00) and treats every ambiguous case — Esc, closing the window, no interactive desktop
handling — as "stay awake". Per-machine settings moved to a gitignored
`auto_run.local.cmd` so the committed `.bat` keeps shipping harmless defaults: no upload,
no sleep. Same split as `config.yaml` vs the overlays.

---

## 2026-08-23 — Every video has been getting the LOCAL thumbnail, not the AI one

Owner reported the AI thumbnail "still isn't working" after a prior session's model-id
fix. It wasn't the model id — `data/logs/auto_2026-08-2{0,1,2,3}.log` showed
`generate_gemini` failing every single day (401 once, then 429 "quota exceeded" three
days running), falling back to the local design, which **does** get set as the YouTube
thumbnail successfully. So a thumbnail was never missing; the AI one specifically never
renders.

Live-tested the exact call against `gemini-3.1-flash-image`: Google returns
`limit: 0` on `generate_content_free_tier_requests` for that model. Image-generation
models get **zero free-tier quota on the API**, full stop — unrelated to usage tier,
unrelated to being able to generate images in the Gemini chat app (different product,
not metered the same way). The only way to use this provider is a billed API key. Owner
doesn't want the cost, so switched `config.kemono.yaml`'s `thumbnail.provider` back to
`local` — no point spending 4 retries a night on a quota that can't open without billing.
See *Local face-preserving AI thumbnail* in `FUTURE_WORK.md` for why a local SDXL-based
replacement wasn't built instead (technically feasible on the 8GB 3050, but heavy and a
departure from the project's dependency-light design).

Since `local` is now the real, permanent default rather than an occasional fallback, gave
it the visual treatment the `gemini` path had and it didn't: `_compose_ctr` now renders
the headline as metallic gradient text (`_title_block`/`_metallic_line`, same Montserrat
Bold assets) instead of flat white, wraps the whole frame in the radiating accent-colored
border (`_red_border`), and puts the streamer credit in a bordered pill (`_name_badge`) —
all reused from code the gemini path already had, just never shared with `local`.

Also fixed a real bug this surfaced: 2026-08-22's *live, already-uploaded* thumbnail was
a giant blown-up crop of the League HUD's champion portrait icon, not the streamer's
face. `_detect_facecam`'s Haar cascade false-positived on the portrait's painted
eye/highlight shapes. Facecam overlays are placed in a corner specifically to stay clear
of the HUD, so added a bottom-center exclusion band to `_detect_facecam`
(`pipeline/publishing/shorts.py`) — re-ran detection against the exact clip that produced
that thumbnail and it now correctly returns no facecam (falls back to the Twitch pfp)
instead of the false positive. This is a heuristic, not a real fix for Haar cascades
false-positiving on painted art in general — see *Face-cam detection: ML upgrade* in
FUTURE_WORK.md.

**Follow-up, same day — three more owner review passes on `_compose_ctr`.** Background:
dropped the cinematic-grade + vignette from `_prep_bg` entirely (it read as too dark) for
plain brightness/saturation/contrast — this is the only treatment now, not a config
choice. Face: went centered, then back to the original lower/bottom-biased position
(`iy = H - d + 0.06*H`) per owner preference — noted as "looks better slightly lowered,"
not actually centered.

Border took three iterations to get right, worth recording so it isn't re-litigated:
1. Sharp corners flush to the edge (`radius=0`) — corners looked "not well-fit."
2. Rounded + `inset=10` (pulled the whole frame in from the edge) — this was wrong: it
   put a visible gap of bare background *outside* the frame along the entire perimeter,
   not just corners. Owner: "outside of it is the rest of the youtube website," i.e. the
   frame must be the literal outermost layer, nothing beyond it.
3. **Fix**: the stroke was never actually non-uniform (verified via SDF math: 10px on a
   straight edge, ~10px radially through the corner) — the real defect was that the
   rounded cut left the four physical JPEG corners unpainted (a small triangular notch of
   bare background), because the fill condition was `(depth >= 0) & (depth < border)`.
   Dropped the `>= 0` floor (now just `depth < border`) so the frame is solid color from
   the rounded inner window all the way out to the literal canvas edge — a picture-frame
   construction, not two concentric rounded rects with a gap between them. This is also
   why it doesn't need to match YouTube's own corner radius: the outermost pixels are
   flat color, so whatever YouTube's UI crops is invisible either way.

Final locked-in default, chosen by the owner from four rendered options (A=sharp/
B=r22/C=r40/**D**=r60 — labelled A-D but C was picked): `_red_border(canvas, color=accent,
radius=40, border=14, glow=12, glow_alpha=140)`.

**Artifact delivery note**: the review artifact (report + a live canvas-based
radius/thickness tuner) never loaded for the owner — spinner, never resolved — across two
publishes, including after shrinking it from ~870KB to ~290KB. Root cause not diagnosed
(could be the per-pixel JS canvas loop, could be unrelated to this session's content).
Fell back to writing labelled JPEGs straight to `data/thumbnail_review/` and reading them
inline instead — that worked immediately. **Lesson: for this owner/environment, prefer
local files over an HTML artifact when the deliverable is "look at a few images and pick
one"** — an artifact adds real failure surface (page weight, JS, the runtime frame itself)
for a task a plain file view already solves. Reach for an artifact when the interaction
itself needs a browser (the live slider was the actual justification here) — but even
then, don't make it the only path: render static alternatives too, if it's cheap.
