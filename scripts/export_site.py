"""Refresh the project website (site/) from one finished night of the pipeline.

    ./venv/Scripts/python.exe scripts/export_site.py --date 2026-09-21
    ./venv/Scripts/python.exe scripts/export_site.py --date 2026-09-21 --dry-run

The site narrates one real night. Everything night-specific is exported from that
night's own files, so nothing is typed in by hand:

  * numbers and times  <- the run log (data/logs/auto_*.log containing "Prefilter <date>")
  * funnel tiles/data  <- work/<date>/vlm_scored.json, api_partial_v4.json (v3 before 09-24), chapters.json
  * stills and loop    <- output/<date>.mp4 at chapter times, raw/<date>/<id>.mp4,
                          work/<date>/crops, thumbnail*.jpg, shorts/
  * text               <- elements marked data-n="key" in site/index.html are rewritten
                          in place, so the page stays plain static HTML (no build step)

Run it the MORNING AFTER the night: raw MP4s are deleted after a day
(cleanup.keep_raw_days), and the filmstrip/waveform need the raw clip. Anything whose
source is missing is skipped with a warning and the old asset stays.

Choices a person used to make by eye are now rules, overridable by flags:
  hero clip      = countdown #2 (--hero-rank). #1 is often a reaction; #2 is usually play.
  loop           = 9 s from 1.0 s into that clip (the name card has finished animating)
  caption still  = first English clip, without a source caption, that has a 3+ word phrase
  translation    = first non-English clip whose source carries its own burned caption
  VLM crops      = banner: first crop at/after the judge's best moment; kill feed: the
                   crop with the most detail (--crop-time to force one)

Afterwards: look at the page (docs/SITE.md, "Working on the site"), then update the
ledger and changelog in docs/SITE.md.
"""
import argparse
import html
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import date as Date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE = ROOT / "site"
ASSETS = SITE / "assets"
STAGES = ["fetch", "prefilter", "vlm_filter", "api_judge", "transcribe", "assemble",
          "credits", "upload", "shorts", "cleanup"]
LANGS = {"pl": "Polish", "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
         "it": "Italian", "ru": "Russian", "cs": "Czech", "sk": "Slovak", "ko": "Korean",
         "ja": "Japanese", "tr": "Turkish", "nl": "Dutch", "sv": "Swedish", "uk": "Ukrainian"}
DRY = False


def warn(msg):
    print(f"  ! {msg}")


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8").rstrip("\x00"))


def ff(*args):
    if DRY:
        return
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


# ---------------------------------------------------------------- the run log

def find_log(date_label):
    needle = f"Prefilter {date_label}:"
    for p in sorted((DATA / "logs").glob("auto_*.log"), reverse=True):
        if needle in p.read_text(encoding="utf-8", errors="replace"):
            return p
    sys.exit(f"no log in data/logs mentions '{needle}' — did that night run?")


def parse_log(text):
    def num(pattern, *groups):
        m = None
        for m in re.finditer(pattern, text):
            pass                                     # last match = the attempt that finished
        return tuple(float(m.group(g)) if "." in m.group(g) else int(m.group(g))
                     for g in groups) if m else None

    n = {}
    pf = num(r"Prefilter \S+: (\d+) scored -> (\d+) passed -> (\d+) kept", 1, 2, 3)
    vf = num(r"Filter: (\d+) -> (\d+) kept", 1, 2)
    aj = num(r"API judge: (\d+) -> (\d+) kept", 1, 2)
    asm = num(r"Assembled \S+ \(([\d.]+) min, (\d+) clips\)", 1, 2)
    if not (pf and vf and aj and asm):
        sys.exit("log is missing a stage summary line (prefilter/filter/judge/assemble)")
    n["scored"], n["passed"], n["kept"] = pf
    n["vlm_kept"] = vf[1]
    n["final"] = asm[1]
    n["minutes"] = asm[0]
    n["switches"] = len(re.findall(r"judge: switching to", text))

    starts = {}
    for m in re.finditer(r"(\d\d:\d\d:\d\d) pipeline\s+INFO\s+\[(\w+)\] starting", text):
        starts[m.group(2)] = m.group(1)             # last attempt wins
    first = re.search(r"(\d\d:\d\d:\d\d) pipeline", text)
    starts["doctor"] = first.group(1) if first else starts.get("feedback", "")
    done = list(re.finditer(r"run finished \S+\s+(\d+):(\d\d):(\d\d)[.\d]*\s+\(exit (\d+)\)", text))
    if done:
        h, mnt, s, code = done[-1].groups()
        n["finish"], n["exit"] = f"{int(h):02d}:{mnt}:{s}", code
    else:
        n["finish"], n["exit"] = starts.get("cleanup", ""), "?"
    n["starts"] = starts
    return n


def secs(hms):
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


def durations(starts, finish):
    order = [s for s in STAGES if s in starts]
    out = {}
    for a, b in zip(order, order[1:] + ["_end"]):
        end = finish if b == "_end" else starts[b]
        d = (secs(end) - secs(starts[a])) % 86400
        out[a] = f"{round(d / 60)} min" if d >= 60 else ""
    return out


# ---------------------------------------------------------------- funnel data

def plain_reason(r):
    r = r or ""
    if r.startswith("PRO_PLAY"):
        return "Looked like a pro broadcast"
    if r.startswith("NO_GAMEPLAY"):
        return "Not gameplay ({} frames)".format(r.split("_")[-1].replace("of", " of "))
    if r.startswith("TITLE_KEYWORD"):
        return "Title says “{}”".format(r.split("_", 2)[-1])
    if r.startswith("HYPE_ONLY"):
        return "Loud reaction, audio {}".format(r.split("_")[-1])
    if r.startswith("KILLS_CONFIRMED"):
        return "Kills seen on screen"
    if r.startswith("MULTIKILL"):
        return "Multikill banner seen"
    if r.startswith("UNCONFIRMED"):
        return "Kill not confirmed"
    if r == "BLACKLIST":
        return "Blacklisted channel"
    return r


def load_api(work):
    """The judge's per-clip verdicts: api_partial_v4 from 2026-09-24, v3 before."""
    for v in (4, 3):
        p = work / f"api_partial_v{v}.json"
        if p.exists():
            return load(p)
    return {}


def export_funnel(work, night):
    scored = load(work / "vlm_scored.json")
    scored = scored["clips"] if isinstance(scored, dict) else scored
    api = load_api(work)
    ranks = {c["clip_id"]: c["rank"] for c in load(work / "chapters.json")}
    clips = []
    if not DRY:
        shutil.rmtree(ASSETS / "clips", ignore_errors=True)
        (ASSETS / "clips").mkdir(parents=True)
    for i, c in enumerate(scored):
        cid = c["id"]
        a = api.get(cid) if c.get("decision") == "KEEP" else None
        stage = 4 if cid in ranks else 3 if a else 2
        thumb = work / "thumbs" / f"{cid}.jpg"
        if thumb.exists() and not DRY:
            subprocess.run(["magick", str(thumb), "-resize", "240x135^", "-gravity", "center",
                            "-extent", "240x135", "-quality", "72",
                            str(ASSETS / "clips" / f"{i:02d}.jpg")], check=True)
        elif not thumb.exists():
            warn(f"no thumbnail for {cid}")
        clips.append(dict(i=i, id=cid, who=c.get("broadcaster_name", "?"), title=c.get("title", ""),
                          stage=stage, local=plain_reason(c.get("reason")), rank=ranks.get(cid),
                          ent=a and a.get("api_entertainment"), pq=a and a.get("api_play_quality"),
                          focus=a and a.get("api_focus"),
                          audio=round(c.get("audio_score") or 0, 2),
                          motion=round(c.get("motion_score") or 0, 3)))
    judged = sum(1 for c in clips if c["stage"] >= 3)
    if judged != night["vlm_kept"]:
        warn(f"log says {night['vlm_kept']} reached the judge, work files say {judged}")
    return clips


# ---------------------------------------------------------------- stills

def still(video, t, out, width=1600):
    ff("-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-1",
       "-q:v", "4", out)


def detail(p):
    """Mean edge strength — picks the crop that actually shows a banner / kill icons."""
    from PIL import Image, ImageFilter, ImageStat
    return ImageStat.Stat(Image.open(p).convert("L").filter(ImageFilter.FIND_EDGES)).mean[0]


def first_phrase(words, n=3):
    """Start time of the first run of n+ words spoken without a pause (caption on screen)."""
    run = []
    for w in words or []:
        if run and w["start"] - run[-1]["end"] > 0.4:
            run = []
        run.append(w)
        if len(run) >= n:
            return run[0]["start"]
    return None


def export_media(args, work, raw, night, clips, chapters, transcripts, burned, api,
                 vlm_all):
    out_mp4 = DATA / "output" / f"{args.date}.mp4"
    by_rank = {c["rank"]: c for c in chapters}
    hero = by_rank.get(args.hero_rank) or by_rank[min(by_rank)]
    hid = hero["clip_id"]
    tile = next((c for c in clips if c["id"] == hid), None)
    info = {"hero_rank": hero["rank"], "hero_who": hero["broadcaster"]}

    if tile and not DRY:
        shutil.copy(ASSETS / "clips" / f"{tile['i']:02d}.jpg", ASSETS / "keycap.jpg")

    if out_mp4.exists():
        ff("-ss", f"{hero['start'] + 1.0:.2f}", "-t", "9", "-i", out_mp4, "-an",
           "-vf", "scale=1280:-2,fps=30", "-c:v", "libx264", "-crf", "30", "-preset", "slow",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", ASSETS / "hero.mp4")
        still(out_mp4, hero["start"] + 2.0, ASSETS / "hero.jpg")

        # an English clip without a source caption, at a moment our caption is on screen
        for c in sorted(chapters, key=lambda c: -c["rank"]):
            t = transcripts.get(c["clip_id"], {})
            if (t.get("lang") == "en" or not t.get("lang")) and not burned.get(c["clip_id"]):
                s = first_phrase(t.get("words"))
                if s is not None:
                    still(out_mp4, c["start"] + s + 0.5, ASSETS / "caption_en.jpg")
                    info["cap_rank"] = c["rank"]
                    break
        else:
            warn("no English clip with a caption phrase; caption_en.jpg kept")

        # a foreign clip that carries its own burned caption -> ours sits above theirs
        for c in sorted(chapters, key=lambda c: -c["rank"]):
            t = transcripts.get(c["clip_id"], {})
            if burned.get(c["clip_id"]) and t.get("lang") not in (None, "en"):
                s = first_phrase(t.get("words"))
                if s is not None:
                    still(out_mp4, c["start"] + s + 0.9, ASSETS / "caption_translate.jpg")
                    info["tr_rank"] = c["rank"]
                    info["tr_lang"] = LANGS.get(t["lang"], t["lang"])
                    break
        else:
            warn("no foreign clip with a source caption; caption_translate.jpg kept")
    else:
        warn(f"{out_mp4} is gone; hero loop and caption stills kept")

    src = raw / f"{hid}.mp4"
    if src.exists():
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(src)], capture_output=True,
                                   text=True).stdout.strip() or 30)
        ff("-i", src, "-vf", f"fps=8/{dur:.3f},scale=320:180,tile=8x1:padding=6:color=white",
           "-frames:v", "1", "-q:v", "4", ASSETS / "filmstrip.jpg")
        ff("-i", src, "-filter_complex",
           "aformat=channel_layouts=mono,showwavespic=s=1600x200:colors=0x5ad6e6",
           "-frames:v", "1", ASSETS / "wave.png")
    else:
        warn(f"raw clip {src.name} is gone (deleted after a day); filmstrip/wave kept")

    crops = work / "crops"
    # Crops come from the best-ranked clip where the model actually READ an announcement
    # (the hero's own check can stop after one frame, leaving a weak example); else hero.
    cid = next((c["clip_id"] for c in sorted(chapters, key=lambda c: c["rank"])
                if (vlm_all.get(c["clip_id"]) or {}).get("announcements")
                and list(crops.glob(f"{c['clip_id']}_bn_*s.jpg"))), hid)
    info["crop_rank"] = next(c["rank"] for c in chapters if c["clip_id"] == cid)
    # the cache doesn't say WHICH frame an announcement was read from, so only quote it
    # when there is a single banner crop (it must be that one)
    single = len(list(crops.glob(f"{cid}_bn_*s.jpg"))) == 1
    info["crop_ann"] = (((vlm_all.get(cid) or {}).get("announcements") or [None])[0]
                        if single else None)

    def at(p):                                        # crop file -> its second in the clip
        return int(re.search(r"_(\d+)s\.jpg$", p.name).group(1))

    for kind, name in (("bn", "crop_banner.jpg"), ("kf", "crop_killfeed.jpg")):
        cands = sorted(crops.glob(f"{cid}_{kind}_*s.jpg"), key=at)
        if args.crop_time is not None:
            cands = [p for p in cands if at(p) == args.crop_time] or cands
        if not cands:
            warn(f"no {kind} crops for the hero clip; {name} kept")
            continue
        if kind == "bn":
            # "most detail" picks a busy teamfight over a banner. Announcements land right
            # after the play, so take the first banner crop at/after the judge's best moment.
            bm = (api.get(cid) or {}).get("api_best_moment_s") or 0
            best = next((p for p in cands if at(p) >= bm), cands[-1])
            info["crop_time"] = at(best)
        else:
            best = max(cands, key=detail)
        if not DRY:
            shutil.copy(best, ASSETS / name)

    for i, src in enumerate([work / "thumbnail.jpg", work / "thumbnail_2.jpg",
                             work / "thumbnail_3.jpg"], 1):
        if src.exists() and not DRY:
            subprocess.run(["magick", str(src), "-quality", "82", str(ASSETS / f"thumb_{i}.jpg")],
                           check=True)

    shorts = sorted((work / "shorts").glob("*.mp4")) if (work / "shorts").exists() else []
    for i, s in enumerate(shorts[:2], 1):
        ff("-ss", "6", "-i", s, "-frames:v", "1", "-vf", "scale=540:-1", "-q:v", "4",
           ASSETS / f"short_{i}.jpg")
    if len(shorts) < 2:
        warn("fewer than 2 Shorts in work/<date>/shorts; short stills kept")
    return info


def verdict_html(v):
    """The judge's real JSON for the hero clip, syntax-coloured like the page's code blocks."""
    keys = [("clip_focus", "api_focus"), ("play_quality", "api_play_quality"),
            ("entertainment", "api_entertainment"), ("what_happens", "api_what_happens"),
            ("best_moment_s", "api_best_moment_s")]
    lines = ["{"]
    for i, (k, src) in enumerate(keys):
        val = v.get(src)
        if isinstance(val, str):
            text = val if len(val) <= 170 else val[:167].rsplit(" ", 1)[0] + "…"
            wrapped, line = [], ""
            for word in text.split():                # wrap long strings like the original
                if len(line) + len(word) > 58 and line:
                    wrapped.append(line)
                    line = word
                else:
                    line = f"{line} {word}".strip()
            wrapped.append(line)
            body = "\n    ".join(html.escape(w) for w in wrapped)
            val_h = f'<span class="s">"{body}"</span>'
        else:
            val_h = f'<span class="n">{html.escape(str(val))}</span>'
        comma = "," if i < len(keys) - 1 else ""
        lines.append(f'  <span class="p">"{k}"</span>: {val_h}{comma}')
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------- write page

def fill(page, values):
    """Replace the contents of every element carrying data-n="key"."""
    missing = set()

    def sub(m):
        key = m.group(3)
        if key not in values:
            missing.add(key)
            return m.group(0)
        val = values[key]
        inner = val if key.endswith("_html") else html.escape(str(val), quote=False)
        return f"{m.group(1)}{inner}{m.group(4)}"

    page = re.sub(r'(<(\w+)\b[^>]*\bdata-n="([\w]+)"[^>]*>)(?:.*?)(</\2>)', sub, page,
                  flags=re.S)
    return page, missing


def words(n):
    small = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
             "ten", "eleven", "twelve"]
    return small[n] if 0 <= n < len(small) else str(n)


def main():
    global DRY
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", required=True, help="the night's date label, e.g. 2026-09-21")
    ap.add_argument("--hero-rank", type=int, default=2, help="countdown rank featured (default 2)")
    ap.add_argument("--crop-time", type=int, help="use the hero clip's crops at this second")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()
    DRY = args.dry_run

    work, raw = DATA / "work" / args.date, DATA / "raw" / args.date
    for p in (work / "vlm_scored.json", work / "chapters.json", raw / "clips.json"):
        if not p.exists():
            sys.exit(f"missing {p}")
    log = find_log(args.date)
    print(f"night {args.date}  |  log {log.name}")
    night = parse_log(log.read_text(encoding="utf-8", errors="replace"))
    fetched = load(raw / "clips.json")
    night["fetched"] = len(fetched["clips"] if isinstance(fetched, dict) else fetched)

    chapters = load(work / "chapters.json")
    transcripts = load(work / "transcripts.json") if (work / "transcripts.json").exists() else {}
    burned = load(work / "burned_captions.json") if (work / "burned_captions.json").exists() else {}
    clips = export_funnel(work, night)
    api = load_api(work)
    vp = next((work / f"vlm_partial_v{v}.json" for v in (4, 3)
               if (work / f"vlm_partial_v{v}.json").exists()), None)   # v4 from 2026-09-24
    vlm_all = load(vp) if vp else {}
    media = export_media(args, work, raw, night, clips, chapters, transcripts, burned, api,
                         vlm_all)
    hero_id = next(c["clip_id"] for c in chapters if c["rank"] == media["hero_rank"])
    ann = media.get("crop_ann")

    d = Date.fromisoformat(args.date)
    st, dur = night["starts"], durations(night["starts"], night["finish"])
    vlm_min = round((secs(st["api_judge"]) - secs(st["vlm_filter"])) % 86400 / 60) \
        if "api_judge" in st and "vlm_filter" in st else 90
    f = night
    values = {
        "fetched": f["fetched"], "scored": f["scored"], "passed": f["passed"], "kept": f["kept"],
        "vlm_kept": f["vlm_kept"], "final": f["final"], "minutes": f["minutes"],
        "dropped_total": f["fetched"] - f["final"],
        "drop_lang": f["fetched"] - f["scored"],
        "date_long": f"{d.day} {d:%B %Y}", "date_short": f"{d.day} {d:%B}",
        "finish": f["finish"], "finish_hm": f["finish"][:5], "exit": f["exit"],
        "gemini_days": f"{words(math.ceil(f['fetched'] / 20))} days",
        "vlm_minutes": vlm_min,
        "judge_switch": ("" if not f["switches"] else
                         " It switched models once that night when the first was overloaded."
                         if f["switches"] == 1 else
                         f" It switched models {f['switches']} times that night."),
        "hero_rank": media["hero_rank"], "hero_who": media["hero_who"],
        "crop_rank": media["crop_rank"],
        # no match that night -> leave these keys out, so the page keeps the text that
        # goes with the still it keeps
        **{k: media[k] for k in ("cap_rank", "tr_rank", "tr_lang") if k in media},
        "crop_note": (f"at {media['crop_time']} seconds: the announcement area"
                      + (f" (it read “{ann}”)" if ann else "") + " and the kill feed"
                      if "crop_time" in media else "the announcement area and the kill feed"),
        "verdict_html": verdict_html(api.get(hero_id, {})),
        "t_doctor": st.get("doctor", ""),
    }
    for s in STAGES:
        values[f"t_{s}"] = st.get(s, "")
        values[f"d_{s}"] = dur.get(s, "")

    print("  " + " -> ".join(str(values[k]) for k in
                             ("fetched", "scored", "passed", "kept", "vlm_kept", "final"))
          + f"  |  {f['minutes']} min  |  finished {f['finish']} exit {f['exit']}")
    print(f"  hero #{media['hero_rank']} {media['hero_who']}  |  caption #{values.get('cap_rank', 'kept')}"
          f"  |  translation #{values.get('tr_rank', 'kept')} ({values.get('tr_lang', '-')})")

    page, missing = fill((SITE / "index.html").read_text(encoding="utf-8"), values)
    if missing:
        warn(f"page keys with no value: {', '.join(sorted(missing))}")
    if DRY:
        print("dry run: nothing written")
        return
    (SITE / "index.html").write_text(page, encoding="utf-8")
    (ASSETS / "funnel.json").write_text(json.dumps(
        {"night": {k: values[k] for k in ("fetched", "scored", "passed", "kept", "vlm_kept",
                                           "final", "date_long")},
         "clips": [{k: v for k, v in c.items() if k != "id"} for c in clips]},
        ensure_ascii=False, indent=0), encoding="utf-8")
    print("done. Now look at the page, then update docs/SITE.md (ledger + changelog).")


if __name__ == "__main__":
    main()
