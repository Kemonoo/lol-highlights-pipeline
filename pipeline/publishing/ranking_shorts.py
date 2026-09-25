"""Ranking Shorts: "TOP 5 PENTAKILLS", "TOP 5 OUTPLAYS", "TOP 5 YASUO PLAYS", ...

Owner request (2026-09-25): besides the per-clip Shorts, post the classic countdown
format — the best N moments of one category, #N down to #1 — built from every clip the
channel has ever published. One of the daily Short slots is given to it
(`ranking_shorts.replace_slots`), so the daily count stays the same and the formats can
be compared by views (each Short's format is recorded in shorts/done.json).

Source of clips: data/clip_log.jsonl (publishing/clip_log.py — one line per published
clip with the Twitch link, all filter and judge data), plus today's clips, which the
clip_log stage only appends after the Shorts stage. Nothing is stored long-term: a clip
whose raw file is gone is re-downloaded from Twitch (checked 2026-09-25: clips from
June still download, ~5 s each) and deleted with the day's work folder.

Categories are config (`ranking_shorts.categories`): a label and a regex over the clip's
text (Twitch title + the judge's description + VLM summary + banners read). The clip log
has no champion field, so champion categories match the judge's text. Clips are ranked by
the judge's `api_rank_score`; a clip is never used in two rankings; at most
`max_per_streamer` clips of one streamer per ranking. The category of the day is the one
used longest ago that still has enough unused clips.

State: data/ranking_shorts.json {"used": [clip ids], "history": [{date, category, ...}]}
— one ranking per date (idempotent: the bat's crash-retry never posts two).
"""
import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("pipeline.ranking_shorts")

STATE_FILE = "ranking_shorts.json"


# ── pure selection (tested) ───────────────────────────────────────────────────

def clip_text(row: dict) -> str:
    f = row.get("filter") or {}
    parts = [row.get("twitch_title"), f.get("api_what_happens"), f.get("vlm_summary"),
             " ".join(str(a) for a in (f.get("announcements") or []))]
    return " ".join(str(p) for p in parts if p).lower()


def score(row: dict) -> float:
    return float((row.get("filter") or {}).get("api_rank_score") or 0.0)


def eligible(rows: list[dict], category: dict, used: set, min_score: float = 0.0,
             max_per_streamer: int = 2) -> list[dict]:
    """Unused clips matching the category, best first, capped per streamer."""
    pat = re.compile(category["pattern"], re.I)
    seen: set = set()
    out: list = []
    per: dict = {}
    for r in sorted(rows, key=score, reverse=True):
        cid = r.get("clip_id")
        if not cid or cid in used or cid in seen or score(r) < min_score:
            continue
        if not pat.search(clip_text(r)):
            continue
        who = (r.get("broadcaster") or "").lower()
        if per.get(who, 0) >= max_per_streamer:
            continue
        seen.add(cid)
        per[who] = per.get(who, 0) + 1
        out.append(r)
    return out


def pick_category(categories: list[dict], rows: list[dict], used: set, history: list,
                  n: int, min_score: float = 0.0, max_per_streamer: int = 2) -> dict | None:
    """The category used longest ago (never = first) that still has n unused clips."""
    last = {h.get("category"): i for i, h in enumerate(history)}
    ranked = sorted(categories, key=lambda c: last.get(c["key"], -1))
    for c in ranked:
        if len(eligible(rows, c, used, min_score, max_per_streamer)) >= n:
            return c
    return None


def window(duration: float, best_s: float | None, length: float, pre: float) -> tuple:
    """(start, length) of the segment: `pre` seconds of build-up before the judge's best
    moment, clamped inside the clip."""
    length = min(length, duration)
    peak = best_s if best_s and 0 < best_s < duration else duration / 2
    start = min(max(0.0, peak - pre), max(0.0, duration - length))
    return round(start, 2), round(length, 2)


def title_for(category: dict, n: int, template: str) -> str:
    return template.format(n=n, label=category["label"].lower(),
                           Label=category["label"].title(),
                           LABEL=category["label"].upper())[:95]


def description_for(category: dict, picks: list[dict], n: int) -> str:
    lines = [f"Top {n} {category['label'].title()} from the Twitch streams we featured.", ""]
    for rank, r in zip(range(n, 0, -1), picks):
        lines.append(f"#{rank} {r.get('broadcaster', '?')} — {r.get('broadcaster_url', '')}"
                     f"  (clip: {r.get('clip_url', '')})")
    lines += ["", "Every clip belongs to its streamer — go follow them!",
              "#LeagueOfLegends #LoL #Shorts #TwitchClips"]
    return "\n".join(lines)


# ── files ─────────────────────────────────────────────────────────────────────

def _load_state(data: Path) -> dict:
    p = data / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8").rstrip("\x00"))
        except ValueError:
            pass
    return {"used": [], "history": []}


def _save_state(data: Path, st: dict) -> None:
    (data / STATE_FILE).write_text(json.dumps(st, indent=1, ensure_ascii=False),
                                   encoding="utf-8")


def _pool(cfg: dict, state, date_label: str) -> list[dict]:
    """Every published clip: the clip log, plus today's (not logged until after Shorts)."""
    data = Path(cfg["paths"]["data_abs"])
    rows: dict = {}
    log_path = data / "clip_log.jsonl"
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").rstrip("\x00").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rows[(r.get("date"), r.get("clip_id"))] = r
    try:
        from .clip_log import entries_for_date
        for r in entries_for_date(cfg, state, date_label):
            rows[(r.get("date"), r.get("clip_id"))] = r
    except Exception as e:
        log.debug("ranking: today's clips not added (%s)", e)
    return list(rows.values())


def _source(cfg: dict, row: dict, work: Path) -> Path | None:
    """The clip's video: today's raw file if it still exists, else a fresh download."""
    from ..ingestion.fetch import download_clip
    data = Path(cfg["paths"]["data_abs"])
    raw = data / "raw" / str(row.get("date")) / f"{row['clip_id']}.mp4"
    if raw.exists():
        return raw
    dest = work / "ranking" / "src" / f"{row['clip_id']}.mp4"
    if dest.exists() or download_clip(row.get("clip_url", ""), dest, "best"):
        return dest
    log.info("  ranking: %s is gone from Twitch - skipping", row["clip_id"])
    return None


def _probe(mp4: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(mp4)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ── render ────────────────────────────────────────────────────────────────────

RANK_COLORS = {1: "0xFFFFFF", 2: "0xFFE600", 3: "0xFF9A00", 4: "0xFF5A1F", 5: "0xFF2A2A"}
HEADER_H = 300


def list_rows(n: int, current: int, names: dict) -> list[tuple]:
    """The side counter for the segment of rank `current`: every number 1..n, and the
    name only for ranks already revealed (the countdown runs n -> 1, so ranks >= current).
    -> [(rank, label or "")]. Pure (tested)."""
    return [(r, names.get(r, "") if r >= current else "") for r in range(1, n + 1)]


def _segment(cfg: dict, row: dict, src: Path, rank: int, title: tuple, names: dict,
             n: int, work: Path, rc: dict) -> Path | None:
    """One countdown entry in the owner's reference layout (2026-09-25, the "Ranking
    Cutest Golden Retriever Moments" Shorts): a black title band on top, the clip below
    (webcam split or zoomed centre, as regular Shorts), and a coloured 1..n counter down
    the left edge where each streamer's name appears, small, as their clip plays and
    then stays."""
    from ..enrichment.streamer_cam import find_all
    from . import shorts as S

    seg_dir = work / "ranking"
    seg_dir.mkdir(parents=True, exist_ok=True)
    dur = _probe(src)
    if dur <= 1:
        return None
    start, length = window(dur, (row.get("filter") or {}).get("api_best_moment_s"),
                           float(rc.get("segment_seconds", 10)), float(rc.get("pre_roll_s", 6)))
    cut = seg_dir / f"cut_{rank}.mp4"
    subprocess.run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}",
                    "-c", "copy", str(cut)], capture_output=True)
    if not cut.exists():
        return None
    body_h = S.TARGET_H - HEADER_H
    cams = find_all(cfg, cut, length) if cfg.get("shorts", {}).get("detect_facecam", True) else []
    game_h = int(body_h * 0.58) if cams else body_h
    layout = seg_dir / f"layout_{rank}.mp4"
    try:
        S._render_short(cut, layout, cams or None, [], 0, game_h, cfg, total_h=body_h)
    except subprocess.CalledProcessError as e:
        log.warning("  ranking: render failed for #%d: %s", rank,
                    (e.stderr or b"")[-300:].decode(errors="replace"))
        return None

    font = S._cap_font()
    vf = [f"pad={S.TARGET_W}:{S.TARGET_H}:0:{HEADER_H}:black"]
    for i, line in enumerate(title):
        t = S._clean_overlay(line)
        vf.append(f"drawtext=fontfile='{font}':text='{t}':fontsize=84:fontcolor=white:"
                  f"borderw=7:bordercolor=black:x=(w-text_w)/2:y={52 + i * 108}")
    y0, step = HEADER_H + 170, 112
    for i, (r, name) in enumerate(list_rows(n, rank, names)):
        y = y0 + i * step
        vf.append(f"drawtext=fontfile='{font}':text='{r}.':fontsize=96:"
                  f"fontcolor={RANK_COLORS.get(r, '0xFFFFFF')}:borderw=7:bordercolor=black:"
                  f"x=34:y={y}")
        if name:
            nm = S._clean_overlay(name)[:18]
            vf.append(f"drawtext=fontfile='{font}':text='{nm}':fontsize=44:fontcolor=white:"
                      f"borderw=5:bordercolor=black:x=150:y={y + 30}")
    out = seg_dir / f"seg_{rank}.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(layout), "-vf", ",".join(vf) + ",fps=30",
         *S._shorts_enc(cfg), "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
         str(out)], capture_output=True)
    if r.returncode != 0 or not out.exists():
        log.warning("  ranking: overlay failed for #%d: %s", rank,
                    r.stderr[-300:].decode(errors="replace"))
        return None
    return out


def _concat(segs: list[Path], out: Path) -> bool:
    lst = out.parent / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(out)], capture_output=True)
    return r.returncode == 0 and out.exists()


# ── entry (called from shorts.run) ────────────────────────────────────────────

def make(cfg: dict, state, date_label: str, upload_fn=None) -> dict | None:
    """Build (and upload, when Shorts upload) today's ranking Short.
    Returns a done.json entry, or None when there is nothing to post."""
    rc = cfg.get("ranking_shorts", {})
    if not rc.get("enabled", False):
        return None
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    st = _load_state(data)
    if any(h.get("date") == date_label for h in st["history"]):
        log.info("ranking short for %s already made - skip", date_label)
        return None

    n = int(rc.get("count", 5))
    min_score = float(rc.get("min_score", 6.0))
    per = int(rc.get("max_per_streamer", 2))
    used = set(st["used"])
    rows = _pool(cfg, state, date_label)
    cat = pick_category(rc.get("categories", []), rows, used, st["history"], n, min_score, per)
    if cat is None:
        log.info("ranking: no category has %d unused clips (score >= %.1f)", n, min_score)
        return None
    cands = eligible(rows, cat, used, min_score, per)
    log.info("ranking short: TOP %d %s (%d candidates)", n, cat["label"], len(cands))

    title = (f"Ranking Top {n}", cat["label"].title())
    picks: list[dict] = []
    segs: list[Path] = []
    # best n that still download; rendered worst-first so the video counts down to #1
    chosen = []
    for r in cands:
        src = _source(cfg, r, work)
        if src:
            chosen.append((r, src))
        if len(chosen) == n:
            break
    if len(chosen) < n:
        log.info("ranking: only %d of %d clips could be fetched - skip", len(chosen), n)
        return None
    from . import shorts as S
    names = {rank: S._ascii_name({"broadcaster_name": r.get("broadcaster", "")})
             for rank, (r, _) in zip(range(1, n + 1), chosen)}
    for rank, (r, src) in zip(range(n, 0, -1), reversed(chosen)):
        seg = _segment(cfg, r, src, rank, title, names, n, work, rc)
        if seg is None:
            log.warning("ranking: segment #%d failed - skip the ranking today", rank)
            return None
        segs.append(seg)
        picks.append(r)

    out = work / "shorts" / f"ranking_{cat['key']}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not _concat(segs, out):
        log.warning("ranking: concat failed")
        return None
    log.info("  ranking short rendered -> %s (%.0f s)", out.name, _probe(out))

    entry = {"format": f"ranking:{cat['key']}", "rendered": str(out),
             "clips": [r["clip_id"] for r in picks]}
    if upload_fn is not None:
        title = title_for(cat, n, rc.get("title", "Ranking Top {n} {Label} | League of Legends #Shorts"))
        desc = description_for(cat, picks, n)
        tags = ["league of legends", "lol", "shorts", "top 5", cat["label"].lower(),
                "twitch clips"] + [str(r.get("broadcaster", "")).lower() for r in picks]
        vid = upload_fn(out, title, desc, tags)
        if vid:
            entry.update(youtube_id=vid, url=f"https://youtube.com/shorts/{vid}", title=title)
            log.info("  ranking short uploaded: %s", entry["url"])
    st["used"] = sorted(used | set(entry["clips"]))
    st["history"].append({"date": date_label, "category": cat["key"],
                          "clips": entry["clips"], "youtube_id": entry.get("youtube_id")})
    _save_state(data, st)
    return entry
