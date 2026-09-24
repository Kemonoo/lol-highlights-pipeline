"""Webcam choice (shorts.pick_facecam) — cases taken from real clips of 2026-09-22.

Numbers are the measured cluster stats: votes = frames of 9 the face was found in,
live = median frame-to-frame change of the box, skin = share of skin-coloured pixels.
"""
from pipeline.publishing.shorts import pick_facecam


def c(votes, live, skin, name):
    return dict(votes=votes, live=live, skin=skin, box=name)


def test_static_hud_portrait_loses_to_the_webcam():
    # clip 05: a HUD champion portrait was found in all 9 frames but never changes
    hud, cam = c(9, 0.01, 0.60, "hud"), c(4, 38.0, 0.60, "cam")
    assert pick_facecam([hud, cam])["box"] == "cam"


def test_kill_feed_portrait_loses_to_a_dim_webcam():
    # clip 03: the kill feed changes and looks skin-toned, but the webcam is in more frames
    feed, cam = c(6, 48.3, 0.75, "feed"), c(9, 47.0, 0.16, "cam")
    assert pick_facecam([feed, cam])["box"] == "cam"


def test_art_without_skin_is_not_a_face():
    # clip 39: side-panel art that changes a little but has no skin tones
    assert pick_facecam([c(5, 19.0, 0.03, "art")]) is None


def test_too_few_frames_is_not_a_webcam():
    # clip 44: champion art on the honor screen, found in only 3 of 9 frames
    assert pick_facecam([c(3, 41.9, 0.24, "a"), c(3, 35.9, 0.16, "b")]) is None


def test_nothing_found_means_no_split_layout():
    assert pick_facecam([]) is None


def test_thresholds_are_configurable():
    assert pick_facecam([c(4, 2.0, 0.5, "x")], min_live=1.5)["box"] == "x"
