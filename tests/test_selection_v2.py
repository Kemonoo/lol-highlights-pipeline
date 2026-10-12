"""Selection redesign 2026-10-12: moments, signal ranking, sanity mode, judge hard facts,
streamer cap, stricter fillers, caption hygiene. Pure decision layer only."""
from pipeline.enrichment.transcribe import drop_hallucinations
from pipeline.filtering.api_judge import decide_api, hard_fail, select, streamer_cap
from pipeline.filtering.moments import cluster, dedupe, rank_score, view_percentile
from pipeline.filtering.prefilter import restatus
from pipeline.filtering.vlm_filter import decide
from pipeline.production.assemble import mask_text


def C(i, vod="v1", off=0.0, views=10, who="a", **kw):
    return {"id": i, "video_id": vod, "vod_offset": off, "view_count": views,
            "broadcaster_name": who, "broadcaster_id": who, "creator_id": "x",
            "duration": 30, **kw}


# ── moments ──────────────────────────────────────────────────────────────────
def test_baus_1007_three_clips_of_one_quadra_are_one_moment():
    clips = [C("a", off=9186, views=44), C("b", off=9190, views=23),
             C("c", off=9192, views=208), C("d", off=10987, views=46)]
    groups = sorted(len(g) for g in cluster(clips))
    assert groups == [1, 3]
    keep = dedupe(clips, published=[])
    assert {c["id"] for c in keep} == {"c", "d"}            # most viewed stands for it
    assert clips[0]["prefilter_status"] == "DUPLICATE_MOMENT"
    assert clips[2]["moment_size"] == 3


def test_moment_published_in_the_last_days_is_not_reused():
    published = [C("old", off=10990)]
    keep = dedupe([C("d", off=10987), C("e", vod="v2", off=10987)], published)
    assert [c["id"] for c in keep] == ["e"]


def test_clips_without_vod_info_are_never_merged():
    clips = [C("a", vod=None, off=None), C("b", vod=None, off=None)]
    assert len(dedupe(clips, [])) == 2


def test_self_clip_and_rank_signals():
    me = C("a", creator_id="a")                # creator == broadcaster
    dedupe([me], [])
    assert me["self_clip"]
    pool = [C(str(i), vod=f"v{i}", views=i) for i in range(5)]
    view_percentile(pool)
    assert pool[0]["view_pct"] == 0 and pool[-1]["view_pct"] == 1
    loud = {"audio_score": 1.0, "view_pct": 0}
    cluster3 = {"moment_size": 3, "view_pct": 0}
    assert rank_score(cluster3) > rank_score(loud)          # loudness is a small term


# ── prefilter ────────────────────────────────────────────────────────────────
PF = {"audio_exclude": 0.22, "audio_pass": 0.52, "motion_exclude": 0.015}


def test_quiet_gate_off_lets_quiet_clips_compete():
    c = {"prefilter_status": "AUDIO_EXCLUDE", "audio_score": 0.11, "motion_score": 0.05}
    restatus(c, {**PF, "quiet_gate": True})
    assert c["prefilter_status"] == "AUDIO_EXCLUDE"
    restatus(c, {**PF, "quiet_gate": False})
    assert c["prefilter_status"] == "BORDERLINE_PASS"
    still = {"prefilter_status": "BORDERLINE_PASS", "audio_score": 0.3, "motion_score": 0.001}
    restatus(still, {**PF, "quiet_gate": False})
    assert still["prefilter_status"] == "MOTION_EXCLUDE"           # static stays a gate
    t = {"prefilter_status": "TOURNAMENT_EXCLUDE"}
    restatus(t, PF)
    assert t["prefilter_status"] == "TOURNAMENT_EXCLUDE"


# ── vlm sanity mode ──────────────────────────────────────────────────────────
def test_sanity_mode_keeps_quiet_no_kill_gameplay_and_pro_flags_go_to_the_judge():
    jankos = {"vlm_gameplay": True, "kills_max": 1, "kill_frames": 0, "audio_score": 0.29}
    decide(dict(jankos), {"mode": "gates"})
    gated = dict(jankos); decide(gated, {"mode": "gates"})
    assert gated["decision"] == "REJECT"
    s = dict(jankos); decide(s, {"mode": "sanity"})
    assert s["decision"] == "KEEP"
    pro = {"vlm_gameplay": True, "vlm_pro_play": True}
    decide(pro, {"mode": "sanity"})
    assert pro["decision"] == "KEEP" and pro["reason"].startswith("PRO_UI?")
    nog = {"vlm_gameplay": False}
    decide(nog, {"mode": "sanity"})
    assert nog["decision"] == "REJECT"


# ── judge facts, cap, fillers ────────────────────────────────────────────────
AJ = {"hard_facts": True, "min_entertainment": 6, "min_play_quality": 7,
      "filler_min_entertainment": 5, "filler_min_play": 4, "filler_focus": ["gameplay"]}


def J(i, ent, pq, who="a", focus="gameplay", **facts):
    return {"id": i, "broadcaster_name": who, "duration": 30, "api_focus": focus,
            "api_entertainment": ent, "api_play_quality": pq, **facts}


def test_wow_and_spectating_are_dropped_even_with_high_scores():
    wow = J("w", 8, 8, api_is_league_game=False)
    spec = J("s", 8, 8, api_streamer_playing=False)
    pro = J("p", 9, 9, api_pro_broadcast=True)
    ok = J("o", 8, 8, api_is_league_game=True, api_streamer_playing=True)
    for c in (wow, spec, pro, ok):
        decide_api(c, AJ)
    assert [c["api_decision"] for c in (wow, spec, pro, ok)] == ["DROP"] * 3 + ["KEEP"]
    assert wow["api_reason"].endswith("NOT_LEAGUE")
    assert hard_fail(J("old", 8, 8), AJ) == ""                # v4 verdicts: no facts
    assert hard_fail(wow, {**AJ, "hard_facts": False}) == ""


def test_streamer_cap_and_stricter_fillers():
    kept = [J(str(i), 9, 9, who="baus") for i in range(4)] + [J("x", 8, 8, who="b")]
    assert len(streamer_cap(kept, 2)) == 3
    judged = [J("f1", 5, 4), J("f2", 5, 3), J("f3", 4, 9), J("f4", 6, 5, focus="reaction"),
              J("f5", 6, 6, api_is_league_game=False)]
    for c in judged:
        c["api_decision"], c["api_reason"], c["api_rank_score"] = "DROP", "x", 5
    out, _ = select(judged, [], {"target_minutes_ideal": 8, "target_minutes_max": 11}, AJ)
    assert [c["id"] for c in out] == ["f1"]                 # only ent>=5, pq>=4, gameplay, League


# ── captions ─────────────────────────────────────────────────────────────────
def test_invented_sign_off_is_dropped_but_real_speech_stays():
    w = [{"word": x, "start": i, "end": i + 0.4} for i, x in
         enumerate("NO WAY! Thank you so much for watching, see you in the next one, bye!".split())]
    assert [x["word"] for x in drop_hallucinations(w)] == ["NO", "WAY!"]
    keep = [{"word": x, "start": i, "end": i + 0.4} for i, x in enumerate("he was watching me".split())]
    assert drop_hallucinations(keep) == keep


def test_masked_words():
    assert mask_text("literally AIDS man", ["aids"]) == "literally A*** man"
    assert mask_text("he aidsn't", ["aids"]) == "he aidsn't"
    assert mask_text("nothing", []) == "nothing"


def test_a_freshly_scored_clip_gets_a_status():
    c = {"audio_score": 0.6, "motion_score": 0.05}
    restatus(c, PF)
    assert c["prefilter_status"] == "AUDIO_PASS"


def test_asian_language_clip_needs_a_real_outplay():
    aj = {**AJ, "outplay_only_languages": ["ja", "zh"], "outplay_min_play": 7}
    death = J("d", 8, 4, language="ja")
    outplay = J("o", 8, 8, language="ja")
    english = J("e", 8, 4, language="en")
    for c in (death, outplay, english):
        decide_api(c, aj)
    assert death["api_decision"] == "DROP" and death["api_reason"].endswith("LANG_NEEDS_OUTPLAY")
    assert outplay["api_decision"] == "KEEP" and english["api_decision"] == "KEEP"
