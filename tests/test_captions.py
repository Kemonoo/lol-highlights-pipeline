"""Caption grouping: the pure layer under the burned English captions.

The two defects these cover were both arithmetic, not rendering — words drawn on top
of each other, and a pace no viewer can read — so they are testable without ffmpeg.
"""
from pipeline.production.assemble import _caption_dt, _caption_groups, _is_english


def W(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_empty_input():
    assert _caption_groups(None) == []
    assert _caption_groups([]) == []


def test_groups_are_never_overlapping():
    # real whisper output: 0.06-0.08s words, back to back. The old code gave each a
    # 0.15s floor, which ran a word's window past the next word's start.
    words = [W("Nice", 2.50, 2.90), W("try", 2.90, 3.06), W("to", 3.06, 3.14),
             W("deal", 3.14, 3.28), W("those.", 3.28, 3.60), W("I'm", 5.08, 5.48),
             W("surprised", 5.48, 5.68), W("the", 5.68, 5.86)]
    groups = _caption_groups(words)
    assert groups
    for a, b in zip(groups, groups[1:]):
        assert a["end"] <= b["start"], f"{a} overlaps {b}"
        assert a["end"] > a["start"]


def test_words_are_grouped_not_flashed_one_at_a_time():
    words = [W(w, i * 0.2, i * 0.2 + 0.18) for i, w in
             enumerate("one two three four five six".split())]
    groups = _caption_groups(words, max_words=3)
    assert all(1 <= len(g["text"].split()) <= 3 for g in groups)
    assert len(groups) == 2
    assert groups[0]["text"] == "one two three"


def test_silence_starts_a_new_phrase():
    words = [W("hello", 0.0, 0.3), W("there", 0.3, 0.6), W("wow", 4.0, 4.3)]
    groups = _caption_groups(words, max_gap=0.65)
    assert len(groups) == 2
    assert groups[1]["text"] == "wow"


def test_sentence_end_starts_a_new_phrase():
    words = [W("stop.", 0.0, 0.3), W("go", 0.35, 0.6), W("now", 0.6, 0.9)]
    groups = _caption_groups(words, max_words=3)
    assert groups[0]["text"] == "stop."
    assert groups[1]["text"] == "go now"


def test_long_span_is_split():
    words = [W("aaa", 0.0, 0.5), W("bbb", 0.5, 1.2), W("ccc", 1.2, 3.0)]
    groups = _caption_groups(words, max_words=5, max_seconds=1.9, max_gap=5.0)
    assert len(groups) == 2


def test_short_phrase_is_held_long_enough_to_read():
    words = [W("go", 0.0, 0.06)]
    groups = _caption_groups(words, min_seconds=0.62)
    assert groups[0]["end"] - groups[0]["start"] >= 0.62


def test_min_seconds_never_wins_over_the_next_phrase():
    # holding for min_seconds would collide with the next group; disjointness wins.
    words = [W("a", 0.0, 0.05), W("b", 0.30, 0.35)]
    groups = _caption_groups(words, max_words=1, min_seconds=1.5, pad=0.06)
    assert groups[0]["end"] <= groups[1]["start"]


def test_unsortable_or_broken_words_are_skipped():
    words = [W("ok", 0.0, 0.4), {"word": "", "start": 1.0, "end": 1.2},
             {"word": "bad"}, {"word": "nan", "start": "x", "end": "y"}]
    groups = _caption_groups(words)
    assert [g["text"] for g in groups] == ["ok"]


def test_drawtext_chain_is_disjoint_and_upper_case():
    v = {"height": 1080, "caption_y_frac": 0.70}
    words = [W("he's", 0.0, 0.2), W("gone", 0.2, 0.5), W("now.", 0.5, 0.8),
             W("wow", 2.0, 2.4)]
    chain = _caption_dt(words, v)
    assert "HE’S GONE NOW." in chain      # apostrophe survives as U+2019
    assert chain.count("drawtext=") == 2
    assert "y=756" in chain


def test_drawtext_empty_without_words():
    assert _caption_dt(None, {"height": 1080}) == ""
    assert _caption_dt([], {"height": 1080}) == ""


def test_is_english_variants():
    assert _is_english("en") and _is_english("EN-US") and _is_english("English")
    assert not _is_english("pt") and not _is_english("") and not _is_english(None)


# ── source-caption detection: the pure sampling layer ─────────────────────────

def test_speech_windows_merges_adjacent_words():
    from pipeline.enrichment.burned_captions import speech_windows
    words = [W("a", 0.0, 0.2), W("b", 0.3, 0.5),        # same breath
             W("c", 5.0, 5.4)]                           # separate run
    assert speech_windows(words) == [(0.0, 0.5), (5.0, 5.4)]


def test_speech_windows_ignores_broken_entries():
    from pipeline.enrichment.burned_captions import speech_windows
    assert speech_windows([{"word": "x"}, {"word": "", "start": 0, "end": 1},
                           {"word": "y", "start": "a", "end": "b"}]) == []
    assert speech_windows(None) == []


def test_sample_times_steps_through_one_long_run():
    """A single unbroken sentence must still yield several speech samples.

    Taking one point per run left such clips with too few frames to compare, which read
    as "no caption" on clips that plainly had one (2026-09-08 #14: one 11.7s run).
    """
    from pipeline.enrichment.burned_captions import sample_times
    words = [W(str(i), 18.1 + i * 0.3, 18.1 + i * 0.3 + 0.28) for i in range(38)]
    speech, silence = sample_times(words, 30.0, 6)
    assert len(speech) == 6
    assert all(18.1 <= t <= 29.9 for t in speech)
    assert len(silence) >= 2


def test_sample_times_keeps_silence_clear_of_speech():
    from pipeline.enrichment.burned_captions import sample_times
    words = [W("a", 5.0, 5.3), W("b", 5.3, 6.0)]
    speech, silence = sample_times(words, 20.0, 6)
    assert speech and silence
    # a caption lingers a little past the last word, so the baseline must stay clear
    assert all(t < 4.2 or t > 6.8 for t in silence)


def test_sample_times_needs_a_real_run():
    from pipeline.enrichment.burned_captions import sample_times
    assert sample_times([W("hi", 1.0, 1.2)], 20.0, 6) == ([], [])   # run too short
    assert sample_times(None, 20.0, 6) == ([], [])
    assert sample_times([W("a", 0.0, 3.0)], 0.0, 6) == ([], [])     # no duration


def test_detect_is_a_no_op_when_disabled():
    from pipeline.enrichment.burned_captions import detect
    cfg = {"video": {"skip_caption_when_source_has_one": False},
           "paths": {"data_abs": "/nonexistent"}}
    assert detect(cfg, "2026-01-01", [{"id": "x"}]) == {}
