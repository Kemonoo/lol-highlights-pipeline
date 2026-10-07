"""Livestream playlist + reconnect position + encoder flags (pure)."""
from pipeline.tools.stream import build_playlist, encode_args, playlist_text, resume_from

VIDS = [("a.mp4", 600.0), ("b.mp4", 700.0), ("c.mp4", 0.0)]


def test_playlist_newest_first_repeats_until_target():
    p = build_playlist(VIDS, 2500)
    assert [x[0] for x in p] == ["b.mp4", "a.mp4", "b.mp4", "a.mp4"]
    assert sum(d for _, d in p) >= 2500
    assert build_playlist([], 100) == [] and build_playlist(VIDS, 0) == []


def test_shuffle_is_seeded():
    v = [(f"{i}.mp4", 60.0) for i in range(10)]
    assert build_playlist(v, 600, "shuffle", "x") == build_playlist(v, 600, "shuffle", "x")


def test_resume_from_finds_the_playing_video():
    p = [("a", 100.0), ("b", 50.0)]
    assert resume_from(p, 0) == (0, 0)
    assert resume_from(p, 120) == (1, 20)
    assert resume_from(p, 151)[0] == 2


def test_encoder_flags_are_cbr_with_2s_keyframes():
    a = encode_args("h264_nvenc", 6000, 30)
    assert "cbr" in a and a[a.index("-g") + 1] == "60" and "6000k" in a
    assert "nal-hrd=cbr" in encode_args("libx264", 4500, 30)


def test_reconnect_starts_the_first_file_part_way():
    t = playlist_text([("C:/x/a.mp4", 10.0), ("C:/x/b.mp4", 10.0)], 4.5)
    assert t.splitlines() == ["file 'C:/x/a.mp4'", "inpoint 4.50", "file 'C:/x/b.mp4'"]
    assert "inpoint" not in playlist_text([("a.mp4", 1.0)])
