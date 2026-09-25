"""Style-A ("clip") thumbnail: geometry + composition on synthetic frames (no ffmpeg)."""
import numpy as np
from PIL import Image

from pipeline.production.thumbnail import (
    THUMB_H,
    THUMB_W,
    _rgb,
    action_centre,
    compose_clip,
    project_box,
    zoom_window,
)


def test_zoom_window_stays_inside_the_frame():
    x0, y0, cw, ch = zoom_window(0.99, 0.01, 1.25, 1920, 1080)
    assert (cw, ch) == (1536, 864)
    assert x0 == 1920 - 1536 and y0 == 0


def test_project_box_maps_and_clips():
    win = (0, 0, 1536, 864)                              # zoom 1.25 at the top-left
    r = project_box((0, 700, 400, 380), win, 1280, 720)  # webcam hanging off the bottom
    assert r[0] == 0 and r[3] == 720                     # clipped to the thumbnail
    assert project_box((1700, 0, 100, 100), win, 1280, 720) is None   # outside the crop


def test_action_centre_finds_the_colourful_spot_and_ignores_the_hud():
    hsv = np.zeros((108, 192, 3), dtype=np.uint8)
    hsv[30:40, 50:60] = (0, 255, 255)                    # a fight
    hsv[95:108, :] = (0, 255, 255)                       # bright HUD bar: masked
    x, y = action_centre(hsv)
    assert abs(x - 55 / 192) < 0.05 and abs(y - 35 / 108) < 0.05
    assert action_centre(np.zeros((108, 192, 3))) == (0.5, 0.5)


def test_rgb_parses_hex_and_lists():
    assert _rgb("#E10600") == (225, 6, 0)
    assert _rgb([64, 224, 208]) == (64, 224, 208)
    assert _rgb("nonsense", (1, 2, 3)) == (1, 2, 3)


def _frame():
    a = np.full((1080, 1920, 3), (40, 90, 40), dtype=np.uint8)   # grass
    a[750:1080, 0:470] = (200, 170, 150)                           # webcam, bottom-left
    return Image.fromarray(a)


def test_compose_clip_border_webcam_frame_and_size():
    img = compose_clip(_frame(), (0, 750, 470, 330), "ONE SHOT", {})
    assert img.size == (THUMB_W, THUMB_H)
    assert img.getpixel((3, THUMB_H // 2)) == (225, 6, 0)           # red border
    # white frame of the webcam panel, bottom-left, inset from the border
    px = np.asarray(img)
    turq = (np.abs(px.astype(int) - (255, 255, 255)).sum(axis=2) < 40)
    ys, xs = np.nonzero(turq)
    assert xs.min() >= 40 - 1 and xs.max() < THUMB_W // 2 and ys.max() <= THUMB_H - 40


def test_compose_clip_without_webcam_or_text():
    img = compose_clip(_frame(), None, "", {"clip_border_px": 0})
    assert img.size == (THUMB_W, THUMB_H)
    px = np.asarray(img).astype(int)
    assert not (np.abs(px - (255, 255, 255)).sum(axis=2) < 40).any()   # no panel
