# Contributing

Thanks for looking. This is a personal project that runs a real channel, so it has some
opinions — worth knowing them before you spend time on a change.

## Getting set up

```bash
pip install -e ".[dev]"
pytest -q
ruff check pipeline tests
```

Both must pass, and neither needs a GPU, an API key, or FFmpeg. If a test you're adding
needs any of those, it's testing the wrong layer — see below.

`python -m pipeline.doctor` tells you what your machine is missing for an actual run.

## What tests cover

The decision rules and config resolution — the parts where a regression silently changes
what gets published. `decide()`, `decide_api()`, `local_judge()`, role resolution,
overlay merging, and encoder argument construction are all pure functions with no I/O,
which is what makes them testable at all.

Model calls, FFmpeg invocations, and network access are deliberately **not** covered.
Mocking them thoroughly enough to be meaningful costs more than it's worth on a project
this size; the visual audit at `data/work/<date>/report.html` is the real check on
end-to-end behaviour.

If you change a filter threshold or rule, add or update a test that pins the behaviour
you intended. Those tests double as documentation of *why* a rule exists — several
encode findings from hand-labelling sessions that aren't obvious from the code.

## Things that will get a PR pushed back

Not because they're bad ideas generally — because they'd undo a deliberate decision:

- **Making the `vlm` role use a paid API.** It sees ~10x more clips than the judge.
  Keeping it local is what lets the pipeline run for free.
- **Adding an orchestration framework.** Stages are modules with
  `run(cfg, state, date_label)` and `run_daily.py` is an ordered list. That's the whole
  design.
- **moviepy / OpenCV for rendering.** All rendering is FFmpeg via subprocess. OpenCV is
  allowed in prefilter scoring and facecam detection only.
- **Removing the per-streamer credits or chapters.** They're a legal and monetization
  requirement, not decoration.
- **Defaulting `upload.enabled` or `privacy` to anything public.** A fresh clone must
  never publish to someone's channel.
- **Hardcoding a model ID outside `llm.roles`.** Every model ID lives in one place so a
  retirement is a one-line fix.
- **Broad style refactors.** The lint config in `pyproject.toml` is scoped on purpose;
  restyling working render code is churn.

## Cache versioning

Changing **detection** semantics — a prompt, a crop region, what a model is asked —
means bumping the cache filename (`vlm_partial_v3.json` → `v4`, `api_partial_v2` → `v3`).
Changing only **decision** rules never does; those recompute from cache every run.

Getting this backwards mixes old and new model outputs and produces decisions that
match neither version. It's the easiest way to break this codebase subtly.

## Adding a model provider

Subclass `Provider` in `pipeline/providers/`, declare capabilities
(`supports_images` / `supports_video` / `supports_image_generation`), implement
`_complete`, and register it in `providers/__init__.py:build()`. Implement `health()`
too — it's what `doctor` reports.

Map the `schema` argument onto whatever native structured-output mechanism the backend
has rather than relying on the brace-scraping fallback in `base.parse_json()`.

## Style

Match the file you're editing. Comments explain *why*, not *what* — the codebase leans
on module docstrings being accurate and current, so update them when behaviour changes.

Line length is 100. Run `ruff check --fix` before pushing.

## Reporting problems

Include the output of `python -m pipeline.doctor` and the relevant chunk of
`data/logs/`. If it's a filtering complaint ("this clip should/shouldn't have been
kept"), `data/work/<date>/report.html` shows the decision and the reason — that's the
useful artifact.

Don't paste API keys. `doctor` doesn't print them, but logs occasionally include URLs.
