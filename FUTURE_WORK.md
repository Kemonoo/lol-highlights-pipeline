# Future Work

Ideas and improvements to explore. Ordered roughly by expected impact.

---

## High impact

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

### Thumbnail generation
Auto-generate a thumbnail from the best frame of the top-ranked clip + text overlay.
- FFmpeg can extract the frame at `api_best_moment_s`
- Text: streamer name, clip count, hook phrase from credits.py
- A/B test: generate 2 title variants per day, pick the one with higher CTR after 24 h

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
Upgrade path: YOLOv8-face (ultralytics, free) — runs on CUDA, more accurate, same
frame-sampling approach. The `_detect_facecam` function is the only place to change.

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
