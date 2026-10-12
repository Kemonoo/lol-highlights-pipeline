"""Same moment, several clips — and other free candidate signals (owner, 2026-10-12).

Viewers clip the same play independently, each with their own clip id and title. On
2026-10-07 six of the top seven countdown slots were two Baus moments (VOD offsets
9186/9190/9192 and 10987/10989/10998), and the 10-07 #1 came back as #1 on 10-09. Twitch
gives every clip its `video_id` (the stream's VOD) and `vod_offset` (seconds into it), so
"same moment" is two clips of one VOD within `window_s` of each other.

The size of such a cluster is also the best cheap quality signal found: several people
clipping the same moment means chat thought it was worth it (judged clips in a cluster
averaged entertainment 7.2 vs 5.4, 10-06..10-11). A clip its own streamer made
(creator == broadcaster) is a weaker positive.

Pure functions; prefilter applies them before anything is downloaded.
"""
import json
from datetime import date as _date, timedelta
from pathlib import Path


def _key(c: dict):
    vid, off = c.get("video_id"), c.get("vod_offset")
    if not vid or off is None:
        return None
    return str(vid), float(off)


def cluster(clips: list[dict], window_s: float = 90) -> list[list[dict]]:
    """Group clips of the same VOD within `window_s` (chained: a-b-c with gaps under the
    window are one moment). Clips without VOD info are each their own group."""
    by_vod: dict[str, list[dict]] = {}
    groups: list[list[dict]] = []
    for c in clips:
        k = _key(c)
        if k is None:
            groups.append([c])
        else:
            by_vod.setdefault(k[0], []).append(c)
    for vod in by_vod.values():
        vod.sort(key=lambda c: float(c["vod_offset"]))
        g = [vod[0]]
        for c in vod[1:]:
            if float(c["vod_offset"]) - float(g[-1]["vod_offset"]) <= window_s:
                g.append(c)
            else:
                groups.append(g)
                g = [c]
        groups.append(g)
    return groups


def representative(group: list[dict]) -> dict:
    """The clip that stands for a moment: most views, then longest (more setup)."""
    return max(group, key=lambda c: (c.get("view_count", 0), c.get("duration", 0)))


def same_moment(a: dict, b: dict, window_s: float = 90) -> bool:
    ka, kb = _key(a), _key(b)
    return bool(ka and kb and ka[0] == kb[0] and abs(ka[1] - kb[1]) <= window_s)


def dedupe(clips: list[dict], published: list[dict], window_s: float = 90) -> list[dict]:
    """Annotate every clip with `moment_size`, `self_clip`; mark duplicates.

    Returns the clips to keep: one representative per moment, minus moments already
    published within the lookback (`published`: clip dicts with video_id/vod_offset).
    Dropped clips get `prefilter_status` DUPLICATE_MOMENT / ALREADY_PUBLISHED."""
    keep = []
    for g in cluster(clips, window_s):
        rep = representative(g)
        for c in g:
            c["moment_size"] = len(g)
            c["self_clip"] = bool(c.get("creator_id") and
                                  c.get("creator_id") == c.get("broadcaster_id"))
            if c is not rep:
                c["prefilter_status"] = "DUPLICATE_MOMENT"
                c["duplicate_of"] = rep.get("id")
        rep["self_clip"] = any(c["self_clip"] for c in g)   # any clipper being the streamer
        if any(same_moment(rep, p, window_s) for p in published):
            for c in g:
                c["prefilter_status"] = "ALREADY_PUBLISHED"
            continue
        keep.append(rep)
    return keep


def published_clips(data: Path, date_label: str, days: int) -> list[dict]:
    """Clip dicts (with video_id/vod_offset) of every clip PUBLISHED in the `days`
    before date_label: chapters.json says which, raw/<date>/clips.json has the VOD info
    (it survives the MP4 cleanup)."""
    out = []
    d0 = _date.fromisoformat(date_label)
    for k in range(1, max(int(days), 0) + 1):
        d = (d0 - timedelta(days=k)).isoformat()
        ch, raw = data / "work" / d / "chapters.json", data / "raw" / d / "clips.json"
        if not (ch.exists() and raw.exists()):
            continue
        try:
            used = {c["clip_id"] for c in json.loads(
                ch.read_text(encoding="utf-8").rstrip("\x00"))}
            r = json.loads(raw.read_text(encoding="utf-8").rstrip("\x00"))
        except (ValueError, KeyError):
            continue
        out += [c for c in (r.get("clips", r) if isinstance(r, dict) else r)
                if c.get("id") in used]
    return out


def view_percentile(clips: list[dict]) -> None:
    """`view_pct` 0..1: the clip's view rank within the day (1 = most viewed). Raw view
    counts mean little across days; the rank within one day's pool is comparable."""
    order = sorted(clips, key=lambda c: c.get("view_count", 0))
    n = max(len(order) - 1, 1)
    for i, c in enumerate(order):
        c["view_pct"] = round(i / n, 3)


def rank_score(c: dict, audio_weight: float = 0.10) -> float:
    """Candidate order before any model has seen the clip (prefilter `rank: signals`).
    Loudness is at most a small term (`prefilter.audio_weight`; the owner overlay sets
    0): among judged clips it correlated NEGATIVELY with the judge (-0.19, 10-06..10-11),
    the quiet gate dropped clips the owner rated good, and a "perfect" KatEvolved
    base-teamfight outplay (10-10) has the streamer silent the whole time."""
    return round(0.30 * bool(c.get("keyword"))
                 + 0.25 * min(int(c.get("moment_size", 1)) - 1, 3) / 3
                 + 0.15 * bool(c.get("self_clip"))
                 + 0.20 * float(c.get("view_pct", 0))
                 + audio_weight * min(float(c.get("audio_score", 0)), 1.0), 3)
