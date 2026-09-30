"""GPU isolation: a crashing whisper child costs one clip, not the run.

The worker is a stand-in script speaking the same RESULT/ERROR line protocol, so this
needs no GPU, no model and no ffmpeg - only a Python subprocess."""
import sys

from pipeline.enrichment.transcribe import Transcriber

# Dies natively (hard exit, no Python exception) on any audio named "poison*",
# answers ERROR for "bad*", else echoes a result.
FAKE = r'''
import json, os, sys
for line in sys.stdin:
    a = json.loads(line)["audio"]
    name = os.path.basename(a)
    if name.startswith("poison"):
        os._exit(127)
    if name.startswith("bad"):
        print("ERROR ValueError: unreadable", flush=True); continue
    print("noise a library printed", flush=True)
    print("RESULT " + json.dumps({"lang": "en", "lang_prob": 1.0, "text": name,
                                  "words": [], "pid": os.getpid()}), flush=True)
'''
CFG = {"transcribe": {"device": "cuda", "compute_type": "int8_float16",
                      "max_gpu_crashes": 2}}


def _t(cfg=CFG):
    cpu = []
    t = Transcriber(cfg, worker_cmd=[sys.executable, "-c", FAKE],
                    fallback=lambda audio, max_s: cpu.append(str(audio)) or
                    {"lang": "en", "lang_prob": 1.0, "text": "cpu", "words": []})
    return t, cpu


def test_crash_redoes_that_clip_on_cpu_and_keeps_the_gpu_for_the_rest():
    t, cpu = _t()
    with t:
        a = t("a.mp4", 60)
        assert a["text"] == "a.mp4"
        assert t("poison.mp4", 60)["text"] == "cpu" and cpu == ["poison.mp4"]
        b = t("b.mp4", 60)
        assert b["text"] == "b.mp4" and b["pid"] != a["pid"]    # a fresh child
    assert t.crashes == 1 and t.isolate and t.used == "cuda"


def test_too_many_crashes_moves_the_rest_to_cpu():
    t, cpu = _t()
    with t:
        t("poison1.mp4")
        t("poison2.mp4")
        assert not t.isolate and t.device == "cpu" and t.used == "cpu"
    assert cpu == ["poison1.mp4", "poison2.mp4"]


def test_python_error_in_the_child_is_an_exception_not_a_crash():
    t, cpu = _t()
    with t:
        try:
            t("bad.mp4")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "unreadable" in str(e)
        assert t("c.mp4")["text"] == "c.mp4"
    assert t.crashes == 0 and cpu == []


def test_cpu_device_never_spawns_a_worker():
    t, _ = _t({"transcribe": {"device": "cpu"}})
    assert not t.isolate and t._proc is None
    t2, _ = _t({"transcribe": {"device": "cuda", "gpu_isolation": False}})
    assert not t2.isolate
