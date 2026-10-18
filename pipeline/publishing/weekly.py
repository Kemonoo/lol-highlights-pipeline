"""Weekly compilation: "Best League Plays of the Week #N" — a second long video, once a week.

Owner request (2026-10-18): views of the daily video collapsed to 1-10; besides the daily,
publish one compilation a week built from the best clips the channel already published
that week. The daily video is unchanged; this is a second upload that day, scheduled at
least `upload.min_gap_hours` after it (upload.publish_slot does the spacing).

Runs as the stage `weekly` after clip_log: it no-ops unless the date being rendered is the
last day of the week (`weekly.weekday` = the day the compilation goes out, i.e. the night
run that renders the day before it). So no .bat change - the nightly run builds it. By hand:

    python -m pipeline.publishing.weekly --week-ending 2026-10-17 [--config ...] [--no-upload]

Source: data/clip_log.jsonl (one line per published clip with the judge's data). Raw MP4s
are deleted after a day, so the clips are re-downloaded from Twitch (as ranking_shorts
does). The compilation is rendered by the regular stages under the label
`weekly_<week-ending>`: work/weekly_<d>/vlm_filtered.json is written here, then
transcribe -> assemble -> metadata -> thumbnail -> upload run on it, so it gets the same
transitions, nameplates, countdown badges, captions and trim as the daily, and the
mandatory per-streamer credits. Captions and burned-caption verdicts are copied from the
source days' work folders, so whisper only runs for a clip whose cache is gone.

Idempotent: the selection is frozen in vlm_filtered.json (a retry never re-picks), each
step skips when its output exists, and upload.run refuses a label already in state.json.
Ledger data/weekly.json {"used": [clip ids], "history": [{week_ending, ...}]}: written on
upload only — a clip is never in two compilations, and `#N` counts uploaded weeks.
"""
import argparse
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("pipeline.weekly")

STATE_FILE = "weekly.json"
PREFIX = "weekly_"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ── pure selection (tested) ───────────────────────────────────────────────────

def label_for(week_ending: str) -> str:
    return f"{PREFIX}{week_ending}"


def is_last_day(date_label: str, weekday: str) -> bool:
    """True when `date_label` is the day before `weekday` (the compilation goes out the
    morning after the week's last daily is rendered)."""
    if not _DATE.fullmatch(date_label or ""):
        return False
    wd = _WEEKDAYS.index(str(weekday).lower())
    return (date.fromisoformat(date_label) + timedelta(days=1)).weekday() == wd


def week_dates(week_ending: str, days: int = 7) -> list[str]:
    end = date.fromisoformat(week_ending)
    return [(end - timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]


def score(row: dict) -> tuple:
    """Judge rank score, then entertainment + play quality as the tie-break."""
    f = row.get("filter") or {}
    return (float(f.get("api_rank_score") or 0.0),
            float(f.get("api_entertainment") or 0) + float(f.get("api_play_quality") or 0))


def _category(c: dict) -> dict:
    from .ranking_shorts import clip_text
    pat = re.compile(c["pattern"], re.I)
    return {"key": c["key"], "label": c["label"],
            "pred": lambda r: bool(pat.search(clip_text(r)))}


def _champion(name: str) -> dict:
    from .ranking_shorts import clip_text
    pat = re.compile(r"\b" + re.escape(name) + r"\b", re.I)
    label = " ".join(w[:1].upper() + w[1:] for w in name.split())
    return {"key": f"champion:{name.lower()}", "label": f"{label} Plays",
            "pred": lambda r: bool(pat.search(clip_text(r)))}


def _streamer(who: str) -> dict:
    return {"key": f"streamer:{who.lower()}", "label": f"{who} Plays", "streamer": True,
            "pred": lambda r: (r.get("broadcaster") or "").lower() == who.lower()}


def theme_filter(theme: str, categories: list[dict]) -> dict | None:
    """One fixed `weekly.theme` -> {key, label, pred[, streamer]}, or None for "best".
    "<category key>" (ranking_shorts.categories), "streamer:<login>", "champion:<name>"."""
    theme = (theme or "best").strip()
    if theme.lower() == "best":
        return None
    if theme.lower().startswith("streamer:"):
        return _streamer(theme.split(":", 1)[1].strip())
    if theme.lower().startswith("champion:"):
        return _champion(theme.split(":", 1)[1].strip())
    for c in categories or []:
        if c.get("key") == theme:
            return _category(c)
    raise ValueError(f"weekly.theme {theme!r}: not 'best', 'rotate', a ranking_shorts "
                     f"category key, 'streamer:<login>' or 'champion:<name>'")


def all_themes(categories: list[dict], champions: list[str], streamers: list[str]) -> list:
    """Every theme `rotate` may pick, in preference order: the regex categories, then
    one per champion name, then one per streamer."""
    return ([_category(c) for c in categories or []] + [_champion(c) for c in champions]
            + [_streamer(s) for s in streamers])


def pick_theme(themes: list[dict], history: list[dict], qualifies) -> dict | None:
    """The theme used longest ago (never = first, then list order) for which
    `qualifies(theme)` returns a selection; -> {**theme, "picks": selection} or None."""
    last = {h.get("theme_key"): i for i, h in enumerate(history or [])}
    for t in sorted(themes, key=lambda t: last.get(t["key"], -1)):
        picks = qualifies(t)
        if picks:
            return {**t, "picks": picks}
    return None


def select(rows: list[dict], dates: list[str], used: set, *, length, min_score: float = 6.0,
           max_per_streamer: int = 3, target_s: float = 540, max_s: float = 630,
           max_clips: int = 20, moment_window_s: float = 90, predicate=None) -> list[dict]:
    """The week's best clips, in COUNTDOWN order (weakest first, #1 last).

    rows: clip_log lines; only those dated inside `dates`, not `used`, scoring at least
    `min_score` (and matching `predicate`) count. One clip per moment (same VOD within
    `moment_window_s`, filtering/moments.same_moment) and per clip id (a clip can be in
    two dailies). Best-first fill until `target_s` of (trimmed) `length(row)`; a clip
    that would push past `max_s` is passed over for a shorter one."""
    from ..filtering.moments import same_moment
    days = set(dates)
    pool = sorted((r for r in rows
                   if r.get("date") in days and r.get("clip_id")
                   and r["clip_id"] not in used and score(r)[0] >= min_score
                   and (predicate is None or predicate(r))),
                  key=lambda r: (score(r), r.get("date", "")), reverse=True)
    picked: list = []
    ids: set = set()
    per: dict = {}
    total = 0.0
    for r in pool:
        if total >= target_s or len(picked) >= max_clips:
            break
        if r["clip_id"] in ids:
            continue
        who = (r.get("broadcaster") or "").lower()
        if max_per_streamer and per.get(who, 0) >= max_per_streamer:
            continue
        f = r.get("filter") or {}
        if any(same_moment(f, p.get("filter") or {}, moment_window_s) for p in picked):
            continue
        n = float(length(r))
        if total + n > max_s:
            continue
        picked.append(r)
        ids.add(r["clip_id"])
        per[who] = per.get(who, 0) + 1
        total += n
    return sorted(picked, key=score)


def to_clips(picks: list[dict]) -> list[dict]:
    """vlm_filtered.json rows for assemble: the logged filter data (it IS the daily's clip
    row) with a fresh countdown rank n..1. Pure."""
    out = []
    n = len(picks)
    for i, r in enumerate(picks):
        c = dict(r.get("filter") or {})
        c.setdefault("id", r["clip_id"])
        c.setdefault("url", r.get("clip_url"))
        c.setdefault("broadcaster_name", r.get("broadcaster"))
        c.setdefault("title", r.get("twitch_title") or "")
        c.pop("local_path", None)
        c["countdown_rank"] = n - i
        c["weekly_source_date"] = r.get("date")
        c["weekly_source_video"] = r.get("video_youtube_id")
        out.append(c)
    return out


def title_for(template: str, episode: int, n: int, label: str = "") -> str:
    return re.sub(r"\s{2,}", " ", template.format(episode=episode, n=n, label=label.lower(),
                                                  Label=label,
                                                  LABEL=label.upper())).strip()[:100]


DEFAULT_THEME_TITLE = "Top {n} {Label} of the Week"


def pick_title(templates: list, episode: int) -> str:
    """Hook templates rotate by episode, so consecutive weeks never share one. Pure."""
    return templates[(int(episode) - 1) % len(templates)]


def thumb_text(wc: dict, n: int, label: str = "") -> tuple[str, str]:
    """(shouted word, tag) for the grid thumbnail: ("PENTAKILL", "x10") for a theme,
    ("BEST PLAYS", "TOP 19") for the plain best-of. Pure."""
    from ..production.thumbnail import thumb_word
    tag = wc.get("theme_thumb_tag", "x{n}") if label else wc.get("thumb_tag", "TOP {n}")
    return thumb_word(label), str(tag).format(n=n)


def description_head(n: int, label: str, sources: list[str]) -> list[str]:
    what = f"the best {label.lower()}" if label else "the best League of Legends plays"
    lines = [f"🏆 Top {n}: {what} of the week, counted down to #1. Which one deserved "
             f"#1? Drop the number in the comments 👇 — and SUBSCRIBE for daily LoL "
             f"highlights!"]
    if sources:
        lines += ["", "From this week's daily videos:"] + [f"https://youtu.be/{v}"
                                                           for v in sources]
    return lines


# ── files ─────────────────────────────────────────────────────────────────────

def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8").rstrip("\x00"))
    except (OSError, ValueError):
        return default


def _load_state(data: Path) -> dict:
    st = _read(data / STATE_FILE) or {}
    return {"used": st.get("used", []), "history": st.get("history", [])}


def _save_state(data: Path, st: dict) -> None:
    (data / STATE_FILE).write_text(json.dumps(st, indent=1, ensure_ascii=False),
                                   encoding="utf-8")


def _rows(data: Path) -> list[dict]:
    p = data / "clip_log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").rstrip("\x00").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _seed_caches(data: Path, work: Path, clips: list[dict]) -> None:
    """Copy each clip's transcript + burned-caption verdict from its source day."""
    from ..enrichment import burned_captions
    for name in ("transcripts.json", burned_captions.CACHE):
        mine = _read(work / name, {}) or {}
        added = 0
        for c in clips:
            if c["id"] in mine:
                continue
            src = _read(data / "work" / str(c.get("weekly_source_date")) / name, {}) or {}
            if c["id"] in src:
                mine[c["id"]] = src[c["id"]]
                added += 1
        if added:
            (work / name).write_text(json.dumps(mine, ensure_ascii=False), encoding="utf-8")
            log.info("  weekly: %d cached %s entries reused", added, name)


def _download(cfg: dict, label: str, clips: list[dict]) -> list[dict]:
    """Full-quality copies into raw/<label>/; clips gone from Twitch are dropped."""
    from ..ingestion.fetch import download_clip
    raw = Path(cfg["paths"]["data_abs"]) / "raw" / label
    keep = []
    for c in clips:
        dest = raw / f"{c['id']}.mp4"
        if dest.exists() or download_clip(c.get("url", ""), dest, "best"):
            keep.append(c)
        else:
            log.info("  weekly: %s is gone from Twitch - dropped", c["id"])
    return keep


def _length_fn(cfg: dict):
    from ..production.trim import length
    v = cfg.get("video", {})
    return lambda r: length(r.get("filter") or {}, v)


def _champions(data: Path) -> list[str]:
    """Champion names (lower case) from the thumbnail stage's Data Dragon cache."""
    names = _read(data / "cache" / "ddragon_champions.json", {}) or {}
    return sorted(n for n in names if len(n) > 2)


def plan(cfg: dict, week_ending: str, history: list | None = None) -> tuple[list[dict], dict]:
    """(countdown-ordered vlm_filtered rows, theme {key, label} - empty for best-of)."""
    wc = cfg.get("weekly", {})
    data = Path(cfg["paths"]["data_abs"])
    rows = _rows(data)
    st = _load_state(data)
    used = set(st["used"])
    dates = week_dates(week_ending, int(wc.get("days", 7)))
    kw = dict(length=_length_fn(cfg), min_score=float(wc.get("min_score", 6.0)),
              max_per_streamer=int(wc.get("max_per_streamer", 3)),
              target_s=float(wc.get("target_minutes", 9)) * 60,
              max_s=float(wc.get("max_minutes", 10.5)) * 60,
              max_clips=int(wc.get("max_clips", 20)),
              moment_window_s=float(cfg.get("prefilter", {}).get("moment_window_s", 90)))
    tkw = {**kw, "min_score": float(wc.get("theme_min_score", 7.0)),
           "max_clips": int(wc.get("theme_max_clips", 10)), "target_s": kw["max_s"]}
    need = int(wc.get("theme_min_clips", 8))
    # A theme QUALIFIES on its strong clips (>= theme_min_score); the video then fills
    # toward theme_target_minutes with clips >= theme_fill_score (owner 2026-10-18: ~8 min,
    # the strict 10-clip pentakill week was 5.6). Defaults keep the strict-only behaviour.
    fill = {**tkw, "min_score": float(wc.get("theme_fill_score", tkw["min_score"]))}
    if wc.get("theme_target_minutes"):
        fill["target_s"] = float(wc["theme_target_minutes"]) * 60

    def qualifies(t: dict) -> list:
        k = {**tkw, "max_per_streamer": 0} if t.get("streamer") else tkw
        if len(select(rows, dates, used, predicate=t["pred"], **k)) < need:
            return []
        f = {**fill, "max_per_streamer": 0} if t.get("streamer") else fill
        return select(rows, dates, used, predicate=t["pred"], **f)

    cats = wc.get("categories") or cfg.get("ranking_shorts", {}).get("categories", [])
    mode = str(wc.get("theme", "rotate")).strip()
    theme = None
    if mode.lower() == "rotate":
        week = {r.get("broadcaster") for r in rows if r.get("date") in set(dates)}
        themes = all_themes(cats, _champions(data) if wc.get("champion_themes", True) else [],
                            sorted(w for w in week if w) if wc.get("streamer_themes", True)
                            else [])
        theme = pick_theme(themes, st["history"] if history is None else history,
                           qualifies)
    else:
        t = theme_filter(mode, cats)
        if t:
            picks = qualifies(t)
            theme = {**t, "picks": picks} if picks else None
    if theme:
        return to_clips(theme["picks"]), {"key": theme["key"], "label": theme["label"]}
    log.info("weekly: no theme has %d clips >= %.1f this week - best of the week", need,
             tkw["min_score"])
    return to_clips(select(rows, dates, used, **kw)), {}


# ── entry ─────────────────────────────────────────────────────────────────────

def build(cfg: dict, state, week_ending: str, upload: bool = True) -> Path | None:
    """Select, render and (when allowed) upload the compilation for the week ending
    `week_ending`. Returns the master's path, or None when there is nothing to build."""
    from ..enrichment import transcribe
    from ..production import assemble, credits, thumbnail
    from . import upload as _upload

    wc = cfg.get("weekly", {})
    data = Path(cfg["paths"]["data_abs"])
    label = label_for(week_ending)
    work = data / "work" / label
    work.mkdir(parents=True, exist_ok=True)
    out = data / "output" / f"{label}.mp4"
    meta_f = data / "output" / f"{label}.meta.json"
    sel_f = work / "vlm_filtered.json"

    if state is not None and state.uploaded_id(label):
        log.info("weekly %s already uploaded as %s - skip", label, state.uploaded_id(label))
        return out

    sel = _read(sel_f)
    if sel is None:
        clips, theme = plan(cfg, week_ending)
        min_clips = int(wc.get("min_clips", 6))
        clips = _download(cfg, label, clips)
        if len(clips) < min_clips:
            log.info("weekly: only %d clips for the week ending %s (min %d) - no "
                     "compilation", len(clips), week_ending, min_clips)
            return None
        n = len(clips)
        for i, c in enumerate(clips):                  # re-rank after any drops
            c["countdown_rank"] = n - i
        sel = {"clips": clips, "theme": theme, "week_ending": week_ending}
        sel_f.write_text(json.dumps(sel, indent=1, ensure_ascii=False), encoding="utf-8")
        log.info("weekly %s: %d clips%s, %.1f min before trim", label, n,
                 f" ({theme['label']})" if theme else "",
                 sum(float(c.get("duration", 30)) for c in clips) / 60)
    clips, theme = sel["clips"], sel.get("theme") or {}
    tl = theme.get("label", "")
    _download(cfg, label, clips)                       # a retry after raw cleanup

    _seed_caches(data, work, clips)
    if not (work / "transcripts.done.json").exists():
        transcribe.run(cfg, state, label)
    if not out.exists():
        assemble.run(cfg, state, label)
    st = _load_state(data)
    if not meta_f.exists():
        chapters = json.loads((work / "chapters.json").read_text(encoding="utf-8")
                              .rstrip("\x00"))
        episode = len(st["history"]) + 1
        tmpl = pick_title(wc.get("theme_titles") or [DEFAULT_THEME_TITLE], episode) if tl \
            else wc.get("title", "Best League Plays of the Week #{episode}")
        meta = credits.build_metadata(cfg, label, chapters, clips, episode)
        meta["title"] = title_for(tmpl, episode, len(chapters), tl)
        sources = list(dict.fromkeys(c["weekly_source_video"] for c in clips
                                     if c.get("weekly_source_video")))
        body = meta["description"].split("\n", 1)[1]
        meta["description"] = "\n".join(description_head(len(chapters), tl, sources)) \
            + "\n" + body
        meta["tags"] = (["lol best plays of the week", "league of legends weekly"]
                        + meta["tags"])[:30]
        meta["weekly"] = {"episode": episode, "theme": theme, "week_ending": week_ending}
        meta_f.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("weekly metadata: %s", meta["title"])
    meta = _read(meta_f) or {}
    if not (work / "thumbnail.jpg").exists():
        word, tag = thumb_text(wc, len(clips), tl)
        if not thumbnail.generate_weekly(cfg, label, word, tag):
            thumbnail.run(cfg, state, label)        # no frame at all: the daily style

    if not (upload and wc.get("upload", False)):
        log.info("weekly: upload off (weekly.upload) - %s left in data/output/", out.name)
        return out
    _upload.run(cfg, state, label)
    vid = state.uploaded_id(label) if state is not None else None
    if vid:
        st["used"] = sorted(set(st["used"]) | {c["id"] for c in clips})
        if not any(h.get("week_ending") == week_ending for h in st["history"]):
            st["history"].append({"week_ending": week_ending, "label": label,
                                  "theme_key": theme.get("key"), "theme": tl,
                                  "clips": [c["id"] for c in clips],
                                  "episode": (meta.get("weekly") or {}).get("episode"),
                                  "youtube_id": vid})
        _save_state(data, st)
    return out


def run(cfg: dict, state, date_label: str) -> None:
    """Stage: build the compilation on the week's last day; no-op on every other day."""
    wc = cfg.get("weekly", {})
    if not wc.get("enabled", False):
        return
    if not is_last_day(date_label, wc.get("weekday", "sunday")):
        log.info("weekly: not the week's last day (%s) - nothing to do", date_label)
        return
    try:
        build(cfg, state, date_label)
    except Exception as e:      # the daily is already out; never fail the night for this
        log.warning("weekly compilation failed (re-run with python -m "
                    "pipeline.publishing.weekly --week-ending %s): %s", date_label, e)


if __name__ == "__main__":
    from ..config import load_config
    from ..hardware import apply_limits
    from ..state import State

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week-ending", required=True, help="last day of the week, YYYY-MM-DD")
    ap.add_argument("--config", default="", help="overlay yaml(s), comma-separated")
    ap.add_argument("--no-upload", action="store_true", help="render only")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)-22s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config(overlay=a.config or None)
    apply_limits(cfg)
    st = State(cfg["paths"]["data_abs"])
    build(cfg, st, a.week_ending, upload=not a.no_upload)
