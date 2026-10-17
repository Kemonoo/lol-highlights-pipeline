"""2026-10-18: trim to the judge's action window, shorter clip per moment, Claude queue
order. Pure decision layer only (no CLI, no ffmpeg)."""
from pipeline.filtering.api_judge import select
from pipeline.filtering.moments import dedupe, representative
from pipeline.filtering.prescreen import queue_score
from pipeline.production.trim import length, shift_words, window

V = {"trim": {"enabled": True, "pad_before_s": 3, "pad_after_s": 3, "min_cut_s": 6,
              "min_head_cut_s": 10, "min_len_s": 10}}


def test_baus_1012_dead_tail_is_cut():
    c = {"duration": 53.6, "api_action_start_s": 8.0, "api_action_end_s": 18.0}
    assert window(c, V) == (0.0, 21.0)          # short setup kept, 32 s dead tail gone


def test_long_dead_head_is_cut_short_one_kept():
    assert window({"duration": 60, "api_action_start_s": 40, "api_action_end_s": 58}, V) \
        == (37.0, 60)
    assert window({"duration": 30, "api_action_start_s": 9, "api_action_end_s": 28}, V) is None


def test_no_window_disabled_or_tiny_window():
    assert window({"duration": 30, "api_action_start_s": 0, "api_action_end_s": 0}, V) is None
    assert window({"duration": 30}, V) is None
    assert window({"duration": 53, "api_action_start_s": 8, "api_action_end_s": 18},
                  {"trim": {"enabled": False}}) is None
    w = window({"duration": 60, "api_action_start_s": 30, "api_action_end_s": 31}, V)
    assert w[1] - w[0] >= 10


def test_captions_follow_the_cut():
    words = [{"word": "a", "start": 1, "end": 2}, {"word": "b", "start": 38, "end": 39},
             {"word": "c", "start": 61, "end": 62}]
    out = shift_words(words, 37.0, 60.0)
    assert [w["word"] for w in out] == ["b"] and out[0]["start"] == 1.0


def test_selection_counts_trimmed_length():
    c = {"duration": 53.6, "api_action_start_s": 8.0, "api_action_end_s": 18.0}
    assert length(c, V) == 21.0
    kept = [dict(c, id=str(i), api_decision="KEEP") for i in range(3)]
    _, total = select(kept, list(kept), {**V, "target_minutes_ideal": 0.5,
                                         "target_minutes_max": 10}, {})
    assert total == 63.0


def _m(i, dur, views, off):
    return {"id": i, "video_id": "v", "vod_offset": off, "view_count": views,
            "duration": dur, "broadcaster_id": "b", "creator_id": "x"}


def test_shorter_clip_of_a_moment_when_views_are_comparable():
    g = [_m("long", 60, 100, 0), _m("short", 25, 60, 10), _m("tiny", 8, 90, 20)]
    assert representative(g)["id"] == "long"                      # default unchanged
    assert representative(g, prefer="short")["id"] == "short"      # tiny < min_s
    g2 = [_m("long", 60, 100, 0), _m("short", 25, 20, 10)]          # 20 < half of 100
    assert representative(g2, prefer="short")["id"] == "long"
    kept = dedupe(g, [], 90, prefer="short")
    assert [c["id"] for c in kept] == ["short"]


def test_queue_blends_claude_and_signals_and_unscored_keeps_its_place():
    good = {"keep_score": 0.10, "prescreen_score": 9}
    meh = {"keep_score": 0.20, "prescreen_score": 2}
    assert queue_score(good, 0.3) > queue_score(meh, 0.3)
    assert queue_score({"keep_score": 0.15}, 0.3) == 0.15
