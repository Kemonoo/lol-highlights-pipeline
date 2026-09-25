"""Pure parts of enrichment.streamer_cam: box parsing, frame agreement, edge snapping."""
import numpy as np

from pipeline.enrichment.streamer_cam import (
    agree,
    consensus,
    iou,
    pad_unsnapped,
    parse_boxes,
    snap,
    snap_sides,
    to_pixels,
)


def test_qwen_box_is_0_to_1000_relative():
    # clip 03 of 2026-09-23: webcam bottom-left, frame 1280x720
    assert to_pixels([25, 590, 265, 820], 1280, 720) == (32, 424, 307, 165)


def test_reversed_and_out_of_range_coordinates_are_normalised():
    assert to_pixels([265, 820, 25, 590], 1280, 720) == (32, 424, 307, 165)
    assert to_pixels([-10, 0, 300, 1200], 1000, 1000) == (0, 0, 300, 1000)


def test_slivers_and_whole_frame_are_not_overlays():
    assert to_pixels([500, 500, 510, 900], 1280, 720) is None
    assert to_pixels([0, 0, 1000, 1000], 1280, 720) is None
    assert to_pixels(None, 1280, 720) is None
    assert to_pixels([1, 2, 3], 1280, 720) is None


def test_parse_accepts_the_shapes_a_small_model_produces():
    two = {"streamers": [{"bbox_2d": [0, 0, 200, 200]}, {"bbox_2d": [700, 700, 900, 900]}]}
    assert len(parse_boxes(two, 1000, 1000)) == 2
    assert len(parse_boxes([[0, 0, 200, 200]], 1000, 1000)) == 1
    assert len(parse_boxes({"streamer": True, "bbox_2d": [0, 0, 200, 200]}, 1000, 1000)) == 1
    assert parse_boxes({"streamer": False, "bbox_2d": [0, 0, 200, 200]}, 1000, 1000) == []
    assert parse_boxes({"streamers": []}, 1000, 1000) == []
    assert parse_boxes(None, 1000, 1000) == []


def test_iou():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 5, 5)) == 0.0


def test_two_of_three_frames_agree():
    cam, near, stray = (970, 480, 300, 230), (975, 485, 295, 225), (100, 100, 80, 80)
    x, y, w, h = agree([cam, stray, near])
    assert abs(x - 972) <= 1 and abs(w - 297) <= 1


def test_one_box_alone_is_not_enough():
    assert agree([(970, 480, 300, 230), None, None]) is None
    assert agree([None, None, None]) is None


def test_duo_stream_keeps_both_webcams():
    # 09-24 #24 (Dantes): one webcam top-left, one bottom-left, both seen on most frames
    top, bottom = (0, 0, 300, 200), (0, 380, 330, 300)
    frames = [[top, bottom], [bottom, top], [(2, 385, 325, 295)]]
    got = consensus(frames, max_n=2)
    assert len(got) == 2 and got[0][1] > 300 and got[1][1] < 50   # bottom has 3 votes


def test_a_frame_votes_once_and_overlapping_boxes_are_one_overlay():
    cam = (970, 480, 300, 230)
    frames = [[cam, (980, 490, 280, 210)], [], []]   # the same overlay twice, one frame
    assert consensus(frames) == []
    assert len(consensus([[cam], [(960, 470, 320, 250)], []])) == 1


def test_max_n_caps_the_count():
    boxes = [(0, 0, 100, 100), (400, 0, 100, 100), (800, 0, 100, 100)]
    assert len(consensus([boxes, boxes], max_n=2)) == 2
    assert len(consensus([boxes, boxes], max_n=3)) == 3


def _gameplay(k):
    """A smooth, shifting gradient with mild noise: neighbouring pixels differ by far
    less than snap's 18-grey step, as in a real game frame."""
    rng = np.random.default_rng(k)
    yy, xx = np.mgrid[0:360, 0:640]
    base = 80 + 30 * np.sin((xx + 25 * k) / 60.0) * np.cos((yy - 15 * k) / 45.0)
    return (base + rng.uniform(-4, 4, base.shape)).astype(np.float32)


def _frames_with_overlay(x0, y0, x1, y1, n=4, seed=0):
    """Moving 'gameplay' (smooth, like a real frame) with a flat rectangle pasted on top."""
    out = []
    for k in range(n):
        f = _gameplay(k + seed)
        f[y0:y1, x0:x1] = 200.0
        out.append(f)
    return out


def test_snap_moves_rough_edges_onto_the_overlay_outline():
    frames = _frames_with_overlay(400, 200, 600, 330)
    x, y, w, h = snap(frames, (410, 190, 180, 150))        # a few % off on every side
    assert abs(x - 400) <= 3 and abs(y - 200) <= 3
    assert abs(x + w - 600) <= 3 and abs(y + h - 330) <= 3


def test_snap_keeps_the_rough_box_when_there_is_no_outline():
    frames = [_gameplay(k) for k in range(4)]
    assert snap(frames, (200, 100, 150, 120)) == (200, 100, 150, 120)


def test_snapped_sides_are_reported():
    frames = _frames_with_overlay(400, 200, 600, 330)
    _, sides = snap_sides(frames, (410, 190, 180, 150))
    assert sides == {"l", "r", "t", "b"}
    _, none = snap_sides([_gameplay(k) for k in range(4)], (200, 100, 150, 120))
    assert none == set()


def test_padding_only_widens_unsnapped_sides_and_stays_in_frame():
    assert pad_unsnapped((100, 100, 200, 100), set(), 0.1, 1920, 1080) == (80, 90, 240, 120)
    assert pad_unsnapped((100, 100, 200, 100), {"l", "t"}, 0.1, 1920, 1080) == (100, 100, 220, 110)
    assert pad_unsnapped((0, 1000, 200, 80), set(), 0.5, 1920, 1080) == (0, 960, 300, 120)


def test_snap_ignores_static_lines_inside_the_webcam():
    # 09-23 #11: a shelf inside the webcam is a static line too; the real outline is the
    # one with moving gameplay outside it
    frames = _frames_with_overlay(100, 100, 400, 300)
    for f in frames:
        f[100:300, 100:160] = 120.0          # darker wall strip -> a line at x=160
    x, y, w, h = snap(frames, (130, 110, 265, 180))       # real edge 30 px out, shelf at 160
    assert abs(x - 100) <= 3
