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


def test_fame_tiers_and_owner_picks():
    from pipeline.enrichment.fame import is_famous, tier
    cfg = {"video": {"fame": {"famous_followers": 250_000}},
           "prefilter": {"boost_streamers": {"thebausffs": 0.3}}}
    fame = {"katevolved": 446_209, "doxamid": 4057, "raikokatarina": 16_935}
    assert tier(446_209, 250_000) == "famous" and tier(16_935, 250_000) == "known"
    assert tier(4057, 250_000) == "small" and tier(None, 250_000) == "unknown"
    assert is_famous("KatEvolved", fame, cfg)
    assert is_famous("Thebausffs", {}, cfg)            # boost list = always famous
    assert not is_famous("doxamid", fame, cfg)


def test_unknown_streamer_name_is_appended_for_search():
    from pipeline.publishing.shorts import name_suffix
    assert name_suffix("This Streamer 1v3s In Base", "doxamid") == "This Streamer 1v3s In Base | doxamid"
    assert name_suffix("doxamid 1v3s", "doxamid") == "doxamid 1v3s"
    assert len(name_suffix("x" * 100, "doxamid", 80)) == 80


def test_family_friendly_titles_and_captions():
    from pipeline.production.safe_text import mask
    assert mask("PinkWardlol Made K'Sante Kill HIMSELF") == "PinkWardlol Made K'Sante K*ll HIMSELF"
    assert mask("what the fuck was that") == "what the f*** was that"
    assert mask("holy shit, motherfucker", style="stars") == "holy ****, ****"
    assert mask("pass the class, pentakill kills") == "pass the class, pentakill kills"
    assert mask("he is an ass", style="stars") == "he is an ****"


def test_title_without_series_name():
    from pipeline.production.credits import hook_title
    assert hook_title("Baus Called the PENTA", "", 60) == "Baus Called the PENTA #60"
    assert hook_title("Baus Called the PENTA", "LoL Daily Clips", 60) \
        == "Baus Called the PENTA... LoL Daily Clips #60"


def test_caption_censor_in_groups():
    from pipeline.production.assemble import mask_text
    assert mask_text("fucking insane", (), "stars") == "**** insane"
    assert mask_text("fucking insane") == "fucking insane"          # off by default
