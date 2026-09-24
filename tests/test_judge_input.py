"""Which copy the judge may watch (api_judge.landscape_at_least)."""
from pipeline.filtering.api_judge import landscape_at_least


def test_twitch_portrait_360_is_rejected():
    # yt-dlp "worst" = Twitch portrait-360: 360x640 with the game shrunk inside
    assert not landscape_at_least(360, 640, 720)


def test_small_landscape_is_rejected():
    assert not landscape_at_least(640, 360, 720)


def test_real_720p_landscape_is_reused():
    assert landscape_at_least(1280, 720, 720)
