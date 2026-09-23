"""Progress reporting — derived purely from the artifacts a run leaves on disk.

These build a fake work directory rather than running anything, which is the point:
the reporter must never need the pipeline to be instrumented or running.
"""
import json

import pytest

from pipeline.progress import DONE, PENDING, RUNNING, SKIPPED, collect, render


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "output").mkdir()
    return {
        "paths": {"data_abs": str(tmp_path)},
        "twitch": {"timezone": "UTC"},
        "api_judge": {"enabled": True}, "transcribe": {"enabled": True},
        "commentary": {"enabled": False}, "tts": {"enabled": False},
        "thumbnail": {"enabled": True}, "upload": {"enabled": False},
        "shorts": {"enabled": True, "count": 3},
        "vlm_filter": {"max_keep": 18}, "video": {},
    }


def write(cfg, rel, payload):
    from pathlib import Path
    p = Path(cfg["paths"]["data_abs"]) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def states(cfg, date="2026-07-27"):
    rows, _, _ = collect(cfg, date)
    return {r["name"]: r["state"] for r in rows}


def test_nothing_started_is_all_pending(cfg):
    s = states(cfg)
    assert s["fetch"] == PENDING
    assert s["vlm_filter"] == PENDING


def test_disabled_stages_report_skipped_not_pending(cfg):
    """A stage that's off in config must not look like work still to come."""
    s = states(cfg)
    assert s["commentary"] == SKIPPED
    assert s["tts"] == SKIPPED
    assert s["upload"] == SKIPPED


def test_partial_cache_means_running(cfg):
    write(cfg, "raw/2026-07-27/clips.json", {"clips": [{}] * 219})
    write(cfg, "work/2026-07-27/prefiltered.json", {"clips": [{}] * 40})
    write(cfg, "work/2026-07-27/vlm_partial_v4.json", {f"c{i}": {} for i in range(12)})
    s = states(cfg)
    assert s["fetch"] == DONE
    assert s["prefilter"] == DONE
    assert s["vlm_filter"] == RUNNING


def test_output_file_marks_the_stage_done(cfg):
    write(cfg, "work/2026-07-27/vlm_partial_v4.json", {"c": {}})
    write(cfg, "work/2026-07-27/vlm_scored.json", {"clips": []})
    assert states(cfg)["vlm_filter"] == DONE


def test_progress_totals_come_from_the_previous_stage(cfg):
    write(cfg, "work/2026-07-27/prefiltered.json", {"clips": [{}] * 40})
    write(cfg, "work/2026-07-27/vlm_partial_v4.json", {f"c{i}": {} for i in range(12)})
    rows, _, _ = collect(cfg, "2026-07-27")
    vlm = next(r for r in rows if r["name"] == "vlm_filter")
    assert (vlm["have"], vlm["total"]) == (12, 40)


def test_a_half_written_cache_does_not_crash_the_reporter(cfg):
    """Caches are flushed after every clip, so a read can land mid-write."""
    from pathlib import Path
    p = Path(cfg["paths"]["data_abs"]) / "work/2026-07-27/vlm_partial_v4.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"c1": {"a": 1}, "c2":', encoding="utf-8")     # truncated JSON
    assert states(cfg)["vlm_filter"] == PENDING                   # counts 0, no raise


def test_nul_padded_cache_is_tolerated(cfg):
    """work/ files can carry NUL padding from a filesystem-sync quirk."""
    from pathlib import Path
    p = Path(cfg["paths"]["data_abs"]) / "work/2026-07-27/vlm_partial_v4.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"c1": {}, "c2": {}}) + "\x00\x00", encoding="utf-8")
    rows, _, _ = collect(cfg, "2026-07-27")
    assert next(r for r in rows if r["name"] == "vlm_filter")["have"] == 2


# ── the manifest must beat the watcher's config ───────────────────────────────

def test_manifest_overrides_config_for_stage_enablement(cfg):
    """The bug this prevents: a run launched with an overlay that enables uploading,
    watched from a terminal that didn't pass that overlay, reported 'upload disabled'
    while the upload was about to happen."""
    assert states(cfg)["upload"] == SKIPPED          # config says off
    write(cfg, "work/2026-07-27/run.json",
          {"stages": {"upload": True}, "upload_privacy": "public"})
    assert states(cfg)["upload"] == PENDING          # the run says on


def test_manifest_can_also_disable_a_stage_config_enables(cfg):
    assert states(cfg)["transcribe"] == PENDING
    write(cfg, "work/2026-07-27/run.json", {"stages": {"transcribe": False}})
    assert states(cfg)["transcribe"] == SKIPPED


def test_upload_note_states_the_privacy_the_run_will_use(cfg):
    write(cfg, "work/2026-07-27/run.json",
          {"stages": {"upload": True}, "upload_privacy": "public"})
    rows, _, _ = collect(cfg, "2026-07-27")
    assert "public" in next(r for r in rows if r["name"] == "upload")["note"]


def test_config_is_still_used_when_no_run_has_happened(cfg):
    """A date that never ran has no manifest; falling back keeps --date useful."""
    assert states(cfg, "2030-01-01")["commentary"] == SKIPPED


def test_corrupt_manifest_falls_back_instead_of_crashing(cfg):
    from pathlib import Path
    p = Path(cfg["paths"]["data_abs"]) / "work/2026-07-27/run.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert states(cfg)["upload"] == SKIPPED           # config default, no exception


# ── upload completion lives in state.json, not an output file ─────────────────

def test_upload_is_done_once_state_records_a_youtube_id(cfg):
    """upload writes no output file, so without state.json it would sit at PENDING
    forever - and `--watch` would never print 'Run complete'."""
    write(cfg, "work/2026-07-27/run.json", {"stages": {"upload": True}})
    assert states(cfg)["upload"] == PENDING
    write(cfg, "state.json",
          {"videos": [{"date": "2026-07-27", "youtube_id": "abc123"}]})
    rows, _, _ = collect(cfg, "2026-07-27")
    up = next(r for r in rows if r["name"] == "upload")
    assert up["state"] == DONE and "abc123" in up["note"]


def test_upload_ignores_a_youtube_id_from_another_date(cfg):
    write(cfg, "work/2026-07-27/run.json", {"stages": {"upload": True}})
    write(cfg, "state.json",
          {"videos": [{"date": "2026-07-26", "youtube_id": "old999"}]})
    assert states(cfg)["upload"] == PENDING


def test_a_rendered_but_unuploaded_date_is_not_marked_done(cfg):
    """add_video() also records renders with no youtube_id - those aren't uploads."""
    write(cfg, "work/2026-07-27/run.json", {"stages": {"upload": True}})
    write(cfg, "state.json", {"videos": [{"date": "2026-07-27", "clips": 11}]})
    assert states(cfg)["upload"] == PENDING


def test_render_produces_a_bar_and_survives_empty_state(cfg):
    write(cfg, "work/2026-07-27/prefiltered.json", {"clips": [{}] * 40})
    write(cfg, "work/2026-07-27/vlm_partial_v4.json", {f"c{i}": {} for i in range(20)})
    rows, started, last = collect(cfg, "2026-07-27")
    text = render(rows, "2026-07-27", started, last, color=False)
    assert "20/40" in text and "vlm_filter" in text

    rows, started, last = collect(cfg, "1999-01-01")
    assert "1999-01-01" in render(rows, "1999-01-01", started, last, color=False)


# ── the transcribe done-marker ────────────────────────────────

def test_partial_transcripts_report_running_not_done(cfg):
    """transcripts.json is flushed per clip, so it exists as soon as the FIRST clip
    lands. Treating it as the stage output made a mid-stage crash look complete: the
    retry skipped the rest and the video shipped with captions on only some clips
    (all 7 August GPU-crash days, worst case 2 of 9). Only the explicit done-marker,
    written after every clip has been attempted, counts as done."""
    write(cfg, "work/2026-07-27/transcripts.json", {"clip_a": {"text": "hi"}})
    assert states(cfg)["transcribe"] == RUNNING

    write(cfg, "work/2026-07-27/transcripts.done.json", {"clips": 1})
    assert states(cfg)["transcribe"] == DONE
