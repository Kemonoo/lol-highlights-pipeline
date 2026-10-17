"""Cut a clip to the judge's action window (`video.trim`, owner 2026-10-18).

The judge records where the play a viewer needs starts and ends (api_action_start_s /
api_action_end_s, api_judge v5). A 54 s Baus clip on 10-12 had its play at 8-18 s and
the rest was dead time. Pure: assemble cuts the file, api_judge.select counts the
trimmed length toward the video's target minutes.
"""


def window(clip: dict, v: dict) -> tuple[float, float] | None:
    """(start, end) seconds to keep, or None to keep the whole clip."""
    t = v.get("trim", {}) or {}
    if not t.get("enabled", False):
        return None
    dur = float(clip.get("duration", 0) or 0)
    a, b = clip.get("api_action_start_s"), clip.get("api_action_end_s")
    if not dur or a is None or b is None:
        return None
    a, b = float(a), float(b)
    if b <= a or b <= 0:                 # judge left the default 0/0: no window known
        return None
    start = max(0.0, a - float(t.get("pad_before_s", 3.0)))
    end = min(dur, b + float(t.get("pad_after_s", 3.0)))
    if start < float(t.get("min_head_cut_s", 10)):
        start = 0.0                      # setup is worth keeping unless it is long
    if dur - end < float(t.get("min_cut_s", 6)):
        end = dur
    min_len = float(t.get("min_len_s", 10))
    if end - start < min_len:            # too short: grow the tail first (the reaction)
        end = min(dur, start + min_len)
        start = max(0.0, end - min_len)
    if start == 0.0 and end == dur:
        return None
    return round(start, 2), round(end, 2)


def length(clip: dict, v: dict) -> float:
    """The clip's length in the video, after trimming."""
    w = window(clip, v)
    return (w[1] - w[0]) if w else float(clip.get("duration", 30) or 30)


def shift_words(words: list | None, start: float, end: float) -> list | None:
    """Caption words moved onto the trimmed clip's timeline; words outside are dropped."""
    if not words:
        return words
    out = []
    for w in words:
        s, e = float(w.get("start", 0)), float(w.get("end", 0))
        if e <= start or s >= end:
            continue
        out.append({**w, "start": max(0.0, s - start), "end": min(end, e) - start})
    return out
