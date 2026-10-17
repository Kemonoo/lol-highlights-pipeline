"""Thumbnail frame choice (production/thumbnail.frame_times / pick_frame): pure."""
from pipeline.production.thumbnail import frame_times, pick_frame


def test_frames_span_the_action_window_best_moment_first():
    ts = frame_times({"api_action_start_s": 10, "api_action_end_s": 24,
                      "api_best_moment_s": 15}, 40, n=8)
    assert ts[0] == 15 and len(ts) == 8
    assert min(ts) >= 10 and max(ts) <= 24


def test_no_window_falls_back_to_the_best_moment_then_the_middle():
    ts = frame_times({"api_best_moment_s": 20}, 40, n=5)
    assert min(ts) >= 16 and max(ts) <= 24
    ts = frame_times({}, 40, n=5)
    assert min(ts) >= 12 and max(ts) <= 28


def test_scoreboard_and_no_action_frames_are_never_picked():
    cands = [{"colour": 0.9, "scoreboard": True},
             {"colour": 0.8, "action": False},
             {"colour": 0.3, "action": True},
             {"colour": 0.5}]                       # unasked (vlm failed)
    assert pick_frame(cands) == 2                   # confirmed action beats colour
    assert pick_frame(cands[:2]) is None
    assert pick_frame([{"colour": 0.2}, {"colour": 0.4}]) == 1    # no vlm: colour
