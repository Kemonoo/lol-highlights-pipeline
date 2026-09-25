"""Speaker-coloured captions: answer parsing and caption placement (pure)."""
from pipeline.enrichment.speakers import parse, prompt, where
from pipeline.publishing.shorts import TARGET_H, caption_style


def test_parse_maps_answers_and_defaults_to_other():
    ans = {"speakers": [{"i": 0, "who": "A"}, {"i": 1, "who": "b"}, {"i": 2, "who": "Z"}]}
    assert parse(ans, 4, 2) == ["A", "B", "other", "other"]


def test_parse_rejects_unusable_answers():
    assert parse(None, 3, 2) is None
    assert parse({"speakers": []}, 3, 2) is None
    assert parse({"speakers": [{"i": 0, "who": "A"}]}, 6, 2) is None    # 1 of 6 labelled


def test_webcams_are_named_by_position():
    assert where((0, 26, 453, 353), 1920, 1080) == "top-left"
    assert where((0, 599, 731, 481), 1920, 1080) == "bottom-left"
    p = prompt([(0, 26, 453, 353)], [{"start": 0.0, "end": 1.0, "text": "hi"}], 1920, 1080)
    assert "A: the webcam at the top-left" in p and '0: 0.0-1.0s "hi"' in p


def test_no_speaker_info_keeps_the_old_caption():
    assert caption_style(None, 2, 400, 64) == ("(w-text_w)/2", TARGET_H - 464, "white", 64)


def test_duo_puts_each_speaker_in_their_own_panel_and_colour():
    xa, ya, ca, fa = caption_style("A", 2, 400, 64)
    xb, _, cb, _ = caption_style("B", 2, 400, 64)
    assert "0+(540-text_w)/2" in xa and "540+(540-text_w)/2" in xb
    assert ca != cb and fa <= 50 and ya > TARGET_H - 200
    assert caption_style("other", 2, 400, 64)[2] == "white"


def test_single_webcam_second_voice_gets_its_own_colour():
    assert caption_style("A", 1, 400, 64)[2] == "white"
    assert caption_style("other", 1, 400, 64)[2] != "white"
