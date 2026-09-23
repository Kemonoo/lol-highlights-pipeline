"""Review queue: every fetched clip is traced to the gate that stopped it, and sampling
covers every gate. Pure — built from an in-memory day, no files, no server."""
from pipeline.tools.review_queue import GATES, plain_vlm, sample, stats, trace

CFG = {"prefilter": {"include_languages": ["en"], "audio_exclude": 0.22,
                     "motion_exclude": 0.015},
       "blacklist": {"broadcasters": ["evtchannel"]}}


def _clip(cid, lang="en", who="s", title="t", views=10):
    return {"id": cid, "language": lang, "broadcaster_name": who, "title": title,
            "view_count": views, "duration": 30}


def _day():
    raw = [_clip("bl", who="EvtChannel"), _clip("ja", lang="ja"),
           _clip("quiet"), _clip("still"), _clip("cap"), _clip("nogame"), _clip("pro"),
           _clip("weak"), _clip("vcap"), _clip("jdrop"), _clip("trim"), _clip("pick"),
           _clip("nofile")]
    pf = {c: {"prefilter_status": "AUDIO_PASS", "audio_score": 0.6, "motion_score": 0.05}
          for c in ("cap", "nogame", "pro", "weak", "vcap", "jdrop", "trim", "pick")}
    pf["quiet"] = {"prefilter_status": "AUDIO_EXCLUDE", "audio_score": 0.1, "motion_score": 0.05}
    pf["still"] = {"prefilter_status": "MOTION_EXCLUDE", "audio_score": 0.4, "motion_score": 0.01}
    pf["nofile"] = {"prefilter_status": "NO_FILE"}
    vlm = {"nogame": {"id": "nogame", "decision": "REJECT", "reason": "NO_GAMEPLAY_0of3"},
           "pro": {"id": "pro", "decision": "REJECT", "reason": "PRO_PLAY_UI"},
           "weak": {"id": "weak", "decision": "REJECT", "reason": "UNCONFIRMED_k1x1_a0.10"}}
    for c in ("vcap", "jdrop", "trim", "pick"):
        vlm[c] = {"id": c, "decision": "KEEP", "reason": "HYPE_ONLY_0.6", "keep_score": 0.5}
    judged = {
        "jdrop": {"id": "jdrop", "api_decision": "DROP", "api_reason": "gameplay_ent2_pq2",
                  "api_what_happens": "nothing"},
        "trim": {"id": "trim", "api_decision": "DROP", "api_reason": "gameplay_ent7_pq7_OVER_COUNT"},
        "pick": {"id": "pick", "api_decision": "KEEP", "api_reason": "gameplay_ent9_pq9",
                 "api_what_happens": "a pentakill", "countdown_rank": 1},
    }
    return {"raw": raw, "prefilter": pf,
            "prefiltered": ["nogame", "pro", "weak", "vcap", "jdrop", "trim", "pick"],
            "vlm": vlm, "judged": judged, "final": {"pick": judged["pick"]}}


def test_trace_assigns_every_gate():
    recs = {r["id"]: r for r in trace(_day(), CFG, "2026-09-21")}
    assert {k: r["gate"] for k, r in recs.items()} == {
        "bl": "blacklist", "ja": "language", "quiet": "quiet", "still": "static",
        "cap": "rank_cap", "nogame": "vlm_no_gameplay", "pro": "vlm_pro",
        "weak": "vlm_weak", "vcap": "vlm_cap", "jdrop": "judge_drop",
        "trim": "trimmed", "pick": "selected"}
    assert "nofile" not in recs                     # never measured -> nothing to review
    assert recs["cap"]["claim"] is None             # a rank cut makes no claim to check
    assert "a pentakill" in recs["pick"]["claim"]
    assert recs["pick"]["path"][-1] == {"step": "In the video", "ok": True, "detail": "#1"}


def test_sample_covers_gates_skips_reviewed_and_is_stable():
    recs = trace(_day(), CFG, "2026-09-21")
    a = sample(recs, {"pick"}, per_gate=1, n_random=0, seed="d")
    assert "pick" not in {r["id"] for r in a}
    assert {r["gate"] for r in a} == {r["gate"] for r in recs} - {"selected"}
    assert [r["id"] for r in a] == [r["id"] for r in sample(recs, {"pick"}, 1, 0, "d")]


def test_stats_counts_claims_and_quality():
    reviews = {"a": {"gate": "quiet", "claim_ok": True, "quality": "good"},
               "b": {"gate": "quiet", "claim_ok": False, "quality": "bad"},
               "c": {"gate": "quiet", "claim_ok": None, "quality": "good"}}
    (row,) = stats(reviews)
    assert (row["n"], row["claim_true"], row["claim_n"], row["good"]) == (3, 1, 2, 2)
    assert row["label"] == GATES["quiet"][1]


def test_plain_vlm():
    assert plain_vlm("NO_GAMEPLAY_1of3") == "not gameplay (1 of 3 frames said gameplay)"
    assert plain_vlm("HYPE_ONLY_0.63").startswith("loud reaction")
