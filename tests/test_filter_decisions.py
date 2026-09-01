"""The keep/reject rules — the layer that decides what ends up in the video.

These are the highest-value tests in the repo: `decide()` and `decide_api()` are pure
functions recomputed from cache on every run (see CLAUDE.md), so they encode every
tuning decision the filter has ever made, and they can be exercised with no GPU, no
API key, and no ffmpeg. A regression here silently changes what gets published.
"""
import pytest

from pipeline.filtering.api_judge import (SCHEMA, classify_failure, decide_api,
                                          local_judge,
                                          parse_verdict)
from pipeline.filtering.vlm_filter import decide

VF = {"kill_audio_min": 0.30, "hype_only_min": 0.55, "japanese_needs_kills": True}
AJ = {"min_entertainment": 6, "min_entertainment_reaction": 7, "min_play_quality": 7,
      "fallback_audio_min": 0.72, "fallback_motion_min": 0.11}


def clip(**kw) -> dict:
    """A clip that passes the structural gates, so each test varies one thing."""
    base = {"vlm_gameplay": True, "vlm_pro_play": False, "title": "some clip",
            "broadcaster_name": "streamer", "audio_score": 0.0, "kills_max": 0,
            "kill_frames": 0, "announcements": [], "eventlog": [], "multikill": False}
    return {**base, **kw}


# ── structural rejects ────────────────────────────────────────────────────────

def test_blacklisted_broadcaster_is_rejected_before_anything_else():
    c = clip(broadcaster_name="K4sen", multikill=True, audio_score=1.0)
    decide(c, VF, blacklist=frozenset({"k4sen"}))
    assert c["decision"] == "REJECT"
    assert c["reason"] == "BLACKLIST"


def test_non_gameplay_is_rejected():
    c = clip(vlm_gameplay=False, multikill=True)
    decide(c, VF)
    assert c["decision"] == "REJECT"
    assert c["reason"].startswith("NO_GAMEPLAY")


def test_pro_play_broadcast_is_rejected():
    c = clip(vlm_pro_play=True, multikill=True)
    decide(c, VF)
    assert c["decision"] == "REJECT"
    assert c["reason"] == "PRO_PLAY_UI"


# ── keep paths ────────────────────────────────────────────────────────────────

def test_title_keyword_keeps_even_without_detected_kills():
    """Viewer-written titles are the highest-precision signal we have."""
    c = clip(keyword="penta")
    decide(c, VF)
    assert c["decision"] == "KEEP"
    assert c["reason"] == "TITLE_KEYWORD_penta"


def test_symmetric_format_keyword_is_not_an_outplay_claim():
    """'1v1' describes a game mode; '1v5' describes an outplay."""
    c = clip(keyword="1v1")
    decide(c, VF)
    assert c["decision"] == "REJECT"

    c = clip(keyword="1v5")
    decide(c, VF)
    assert c["decision"] == "KEEP"


def test_multikill_announcement_keeps():
    c = clip(multikill=True)
    decide(c, VF)
    assert c["decision"] == "KEEP"
    assert c["reason"] == "MULTIKILL"


def test_confirmed_kills_need_audio_above_threshold():
    quiet = clip(kill_frames=2, audio_score=0.10)
    decide(quiet, VF)
    assert quiet["decision"] == "REJECT"

    loud = clip(kill_frames=2, audio_score=0.40)
    decide(loud, VF)
    assert loud["decision"] == "KEEP"


def test_loud_clip_with_no_kills_keeps_on_hype_alone():
    c = clip(audio_score=0.80)
    decide(c, VF)
    assert c["decision"] == "KEEP"


# ── the JP-title rule (owner finding: JP clips are usually talk-context) ───────

def test_japanese_title_cannot_pass_on_audio_hype_alone():
    c = clip(title="ペンタキル", audio_score=0.90)
    decide(c, VF)
    assert c["decision"] == "REJECT"
    assert c["title_japanese"] is True


def test_japanese_title_still_keeps_with_confirmed_kills():
    c = clip(title="ペンタキル", audio_score=0.90, multikill=True)
    decide(c, VF)
    assert c["decision"] == "KEEP"


def test_cjk_ideographs_alone_are_not_japanese():
    """Chinese titles have no kana, so the JP rule must not fire on them."""
    c = clip(title="五杀时刻", audio_score=0.80)
    decide(c, VF)
    assert c["title_japanese"] is False
    assert c["decision"] == "KEEP"


# ── scoring is monotonic where it should be ───────────────────────────────────

def test_keep_score_rises_with_evidence():
    weak = clip(audio_score=0.2)
    strong = clip(audio_score=0.2, multikill=True, kills_max=3, kill_frames=2)
    decide(weak, VF)
    decide(strong, VF)
    assert strong["keep_score"] > weak["keep_score"]


# ── the API judge's decision layer ────────────────────────────────────────────

def test_judge_keeps_entertaining_gameplay():
    c = {"api_focus": "gameplay", "api_entertainment": 7, "api_play_quality": 5}
    decide_api(c, AJ)
    assert c["api_decision"] == "KEEP"


def test_reaction_clips_face_a_higher_entertainment_bar():
    """Explicit owner decision (the 'slap' clip reversal): reaction-focus clips need
    ent>=7 to keep outright, where gameplay needs 6."""
    six = {"api_focus": "reaction", "api_entertainment": 6, "api_play_quality": 2}
    decide_api(six, AJ)
    assert six["api_decision"] == "DROP"

    seven = {"api_focus": "reaction", "api_entertainment": 7, "api_play_quality": 2}
    decide_api(seven, AJ)
    assert seven["api_decision"] == "KEEP"


def test_impressive_play_keeps_regardless_of_entertainment():
    c = {"api_focus": "gameplay", "api_entertainment": 2, "api_play_quality": 8}
    decide_api(c, AJ)
    assert c["api_decision"] == "KEEP"


def test_talk_clips_drop():
    c = {"api_focus": "talk", "api_entertainment": 9, "api_play_quality": 1}
    decide_api(c, AJ)
    assert c["api_decision"] == "DROP"


def test_unjudged_clips_fall_back_to_local_scoring():
    c = {"api_focus": "unjudged", "multikill": True, "audio_score": 0.6,
         "motion_score": 0.2}
    decide_api(c, AJ)
    assert c["api_focus"] == "local"
    assert c["api_decision"] == "KEEP"


# ── the quota-dead path must not fail open ────────────────────────────────────

def test_local_judge_drops_loud_but_static_clips():
    """Loud audio without motion is singing/chatting, not a highlight. This is the
    rule that stops a dead-quota day from publishing 20 minutes of talking."""
    c = {"kills_confirmed": 0, "kills_max": 0, "multikill": False,
         "audio_score": 0.95, "motion_score": 0.01}
    local_judge(c, AJ)
    assert c["api_decision"] == "DROP"


def test_local_judge_keeps_confirmed_multikills():
    c = {"kills_max": 3, "multikill": True, "audio_score": 0.3, "motion_score": 0.05}
    local_judge(c, AJ)
    assert c["api_decision"] == "KEEP"


@pytest.mark.parametrize("kills,expected", [(0, "DROP"), (2, "KEEP")])
def test_local_judge_keeps_on_kill_count(kills, expected):
    c = {"kills_max": kills, "kills_confirmed": kills, "multikill": False,
         "audio_score": 0.3, "motion_score": 0.02}
    local_judge(c, AJ)
    assert c["api_decision"] == expected


# ── a mute provider must not look like a confident rejection ──────────────────

def test_detect_raises_when_the_model_answers_nothing(monkeypatch, tmp_path):
    """Regression: Ollama 0.32's /api/generate returned an empty string whenever
    `format` was sent with images. Every frame parsed to None, every None counted as
    a no-vote, and the whole day's clips were rejected as 'not gameplay' AND cached.

    A provider that says nothing must raise, so the caller's existing handler gives
    the clip the benefit of the doubt and — critically — does not cache the result."""
    from pipeline.filtering import vlm_filter

    monkeypatch.setattr(vlm_filter, "sample_frames", lambda mp4, n: [b"x"] * n)
    clip: dict = {}
    with pytest.raises(RuntimeError, match="no usable answer"):
        vlm_filter.detect(clip, tmp_path / "c.mp4", lambda imgs, p: None, VF, None)


def test_detect_trusts_a_real_negative(monkeypatch, tmp_path):
    """The mirror case: when the model genuinely answers 'no', that IS a rejection."""
    from pipeline.filtering import vlm_filter

    monkeypatch.setattr(vlm_filter, "sample_frames", lambda mp4, n: [b"x"] * n)
    clip: dict = {}
    vlm_filter.detect(clip, tmp_path / "c.mp4", lambda imgs, p: {"gameplay": False},
                      VF, None)
    assert clip["vlm_gameplay"] is False
    assert clip["gameplay_votes"] == "0of3"


# ── parsing the judge's response ──────────────────────────────────────────────
# Gemini returns a partial object unless the schema marks the fields required. The
# scores it drops are `entertainment` and `best_moment_s`, and reading them with
# `.get(field, 0)` turned "no answer" into the harshest score on the scale — which
# both dropped clips outright and deflated api_rank_score on the ones it kept.

def test_complete_verdict_parses():
    v = parse_verdict({"clip_focus": "Gameplay", "play_quality": 8,
                       "entertainment": 7, "what_happens": "a quadra",
                       "best_moment_s": 12.4})
    assert v == {"api_focus": "gameplay", "api_play_quality": 8,
                 "api_entertainment": 7, "api_what_happens": "a quadra",
                 "api_best_moment_s": 12}


@pytest.mark.parametrize("missing", ["entertainment", "play_quality"])
def test_missing_score_is_not_a_verdict_of_zero(missing):
    """The regression: a real 08-01 quadra+ace came back with no `entertainment` and
    was recorded as ent0_pq8 — scored as maximally boring and ranked 4.8 instead of 8."""
    r = {"clip_focus": "gameplay", "play_quality": 8, "entertainment": 7,
         "what_happens": "scores a Quadra Kill and secures an Ace", "best_moment_s": 9}
    del r[missing]
    assert parse_verdict(r) is None


def test_missing_verdict_routes_to_local_scoring():
    """None must reach local_judge(), not be silently treated as a zero-score verdict."""
    c = {"api_focus": "unjudged", "multikill": True, "audio_score": 0.6,
         "motion_score": 0.2}
    assert parse_verdict(None) is None
    decide_api(c, AJ)
    assert c["api_focus"] == "local" and c["api_decision"] == "KEEP"


def test_absent_best_moment_is_allowed_and_defaults_to_zero():
    """Unlike the scores, best_moment_s only positions a replay — callers already
    read 0 as 'no timestamp', so it must not invalidate an otherwise good verdict."""
    v = parse_verdict({"clip_focus": "gameplay", "play_quality": 8,
                       "entertainment": 7, "what_happens": "x"})
    assert v is not None and v["api_best_moment_s"] == 0


def test_schema_requires_every_field_it_declares():
    """Guards the actual bug: `properties` without `required` lets the model omit."""
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


# ── classifying judge failures ───────────────────────────────────────────────
# api_judge stops calling the API after repeated failures. Counting every exception
# the same way meant two network timeouts, or two clips the model declined on content
# grounds, silently downgraded the REST of the run to local scoring.

def _err(**kw):
    from pipeline.providers.base import ProviderError
    return ProviderError("boom", **kw)


def test_content_refusal_is_about_one_clip_not_the_key():
    """RECITATION/SAFETY is this clip's content; the next clip may be fine."""
    assert classify_failure(_err(reason="RECITATION")) == "refusal"
    assert classify_failure(_err(reason="SAFETY")) == "refusal"


def test_rejected_credentials_stop_the_run_immediately():
    """401/403 will not heal mid-run — 14 straight August days were downgraded by these."""
    assert classify_failure(_err(status=401)) == "dead"
    assert classify_failure(_err(status=403)) == "dead"


def test_rate_limits_and_network_errors_stay_transient():
    """429/503 are worth counting toward the threshold, not treating as a dead key."""
    assert classify_failure(_err(status=429)) == "transient"
    assert classify_failure(_err(status=503)) == "transient"
    assert classify_failure(RuntimeError("read timed out")) == "transient"


def test_provider_error_preserves_status_through_retry_exhaustion():
    """The give-up path used to flatten the cause to a string, losing the status the
    caller needs to tell a dead key from a rate limit."""
    from pipeline.providers.base import ProviderError, RoleConfig, retrying, _Retryable
    rc = RoleConfig(role="judge", provider="gemini", max_retries=1, retry_base_s=0)

    def boom():
        raise _Retryable("HTTP 429 - quota", status=429)

    with pytest.raises(ProviderError) as ei:
        retrying(boom, rc, "judge")
    assert ei.value.status == 429


# ── thumbnail hook text ──────────────────────────────────────────────────────

def _clips(summary: str):
    return [{"api_rank_score": 9, "vlm_summary": summary, "title": ""}]


def test_hook_prefers_the_specific_moment_over_the_generic():
    from pipeline.production.credits import _hook
    assert _hook(_clips("he scores a Quadra Kill"), "2026-08-01") == "QUADRA KILL"
    assert _hook(_clips("a chaotic team fight near dragon"), "2026-08-01") == "CHAOS TEAMFIGHT"


def test_generic_hook_rotates_by_date_but_is_stable_per_date():
    """A constant fallback put the same two words on 15 of 29 thumbnails."""
    from pipeline.production.credits import _hook
    c = _clips("nothing notable happens at all")
    picks = {_hook(c, f"2026-08-{d:02d}") for d in range(1, 20)}
    assert len(picks) > 1                                    # not a constant
    assert _hook(c, "2026-08-07") == _hook(c, "2026-08-07")   # deterministic


def test_hook_question_suffix_does_not_double_punctuation():
    """Variant 1 appends '?!'; a hook already ending in punctuation gave 'WHAT?!?!'."""
    from pipeline.production.thumbnail import _ask
    assert _ask("INSANE PLAYS") == "INSANE PLAYS?!"
    assert _ask("BACKDOOR!") == "BACKDOOR!"
    assert _ask("HE DID WHAT?!") == "HE DID WHAT?!"


def test_generic_hooks_carry_no_trailing_punctuation():
    """Keeps _ask() meaningful and the headline short enough to stay large."""
    from pipeline.production.credits import _GENERIC_HOOKS
    assert all(h[-1] not in "?!." for h in _GENERIC_HOOKS)
