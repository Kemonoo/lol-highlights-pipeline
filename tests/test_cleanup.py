"""Output pruning — the only stage that deletes something irreplaceable.

data/output/<date>.mp4 is the finished video. Once uploaded, YouTube holds the
canonical copy and the local master is ~550-850 MB of duplicate; left alone it grows
about 200 GB/year. But on a machine with uploading disabled that file is the ONLY
copy, so the deletion predicate has to be conservative in a way the other cleanup
paths (raw clips, scratch renders — all reproducible) do not.
"""
import json
from datetime import datetime

from pipeline.publishing.cleanup import _prune_outputs


class FakeState:
    """Only the bit _prune_outputs depends on: proof a date reached YouTube."""

    def __init__(self, uploaded: dict):
        self._u = uploaded

    def uploaded_id(self, date_label: str):
        return self._u.get(date_label)


def _out(tmp_path, dates, size=1024):
    d = tmp_path / "output"
    d.mkdir(parents=True, exist_ok=True)
    for date in dates:
        (d / f"{date}.mp4").write_bytes(b"x" * size)
        (d / f"{date}.meta.json").write_text(json.dumps({"title": date}), encoding="utf-8")
    return d


def _run(tmp_path, dates, uploaded, keep_days, today="2026-09-02"):
    out = _out(tmp_path, dates)
    freed = _prune_outputs(tmp_path, {"keep_output_days": keep_days},
                           datetime.strptime(today, "%Y-%m-%d"), FakeState(uploaded))
    return out, freed


def test_published_masters_older_than_the_window_are_removed(tmp_path):
    out, freed = _run(tmp_path, ["2026-08-01"], {"2026-08-01": "yt1"}, keep_days=7)
    assert not (out / "2026-08-01.mp4").exists()
    assert freed > 0


def test_an_unpublished_master_is_never_removed(tmp_path):
    """The safety property. A run that failed to upload, or a machine with
    upload.enabled false, holds the only copy of that video — age is irrelevant."""
    out, freed = _run(tmp_path, ["2026-08-01"], {}, keep_days=7)
    assert (out / "2026-08-01.mp4").exists()
    assert freed == 0


def test_recent_masters_are_kept_even_when_published(tmp_path):
    out, _ = _run(tmp_path, ["2026-09-01"], {"2026-09-01": "yt1"}, keep_days=7)
    assert (out / "2026-09-01.mp4").exists()


def test_metadata_is_never_pruned(tmp_path):
    """meta.json records the per-streamer credits, which is a legal requirement to
    keep (see credits.py) and costs kilobytes."""
    out, _ = _run(tmp_path, ["2026-08-01"], {"2026-08-01": "yt1"}, keep_days=7)
    assert not (out / "2026-08-01.mp4").exists()
    assert (out / "2026-08-01.meta.json").exists()


def test_zero_days_keeps_everything(tmp_path):
    """The default. A fresh clone must never delete its own work uninvited."""
    out, freed = _run(tmp_path, ["2026-01-01"], {"2026-01-01": "yt1"}, keep_days=0)
    assert (out / "2026-01-01.mp4").exists()
    assert freed == 0


def test_no_state_disables_pruning(tmp_path):
    """Without state there is no way to prove anything was uploaded, so prune nothing."""
    out = _out(tmp_path, ["2026-01-01"])
    freed = _prune_outputs(tmp_path, {"keep_output_days": 7},
                           datetime.strptime("2026-09-02", "%Y-%m-%d"), None)
    assert (out / "2026-01-01.mp4").exists()
    assert freed == 0


def test_non_date_filenames_are_ignored(tmp_path):
    out = _out(tmp_path, ["2026-08-01"])
    (out / "scratch.mp4").write_bytes(b"x")
    _prune_outputs(tmp_path, {"keep_output_days": 7},
                   datetime.strptime("2026-09-02", "%Y-%m-%d"),
                   FakeState({"2026-08-01": "yt1"}))
    assert (out / "scratch.mp4").exists()
