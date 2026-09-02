"""The zero-clip guard — the check that would have stopped a public empty upload.

On 2026-08-13 yt-dlp's Twitch extractor broke (KeyError('data')) and all 181 downloads
failed. Every stage then handled "zero items" gracefully, exactly as designed: prefilter
scored 181 -> 0 kept, the judge selected 0 clips, assemble produced a 14.9s
intro-plus-outro with no clips and nobody credited, credits titled it, and upload
published it PUBLICLY. The run exited 0.

A broken extractor is an external event that will recur, so these tests pin the
behaviour that makes the next one loud instead of published.
"""
import json

import pytest


def _work(tmp_path, date, nclips):
    w = tmp_path / "work" / date
    w.mkdir(parents=True)
    (w / "vlm_filtered.json").write_text(json.dumps(
        {"clips": [{"id": f"c{i}", "duration": 30} for i in range(nclips)]}),
        encoding="utf-8")
    return w


@pytest.mark.parametrize("nclips", [0, 1, 2])
def test_assemble_refuses_a_video_with_too_few_clips(tmp_path, nclips):
    from pipeline.production.assemble import run
    _work(tmp_path, "2026-08-13", nclips)
    with pytest.raises(RuntimeError, match="refusing to assemble"):
        run({"paths": {"data_abs": str(tmp_path)}, "video": {"min_clips": 3}},
            None, "2026-08-13")


def test_assemble_proceeds_at_the_threshold(tmp_path):
    """The guard must not block a legitimately thin day — several real runs shipped
    3-9 clips. It fires on INPUT failure, not on a quiet Tuesday."""
    from pipeline.production.assemble import run
    _work(tmp_path, "2026-08-13", 3)
    with pytest.raises(Exception) as ei:      # fails later for want of real media
        run({"paths": {"data_abs": str(tmp_path)}, "video": {"min_clips": 3}},
            None, "2026-08-13")
    assert "refusing to assemble" not in str(ei.value)


def test_min_clips_zero_disables_the_guard(tmp_path):
    from pipeline.production.assemble import run
    _work(tmp_path, "2026-08-13", 0)
    with pytest.raises(Exception) as ei:
        run({"paths": {"data_abs": str(tmp_path)}, "video": {"min_clips": 0}},
            None, "2026-08-13")
    assert "refusing to assemble" not in str(ei.value)


@pytest.mark.parametrize("nchapters", [0, 2])
def test_upload_refuses_an_empty_master_from_the_stage_skip_path(tmp_path, nchapters):
    """Defence in depth. assemble is skipped when its output already exists, so a
    master built before the guard (or by an older version) would otherwise go straight
    to YouTube. Publishing is the irreversible step."""
    from pipeline.publishing.upload import run
    w = tmp_path / "work" / "2026-08-13"
    w.mkdir(parents=True)
    (w / "chapters.json").write_text(json.dumps([{"rank": i} for i in range(nchapters)]),
                                     encoding="utf-8")
    out = tmp_path / "output"
    out.mkdir()
    (out / "2026-08-13.mp4").write_bytes(b"x")
    (out / "2026-08-13.meta.json").write_text(json.dumps({"title": "t"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to publish"):
        run({"paths": {"data_abs": str(tmp_path)}, "video": {"min_clips": 3},
             "upload": {"enabled": True, "allow_reupload": True}}, None, "2026-08-13")
