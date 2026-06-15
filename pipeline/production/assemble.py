"""Stage 8 — Assemble the final long-form video, v3.

Structure:
  [intro card]   branded title + date, intro voiceover, fade
  [clip N..1]    countdown order (worst -> best) with a #N badge,
                 animated streamer lower-third, VO ducking, 0.3s fades;
                 top-ranked clips get a 0.5x SLOW-MO REPLAY of the best moment
                 (timestamp from the Gemini judge)
  [outro card]   featured streamers + "new video every day"
  [master]       looped music bed + loudness normalization

Outputs: data/output/<date>.mp4, work/<date>/chapters.json
Segments cache in work/<date>/segments/ and re-render when their VO appears.
"""
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("pipeline.assemble")

ENC = ["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_CJK_CANDIDATES = [
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _ff(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-900:]}")


def _duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(proc.stdout.strip())


def _font(text: str, v: dict) -> str:
    cands = (FONT_CJK_CANDIDATES if any(ord(ch) > 0x2E80 for ch in text)
             else FONT_CANDIDATES)
    if v.get("font"):
        cands = [v["font"], *cands]
    for f in cands:
        if Path(f).exists():
            return f.replace(":", r"\:")
    return cands[-1].replace(":", r"\:")


def _esc(text: str) -> str:
    return "".join(ch for ch in text if ch not in "\\'%:,[]=;").strip()


# ── intro / outro cards ───────────────────────────────────────────────────────

def _card(out: Path, v: dict, dur: float, drawtexts: str, vo: Path | None) -> None:
    w, h, fps = v["width"], v["height"], v["fps"]
    vf = drawtexts + f",fade=t=out:st={dur-0.4:.2f}:d=0.4"
    args = ["-f", "lavfi", "-i", f"color=c=0x0b0e14:s={w}x{h}:r={fps}:d={dur:.2f}"]
    if vo is not None and vo.exists():
        args += ["-i", str(vo),
                 "-filter_complex", f"[0:v]{vf}[v];[1:a]aresample=44100,apad[a]",
                 "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.2f}",
                 "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a"]
    args += ["-t", f"{dur:.2f}", *ENC, "-preset", v.get("preset", "veryfast"), str(out)]
    _ff(args)


def render_intro(out: Path, date_label: str, vo: Path | None, v: dict) -> None:
    h = v["height"]
    title = _esc(v.get("intro_title", "DAILY LEAGUE HIGHLIGHTS"))
    f = _font(title, v)
    dur = 3.5
    if vo is not None and vo.exists():
        dur = max(3.5, _duration(vo) + 0.7)
    dt = (
        f"drawbox=x=(iw-700)/2:y={int(h*0.56)}:w=700:h=6:color=0x9146FF@0.9:t=fill:"
        f"enable='gte(t,0.6)',"
        f"drawtext=fontfile='{f}':text='{title}':fontsize=86:fontcolor=white:"
        f"x=(w-text_w)/2:y=(h-text_h)/2-60:alpha='min(1,t/0.7)',"
        f"drawtext=fontfile='{f}':text='{_esc(date_label)}':fontsize=40:"
        f"fontcolor=0xBBBBBB:x=(w-text_w)/2:y=(h)/2+92:"
        f"alpha='if(lt(t,0.5),0,min(1,(t-0.5)/0.7))'"
    )
    _card(out, v, dur, dt, vo)


def render_outro(out: Path, streamers: list[str], v: dict) -> None:
    f = _font("x", v)
    names = _esc("  ·  ".join(streamers[:6]))
    dt = (
        f"drawtext=fontfile='{f}':text='THANKS FOR WATCHING':fontsize=72:"
        f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-90:alpha='min(1,t/0.6)',"
        f"drawtext=fontfile='{_font(names or 'x', v)}':text='{names}':fontsize=36:"
        f"fontcolor=0xB48BFF:x=(w-text_w)/2:y=(h)/2+10:"
        f"alpha='if(lt(t,0.4),0,min(1,(t-0.4)/0.6))',"
        f"drawtext=fontfile='{f}':text='New video every day':fontsize=34:"
        f"fontcolor=0xBBBBBB:x=(w-text_w)/2:y=(h)/2+90:"
        f"alpha='if(lt(t,0.8),0,min(1,(t-0.8)/0.6))'"
    )
    _card(out, v, 4.5, dt, None)


# ── per-clip segment (main + optional slow-mo replay) ─────────────────────────

def _main_part(clip: dict, mp4: Path, vo: Path | None, out: Path, v: dict) -> None:
    w, h, fps = v["width"], v["height"], v["fps"]
    dur = _duration(mp4)
    lt_end = min(v.get("lower_third_seconds", 5.5) + 0.6, max(dur - 1, 2))
    name = _esc(clip.get("broadcaster_name", ""))
    handle = _esc(f"twitch.tv/{clip.get('broadcaster_name', '')}".lower())

    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}")
    if name:
        fn = _font(name, v)
        slide = "min(1,max(0,(t-0.6)/0.45))"
        fadeout = f"if(lt(t,{lt_end:.2f}),1,max(0,1-(t-{lt_end:.2f})/0.4))"
        vf += (
            f",drawtext=fontfile='{fn}':text='{name}':fontsize=58:fontcolor=white:"
            f"box=1:boxcolor=0x0b0e14@0.72:boxborderw=16:"
            f"x=64:y='h-236+24*(1-{slide})':alpha='{slide}*{fadeout}',"
            f"drawtext=fontfile='{fn}':text='{handle}':fontsize=30:fontcolor=0xB48BFF:"
            f"box=1:boxcolor=0x0b0e14@0.72:boxborderw=12:"
            f"x=66:y='h-156+24*(1-{slide})':alpha='{slide}*{fadeout}'"
        )
    rank = clip.get("countdown_rank")
    if v.get("countdown_enabled", True) and rank:
        fb = _font("#", v)
        vf += (
            f",drawtext=fontfile='{fb}':text='#{rank}':fontsize=110:fontcolor=white:"
            f"borderw=5:bordercolor=0x9146FF:x=w-text_w-64:y=56:"
            f"alpha='if(lt(t,0.4),t/0.4,if(lt(t,3.6),1,max(0,1-(t-3.6)/0.4)))'"
        )
    vf += f",fade=t=in:st=0:d=0.3,fade=t=out:st={max(dur-0.35,0):.2f}:d=0.35"

    afade = f"afade=t=in:st=0:d=0.3,afade=t=out:st={max(dur-0.35,0):.2f}:d=0.35"
    args = ["-i", str(mp4)]
    if vo is not None and vo.exists():
        vo_d = _duration(vo)
        duck = v.get("voiceover_duck_db", -10)
        args += ["-i", str(vo), "-filter_complex",
                 f"[0:v]{vf}[v];"
                 f"[0:a]aresample=44100,"
                 f"volume={duck}dB:enable='between(t,0,{vo_d:.2f})'[ducked];"
                 f"[1:a]aresample=44100[vo];"
                 f"[ducked][vo]amix=inputs=2:duration=first:normalize=0,{afade}[a]",
                 "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-filter_complex",
                 f"[0:v]{vf}[v];[0:a]aresample=44100,{afade}[a]",
                 "-map", "[v]", "-map", "[a]"]
    args += [*ENC, "-preset", v.get("preset", "veryfast"), str(out)]
    _ff(args)


def _replay_part(clip: dict, mp4: Path, out: Path, v: dict) -> bool:
    """0.5x slow-mo of the judge's best moment. Returns False if not applicable."""
    bm = clip.get("api_best_moment_s") or 0
    if not bm:
        return False
    dur = _duration(mp4)
    rs = v.get("replay_seconds", 6)
    start = min(max(bm - rs / 2, 0), max(dur - rs, 0))
    w, h, fps = v["width"], v["height"], v["fps"]
    f = _font("REPLAY", v)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setpts=2.0*PTS,fps={fps},"
          f"drawtext=fontfile='{f}':text='REPLAY':fontsize=64:fontcolor=white:"
          f"borderw=4:bordercolor=0x9146FF:x=(w-text_w)/2:y=64:"
          f"alpha='0.65+0.35*abs(sin(2*t))',"
          f"fade=t=in:st=0:d=0.25,fade=t=out:st={rs*2-0.4:.2f}:d=0.4")
    af = (f"atempo=0.5,volume=-6dB,"
          f"afade=t=in:st=0:d=0.25,afade=t=out:st={rs*2-0.4:.2f}:d=0.4")
    _ff(["-ss", f"{start:.2f}", "-t", f"{rs:.2f}", "-i", str(mp4),
         "-filter_complex", f"[0:v]{vf}[v];[0:a]aresample=44100,{af}[a]",
         "-map", "[v]", "-map", "[a]",
         *ENC, "-preset", v.get("preset", "veryfast"), str(out)])
    return True


def render_segment(clip: dict, mp4: Path, vo: Path | None, out: Path, v: dict) -> None:
    """Main part + optional replay, concatenated into one segment file."""
    main = out.with_suffix(".main.mp4")
    _main_part(clip, mp4, vo, main, v)
    want_replay = (v.get("replay_enabled", False)
                   and clip.get("api_rank_score", 0) >= v.get("replay_min_score", 7))
    replay = out.with_suffix(".replay.mp4")
    if want_replay and _replay_part(clip, mp4, replay, v):
        lst = out.with_suffix(".txt")
        lst.write_text(f"file '{main.as_posix()}'\nfile '{replay.as_posix()}'",
                       encoding="utf-8")
        _ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
        lst.unlink(missing_ok=True)
        replay.unlink(missing_ok=True)
        main.unlink(missing_ok=True)
    else:
        replay.unlink(missing_ok=True)
        main.replace(out)


# ── master ────────────────────────────────────────────────────────────────────

def master(concat_mp4: Path, out: Path, v: dict) -> None:
    music = v.get("music_path")
    args = ["-i", str(concat_mp4)]
    if music and Path(music).exists():
        args += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                 f"[1:a]aresample=44100,volume={v.get('music_volume_db', -8)}dB[m];"
                 f"[0:a][m]amix=inputs=2:duration=first:normalize=0,"
                 f"loudnorm=I=-16:TP=-1.5:LRA=11[a]",
                 "-map", "0:v", "-map", "[a]"]
    else:
        args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-map", "0:v", "-map", "0:a"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", str(out)]
    _ff(args)


# ── stage entry ───────────────────────────────────────────────────────────────

def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    raw_dir = data / "raw" / date_label
    v = cfg["video"]

    src = work / "vlm_filtered.json"
    if not src.exists():
        src = work / "prefiltered.json"
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]

    seg_dir = work / "segments"
    seg_dir.mkdir(exist_ok=True)
    vo_dir = work / "vo"

    def stale(seg: Path, vo: Path) -> bool:
        marker = seg.with_suffix(".vo")
        had_vo = marker.exists() and marker.read_text() == "1"
        return seg.exists() and vo.exists() and not had_vo

    def mark(seg: Path, vo: Path) -> None:
        seg.with_suffix(".vo").write_text("1" if vo.exists() else "0")

    segments, chapters, t = [], [], 0.0

    if v.get("intro_enabled", True):
        intro = seg_dir / "_intro.mp4"
        ivo = vo_dir / "_intro.mp3"
        if not intro.exists() or stale(intro, ivo):
            render_intro(intro, date_label, ivo, v)
            mark(intro, ivo)
        t += _duration(intro)
        segments.append(intro)

    for c in clips:
        mp4 = raw_dir / f"{c['id']}.mp4"
        if not mp4.exists():
            lp = c.get("local_path") or ""        # NB: Path("") is "." (exists!)
            if lp:
                mp4 = Path(lp)
        if not mp4.exists():                      # selected clip must be full quality
            from ..ingestion.fetch import download_clip
            hq = raw_dir / f"{c['id']}.mp4"
            if download_clip(c.get("url", ""), hq):
                mp4 = hq
            else:
                log.warning("no video file for %s — skipping", c["id"])
                continue
        seg = seg_dir / f"{c['id']}.mp4"
        svo = vo_dir / f"{c['id']}.mp3"
        if not seg.exists() or stale(seg, svo):
            try:
                render_segment(c, mp4, svo, seg, v)
                mark(seg, svo)
            except Exception as e:
                log.warning("segment failed for %s: %s", c["id"], e)
                continue
        dur = _duration(seg)
        chapters.append({"clip_id": c["id"], "start": round(t, 2),
                         "broadcaster": c.get("broadcaster_name", ""),
                         "title": c.get("title", ""),
                         "rank": c.get("countdown_rank"),
                         "broadcaster_url": f"https://twitch.tv/{c.get('broadcaster_name', '')}"})
        t += dur
        segments.append(seg)

    if v.get("outro_enabled", True):
        outro = seg_dir / "_outro.mp4"
        streamers = list(dict.fromkeys(ch["broadcaster"] for ch in chapters))
        render_outro(outro, streamers, v)   # cheap; always re-render (names change)
        segments.append(outro)

    if len(segments) < 2:
        raise RuntimeError("Nothing to assemble — no clip segments rendered.")

    listfile = work / "concat.txt"
    listfile.write_text("\n".join(f"file '{s.as_posix()}'" for s in segments),
                        encoding="utf-8")
    rough = work / "concat_rough.mp4"
    _ff(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(rough)])

    out = data / "output" / f"{date_label}.mp4"
    master(rough, out, v)
    rough.unlink(missing_ok=True)

    (work / "chapters.json").write_text(
        json.dumps(chapters, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Assembled %s (%.1f min, %d clips)", out.name, t / 60, len(chapters))
    return out
