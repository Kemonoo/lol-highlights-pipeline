# Architecture

Two ideas carry most of the weight here: a **cost cascade** that keeps the expensive
model away from 90% of the input, and a **detection/decision split** that makes the
filter's rules free to change and possible to test.

---

## The cost cascade

Roughly 220 clips are fetched per day. Around 20 make the video. If every clip went to a
video-capable LLM the run would take hours, and on a paid backend it would stop being
free. Instead each layer is cheaper than the one after it, and only survivors move up:

```
  220 clips   fetch         metadata only, no downloads yet
      |
      v
  ~40 clips   prefilter     free · local · CPU
      |                     title keywords, audio hype, motion, language, blacklist
      v
  ~12 clips   vlm_filter    free · local · GPU
      |                     is this gameplay? is it esports? are there kills?
      v
   ~8 clips   api_judge     optional · API or local fallback
      |                     watches the whole clip with audio; grades the play
      v
   final selection, ordered worst -> best as a countdown
```

Two properties follow, and both are worth protecting:

- **The `vlm` role must stay local.** It sees ~10x more clips than the judge. Pointing
  it at a paid API is what would make the pipeline expensive to run.
- **Downloads are lazy.** `fetch` stores metadata; `prefilter` pulls low-quality copies
  for scoring; only `vlm_filter` survivors get full-quality downloads. Bandwidth follows
  the same cascade as compute.

## Detection and decision are separate

Every model call is cached per clip. The keep/reject rules are **pure functions
recomputed from that cache on every run**:

```
vlm_filter.detect()   → model calls → vlm_partial_v4.json   (expensive, cached)
vlm_filter.decide()   → pure code, reads the cache          (free, re-runs every time)
```

Three consequences:

1. **Retuning the filter is free.** Change a threshold, re-run, get new decisions
   without a single GPU cycle or API call.
2. **The rules are unit-testable.** `tests/test_filter_decisions.py` covers them with no
   network, no GPU, and no FFmpeg — which is why the whole suite runs in CI.
3. **Cache versioning is load-bearing.** Changing *detection* semantics (prompts, crop
   regions) means bumping the cache filename (`vlm_partial_v4` → `v4`). Changing only
   *decision* rules never does. Getting this backwards silently mixes old and new model
   outputs.

## Decomposed questions for small models

A 4B local vision model asked "is this a good highlight?" produces confident noise. The
same model asked "how many kill banners are in this crop?" is reliable. So the local
layer never makes judgement calls — it answers narrow perceptual questions, one image at
a time, against calibrated crop regions (kill feed, multikill banner, event log). Taste
is deferred entirely to the video judge.

This is also why the local prompts are single-question. Small models fail at multi-part
prompts in a way that looks like success: they answer the first part and confabulate the
rest.

## Provider abstraction

Stages ask for a **role**, never a vendor:

```python
provider = get_provider(cfg, "judge")
verdict  = provider.complete_json(PROMPT, video=clip_bytes, schema=SCHEMA)
```

`llm.roles.<role>` in `config.yaml` decides who serves it. Five roles — `judge`, `vlm`,
`commentary`, `feedback`, `thumbnail_image` — and four backends: `gemini`, `ollama`,
`anthropic`, and `openai` (any OpenAI-compatible endpoint).

Capabilities are declared, not assumed. `supports_video`, `supports_images`, and
`supports_image_generation` are checked up front via `provider.require(...)`, so pointing
the `judge` role at a text-only model produces a clear "this provider cannot ingest
video" at startup rather than a confusing failure per clip.

Structured output is native everywhere: Gemini's `responseSchema`, Ollama's `format`,
OpenAI's `json_schema`, Anthropic's `output_config.format`. That matters most for the
small local models, which otherwise wrap JSON in prose often enough to skew the filter.

## Graceful degradation

The pipeline is designed to finish with **no API keys at all** — just less well curated.
This is deliberate and tested, not emergent:

| Unavailable | Fallback | Where |
|---|---|---|
| Video judge (no key / quota dead) | `local_judge()` scores from kills, audio, motion | `filtering/api_judge.py` |
| Commentary provider mid-run | Sticky switch to the local fallback after 2 consecutive failures | `production/commentary.py` |
| Image generation | Free PIL thumbnail design | `production/thumbnail.py` |
| CUDA | CPU transcription and encoding | `hardware.py` |
| Hardware encoder | libx264 | `hardware.py` |

The one thing with no fallback is FFmpeg. The quota-dead path is deliberately *strict*
rather than fail-open: a loud clip with no motion is someone talking, and
`local_judge()` drops it — otherwise a dead-quota day publishes twenty minutes of
chatting.

## Stage contract

Every stage is a module exposing `run(cfg, state, date_label)`. `run_daily.py` holds the
ordered list and nothing else — there is no orchestration framework, and adding one
would be a regression. A stage is "done" if its output file exists; `--force` redoes it,
`--only` runs one, `--stop-after` truncates the chain.

Slow stages additionally cache per clip, and **failures are never cached** — a clip that
errored is retried next run rather than being permanently marked bad. `KeyboardInterrupt`
anywhere saves progress.

## Data flow

```
data/raw/<date>/     clips.json + downloaded mp4s (+ lq/ low-quality scoring copies)
data/work/<date>/    prefiltered.json -> vlm_scored/vlm_filtered.json -> api_scored.json
                     -> transcripts.json -> commentary.json -> vo/ -> segments/
                     caches: vlm_partial_v4.json, api_partial_v3.json
                     report.html  <- the visual audit of every decision
data/output/         <date>.mp4 + <date>.meta.json
```

## Things that look wrong but aren't

- **`.rstrip("\x00")` on JSON reads.** Work files can carry NUL padding from a
  filesystem-sync quirk. New readers of `work/` files should keep it.
- **PIL + FFmpeg instead of a browser for overlays.** Nameplates and thumbnails are
  composited with Pillow and packed to transparent `.mov`, rather than rendering HTML in
  Playwright. Chosen deliberately: no browser dependency in an unattended daily run.
- **Thumbnail prompts never name the game or a champion.** Naming IP or feeding
  copyrighted art trips the image model's recitation filter and returns no image. The
  model only enhances a real facecam into a reaction on a green screen; everything else
  is composited locally.
- **Japanese-title clips can't pass on audio hype alone.** An empirical finding from
  hand-labelling: they're usually talk-context. Kana in the title triggers it; CJK
  ideographs alone don't, since those may be Chinese.
- **`opencv` is imported but never used for rendering.** It's confined to prefilter
  motion scoring and facecam detection. All rendering is FFmpeg via subprocess.

## Extending it to another game

Honest answer: `twitch.game_id` is configurable, but the prompts, the kill-feed crop
regions, and the thumbnail's champion lookup are all League-specific. Changing the game
ID alone gets you a pipeline that fetches the right clips and then judges them with the
wrong questions.

A real port means: rewriting the prompts in `filtering/vlm_filter.py` and
`filtering/kill_detect.py`, recalibrating `vlm_filter.regions` against a real screenshot
(the `crops/` output exists for exactly this), and replacing the Data Dragon lookups in
`production/thumbnail.py` and `production/brand.py`. The stage structure, provider layer,
and rendering carry over unchanged.
