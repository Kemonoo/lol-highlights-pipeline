"""Thumbnail rotation: date -> layout + palette (pure)."""
from pipeline.production.thumbnail import cover_crop, rotated_opts, rotation_pick

PALS = [{"name": n, "border": b, "text": "#FFFFFF", "frame": b}
        for n, b in [("a", "#000001"), ("b", "#000002"), ("c", "#000003"),
                     ("d", "#000004"), ("e", "#000005")]]
TH = {"clip_rotation": {"enabled": True, "layouts": ["pip", "bigface"], "palettes": PALS}}


def test_same_date_same_pick_and_consecutive_days_differ():
    days = [f"2026-10-{d:02d}" for d in range(1, 11)]
    picks = [rotation_pick(d, 2, 5) for d in days]
    assert picks == [rotation_pick(d, 2, 5) for d in days]
    assert all(a[0] != b[0] and a[1] != b[1] for a, b in zip(picks, picks[1:]))
    assert len(set(picks)) == 10                     # every combination in 10 days


def test_rotation_sets_colours_and_records_the_pick():
    layout, opts, rec = rotated_opts(TH, "2026-10-07", True)
    assert layout in ("pip", "bigface") and rec["layout"] == layout
    assert opts["clip_border_color"] == next(p["border"] for p in PALS if p["name"] == rec["palette"])


def test_no_webcam_falls_back_to_pip_and_off_means_style_a():
    for d in ("2026-10-07", "2026-10-08"):
        assert rotated_opts(TH, d, False)[0] == "pip"
    layout, opts, rec = rotated_opts({"clip_border_color": "#E10600"}, "2026-10-07", True)
    assert layout == "pip" and rec["palette"] is None and opts["clip_border_color"] == "#E10600"


def test_cover_crop_keeps_the_face_inside():
    k, left, top = cover_crop(210, 281, 538, 720, 0.5, 0.8)   # face low in a tall webcam
    assert k * 210 >= 538 and k * 281 >= 720
    assert top == int(k * 281 - 720)                          # clamped to the bottom edge
    assert cover_crop(100, 100, 50, 50, 0.0, 0.0)[1:] == (0, 0)


def test_big_face_only_when_reacting():
    th = {"clip_rotation": {"enabled": True, "layout_by_emotion": True,
                            "layouts": ["pip", "bigface"],
                            "palettes": [{"name": "a", "border": "#000"}]}}
    assert rotated_opts(th, "2026-10-07", True, reacting=True)[0] == "bigface"
    assert rotated_opts(th, "2026-10-07", True, reacting=False)[0] == "pip"
    assert rotated_opts(th, "2026-10-07", True, reacting=None)[0] == "pip"
    assert rotated_opts(th, "2026-10-07", False, reacting=True)[0] == "pip"   # no webcam


def test_ctr_grouped_by_style_weighted_by_impressions():
    from pipeline.tools.thumb_ctr import group
    styles = {"a": {"layout": "pip", "palette": "red"}, "b": {"layout": "bigface", "palette": "red",
              "reacting": True}, "c": {"layout": "pip", "palette": "cyan"}}
    g = group(styles, {"a": (1000, 2.0), "b": (1000, 4.0), "c": (3000, 1.0)})
    imp, clicks, n = g[("pip", "*")]
    assert n == 2 and imp == 4000 and round(100 * clicks / imp, 2) == 1.25
    assert g[("*", "red")][2] == 2 and g[("reacting", "*")][2] == 1
