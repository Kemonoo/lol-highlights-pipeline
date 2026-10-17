"""Claude pre-screen - orders the judge's queue by what is IN the clip (owner, 2026-10-18).

Gemini's free allowance buys ~50 judged clips a night, and with `vlm_filter.mode: sanity`
whichever 50 rank highest IS the choice. That rank was views/keyword/moment-cluster
signals only, and it lost good low-view clips below the cut (10-11's replay: Burrito
ent 8, slimeIRL ent 7). Here the `prescreen` role (default: Claude through the owner's
subscription, providers/claude_code.py) looks at an 8-frame contact sheet of EVERY
gameplay candidate plus its title, and scores its highlight potential 0-10. It has no
audio; Gemini still watches the top of the queue with sound and makes every decision.

Called from api_judge before the queue is cut - not a stage of its own, since its only
output is an ORDER. Scores are cached per clip (`prescreen_v1.json`); failures are not,
and after `max_failures` in a row (usage limit, logged out) it stops for the night and the
signals order stands. Nothing here can drop a clip.
"""
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("pipeline.prescreen")

SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "score": {"type": "number"},
                       "note": {"type": "string"}},
        "required": ["id", "score", "note"]}}},
    "required": ["clips"],
}

SYSTEM = ("You pre-screen Twitch clips for a daily League of Legends highlights video. "
          "Answer only with the requested JSON.")

PROMPT = """Each image is one clip: 8 frames in time order (left to right, top to bottom), evenly spread over the clip. There is no audio.

Score each clip 0-10 for how likely it is a highlight worth a spot in a best-of video:
- 8-10: a visible outplay, multikill, clutch escape, big teamfight won, or a spectacular fail mid-fight
- 5-7: real fighting with a likely payoff, but unclear from frames
- 2-4: laning, farming, walking, a single unremarkable trade
- 0-1: not live League gameplay: menus, champion select, shop, death screen most of the clip, a desktop, someone watching a pro broadcast or another stream

The title is written by a viewer and is a hint, not proof. The video is muted on your side, so a talk/reaction clip can still be funny: do not score it above 5 on frames alone.
Return one entry per clip, using the id shown, with a note of at most 12 words.

Clips:
{clips}"""


def sheet(mp4: Path, out: Path, n: int = 8, width: int = 480) -> bytes | None:
    """8 evenly spaced frames tiled 4x2 into one JPEG (cached next to the thumbs)."""
    if out.exists():
        return out.read_bytes()
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0",
             str(mp4)], capture_output=True, text=True, timeout=30).stdout.strip() or 0)
        fps = n / max(dur, 1.0)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-vf",
                        f"fps={fps:.4f},scale={width}:-2,tile=4x2", "-frames:v", "1",
                        "-q:v", "5", str(out)], capture_output=True, timeout=120, check=True)
        return out.read_bytes()
    except Exception as e:
        log.warning("contact sheet failed for %s: %s", mp4.name, e)
        return None


def describe(c: dict, i: int) -> str:
    return (f'[image {i}] id={c["id"]} | streamer {c.get("broadcaster_name", "?")} | '
            f'{float(c.get("duration", 0)):.0f}s | {c.get("view_count", 0)} views | '
            f'title: "{(c.get("title") or "")[:90]}"')


def queue_score(c: dict, weight: float) -> float:
    """Judge-queue order. Pure. Pre-screened clips blend Claude's 0-10 with the signals
    keep_score (0-1); an unscored clip keeps its keep_score, so a failed night is the old
    order rather than a shuffled one."""
    ks = float(c.get("keep_score", 0) or 0)
    ps = c.get("prescreen_score")
    if ps is None:
        return ks
    return round(weight * float(ps) / 10 + (1 - weight) * ks, 4)


def order(cfg: dict, work: Path, raw_dir: Path, clips: list[dict]) -> list[dict]:
    """Annotate clips with prescreen_score/prescreen_note and return them in queue order."""
    ps = cfg.get("prescreen", {}) or {}
    if not ps.get("enabled", False):
        return clips
    cache_path = work / "prescreen_v1.json"
    cache = (json.loads(cache_path.read_text(encoding="utf-8").rstrip("\x00"))
             if cache_path.exists() else {})
    todo = [c for c in clips if c["id"] not in cache]

    provider = None
    if todo:
        from ..providers import ProviderUnavailable, get_provider
        try:
            provider = get_provider(cfg, "prescreen", vision=True)
            provider.require(images=True)
        except ProviderUnavailable as e:
            log.warning("prescreen unavailable (%s) - judge queue keeps the signals order", e)
            provider = None

    sheets = work / "prescreen_sheets"
    sheets.mkdir(exist_ok=True)
    batch_n = max(1, int(ps.get("batch", 5)))
    fails, max_fails = 0, int(ps.get("max_failures", 2))
    for b in range(0, len(todo) if provider else 0, batch_n):
        batch = []
        for c in todo[b:b + batch_n]:
            mp4 = raw_dir / f"{c['id']}.mp4"
            if not mp4.exists():
                mp4 = raw_dir / "lq" / f"{c['id']}.mp4"
            img = sheet(mp4, sheets / f"{c['id']}.jpg") if mp4.exists() else None
            if img:
                batch.append((c, img))
        if not batch:
            continue
        text = "\n".join(describe(c, i + 1) for i, (c, _) in enumerate(batch))
        try:
            out = provider.complete_json(PROMPT.format(clips=text), schema=SCHEMA,
                                         system=SYSTEM, images=[img for _, img in batch])
            got = {str(x.get("id")): x for x in (out or {}).get("clips", [])}
        except Exception as e:
            fails += 1
            log.warning("prescreen batch failed (%d/%d): %s", fails, max_fails, e)
            if fails >= max_fails:
                log.warning("prescreen: stopping for tonight - the rest keep the signals order")
                break
            continue
        fails = 0
        for c, _ in batch:
            x = got.get(c["id"])
            if x is None:
                continue
            cache[c["id"]] = {"score": max(0.0, min(10.0, float(x.get("score", 0)))),
                              "note": str(x.get("note", ""))[:120]}
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        log.info("prescreen: %d/%d scored", len([c for c in clips if c["id"] in cache]),
                 len(clips))

    for c in clips:
        if c["id"] in cache:
            c["prescreen_score"] = cache[c["id"]]["score"]
            c["prescreen_note"] = cache[c["id"]]["note"]
    w = float(ps.get("weight", 0.3))
    return sorted(clips, key=lambda c: -queue_score(c, w))
