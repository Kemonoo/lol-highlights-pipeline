"""Kill detection from visual cues — v3 (after auditing real crops).

Audit findings (2026-06-09 run): streamer overlays (Twitch chat columns, donation bars,
scoreboards) routinely sit in the old kill-feed region and a 4B model counted chat rows
as "kill banners". Fixes:
  1. Regions measured from a real 1080p screenshot (user-provided).
  2. Prompts describe the exact visual pattern (portrait-sword-portrait) and explicitly
     exclude chat/scoreboard rows.
  3. NEW event-log reader: bottom-left text like "X (Karthus) has slain Y (Neeko) for a
     double kill!" — multi-language keyword matching.
  4. Confirmation logic lives in vlm_filter: one isolated kill-feed count is treated as
     noise; kills need >=2 positive crops OR an announcement/event-log hit.

Crops are saved to work/<date>/crops/ — keep checking them to calibrate regions.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.kill_detect")

KILLFEED_PROMPT = """This image is a crop of the upper-right area of a League of Legends gameplay frame, where kill notifications appear.
A kill notification banner has EXACTLY this pattern: a small dark horizontal box containing [square champion portrait] [sword/weapon icon] [square champion portrait], with a red or blue border.
Do NOT count: rows of plain text (stream chat), usernames, donation overlays, scoreboard rows (champion portrait followed by item icons or numbers), the team score (e.g. "32 vs 24"), timers, or webcams.
Answer ONLY JSON: {"kill_banners": 0}   // count of portrait-sword-portrait banners only"""

BANNER_PROMPT = """This image is a crop of the upper-middle of a League of Legends gameplay frame. When a player gets a multikill, large styled announcement text appears here between two champion portraits, e.g. "DOUBLE KILL!", "TRIPLE KILL!", "QUADRA KILL!", "PENTAKILL!", or "ACE!" (possibly in another language: ダブルキル, 더블킬, 双杀...).
Stream overlays or plain chat text do NOT count.
Answer ONLY JSON: {"announcement": "none"}   // the announcement text you read, or "none" """

EVENTLOG_PROMPT = """This image is a crop of the bottom-left of a League of Legends gameplay frame, where the game event log prints messages like:
"HA0 (Karthus) has slain Yoyo (Neeko) for a double kill!" / "X is on a rampage!" / "X has shut down Y!"
Transcribe the event messages you can read (any language). Ignore player chat banter.
Answer ONLY JSON: {"messages": []}   // list of strings, [] if none readable"""

# Multi-language keyword nets (intentionally wide; validated against labels later).
MULTIKILL_WORDS = (
    "double kill", "triple kill", "quadra", "penta", "ace",
    "ダブルキル", "トリプルキル", "クアドラ", "ペンタ",
    "더블킬", "트리플킬", "쿼드라", "펜타",
    "双杀", "三杀", "四杀", "五杀", "雙殺", "三連殺", "四連殺", "五連殺",
)
KILL_WORDS = MULTIKILL_WORDS + (
    "has slain", "slain", "shut down", "rampage", "killing spree", "unstoppable",
    "キル", "処刑", "처치", "击杀", "擊殺", "終結",
)


def probe_duration(mp4: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def extract_crop(mp4: Path, t: float, region: list[float], width: int = 512) -> bytes | None:
    """One cropped JPEG at time t. Region = (x1, y1, x2, y2) frame fractions."""
    x1, y1, x2, y2 = region
    vf = (f"crop=iw*{x2 - x1:.3f}:ih*{y2 - y1:.3f}:iw*{x1:.3f}:ih*{y1:.3f},"
          f"scale={width}:-1")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "c.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(mp4),
             "-frames:v", "1", "-q:v", "3", "-vf", vf, str(out)],
            capture_output=True,
        )
        return out.read_bytes() if out.exists() else None


def _contains(texts: list[str], words: tuple) -> bool:
    return any(w in t for t in texts for w in words)


def analyze(mp4: Path, ask, vf_cfg: dict, crops_dir: Path | None = None) -> dict:
    """Scan a clip for kill cues. ask(images, prompt) -> dict | None.

    Returns {kills_max, kill_frames, announcements, eventlog, multikill, eventlog_kill}.
    """
    dur = probe_duration(mp4)
    every = vf_cfg.get("killfeed_every_s", 5)
    regions = vf_cfg["regions"]
    times = list(range(2, max(int(dur) - 1, 3), every)) or [max(dur / 2, 1)]

    kf_counts: list[int] = []
    announcements: list[str] = []
    eventlog: list[str] = []

    def save(name: str, data: bytes):
        if crops_dir is not None:
            (crops_dir / name).write_bytes(data)

    for i, t in enumerate(times):
        crop = extract_crop(mp4, t, regions["killfeed"])
        if crop:
            save(f"{mp4.stem}_kf_{int(t)}s.jpg", crop)
            r = ask([crop], KILLFEED_PROMPT)
            try:
                kf_counts.append(max(0, int(r.get("kill_banners", 0))) if r else 0)
            except (ValueError, TypeError):
                kf_counts.append(0)

        if i % 2 == 0:
            bcrop = extract_crop(mp4, t, regions["banner"])
            if bcrop:
                save(f"{mp4.stem}_bn_{int(t)}s.jpg", bcrop)
                rb = ask([bcrop], BANNER_PROMPT)
                a = str(rb.get("announcement", "none")).strip().lower() if rb else "none"
                if a and a not in ("none", "null", ""):
                    announcements.append(a)

            ecrop = extract_crop(mp4, t, regions["eventlog"], width=640)
            if ecrop:
                save(f"{mp4.stem}_ev_{int(t)}s.jpg", ecrop)
                re_ = ask([ecrop], EVENTLOG_PROMPT)
                if re_ and isinstance(re_.get("messages"), list):
                    eventlog.extend(str(m).lower() for m in re_["messages"][:6])

        # short-circuit: once kills are confirmed (>=2 positive crops, a multikill
        # announcement, or kill-feed + event-log agreement) further sampling can't
        # change the decision — stop spending model calls (~30-50% fewer calls/clip)
        confirmed = (sum(1 for k in kf_counts if k > 0) >= 2
                     or _contains(announcements, MULTIKILL_WORDS)
                     or _contains(eventlog, MULTIKILL_WORDS)
                     or (max(kf_counts, default=0) >= 1
                         and _contains(eventlog, KILL_WORDS)))
        if confirmed:
            log.debug("%s kills confirmed at t=%ss — stopping early", mp4.stem, t)
            break

    multikill = (_contains(announcements, MULTIKILL_WORDS)
                 or _contains(eventlog, MULTIKILL_WORDS))
    return {
        "kills_max": max(kf_counts, default=0),
        "kill_frames": sum(1 for c in kf_counts if c > 0),
        "announcements": announcements[:5],
        "eventlog": eventlog[:8],
        "eventlog_kill": _contains(eventlog, KILL_WORDS),
        "multikill": multikill,
    }
