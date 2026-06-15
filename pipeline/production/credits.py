"""Stage 9 — YouTube title, description with credits + chapters, v2.

Title hook is derived from the best clip's API judgment (e.g. a penta -> "PENTAKILL"),
falling back to a generic hook. Music attribution from config is appended when set.

Output: data/output/<date>.meta.json  {title, description, tags}
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("pipeline.credits")


def _ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _hook(clips: list[dict]) -> str:
    """Short title hook from the best clip's content."""
    best = max(clips, key=lambda c: c.get("api_rank_score", 0), default=None) \
        if clips else None
    text = ((best.get("vlm_summary", "") + " " + best.get("title", "")).lower()
            if best else "")
    for word, hook in (("penta", "PENTAKILL"), ("quadra", "QUADRA KILL"),
                       ("triple kill", "TRIPLE KILL"), ("steal", "INSANE STEAL"),
                       ("outplay", "CRAZY OUTPLAYS"), ("1v", "OUTNUMBERED OUTPLAYS")):
        if word in text:
            return hook
    return "BEST PLAYS"


def build_metadata(cfg: dict, date_label: str, chapters: list[dict],
                   clips: list[dict]) -> dict:
    title = cfg["upload"]["title_template"].format(
        date=date_label, hook=_hook(clips))

    lines = ["The best League of Legends moments from yesterday's streams, "
             "with commentary. Which number was your favorite? Tell us in the "
             "comments — and call out any clip that didn't deserve its spot.",
             "", "Chapters & streamers — go follow them:"]
    for ch in chapters:
        n = f"#{ch['rank']} " if ch.get("rank") else ""
        lines.append(f"{_ts(ch['start'])} {n}{ch['broadcaster']} — {ch['broadcaster_url']}")
    lines += ["", "All clips credited to their creators on Twitch. "
              "Contact us for credit changes or removal requests."]
    attribution = cfg.get("video", {}).get("music_attribution", "")
    if attribution:
        lines += ["", attribution]

    streamers = sorted({ch["broadcaster"] for ch in chapters if ch["broadcaster"]})
    tags = ["league of legends", "lol highlights", "lol best moments", "outplays",
            "twitch moments", *[s.lower() for s in streamers[:15]]]
    return {"title": title, "description": "\n".join(lines), "tags": tags[:30]}


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    chapters = json.loads((work / "chapters.json").read_text(encoding="utf-8")
                          .rstrip("\x00"))
    src = work / "vlm_filtered.json"
    clips = (json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
             if src.exists() else [])
    meta = build_metadata(cfg, date_label, chapters, clips)
    out = data / "output" / f"{date_label}.meta.json"
    out.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Metadata: %s", meta["title"])
    return out
