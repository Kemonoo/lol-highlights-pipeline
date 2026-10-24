"""Post-run health check: log parsing, warning grouping, flags (pure)."""
from collections import Counter

from pipeline.tools.health import (expectations, flags, last_run, normalise, parse_run,
                                   render, warnings_in)

LOG = """\
==== run started 23-Oct-26  3:00:02.08 ====
==== run finished 23-Oct-26  3:10:00.00 (exit 1) ====
==== run started 24-Oct-26  3:00:12.57 ====
==== dates: 2026-10-23 ====
03:00:37 pipeline               INFO    [feedback] starting
03:55:33 pipeline               INFO    [api_judge] starting
04:11:40 pipeline.api_judge     WARNING judge: switching to gemini-3.8-flash (its own daily allowance)
04:45:16 pipeline.api_judge     INFO    API judge: 50 -> 23 kept (50 judged by API, 0 local)
04:45:16 pipeline.api_judge     INFO    Selection: 23 clips, 9.4 min
04:47:25 pipeline.transcribe    WARNING transcribe: GPU worker crashed on RoundClearSalmonFunRun-HtraaVSIvrK9pHto.mp4 (exit 3221225620) - redoing that clip on cpu/int8
04:48:14 pipeline.transcribe    WARNING transcribe failed for ProudAliveMetalOhMyDog-1QmGBzXhvwsXF31z: UnicodeEncodeError: 'charmap' codec can't encode character '\\u30ae' in position 77: character maps to <undefined>
[download] 100% of    5.87MiB in 00:00:01 at 4.73MiB/s     03:52:06 pipeline.providers.ollama WARNING qwen3-vl:4b returned empty output under a schema - retrying without one
04:58:44 pipeline.assemble      INFO    Assembled 2026-10-23.mp4 (9.4 min, 23 clips)
05:40:39 pipeline.upload        INFO    Uploaded 2026-10-23.mp4 as https://youtu.be/c51vquXQidA (public)
05:44:11 pipeline.shorts        INFO      uploaded: https://youtube.com/shorts/420n4hpvRbg
05:45:54 pipeline.shorts        INFO      uploaded: https://youtube.com/shorts/zOcG8j0lK2Q
05:50:44 pipeline.ranking_shorts INFO      ranking short uploaded: https://youtube.com/shorts/qqFi36O5d-o
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: bad thing 42
==== run finished 24-Oct-26  5:50:52.60 (exit 0) ====
"""

EXPECT = {"upload": True, "shorts": 3}
HC = {"max_hours": 4, "min_minutes": 6}


def run():
    return parse_run(last_run(LOG))


def test_last_run_block_only():
    r = run()
    assert r["exit"] == 0 and r["dates"] == ["2026-10-23"]
    assert r["duration_s"] == 2 * 3600 + 50 * 60 + 40


def test_parses_outputs():
    r = run()
    assert r["uploads"] == {"2026-10-23": "https://youtu.be/c51vquXQidA"}
    assert len(r["shorts"]) == 2 and len(r["ranking"]) == 1
    assert r["assembled"]["2026-10-23"] == (9.4, 23)
    assert r["selection"] == [(23, 9.4)]
    assert "api_judge" in r["stages"]
    assert r["transcribe_failures"] == 1 and r["judge_local"] == 0


def test_warning_grouping_strips_ids_and_numbers():
    a = normalise("transcribe: GPU worker crashed on RoundClearSalmonFunRun-HtraaVSIvrK9pHto.mp4 (exit 3221225620)")
    b = normalise("transcribe: GPU worker crashed on WittyPatientPassionfruitRuleFive-3-Wy4LsuKOK5vk_B.mp4 (exit 1)")
    assert a == b == "transcribe: GPU worker crashed on <clip> (exit N)"
    assert normalise("see https://ai.dev/rate-limit now") == "see <url> now"
    assert "qwen3-vl:4b" in normalise("qwen3-vl:4b returned empty output")


def test_warnings_counted_incl_mid_line_and_traceback():
    counts, _ = warnings_in(last_run(LOG))
    assert counts["transcribe: GPU worker crashed on <clip> (exit N) - redoing that clip on cpu/int8"] == 1
    assert any(t.startswith("providers.ollama: qwen3-vl:4b") for t in counts)
    assert "traceback: ValueError: bad thing N" in counts
    assert sum(counts.values()) == 5


def test_new_warning_type_flag():
    r = run()
    seen = set(r["warnings"])
    assert flags(r, EXPECT, HC, seen) == ["1 clip(s) failed to transcribe (published without captions)"]
    unicode_t = next(t for t in seen if "UnicodeEncodeError" in t)
    fl = flags(r, EXPECT, HC, seen - {unicode_t})
    assert f"new warning type: {unicode_t}" in fl


def test_failure_flags():
    r = run()
    r.update(exit=None, finished=None, uploads={}, shorts=[], duration_s=5 * 3600,
             assembled={"2026-10-23": (4.0, 6)}, judge_local=3, transcribe_failures=0)
    fl = flags(r, EXPECT, HC, set(r["warnings"]))
    assert any("never reached" in f for f in fl)
    assert "no video uploaded for 2026-10-23" in fl
    assert "only 1 of 3 Shorts uploaded" in fl
    assert any(f.startswith("run took 5.0 h") for f in fl)
    assert any("only 4.0 min" in f for f in fl)
    assert any("local_judge" in f for f in fl)


def test_nonzero_exit_and_quiet_nights():
    r = parse_run("==== run started x  3:00:00 ====\n==== nothing to do - yesterday is "
                  "already finished ====\n==== run finished x  3:00:05 (exit 2) ====\n")
    assert flags(r, EXPECT, HC, set()) == ["run exited with code 2"]
    p = parse_run("==== run started x  9:00:00 ====\n==== postponed to the next "
                  "scheduled run x ====\n")
    assert p["postponed"] and flags(p, EXPECT, HC, set()) == []


def test_expectations_follow_config():
    assert expectations({"upload": {"enabled": False}, "shorts": {"count": 3, "upload": True}}) \
        == {"upload": False, "shorts": 0}
    assert expectations({"upload": {"enabled": True}, "shorts": {"count": 3, "upload": True}}) \
        == {"upload": True, "shorts": 3}


def test_render_mentions_link_title_and_flags():
    r = run()
    text = render("2026-10-24", r, ["x"], {"2026-10-23": "Some Title #60"})
    assert "https://youtu.be/c51vquXQidA" in text and "Some Title #60" in text
    assert "## Flags" in text and "1 flag(s)" in text
    r["warnings"] = Counter()
    assert "- OK" in render("2026-10-24", r, [], {})
