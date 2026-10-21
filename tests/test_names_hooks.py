"""2026-10-21: streamer names in titles/tags (search traffic is names) and the Shorts
first-seconds hook A/B test. Pure helpers only."""
from pipeline.production.credits import _cap_tags, champions_in, name_first
from pipeline.publishing.shorts import clean_hook_text, hook_variant


def test_hook_keeps_a_listed_name_or_gets_one_in_front():
    allowed = ["Thebausffs", "Tyler1"]
    assert name_first("Thebausffs Called the PENTA", "Thebausffs", allowed) \
        == "Thebausffs Called the PENTA"
    assert name_first("He Called the PENTA", "Tyler1", allowed) == "Tyler1: He Called the PENTA"
    # an invented name is never used: falls back to the best clip's streamer
    assert name_first("He Called the PENTA", "Faker", allowed) == "Thebausffs: He Called the PENTA"
    assert name_first("He Called the PENTA", "", []) == "He Called the PENTA"


def test_tags_dedupe_and_stay_under_youtubes_limit():
    tags = _cap_tags(["baus", "baus", "league of legends"] + [f"streamer{i:03d}" for i in range(80)])
    assert tags[:2] == ["baus", "league of legends"]
    assert sum(len(t) + (2 if " " in t else 0) + 1 for t in tags) <= 480


def test_champions_from_the_judges_descriptions():
    clips = [{"api_what_happens": "Kai'Sa dives the Lux. The Lux flashes."},
             {"api_what_happens": "Sion ults into three. Lux dies."}]
    assert champions_in(clips, {"lux", "sion", "ahri"}) == ["lux", "sion"]


def test_hook_arm_is_stable_and_about_half():
    ids = [f"Clip{i}" for i in range(2000)]
    arms = [hook_variant(i, 0.5) for i in ids]
    assert arms == [hook_variant(i, 0.5) for i in ids]
    assert 900 < sum(arms) < 1100
    assert not any(hook_variant(i, 0.0) for i in ids)


def test_hook_text_is_short_caps_ascii():
    assert clean_hook_text("watch the flash") == "WATCH THE FLASH"
    assert clean_hook_text("1v3 in base!") == "1V3 IN BASE!"
    assert clean_hook_text("this is far too many words here") == ""
    assert clean_hook_text("") == ""
