"""Pure parts of enrichment.streamer_cam: box conversion and frame agreement."""
from pipeline.enrichment.streamer_cam import agree, iou, to_pixels


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


def test_iou():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 5, 5)) == 0.0


def test_two_of_three_frames_agree():
    cam = (970, 480, 300, 230)
    near = (975, 485, 295, 225)
    stray = (100, 100, 80, 80)                    # one frame boxed something else
    x, y, w, h = agree([cam, stray, near])
    assert abs(x - 972) <= 1 and abs(w - 297) <= 1


def test_one_box_alone_is_not_enough():
    assert agree([(970, 480, 300, 230), None, None]) is None
    assert agree([None, None, None]) is None
    assert agree([(0, 0, 100, 100), (500, 300, 100, 100), None]) is None
