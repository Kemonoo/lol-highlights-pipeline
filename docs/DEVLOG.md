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
`eval_filter.py`): v2→v4 took precision 0.25→1.0 (on a very small set — see the
2026-09-13 correction at the end of this file). Blacklist seeded from owner labels (JP
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
> **Superseded 2026-09-01 — that last sentence was wrong.** The probe is a *load-time*
> check that the libraries exist; it says nothing about the GPU surviving inference, and
> it did not. See the 2026-09-01 entry: 7 of 32 August runs were killed mid-transcribe.

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


## 2026-09-01 — Two silent-damage bugs found by reading a month of logs

Both had been shipping degraded videos for weeks without a single ERROR line. Neither
was visible from the pipeline's own output; both were found by aggregating `data/logs/`
and the cached JSON across all of August.

### 1. The judge never sent `entertainment` on half the clips

`api_judge.SCHEMA` declared `properties` but no **`required`**. Gemini treats that as
"every field optional" and intermittently returned only
`{clip_focus, play_quality, what_happens}`. Measured: **133 of 233 cached verdicts (57%)
had `entertainment == 0` — and `best_moment_s == 0` on the exact same 133**, while
`what_happens` was never empty. Two fields vanish together; the prose one never does.

The amplifier was `int(r.get("entertainment", 0) or 0)`. On a 0-10 scale 0 is not a
neutral "unknown", it is the harshest score available, so "the model didn't answer"
became "maximally boring" and was indistinguishable from a real verdict on disk
(`gameplay_ent0_pq8` looks like a judgement).

Damage, both halves of it:
- **Dropped clips.** Keep is `ent>=6 or pq>=7`; with ent pinned to 0 only mechanically
  impressive plays could survive. 50% of DROPPED clips had ent==0 vs 14% of kept, and
  **73 clips were dropped sitting at `pq=6`** — one point under the rescue bar with
  entertainment unknown. Hence **14 of the last 16 runs came in under the 8-min target**
  ("Only 3.2 min of keepable content", 2026-08-31) while the day's pool was blamed.
- **Mis-ranked the clips it kept.** `rank = 0.4*ent + 0.6*pq`, so a real 08-01 quadra+ace
  scored `0.4*0 + 0.6*8 = 4.8` instead of ~8. It was kept, but sorted mid-pack — and
  `countdown_rank` sorts on that score, so **the "#1 clip of the day" was frequently not
  the best clip**. This half is the one that also degraded the videos that came out at
  full length.

Fix: `required` on all five fields (verified it survives `to_gemini_schema()`), plus a
pure `parse_verdict()` that returns `None` on a missing score → `local_judge()`, so
absent can never again be read as zero. Cache bumped `api_partial_v2` → **v3** (detection
semantics changed; the 133 poisoned verdicts must be re-judged).

Confirmed live, same clip and prompt, only `required` differing: **4/8 responses
incomplete without it, 0/8 with it**; a previously-`ent0_pq8` clip came back
`ent=7 pq=6 bm=10`, rank 4.8 → 6.4.

> **Quota note found while testing:** free tier is **20 judge requests/day** and a run
> judges ~17. That is the source of the 429/503s in the logs, and an expansion round can
> exceed it — at which point `consecutive_fails >= 2` trips the sticky fallback and
> downgrades the *rest of the run* to `local_judge()`. Not addressed yet.
> Also unaddressed: `finishReason: RECITATION` is a per-clip content refusal but counts
> toward that same quota-dead heuristic, so two unlucky clips can do the same thing.

### 2. A GPU crash in transcribe made the video ship with missing captions

`transcribe` was killed **mid-inference on 7 of 32 August runs** (08-06, 10, 11, 13, 20,
22, 09-01) — always right after "Detected language", always on `cuda/int8_float16` on the
4GB card. **No Python exception**: `run_daily` catches `Exception` and `transcribe` catches
per-clip, and neither logged anything on any of the 7. The interpreter is gone, so
**nothing in-process can catch it** — that is why there is no try/except in the fix.

The real damage was not the 10-minute retry. `transcripts.json` is flushed **per clip**
(deliberately — a crash loses no work), but `_stage_outputs` used that same file as the
"stage done" marker. So the retry saw it, logged *"already done — skipping"*, and **never
transcribed the remaining clips**. Perfect correlation across August: **all 7 crash days
shipped partial captions, all 22 clean days were complete** — 08-31 had captions on
**2 of 9 clips**. In lean mode the burned captions are the entire mechanism carrying the
video without narration, so those uploads were substantially broken and nothing said so.

Fix, two parts:
- **`transcripts.done.json`** — a real done-marker written only after every clip has been
  attempted; `transcripts.json` stays the incremental cache. Same split `shorts.py`
  already uses (renders vs. its own `done.json`). `progress.py` had the same conflation
  and now reports RUNNING (not DONE) on a partial cache.
- **`transcribe.cpu_fallback_after_crash`** (default true) — drop a breadcrumb before
  touching the GPU, clear it on clean completion. A run that finds one knows the last
  attempt was killed there and uses `cpu/int8`. Converts crash→retry→crash into
  crash→retry-on-CPU→**completes**, and part 1 means the retry actually finishes the job.

**Lesson worth generalizing: an incremental cache must never double as a done-marker.**
The two look identical on disk and differ exactly when something went wrong. Worth
auditing the other stages for the same shape.

### 3. Follow-ups the same day: the GPU crash root-caused, and a 14-day silent outage

**The transcribe crash is a specific-clip cuDNN fault, not VRAM.** Reproduced it exactly:
the repro died on the *same clip* the 2026-09-01 production run died on (30.016s, `es`
0.95). Then isolated it — that clip **alone, as the first call, crashes**, so it is the
input, not accumulated state:

| | result |
|---|---|
| clip on `cuda/int8_float16`, `cuda/int8`, `cuda/float16` | **exit 127, every time** |
| same clip on `cpu/int8` | fine, 25 words |
| rest of that day (8 clips) on cuda | all fine |

So: **~1 clip in 9 that day; ~2% of clips overall**, which predicts `1-0.98^12 ≈ 22%` of
runs dying — matching the observed 7 of 32 exactly. Exit **127** is the same code as the
2026-06-19 cuDNN incident ("Could not load symbol cudnnGetLibConfig, error 127"), so this
looks like a lazily-loaded cuDNN kernel that only some input shapes reach. **Corrected a
wrong assumption from earlier in the session: the card reports 8192 MiB with ~2 GB in
use, so this was never an OOM** and the "4GB card" framing in CLAUDE.md is misleading.
The committed `cpu_fallback_after_crash` remedy is now validated against the real
crashing input. Not built: per-clip subprocess isolation, which would cost the poison
clip instead of the whole run — worth it only if one wasted run per ~5 days starts to hurt.

**The judge was fully dead for 14 consecutive days and nothing said so.** 08-05 → 08-19,
**every clip on every one of those days was scored by `local_judge`** (`api_focus ==
"local"`, 0 API verdicts in those logs). Causes, both permanent: `HTTP 401 - Request had
invalid authentication credentials` and `HTTP 429 - Your prepayment credits are depleted`.
Across August, **288 of 521 clips (55%) never saw the paid taste pass.** The videos
published normally and looked fine.

This is the 2026-06-19 lesson repeating verbatim — *a graceful fallback can hide a dead
dependency* — so the fix is aimed at the hiding, not just the failing:

- **`classify_failure()`**: `refusal` (RECITATION/SAFETY — this clip's content, must not
  count) / `dead` (401/403 — stop calling at once) / `transient` (429/503/timeouts —
  count as before). Previously *every* exception counted the same, so two refusals or two
  network timeouts silently switched the rest of the run to local scoring.
- **`ProviderError` now carries `status` and `reason`**, and `retrying()` propagates them
  through the give-up path instead of flattening the cause to a string. Callers could not
  otherwise tell a dead key from a rate limit without grepping message text.
- **A loud end-of-stage alarm**: `JUDGE DEGRADED: n/m clips scored locally` (ERROR when
  all of them, WARNING when some), and the summary line now reads
  `X judged by API, Y local`. This is the line that would have caught the 14 days.

> Still open: free tier is **20 judge requests/day** against ~17 used per run, so there is
> almost no headroom — an expansion round pushes past it. A paid key or a smaller
> `vlm_filter.max_keep` are the two levers.

### 4. Thumbnail: the face was never chosen for expression

The local PIL design was doing three things wrong, all visible in the shipped output for
2026-08-30 and 08-31. (This is NOT the 08-23 gemini/billing issue — `provider: local` is
correct and stays.)

1. **`_reaction_face()` took a single frame at `best_s` and cropped it.** Whatever
   expression happened at that instant is what shipped, and on real output that was
   routinely a flat or averted face. The face is the largest element on the canvas, so
   it decides the click. Now samples 9 frames across the peak window and ranks them:
   the per-pixel **median** face across samples approximates the streamer's RESTING
   face (it is where the clip spends most of its time), so distance from that median
   finds the animated frame, with Laplacian sharpness breaking ties away from motion
   blur. No landmark model, no new dependency — the cv2 already used for facecam
   detection. On 08-31 this turned a flat stare into a genuine open-mouthed laugh.
2. **The headline was the same two words on 15 of 29 days** (52%) because `_hook()` fell
   through to a constant `"INSANE PLAYS"`. Widened `_HOOKS` to match the judge's rich
   `what_happens` text (teamfight/backdoor/misclick/tilted/... — it had been tuned for
   short Twitch titles), and made the fallback a date-seeded rotation. Repetition
   52% → 24%, and the most common hook is now `PENTAKILL`, which is earned rather than
   generic.
3. **Background was the raw frame**, HUD, minimap and streamer chat included, which at
   the ~320px the browse feed renders is mud. Added `thumbnail.background_blur`
   (default 2px). Deliberately blur and **not** darkening — the owner already rejected
   the darker cinematic grade for crushing the edges, and that decision stands.

**Two regressions caught only by rendering it.** The first attempt produced
`HE DID WHAT?!?!` — a generic hook ending in punctuation, plus the `?!` variant 1
appends (`_ask()` now guards this, and the generic hooks carry no trailing punctuation).
And `background_blur: 4` was too strong: the frame stopped reading as League at all.
Dropped to 2. **Neither was visible from the code or the tests — only from looking at
the rendered JPEG.** For this component, render and look before believing it.

**Honest limit:** the picker can only choose the best frame that exists. On 08-30 the
streamer sits in a dark room looking away for the whole clip, so the "after" is barely
better than the "before". A genuinely expressionless source clip needs a different
top-clip choice, not a better crop.

### 5. The judge quota ceiling: measured, not guessed

Left open by the previous two entries. First job was establishing what the limit
actually is, because the error message is genuinely misleading — it reports
`limit: 20` alongside `Please retry in 21s`, which reads like a per-minute rate limit.
It is not. Probing the raw 429 body gives the only reliable discriminator:

```
quotaId:    GenerateRequestsPerDayPerProjectPerModel-FreeTier
quotaValue: 20
RetryInfo:  21s          <- misleading; the allowance is gone for the DAY
```

So: **a hard 20 requests/day/model**, against a run that judges **14-18 clips**
(measured over the last six runs: 14, 15, 15, 16, 17, 18). Three consequences, all fixed:

1. **The retry ladder was burning the budget to prove the budget was gone.** 429 is in
   `RETRY_STATUS`, so a per-day exhaustion retried 4x — three extra charged requests,
   15% of the daily allowance, plus ~120s of backoff, to learn something unlearnable
   within the run. `raise_for_status()` now reads the quotaId and raises a
   non-retryable error when it says PerDay. **Verified against the live API: 0.7s
   instead of ~120s.**
2. **The wall was hit mid-list at an unpredictable point.** Added
   `api_judge.daily_budget` (default 18, 0 = unlimited). Clips are already sorted
   best-first, so a budget means the spend always buys the TOP of the list and the tail
   degrades knowingly, instead of the cut landing wherever the quota happened to run out.
3. **Spend was invisible across runs.** `state.api_spend()/record_api_spend()` track it,
   **keyed to the PACIFIC day** — Google resets at midnight America/Los_Angeles, and a
   03:00 Europe/Amsterdam run is still in the previous Pacific day, so counting against
   local dates would have handed exactly the unattended run a second full budget. Failed
   requests are charged too, because the provider charges them. Verified: 8 clips with
   `daily_budget: 3` makes 3 calls; the bat's crash-retry then makes 0.

> **The ceiling itself cannot be fixed in code** — 20/day is the free tier. The default
> budget of 18 preserves current behaviour while making the wall predictable; it does not
> create headroom. For real headroom the levers are a paid key, or lowering
> `vlm_filter.max_keep` so fewer clips need judging. Left as the owner's call, since
> lowering max_keep trades selection quality for margin.

### 6. The quota ceiling was not a ceiling: the free tier is metered PER MODEL

The previous entry concluded that 20/day was fixable only with a paid key or a lower
`vlm_filter.max_keep`. **That was wrong**, and the evidence was already sitting in the
error body that entry quoted:

```
quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
                                       ^^^^^^^^
```

**PerProjectPer*Model*.** A different model id on the SAME key is a separate allowance.
Verified 2026-09-02 with `gemini-3.6-flash` actively 429ing on quota — all three of
`gemini-3.1-flash-lite`, `gemini-flash-latest` and `gemini-flash-lite-latest` answered a
real clip with native video on the same key, in the same minute.

So `llm.roles.<role>.overflow_models` + `providers.get_provider_chain()`: the stage walks
the chain, and on a PerDay 429 moves to the next id and **re-sends the same clip** rather
than dropping it. Default chain is 4 models → **~72 judge calls/day against a need of
14-18**, free, with no loss of capability (all are native-video Gemini models).
Budget is tracked per model (`state.api_spend("judge:<model>")`).

Verified twice: a simulation where model A allows 3 requests, B allows 3 and C is
unlimited judged all 9 clips via the API with 0 falling back to local; and a live run
against the genuinely exhausted primary, which switched to `gemini-3.1-flash-lite`
mid-run and returned real verdicts.

> Keep the overflow ids genuinely different models. An alias that resolves to the primary
> is not a separate allowance — the chain drops duplicates, but it cannot detect an alias.

**Also corrected: the GPU is an RTX 3050 with 8 GB, not 4 GB.** CLAUDE.md had said 4 GB,
which is what made "VRAM exhaustion" the first theory for the transcribe crash in entry 3
(it was a per-clip cuDNN fault, and the card was at ~2 GB of 8 GB). Installed local
models include `qwen2.5vl:7b` and `llava:13b`, so a frames-based local judge is a real
option if Google ever tightens — Ollama cannot ingest video, so it would judge sampled
frames plus the English transcript instead. Not needed while the chain has this much
headroom; recorded so the option is not rediscovered from scratch.

### 7. data/output was never pruned (14.7 GB, ~200 GB/year)

`cleanup.py` deliberately kept `data/output/` forever while pruning raw clips and
scratch renders. At 26 videos it was **14.7 GB**, growing ~550-850 MB a night — the one
item from the original log audit still unaddressed, and the sort of thing that
eventually breaks the unattended run by filling the disk rather than by failing.

`cleanup.keep_output_days` (default **0 = keep forever**) prunes published masters.
Two conditions, both required, and the second is the point:

- older than `keep_output_days`, and
- `state.uploaded_id(date)` returns a YouTube id — i.e. a copy **provably** exists off
  this machine.

Everything else cleanup removes is reproducible (raw clips re-download, segments
re-render). A finished master is not, and on a clone with `upload.enabled: false` it is
the only copy in existence. So age alone must never be sufficient: a failed upload, or a
machine that does not upload at all, keeps its video regardless of how old it gets.
`<date>.meta.json` is never pruned — it is the record of who was credited, which
credits.py exists to guarantee, and it costs kilobytes.

Default stays 0 in config.yaml (a fresh clone must not delete its own work uninvited);
the owner's `config.kemono.yaml` sets **7**, since that channel publishes publicly and a
week is enough to catch a bad render and re-cut it. Dry run on the real directory: 19 of
26 masters removable, **10.5 GB**, all with confirmed video ids; the 7 kept are all
inside the window, none held back for want of an upload.

> Noticed in the dry run and NOT acted on: `2026-08-13.mp4` is **6 MB** where every other
> master is 300-850 MB. It was published (`lL0xezmbDZg`), so the pruner will take it, but
> a 6 MB daily video is almost certainly a broken render that went out anyway — worth a
> look at that day's log before it disappears.

### 8. A 15-second empty video went out public, and nothing noticed

Found while dry-running the output pruner: `2026-08-13.mp4` was **6 MB** where every
other master is 300-850 MB. It is 14.9 seconds of brand intro plus outro — no clips, no
chapters, nobody credited — and it was **published publicly** as `lL0xezmbDZg`.

The cause was entirely external:

```
download failed for .../CrunchyBumblingCookieBIRB-...:
  ERROR: An extractor error has occurred. (caused by KeyError('data'))
```

**yt-dlp's Twitch extractor broke, and all 181 downloads failed.** From there every
stage did exactly what it was designed to do on an empty input:

```
Prefilter 2026-08-13: 181 scored -> 0 passed -> 0 kept
Filter: 0 -> 0 kept
Selection: 0 clips, 0.0 min
Assembled 2026-08-13.mp4 (0.0 min, 0 clips)
Uploaded 2026-08-13.mp4 as https://youtu.be/lL0xezmbDZg (public)
shorts: 0 processed
```

Run exited **0**. Graceful zero-item handling is what lets a thin day still ship, so no
individual stage was wrong — but nothing asked whether a video actually existed before
publishing one, and `grep` found no such check in assemble.py or upload.py.

`video.min_clips` (default 3) now fails the run at assemble, and upload re-checks the
chapter count independently. The second check is not redundant: assemble is skipped when
its output already exists, so a master built by an older version would otherwise sail
straight to YouTube on the next run, and publishing is the irreversible step. The error
names the likely cause and the fix (`pip install -U yt-dlp`), because that is what a
person reading the log at 3am needs.

Three other days also logged `Selection: 0 clips` (08-05, 08-14, 08-28) without
publishing an empty video — something else stopped them, but the guard genuinely was not
there. **yt-dlp breaking on Twitch is recurring and external; the guard is what makes the
next occurrence a non-event.**

> **Resolved 2026-09-02**: the owner asked for it, so `lL0xezmbDZg` was deleted from
> YouTube via the Data API (`videos().delete`, covered by the existing force-ssl scope)
> after confirming id/title/`PT15S`/public/4 views against the API first. Verified gone
> on a follow-up read — note the read immediately after the delete still returned the
> item, so YouTube's propagation lags a moment; do not treat that as failure and retry.
> The local 6 MB master was removed too. `state.json` keeps its entry: the upload DID
> happen, the record is history, and it also stops any future re-upload of that date.
>
> Still open: **`video.target_minutes_min: 5` has never been read by any code** — dead
> since it was written, and evidently intended as this very guard. Not silently
> repurposed (a 5-minute floor would have blocked real days that shipped 3.2-4.5 min);
> wire it up as a warning or delete the key.

**Process note:** `Selection: 0 clips, 0.0 min` was visible in the FIRST log scan of this
session, sitting in a list of selection lengths. It was read as "a thin day" and passed
over while chasing the entertainment-score bug. A zero is not a small number — it is a
different kind of event, and it deserved its own look.

### 9. Videos were short because of a cap upstream of the judge, not the judge

Owner: videos run short; the ideal range is 8-10 min; a hard minimum-length stop would
be too harsh. Agreed on the last point — the fix is to aim longer, not to abort short.

The target was **already** `target_minutes_ideal: 8` / `max: 10`. Runs were not reaching
it, and the funnel says why:

```
day        prefilter -> vlm_keep -> judged -> kept = minutes
2026-08-25     40         27         18        8     4.3
2026-08-28     40         26         18       13     7.3
2026-09-01     40         26         18       15     8.1
                                     ^^ always exactly 18
```

`vlm_filter.max_keep: 18` was the binding constraint. vlm_filter kept 22-30 clips a day
and this cap threw the surplus away **before the judge ever saw it — 87 viable
candidates discarded over 11 days.** At a ~67% judge keep-rate and ~32s per kept clip,
18 yields ~6.5 min; 24 yields ~8.6.

Raising it is nearly free: all prefiltered clips are VLM-scored and downloaded anyway
(`max_keep` is a slice applied *after* scoring), so the only extra cost is judge
requests — which stopped being scarce the moment the role gained `overflow_models`
(80/day capacity against 24 needed). **Quality is unaffected: the judge still applies the
same bar, it just gets more to choose from.**

Replaying the real cached verdicts at both caps showed the honest limit, though: **4 of
11 days did not move at all**, because `vlm_keep` was only 22-23 — fewer survivors than
the new cap. Those days were limited further upstream by `prefilter.max_keep: 40` at a
~65% VLM pass rate. So that went to **52** (~34 survivors, enough to fill the judge every
day).

> **The real cost is runtime, and it is not small.** vlm_filter runs a local VLM at
> ~2.0 min/clip and is already 79 of the run's 110 minutes; +12 clips is **+24 min**,
> finishing ~05:15 instead of ~04:50. Reverting `prefilter.max_keep` to 40 buys that
> back at the price of short videos on thin days.

Also **deleted `video.target_minutes_min`** rather than wiring it up. It had never been
read by any code. As a hard floor it would have aborted real days that shipped 3-5 min of
genuinely good clips, which is worse than a short video; the failure it looked like it
guarded — a broken extractor yielding nothing — is covered properly by `video.min_clips`.

Invariant worth remembering, since it is easy to break silently:
`api_judge.daily_budget x len(judge chain) >= vlm_filter.max_keep`, currently 20 x 4 = 80
against 24. Set the budget below that and the video quietly gets short again.

---

## 2026-09-09 — Two transcriptions on screen at once; caption grouping

### The purple caption is the streamer's, not ours (settled)

Raised before, dismissed before, never actually verified. It is **burned into the Twitch
source**, and here is the proof rather than the assertion:

* `data/raw/2026-09-08/SmokyHomelyWitchMVGame-*.mp4` at t=25.5s — the *untouched
  download*, before the pipeline opens it — already shows `me whatever you` with a purple
  word-highlight at y≈0.90.
* The same moment in `data/output/2026-09-08.mp4` shows that line **plus** our white
  `YOU` at y=0.70. Two renderers, one of them ours.
* Their ASR and ours disagree on words (`Tavis` vs whisper's `Tabis`), which no single
  renderer would do.

It is a live-caption widget (Streamlabs-style), and it is **common**: sampling one frame
per clip *while the streamer was speaking* found it on **8 of the 17** clips in the
2026-09-08 video, including a Portuguese one. Nothing can remove it — it is pixels.

So `video.skip_caption_when_source_has_one`: when the source already has a caption **and**
the speech is already English, we draw nothing. Non-English keeps ours, because theirs is
in their language and ours is the translation — two lines is the accepted cost there.
The burned caption's language is never read off the screen: a widget transcribes its own
streamer, so whisper's detected language is the same language, for free.

> **Sampling at speech times is the whole trick.** The first version sampled evenly across
> the clip and scored **2/4** — a coin flip — because a caption widget draws nothing during
> silence, so most sampled frames had no caption *even on clips that have the widget*.
> whisper already knows when speech happens. Sampling inside dense word runs makes a
> widget clip show a caption in nearly every frame and a clean clip in none, which is a
> gap wide enough to threshold on (`source_caption_min_ratio: 0.75`). The threshold is
> deliberately high and asymmetric: a false positive costs a clip its captions, a false
> negative only keeps the old behaviour.

### Our own captions: overlapping, and unreadably fast

Both defects were **arithmetic, not rendering**, and both are now in `_caption_groups`
(pure, unit-tested, no ffmpeg):

* **Overlap.** Every word was shown for `max(0.15, end - start)`. whisper routinely emits
  0.06s words back to back — `to` 3.06-3.14, `deal` 3.14-3.28 — so the 0.15s floor pushed
  a word's window past the *next* word's start and drawtext drew both, centred, on top of
  each other. Group windows are now clamped to end before the next one begins, so
  disjointness is structural rather than a consequence of the numbers happening to work.
* **Pace.** One word per 0.15s is ~7 words/second. Captions now group into 1-3 words
  (`caption_max_words`), breaking on silence, sentence punctuation, or 1.9s — which is
  what the streamers' own widgets do, and it buys reading time for free.

`publishing/shorts.py::_caption_filters` had the same two bugs verbatim and now shares
`_caption_groups`.

Also: `_esc` strips `'` (it needs escaping in a filtergraph), so `he's` rendered as `HES`.
Captions now substitute U+2019 first, which passes through untouched.

**Cost:** ~6 local VLM calls per clip, ~+10 min on the nightly run. Cached per date in
`work/<date>/burned_captions.json`; unavailable Ollama = caption everything, as before.

---

## 2026-09-13 — Correcting the record on labelling and "training"

Several docs described the filter as more data-driven than the evidence supports. Checked
against the files themselves, not other docs:

* **"Trained on ~200+ labeled clips using logistic regression"** (`scoring.py`) — not
  verifiable, and overstated either way. The collector *downloaded* 200 clips/day for 8
  days; how many were then labelled is recorded nowhere, and the dataset
  (`data/training/`, gitignored) no longer exists. The owner recalls ~50, which matches
  `train_classifier.py`'s own "aim for at least 50" guidance.
* **Nothing learned is running.** `train_classifier.py` fits a logistic regression and
  prints feature weights + a suggested threshold, and writes `model.json` — which no part
  of the pipeline loads. The prefilter thresholds (0.22 / 0.52 / 0.015) are hand-set and
  unchanged since the first commit; the audio score's internal weights are hand-picked.
  Every model the pipeline actually runs (whisper, qwen3-vl, Gemini, Haar cascade) is
  pretrained and used as-is.
* **"Precision 0.25 → 1.0"** — the only surviving labels are
  `work/2026-06-09/labels.json`: **25 clips (21 bad, 3 good, 1 ok)**, of which only 9
  reached the VLM stage. Re-running `eval_filter --date 2026-06-09` with today's rules
  gives **precision 0.33 on 9 clips**. The original figure was a real result on a tiny
  set with the rules of the time; it is not a benchmark.

What those 25 labels *do* show, and why the blacklist + JP rule exist: all 4 keepers had
English titles, 19 of 21 rejects had Japanese/Chinese titles, and 14 were tagged
`pro-play` — Japanese custom/event tournaments with spectator UI, plus talk-only clips.

The keyword bypass (a title containing "penta"/"1v5"/... passes before the audio check)
has existed since the first commit; the owner remembers it being motivated by a silent,
focused pentakill getting cut, but that incident is not recorded. Known gap: the bypass
depends on the TITLE — a silent pentakill with an unrelated title is still cut by
`prefilter.audio_exclude` before any vision model sees it.

## 2026-09-23 — Review queue; top-20 countdown

**Review queue.** The filter makes ~200 decisions a night and nobody looks at the rejects,
so its quality was unknown. `pipeline/tools/review_queue.py` (run `review.bat`) samples one
clip per gate per finished night plus four random ones, shows the pipeline's path and its
stated reason, and asks two things: is that reason TRUE, and how GOOD is the clip. The
split separates detector errors ("not gameplay" on gameplay) from rule errors (correctly
"quiet", but a good clip). Replaces `label_clips.py` as the day-to-day tool because that one
played local MP4s, which are pruned after a day; this falls back to the Twitch embed, so
every night with a clips.json stays reviewable. Deliberately NOT played: the prefilter's LQ
copy — Twitch serves it as a 360x640 portrait frame (see the open judge-input bug).
Free-text notes are stored but not yet summarised by an LLM; do that once there are enough.

**Top 20.** The 17-clip videos were a side effect of the 8-minute target. New
`video.target_clips` (default 0 = old duration rule; owner overlay 20) fills with judge
fillers and trims the weakest by count, and the expansion loop now measures shortfall in
clips when it is set. Owner overlay raises `vlm_filter.max_keep` 24 -> 32 so the judge has
~30 candidates; the VLM already keeps more than 24 on most nights (the cap cut ~9/day), so
this costs Gemini calls, not GPU time. Expect ~10-11 min videos.

## 2026-09-24 — "222 clips in, 22 out"; website state file

Owner overlay: `twitch.fetch_count`/`max_fetch` 222, `video.target_clips` 22 (was 20),
`vlm_filter.max_keep` 35 (was 32, sized for 20). Purely the owner's pick for a
memorable number; defaults in config.yaml unchanged. Twitch may return a clip or two
fewer than asked (219 of 220 on 09-21). More than 20 judge calls simply walks the
judge model chain.

The project website was redesigned (dark, Dovetail-style) and `docs/SITE.md` added: a
ledger of every fact the site states with its source, plus a **Pending** list that
pipeline work appends to, so site updates don't need a re-read of the codebase. The
site still narrates the 09-21 night (17 clips) until a 222/22 night is re-exported.

## 2026-09-24 — VLM crop regions; thumbnail layout

Owner review of the site's crop examples: the banner crop (0.05–0.22) cut off the crest
above PENTAKILL!/ACE!, and the kill-feed crop (0.08–0.45) ended exactly on the 4th kill
of a quadra. Now banner [0.35, 0.0, 0.65, 0.17] and kill feed bottom 0.52 (+1/5 of its
height), checked by drawing both on real frames from the 09-21 master. Detection
semantics changed, so the cache is `vlm_partial_v4.json` (per the detection/decision
rule); old nights keep their v3 files.

Thumbnails (local provider): the owner liked the teal variant's big streamer circle
(0.95 of the height) and wanted the clip's champion shown sharp, not blurred. All three
variants now use the big circle (`thumbnail.face_scale`), and the splash is zoomed
(`splash_zoom` 1.35) and slid left (`splash_focus_x` 0.24) so the champion sits in the
open area beside the circle instead of behind it; the left text gradient is lighter
(140 vs 210). Variant 2 keeps the gameplay-frame background.

2026-09-24 (later): after one night with 0.95 on every variant, the owner preferred the
slightly smaller avatar — circle sizes are back to 0.86 / 0.82 / 0.95 per variant
(`thumbnail.face_scale` removed). The sharp, slid champion splash stays.

## 2026-09-24 — fetch returns exactly fetch_count

Asking Helix for 222 clips gave 219 (09-23) and 216 (re-fetched on 09-24): pagination
repeats clips across pages and they were de-duplicated afterwards. The fetch now asks for
`twitch.fetch_slack` (20) extra and `run()` trims to exactly `fetch_count` after dedup and
the processed filter (game mode only; broadcaster mode unchanged). Live check for 09-23:
239 unique -> 222. `dedup_by_views` is pure and tested (tests/test_fetch.py).

## 2026-09-24 — Facecam detection: whole frame + "live face" gates

Owner: Shorts showed the wrong face (or half a screen of zoomed HUD) about half the
time; thumbnails occasionally too (both use `shorts._detect_facecam`). Audit of the 52
downloaded clips of 09-22: the Haar detector was wrong on 7 (HUD champion portraits,
champion-select icons, minimap art) and missed 6 (it only searched the bottom third;
webcams also sit top-left/top-right, or are small).

Now: faces are searched in the whole frame (960-px copies of 9 frames), hits clustered
by position, and each cluster must be a LIVE face — median frame-to-frame change >= 3
(static art measured 0-2.6), skin-colour share >= 0.12 (art ~0-0.07; one dim real
webcam 0.16) — found in >= 4 of 9 frames. The most-voted survivor wins, which is what
beats a changing, skin-toned kill-feed portrait (6 frames vs the webcam's 9). The
choice is the pure `pick_facecam()` with tests built from those measured cases.
Keys: `shorts.facecam_min_live`, `shorts.facecam_min_skin`.

Tuning set (09-22): wrong picks 7 -> 0 (excluding pro broadcasts), misses 6 -> 7.
Held-out (09-23, 52 clips, not tuned on): wrong picks 9 -> 2 (old: minimap x3, anime/
VTuber art, a plushie, UI; new: a drawn overlay character — which the old one also picked
— and a "game terminated" menu screen); misses ~7 -> ~7 (different ones). A miss falls
back to the centred layout, which is safe; a wrong pick was the visible failure.
Considered and not done: OpenCV's YuNet DNN detector (better recall on small/turned
faces) — needs a ~230 KB model download; the owner declined adding a download for now.

## 2026-09-24 — Slight zoom for no-webcam Shorts

Owner: when no face is found, the Short showed the whole 16:9 frame small in the middle.
`shorts.center_zoom` (default 1.0 = unchanged) scales the centred gameplay up and crops
its sides; the owner overlay uses 1.3 (compared 1.0 / 1.3 / 1.45 on a real clip — 1.3
mostly trims minimap/HUD edges, 1.45 started cutting the fight). Now that facecam misses
fall back to this layout (see the facecam entry above), it is the common no-face look.

## 2026-09-24 — Shorts: no double captions

The long-form video skipped our captions when the streamer's own English live captions
are burned in (2026-09-09), but Shorts never asked — on 09-23, 2 of 3 Shorts showed both
("OH" over the streamer's "clip that? Oh"). Shorts now call the same
`enrichment/burned_captions.detect` (cached per clip, already filled by assemble) and drop
their caption words when the source has captions AND whisper hears English; foreign
speech keeps ours as the translation. The transcript text still feeds the Short's title.
Checked by re-rendering the 09-23 Shorts with upload off (then restoring the folder).

## 2026-09-24 — Judge watches 720p again (was portrait-360)

Found 2026-09-13, fixed now: api_judge reused the prefilter's LQ copy whenever it was
under `max_mb` — always. Twitch serves `portrait-*` renditions and yt-dlp "worst" picks
portrait-360 (360x640 frame, the landscape picture shrunk inside), so since at least
2026-06-15 Gemini graded clips at ~360x200 effective. The LQ copy is now reused only if
it is landscape and >= `shrink_height` (`lq_is_judge_quality`, pure half tested);
otherwise the full-quality file is shrunk to 720p (checked: 1280x720, 4.7 MB, ~7 s per
clip, ~4 min per night). Input changed -> judge cache bumped to `api_partial_v4.json`
(exporter reads v4, falls back to v3). Judge scores may shift; watch the next nights.

## 2026-09-25 — Clip log (every published clip, forever)

Owner: keep a record of every clip that went into a video — link, title, everything the
filters found — so compilation Shorts (top 5 pentakills of the week, a streamer's best of
the month) can be built from past days. New stage `publishing/clip_log.py` after shorts,
before cleanup → `data/clip_log.jsonl`. Backfilled from the saved work folders: 978 clips
over 67 dates, 3.5 MB; e.g. 18 pentakills in the last 7 days. Gap: cleanup deletes
`work/<date>/shorts/` (with the Short ids) the same night, so only 09-24's 3 Short ids
survived — from now on the log records them before cleanup, and a rebuild carries over
fields it can no longer see. Past Short ids are recoverable from the YouTube API if needed.

## 2026-09-25 — Titles: "<hook>... LoL Daily Clips #N" (built, not switched on)

Owner looked at the channels ranking for "lol moments": they title the SPECIFIC best clip
then a constant series + episode ("AFK Bait That Always Works...LoL Daily Moments Ep
3482"); nobody shows the clip count. Picked "LoL Daily Clips" (not a competitor's series
name). `upload.title_mode: hook` asks the `commentary` role once per date for a 3-7 word
hook + 1-3 word thumbnail text from the top-3 judge descriptions; cached in
title_hook.json; yesterday's title/thumb passed in so days don't repeat. First prompt gave
"X Secures A Pentakill" + "PENTAKILL" every day — the prompt now bans flat verbs and asks
for the day's best STORY. Seen live: the model sometimes copies the series into the hook
("... LoL Daily Clips 29") — hook_title cuts it. Default stays `styles`; the owner overlay
switches with the new thumbnails once the facecam work in thumbnail.py lands.
Thumbnail direction chosen the same day (not built yet): style A = sharp best-moment
gameplay + webcam picture-in-picture over the original cam corner, torero red #E10600
border 10-15 px, 1-3 word hook top-left (yellow/black, white/red or white/black edge;
Bahnschrift Bold Condensed or Arial Black — final pick pending). The auto-aimed red arrow
was dropped: qwen3-vl boxed the wrong thing on 8/9 frames when asked for the player's
champion.


## 2026-09-25 — Streamer location by the local vision model (enrichment/streamer_cam)

The owner asked whether the local VLM could replace the Haar face detector for Shorts
and thumbnails. Qwen3-VL is trained for grounding, so it answers the real question
("where is the streamer's overlay?") and returns the whole webcam rectangle instead of
face + padding. Owner decision: VTuber models and simple animated 2D avatars COUNT as
the streamer (they are the player's presence and they move).

Design: ask for the box on 3 frames, keep it when 2 agree (IoU >= 0.5); then ask about
the crop alone "streamer, or game/UI/static art?". If the model finds nothing, the Haar
candidate is used only when the model confirms its crop. VLM role unreachable -> Haar.
No `schema=` on these calls: with images, Ollama's constrained decoding often returned
empty from qwen3-vl and the provider re-asked, doubling every call.

Measured on all 104 downloaded clips of 09-23 + 09-24 (Haar vs VLM, reviewed by eye):
the VLM found ~17 webcams Haar missed (small, dark, VTuber) and never boxed game art;
Haar-only picks it missed (09-23 #11, #16; 09-24 #24, #35) are recovered by the
confirmed fallback, which also rejected Haar's menu-screen pick. Remaining: one wrong box
on a non-gameplay screen (09-23 #29), and a 2D drawn avatar (09-23 #27) whose crop the
model calls "not the streamer" despite the prompt. ~20-30 s per clip, only for the
Shorts/thumbnail clips (a few minutes per night). `shorts.facecam_method` (default haar;
owner overlay vlm), `facecam_vlm_frames`.

## 2026-09-25 — Thumbnail style A: final spec (to build once the facecam work lands)

Owner-approved after five rounds of mockups on the 09-24 clips. Prototype code (scratch,
not wired in): `docs/prototypes/` — thumb_style_a.py renders it, cam_snap.py snaps boxes.
- Background: sharp frame near the judge's best moment (of 5 frames at ±1.5 s, the most
  colourful/active one), zoomed 1.25x toward the action (brightest-saturation area with
  HUD, minimap and webcam masked), colour/contrast pop + unsharp mask.
- Text: the 1-3 word thumb text (title_hook.json), Arial Black, yellow #FFE600 with a
  black edge, top-left, size CAPPED at what "QUADRA KILL" gets at 800 px (102 pt) — short
  words must not blow up to half the frame.
- Border: torero red #E10600, 15 px. (10 px also fine; 30 px and glow rejected.)
- Webcam: picture-in-picture, bottom corner on the webcam's own side, 40 px in from the
  border, turquoise #40E0D0 7 px frame. Wherever the ORIGINAL webcam lands in the zoomed
  frame is heavily blurred first, so the face never appears twice (owner: keep that).
- No webcam: same without the panel. Red arrow dropped (VLM can't find the player's own
  champion: wrong on 8/9 frames; the own health bar is yellow, not green, and segmented).
- Webcam box snapping (cam_snap.py): the locator's box is rough (a few % off on every
  side). An overlay's outline is a straight line in the same place in every frame while
  the game moves, so per side: strongest line within ±18% of the rough edge, scored as
  the share of the side's length with a >18-grey step, median/min over 6 frames. Accept
  >= 0.15 (dark webcam on dark game measured 0.20-0.40 — real); below that (~0.09) the
  webcam runs off-screen if the frame border is near, else keep the rough edge. Verified
  on 4/4 webcams of 09-24; a sponsor banner glued under a webcam is included (owner: fine).
  Belongs in enrichment/streamer_cam.py so Shorts get it too; no rectangle (green screen,
  VTuber) = nothing snaps = rough box kept.

## 2026-09-25 — Style A built: `thumbnail.provider: clip`

The spec above is now `compose_clip` + `generate_clip` in production/thumbnail.py (pure
geometry tested in tests/test_thumbnail_clip.py). The webcam box comes from
`streamer_cam.find()` — the Shorts session is moving cam_snap's edge snapping into that
module, so the thumbnail does NOT snap again. Default provider stays `local`; the owner
overlay switches to `clip` together with `upload.title_mode: hook` (the thumbnail's words
come from the same title_hook.json).


## 2026-09-25 — Streamer cam: duo streams + edge snapping

Owner review of the 104-clip sheets: the VLM boxes are better on average, but (a) duo
streams (two webcams — e.g. Dantes, #22 in the 09-24 video) got one webcam or a box
straddling both, and (b) boxes are a few % off the real overlay (cut one side, include
gameplay on another). Now: the locate prompt asks for EVERY streamer overlay; boxes that
2 of 3 frames agree on are kept, up to `shorts.facecam_max` (2); each is snapped to the
overlay's outline with the thumbnail session's cam_snap method (strongest persistent
straight line per side, accept >= 0.15, else frame border if near, else rough edge —
moved from docs/prototypes into streamer_cam.snap, tested on synthetic frames). Duo
Shorts put the streamers side by side in the lower panel. `find()` (largest box) is what
the thumbnail uses; `find_all()` the Shorts.
Checked on the owner's named clips: both webcams found and snapped on 09-23 #11, #16 and
09-24 #24; 09-24 #26 now covers the whole webcam; the dark webcam (#41) and the menu
screen ("none") unchanged. Still missed: the 2D avatar 09-23 #27 (its crop fails the
"is this the streamer" check). ~25-60 s per clip (up to ~3 min when the Haar fallback
runs too); only for the Shorts/thumbnail clips.
Coordination: the thumbnail session and this one agreed via SendMessage who edits which
file (this one: streamer_cam.py, shorts.py; that one: thumbnail.py, prototypes).
