# Future Work

Ideas and improvements to explore. Ordered roughly by expected impact.

---

## High impact

### Funny outro (shelved 2026-06-19 — findings)
Goal: end each video on genuinely funny streamer clips (Synapse-style: singing, bits,
absurd moments, funny fails), drawn from a reused pool of evergreen favourites.
Prototyped in `pipeline/tools/find_funny.py` (since removed). What we learned:
- **Detection works**: Gemini watching the clip reliably *rates* funniness with good
  reasons (caught a 9/10 scatting/singing bit; correctly gave a "calmly takes a drink"
  clip 0/10).
- **But it conflated hype with comedy** — pentakill screams / drake shouts scored high.
  The prompt rewarded "shock/rage/laugh" (loud gameplay reactions), not actual jokes.
- **Window localization failed**: Gemini's video timestamps were mostly degenerate
  (`0.2-0.2s`). It knows *what's* funny, not *when*.
- **Wrong candidate pool**: it scanned the play-prefiltered set, which is selected for
  gameplay — pure-comedy clips are filtered out before it looks.

Proper approach when revisited:
1. Separate funny-candidate prefilter: scan a BROADER fetch, score on funny clip TITLES
   (keywords / 😂💀🤣 emoji / joke phrasing), not on kills/motion.
2. Comedy-specific prompt that distinguishes genuine comedy from loud hype.
3. Window: don't trust Gemini timestamps — use the whole short clip, or an audio-energy
   peak, to bound the funny moment.
4. Rolling vetted pool (keep funny≥8) + dedupe so a clip isn't reused within N days.
5. Crop/zoom the facecam for the outro montage so the reaction is the focus.

### Match linker (Phase 2 — planned)
Connect each clip to the actual Riot match being played at that timestamp.
`pipeline/enrichment/match_linker.py` has the full implementation plan in its docstring.
- Config: `match_linker.summoner_map` (twitch login → "GameName#TAG")
- Riot match-v5 API, free dev key (100 req/2 min, enough for daily use)
- Unlocks: champion names in commentary, real KDA, rank tier, multikill counts

### HUD OCR event extractor (Phase 2 — planned)
Read kill feed, scoreboard, multikill banners from sampled frames.
`pipeline/enrichment/hud_ocr.py` has the implementation plan.
- Enables outplay detection as a rule (not a vibe) even without match data
- Could eventually replace or supplement the audio-hype prefilter

### Fail-clip detection
The judge underrates "streamer gets outplayed" moments (e.g. "Top diff!!!"-style clips
that were labeled good but scored ent=2). These are often funny/relatable.
- Known gap documented in CLAUDE.md; adding a dedicated signal is the fix
- Possible approach: fine-tune the judge prompt to explicitly reward fail comedy;
  or add a separate "outplay received" binary from kill-feed (victim = streamer)

### Grounding check for commentary
Reject commentary sentences that contain claims not present in the judge's description
or HUD OCR facts. Prevents hallucinated champion names / kill counts slipping through.
- Simple approach: LLM-as-judge ("does this sentence introduce any fact not in: {facts}?")
- Harder approach: extract entities from commentary, cross-check vs fact set

---

## Medium impact

### Local face-preserving AI thumbnail (researched 2026-08-23, not built)
The `gemini` thumbnail provider needs a BILLED Gemini API key — image-generation models
get **zero free-tier quota** (verified live: `limit: 0` on
`generate_content_free_tier_requests` for `gemini-3.1-flash-image`), unlike the Gemini
chat app's image gen, which is free to consumers because it isn't the metered developer
API. Owner does not want to pay, so `thumbnail.provider` is back to `local` (see DEVLOG
2026-08-23).
Researched whether a local model on the 8GB RTX 3050 could replace it: SDXL +
IP-Adapter-FaceID(-PlusV2) or InstantID can take a real facecam crop and re-render it as
a stylized reaction while keeping the person's likeness, and both are light enough to run
on 8GB VRAM. Not built because the cost is real, not the VRAM:
- New heavy stack (torch+diffusers+insightface) the project has deliberately avoided —
  contradicts the "ffmpeg + local Ollama, no browser/GPU-framework sprawl" design.
- Multi-GB one-time downloads (SDXL base ~6.5GB + adapter/InstantID weights) on a machine
  that also runs NVENC encode + Ollama VLM + Whisper in the same nightly window — VRAM
  contention with a 8GB card is a real risk, not just runtime.
- Identity-preservation quality from these adapters is inconsistent; an unattended job
  can't eyeball whether a given night's face still looks like the streamer.
If ever revisited: prototype offline first against a handful of saved facecam crops,
measure identity fidelity before wiring it into `production/thumbnail.py`.

### Music detection / ducking
Detect copyrighted music segments in the raw clip audio (streamer's Spotify playing)
and duck or mute them to avoid Content ID claims on the assembled video.
- Tool: `essentia` or `dejavu` for audio fingerprinting; or send short segments to
  the ACRCloud / AudD API (free tier generous enough for daily use)

### Permission manager
Track which broadcasters have approved use of their clips. Required for any public
channel once it gains visibility.
- State: `state.json` already has a `permissions` field structure
- Config: `permissions.require_permission: true` restricts the pipeline to approved list
- Workflow: email/DM outreach → mark approved in state → run with flag enabled

### Face-cam detection: ML upgrade
Current Haar cascade + profile trick works for most streamers but struggles with:
- Very dark cameras (CLAHE helps but isn't perfect)
- Unusual angles / virtual cameras
- Painted/stylized art false-positiving as a face — hit production 2026-08-22: the
  League HUD's champion portrait icon (bottom-center) matched the frontal cascade and
  got blown up into the thumbnail as if it were the streamer's webcam. Patched with a
  bottom-center exclusion band (real facecams sit in a corner, never dead-center-bottom
  where the HUD lives) — a heuristic, not a fix for the underlying class of bug.
Upgrade path: YOLOv8-face (ultralytics, free) — runs on CUDA, more accurate, same
frame-sampling approach, and wouldn't need HUD-region carve-outs at all. The
`_detect_facecam` function (`pipeline/publishing/shorts.py`) is the only place to change.

---

## Lower impact / quality of life

### Whisper model size option
Currently uses `whisper small` for speech captions in Shorts. The `medium` model is
noticeably more accurate for accented English and non-English streams.
Add `shorts.whisper_model: small` to config so it's tunable without code changes.

### Commentary style expansion
Current styles: `hype_caster`, `analyst`, `chill`, `gen_z_short`.
Ideas: `storyteller` (narrative arc across the video), `analyst_kr` (Korean esports
broadcast style), `roast` (self-aware, ironic about the meta).

### Per-clip replay calibration
`replay_enabled` is off because `api_best_moment_s` sometimes points after the play.
Potential fix: use HUD OCR timestamps for kill events instead of the judge's estimate,
or add a manual override field to `vlm_filtered.json`.

### Multi-region support
Currently fetches global top clips (game_id=21779). Adding a region filter (e.g.
Korean server highlights only) would let the channel specialize. Twitch Helix has no
server-region filter, but the broadcaster-list mode (mode: broadcasters) can target
a curated list of KR/EU/NA challengers.

### Shorts: clip selection strategy
Currently picks top N clips by `api_rank_score`. Alternative: pick the clips with the
most dramatic face-cam reactions (detected via Whisper energy or frame motion during
the key moment) — likely higher engagement on Shorts.

### Analytics feedback loop
Read YouTube Studio metrics (views, CTR, watch time) via the YouTube Analytics API
and feed them back into clip selection weights. High-CTR clip types → prefer similar
in future runs.
