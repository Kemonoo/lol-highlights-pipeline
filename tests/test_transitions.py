"""Clip transitions: boundary plan + per-edge filters (pure)."""
from pipeline.production.transitions import edges, head_vf, plan, tail_vf


def test_plan_is_seeded_and_never_repeats():
    p = plan(22, "2026-09-25")
    assert len(p) == 21 and p == plan(22, "2026-09-25")
    assert all(a != b for a, b in zip(p, p[1:]))
    assert set(p) <= {"glitch", "whip", "pixelate"}
    assert plan(22, "2026-09-25") != plan(22, "2026-09-26")


def test_plan_weights_and_edge_cases():
    assert plan(1, "d") == [] and plan(0, "d") == []
    assert set(plan(10, "d", {"whip": 1, "glitch": 0})) == {"whip"}
    assert plan(5, "d", {"nope": 3}) == []


def test_edges_first_and_last_clip_have_no_transition_outward():
    k = ["whip", "glitch"]
    assert edges(k, 0) == (None, "whip")
    assert edges(k, 1) == ("whip", "glitch")
    assert edges(k, 2) == ("glitch", None)
    assert edges([], 0) == (None, None)


def test_filters_sit_at_the_right_end_of_the_clip():
    assert head_vf(None) == [] and tail_vf(None, 10) == []
    assert "lte(t,0.100)" in head_vf("glitch")[0]
    assert "gte(t,9.933)" in tail_vf("glitch", 10.0)[0]
    px = tail_vf("pixelate", 10.0)
    assert px[0].startswith("pixelize=w=6") and px[-1].startswith("pixelize=w=48")
    assert "between(t,9.800" in px[0]
    assert head_vf("pixelate")[0].startswith("pixelize=w=48")
    assert tail_vf("whip", 10.0)[0].startswith("scroll=")
