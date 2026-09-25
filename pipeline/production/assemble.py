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

_AUDIO_ENC = ["-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]


def enc(v: dict, threads: int | None = None) -> list[str]:
    """Full encode args (video codec + quality + preset + audio) for the `video`
    config section. Hardware-accelerated when this machine has an encoder for it —
    see pipeline/hardware.py. Every segment must use this so concat stays clean.

    The thread cap defaults to whatever `hardware.apply_limits()` stashed on the video
    config at startup, so every call site inherits it without threading a parameter
    through the whole render path."""
    from ..hardware import encoder_args
    if threads is None:
        threads = int(v.get("_cpu_threads", 0))
    return [*encoder_args(v, threads), *_AUDIO_ENC]

# Windows -> macOS -> Linux. Windows is the tested platform; the rest keep a clone
# rendering rather than silently drawing nothing. Override with video.font.
FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_CJK_CANDIDATES = [
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
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


_SENT_END = ".!?"


def _is_english(lang: str | None) -> bool:
    """Whisper reports 'en', but also 'en-US' / 'English' depending on the backend."""
    s = (lang or "").strip().lower().replace("_", "-")
    return s == "english" or s == "en" or s.startswith("en-")


def _ends_sentence(word: str) -> bool:
    """True if `word` closes a sentence — trailing quotes/brackets don't hide the mark."""
    return word.rstrip("\"'’)]").endswith(tuple(_SENT_END))


def _caption_groups(words: list | None, max_words: int = 3, max_gap: float = 0.65,
                    max_seconds: float = 1.9, min_seconds: float = 0.62,
                    pad: float = 0.06) -> list[dict]:
    """Group per-word timestamps into short readable phrases with DISJOINT windows.

    Two defects in the old one-word-at-a-time chain are fixed here, and both were
    arithmetic rather than rendering:

    * Overlap. Each word was shown for `max(0.15, end - start)`. Whisper routinely
      emits words 0.06s long back to back ("to" 3.06-3.14, "deal" 3.14-3.28), so the
      0.15s floor pushed a word's window past the start of the next one and drawtext
      happily drew both — one on top of the other, since both are centred.
    * Unreadable pace. Even without overlap, one word per 0.15s is ~7 words/second;
      nobody reads that. Grouping is what the streamers' own caption widgets do, and
      it buys reading time for free because a 3-word phrase holds for the sum of its
      words' durations.

    Groups break on: `max_words`, a silence longer than `max_gap`, a total longer than
    `max_seconds`, or sentence-ending punctuation. Windows are then clamped so a group
    always ends `pad` before the next one begins — that is the structural guarantee
    that nothing ever overlaps, whatever whisper reports.
    """
    if not words:
        return []
    clean = []
    for w in words:
        text = (w.get("word") or "").strip()
        if not text:
            continue
        try:
            t0, t1 = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        clean.append({"word": text, "start": t0, "end": max(t0, t1)})
    if not clean:
        return []
    clean.sort(key=lambda x: x["start"])

    groups: list[dict] = []
    cur: list[dict] = []
    for w in clean:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            span = w["end"] - cur[0]["start"]
            if (len(cur) >= max_words or gap > max_gap or span > max_seconds
                    or _ends_sentence(cur[-1]["word"])):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    out = []
    for i, g in enumerate(groups):
        t0 = g[0]["start"]
        t1 = max(g[-1]["end"], t0 + min_seconds)
        if i + 1 < len(groups):
            t1 = min(t1, groups[i + 1][0]["start"] - pad)
        if t1 - t0 < 0.08:                       # too crushed to be readable, drop it
            continue
        out.append({"text": " ".join(x["word"] for x in g), "start": t0, "end": t1})
    return out


def _caption_dt(words: list | None, v: dict, fontsize: int = 52) -> str:
    """drawtext chain (one short phrase visible at a time) for burned English captions
    on a clip — bottom-centre, above the in-game HUD. '' when no words.

    Height is `caption_y_frac` of frame height. There is no universally safe band: the
    LoL HUD owns the bottom ~13%, and streamers stack their own overlays (rank badges,
    respawn timers, and increasingly their own live captions) immediately above it.
    The old fixed 0.78 landed on top of those often enough to look broken.
    """
    groups = _caption_groups(
        words,
        max_words=int(v.get("caption_max_words", 3)),
        max_gap=float(v.get("caption_group_gap", 0.65)),
        max_seconds=float(v.get("caption_max_seconds", 1.9)),
        min_seconds=float(v.get("caption_min_seconds", 0.62)))
    if not groups:
        return ""
    f = _font("x", v)
    y = int(v["height"] * float(v.get("caption_y_frac", 0.70)))
    seg = []
    for g in groups:
        # ' is stripped by _esc (it would need escaping in the filtergraph), which turned
        # "he's" into "hes"; the typographic apostrophe passes through untouched.
        text = _esc(g["text"].replace("'", "’").upper())
        if not text:
            continue
        seg.append(
            f"drawtext=fontfile='{f}':text='{text}':fontsize={fontsize}:fontcolor=white:"
            f"borderw=6:bordercolor=black:x=(w-text_w)/2:y={y}:"
            f"enable='between(t,{g['start']:.2f},{g['end']:.2f})'")
    return ",".join(seg)


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
    args += ["-t", f"{dur:.2f}", *enc(v), str(out)]
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


def _find_drop(track: str, win: float, frac: float = 0.33) -> float:
    """Start (seconds) of the most energetic `win`-second window — the song's drop.
    Falls back to frac*duration if librosa analysis fails."""
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(track, sr=22050, mono=True)
        dur = len(y) / sr
        if dur <= win:
            return 0.0
        hop = 2048
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        tps = hop / sr
        wlen = max(1, int(win / tps))
        csum = np.cumsum(np.insert(rms, 0, 0))
        means = (csum[wlen:] - csum[:-wlen]) / wlen
        start = int(np.argmax(means)) * tps
        return float(min(max(start, 0.0), max(dur - win, 0.0)))
    except Exception:
        try:
            d = _duration(Path(track))
            return min(d * frac, max(d - win, 0.0))
        except Exception:
            return 0.0


def _branded_music_outro(out: Path, v: dict, cfg: dict, music: str) -> None:
    """KEMONO logo (slow push-in) over the drop of an NCS track."""
    from . import brand
    logo = brand.build_logo(cfg)
    w, h, fps = v["width"], v["height"], v["fps"]
    dur = float(v.get("outro_seconds", 12))
    drop = _find_drop(music, dur, float(v.get("outro_music_start", 0.33)))

    # Lead-in: start BEFORE the drop, not on it. _find_drop returns the track's energy
    # peak, so cutting there put the loudest moment of the music 0.5s after the last
    # clip's own peak. Measured on 2026-09-05: the final clip crested at -13.9 LUFS (a
    # pentakill), hard-cut, and the outro was back at full -19.6 LUFS 0.6s later — a
    # ~24 LU swing inside a second, which reads as a jumpscare rather than an ending.
    # Backing up into the track's build means the drop lands a beat AFTER the fade
    # completes, which is what the build is for.
    lead = float(v.get("outro_music_leadin", 2.5))
    start = max(0.0, drop - lead)
    fade_in = float(v.get("outro_fade_in", 2.5))
    fade_in = max(0.1, min(fade_in, dur / 2))

    zoom = (f"scale={w*2}:-1,zoompan=z='min(zoom+0.0009,1.08)':d={int(dur*fps)}:"
            f"s={w}x{h}:fps={fps}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
    vf = (f"{zoom},fade=t=in:st=0:d={min(fade_in, 1.5):.2f},"
          f"fade=t=out:st={dur-0.6:.2f}:d=0.6,format=yuv420p")
    # loudnorm BEFORE the fade: it measures the whole segment and re-gains it, so a fade
    # applied first gets partly normalised back out. Fading last is what actually ramps.
    af = (f"aresample=44100,loudnorm=I=-16:TP=-1.5:LRA=11,"
          f"afade=t=in:st=0:d={fade_in:.2f}:curve=ihsin,"
          f"afade=t=out:st={dur-1.0:.2f}:d=1.0")
    _ff(["-loop", "1", "-i", str(logo),
         "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(music),
         "-filter_complex", f"[0:v]{vf}[v];[1:a]{af}[a]",
         "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
         *enc(v), str(out)])


def render_outro(out: Path, streamers: list[str], v: dict, cfg: dict) -> None:
    music = v.get("outro_music_path", "")
    if v.get("brand", {}).get("enabled") and music and Path(music).exists():
        _branded_music_outro(out, v, cfg, music)
        return
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

def _wants_replay(clip: dict, v: dict) -> bool:
    return bool(v.get("replay_enabled", False)
                and clip.get("api_rank_score", 0) >= v.get("replay_min_score", 7))


def _main_part(clip: dict, mp4: Path, vo: Path | None, out: Path, v: dict,
               nameplate: Path | None = None, sfx: Path | None = None,
               captions: list | None = None, tail_fade: float | None = None,
               badge: Path | None = None, head_tr: str | None = None,
               tail_tr: str | None = None) -> None:
    from . import transitions as _tr
    w, h, fps = v["width"], v["height"], v["fps"]
    dur = _duration(mp4)
    lt_end = min(v.get("lower_third_seconds", 5.5) + 0.6, max(dur - 1, 2))
    name = _esc(clip.get("broadcaster_name", ""))
    handle = _esc(f"twitch.tv/{clip.get('broadcaster_name', '')}".lower())

    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}")
    # animated nameplate replaces the plain drawtext lower-third when present
    if name and nameplate is None:
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
    cap = _caption_dt(captions, v)               # burned English speech captions
    if cap:
        vf += "," + cap
    # 0.35s everywhere, except the clip that hands over to the outro: cutting from a
    # peak (a pentakill scream) straight into music reads as a jumpscare, so the last
    # clip eases out over `outro_handoff_fade` instead.
    # A transition (production/transitions) replaces the fade on its side of the cut.
    fo = float(tail_fade if tail_fade else 0.35)
    fo = max(0.2, min(fo, max(dur - 0.5, 0.2)))
    if not head_tr:
        vf += ",fade=t=in:st=0:d=0.3"
    if not tail_tr:
        vf += f",fade=t=out:st={max(dur-fo,0):.2f}:d={fo:.2f}"
    fi = _tr.AUDIO_FADE_S if head_tr else 0.3
    if tail_tr:
        fo = _tr.AUDIO_FADE_S
    afade = (f"afade=t=in:st=0:d={fi:.2f},"
             f"afade=t=out:st={max(dur-fo,0):.2f}:d={fo:.2f}:curve=ihsin")

    # assign input indices in the order the inputs are appended below
    args = ["-i", str(mp4)]
    idx = 1
    vo_i = np_i = sfx_i = None
    if vo is not None and vo.exists():
        vo_i = idx; idx += 1; args += ["-i", str(vo)]
    if nameplate is not None:
        np_i = idx; idx += 1; args += ["-i", str(nameplate)]
    cd_i = None
    if badge is not None and badge.exists():
        cd_i = idx; idx += 1; args += ["-i", str(badge)]
    if sfx is not None:
        sfx_i = idx; idx += 1; args += ["-i", str(sfx)]

    fc = []
    # video: base render, then the alpha overlays (nameplate, countdown badge), each
    # only for its own window. Chained rather than nested so either can be absent.
    fc.append(f"[0:v]{vf}[vb]")
    stage = "vb"
    if np_i is not None:
        npc = v.get("nameplate", {})
        np_end = float(npc.get("hold_seconds", 5.0)) + 0.6
        fc.append(f"[{stage}][{np_i}:v]overlay=0:0:eof_action=pass:"
                  f"enable='lte(t,{np_end:.2f})'[vnp]")
        stage = "vnp"
    if cd_i is not None:
        cdc = v.get("countdown", {}) or {}
        cd_end = float(cdc.get("hold_seconds", 3.4)) + 0.8
        fc.append(f"[{stage}][{cd_i}:v]overlay=0:0:eof_action=pass:"
                  f"enable='lte(t,{cd_end:.2f})'[vcd]")
        stage = "vcd"
    # transitions last, so the cut treats overlays and captions like the picture
    trf = _tr.head_vf(head_tr) + _tr.tail_vf(tail_tr, dur)
    fc.append(f"[{stage}]{','.join(trf) or 'null'}[v]")

    # audio: clip (ducked under VO) → mix VO → mix SFX → fade
    if vo_i is not None:
        vo_d = _duration(vo)
        duck = v.get("voiceover_duck_db", -10)
        fc.append(f"[0:a]aresample=44100,volume={duck}dB:"
                  f"enable='between(t,0,{vo_d:.2f})'[ducked]")
        fc.append(f"[{vo_i}:a]aresample=44100[vov]")
        fc.append("[ducked][vov]amix=inputs=2:duration=first:normalize=0[am]")
    else:
        fc.append("[0:a]aresample=44100[am]")
    if sfx_i is not None:
        npc = v.get("nameplate", {})
        delay = int(npc.get("sfx_delay_ms", 120))
        gain = npc.get("sfx_gain_db", -12)
        fc.append(f"[{sfx_i}:a]adelay={delay}|{delay},volume={gain}dB[sx]")
        fc.append("[am][sx]amix=inputs=2:duration=first:normalize=0[asum]")
        a_in = "[asum]"
    else:
        a_in = "[am]"
    fc.append(f"{a_in}{afade}[a]")

    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]"]
    args += [*enc(v), str(out)]
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
         *enc(v), str(out)])
    return True


def render_segment(clip: dict, mp4: Path, vo: Path | None, out: Path, v: dict,
                   nameplate: Path | None = None, sfx: Path | None = None,
                   captions: list | None = None, tail_fade: float | None = None,
                   badge: Path | None = None, head_tr: str | None = None,
                   tail_tr: str | None = None) -> None:
    """Main part + optional replay, concatenated into one segment file.

    `head_tr`/`tail_tr`: the transition kind at each end (production/transitions); with
    a replay the tail transition is dropped (the replay keeps its own fades).

    `tail_fade` lengthens the closing fade, used on the LAST clip so the video eases
    into the outro instead of cutting from a peak straight into music."""
    main = out.with_suffix(".main.mp4")
    _main_part(clip, mp4, vo, main, v, nameplate=nameplate, sfx=sfx, captions=captions,
               tail_fade=tail_fade if not _wants_replay(clip, v) else None, badge=badge,
               head_tr=head_tr, tail_tr=tail_tr if not _wants_replay(clip, v) else None)
    want_replay = _wants_replay(clip, v)
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
        music_db  = v.get("music_volume_db", -14)
        threshold = v.get("music_duck_threshold", 0.02)
        ratio     = v.get("music_duck_ratio", 4)
        attack    = v.get("music_duck_attack_ms", 50)
        release   = v.get("music_duck_release_ms", 400)
        args += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                 f"[1:a]aresample=44100,volume={music_db}dB[music];"
                 f"[0:a]asplit=2[clip_out][sc];"
                 f"[music][sc]sidechaincompress=threshold={threshold}:ratio={ratio}"
                 f":attack={attack}:release={release}[music_ducked];"
                 f"[clip_out][music_ducked]amix=inputs=2:duration=first:normalize=0,"
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

    # Refuse to build a video that has no video in it.
    #
    # Every stage handles "zero items" gracefully, which is how a thin day still ships —
    # but nothing used to ask whether a video actually existed before publishing one.
    # On 2026-08-13 yt-dlp's Twitch extractor broke (KeyError('data')) and ALL 181
    # downloads failed; prefilter scored 181 -> 0 kept, the judge selected 0 clips, and
    # assemble cheerfully produced a 14.9s intro-plus-outro with no clips, no chapters
    # and nobody credited — which credits.py titled and upload.py published PUBLICLY,
    # with the run exiting 0. A broken extractor is an external event that WILL recur;
    # failing here makes the next one a loud non-event instead of a bad upload.
    min_clips = int(v.get("min_clips", 3) or 0)
    if min_clips and len(clips) < min_clips:
        raise RuntimeError(
            f"only {len(clips)} clip(s) selected for {date_label} "
            f"(video.min_clips={min_clips}) - refusing to assemble. This is normally an "
            f"INPUT failure rather than a thin day: check the log above for download "
            f"errors (a broken yt-dlp extractor is the usual cause - try "
            f"`pip install -U yt-dlp`). Lower video.min_clips to allow a video this short.")

    seg_dir = work / "segments"
    seg_dir.mkdir(exist_ok=True)
    vo_dir = work / "vo"

    from ..enrichment.transcribe import load as _load_transcripts
    transcripts = _load_transcripts(work)   # clip_id -> {lang, text, words}

    # Streamers increasingly burn their own live captions into the stream. We can't
    # remove those, so where one exists AND the speech is already English, ours is a
    # second transcription of the same words — skip it. See enrichment/burned_captions.
    from ..enrichment.burned_captions import detect as _detect_burned
    has_source_caption = _detect_burned(cfg, date_label, clips, transcripts)

    # animated streamer nameplate (per clip) + its synthesized flush-in SFX
    np_cfg = v.get("nameplate", {}) or {}
    sfx_path = None
    if np_cfg.get("enabled", False) and np_cfg.get("sfx_enabled", True):
        from ..tools.gen_sfx import ensure as _ensure_sfx
        p = Path(np_cfg.get("sfx_path", "assets/sfx/nameplate.wav"))
        if not p.is_absolute():
            p = data.parent / p
        sfx_path = _ensure_sfx(p)

    def stale(seg: Path, vo: Path) -> bool:
        marker = seg.with_suffix(".vo")
        had_vo = marker.exists() and marker.read_text() == "1"
        return seg.exists() and vo.exists() and not had_vo

    def mark(seg: Path, vo: Path) -> None:
        seg.with_suffix(".vo").write_text("1" if vo.exists() else "0")

    segments, chapters, t = [], [], 0.0

    # clip-to-clip transitions: one kind per boundary, seeded by the date so a re-run
    # plans the same ones; a segment re-renders when its planned edges change (.tr)
    from . import transitions as _tr
    trc = v.get("transitions", {}) or {}
    kinds = (_tr.plan(len(clips), date_label, trc.get("weights"))
             if trc.get("enabled", False) else [])

    def tr_stale(seg: Path, key: str) -> bool:
        m = seg.with_suffix(".tr")
        return seg.exists() and (m.read_text() if m.exists() else "|") != key

    if v.get("intro_enabled", True):
        intro = seg_dir / "_intro.mp4"
        ivo = vo_dir / "_intro.mp3"
        if not intro.exists() or stale(intro, ivo):
            if v.get("brand", {}).get("enabled"):
                from . import brand
                brand.build_intro(cfg, intro)        # KEMONO logo sting
            else:
                render_intro(intro, date_label, ivo, v)
            mark(intro, ivo)
        t += _duration(intro)
        segments.append(intro)

    for ci, c in enumerate(clips):
        head_tr, tail_tr = _tr.edges(kinds, ci)
        tr_key = f"{head_tr or ''}|{tail_tr or ''}"
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
                log.warning("no video file for %s - skipping", c["id"])
                continue
        seg = seg_dir / f"{c['id']}.mp4"
        svo = vo_dir / f"{c['id']}.mp3"
        if not seg.exists() or stale(seg, svo) or tr_stale(seg, tr_key):
            np_path = None
            if np_cfg.get("enabled", False):
                from . import nameplate as _np
                np_path = _np.build(cfg, c)
            try:
                tr = transcripts.get(c["id"], {})
                caps = tr.get("words")            # burned English captions
                if has_source_caption.get(c["id"]) and _is_english(tr.get("lang")):
                    caps = None                   # their caption already says this
                from . import countdown as _cd
                badge = _cd.build(cfg, c.get("countdown_rank"))
                is_last = c is clips[-1]
                render_segment(c, mp4, svo, seg, v, nameplate=np_path,
                               sfx=sfx_path if np_path else None, captions=caps,
                               tail_fade=(float(v.get("outro_handoff_fade", 1.2))
                                          if is_last and v.get("outro_enabled", True)
                                          else None),
                               badge=badge, head_tr=head_tr, tail_tr=tail_tr)
                mark(seg, svo)
                seg.with_suffix(".tr").write_text(tr_key)
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
        render_outro(outro, streamers, v, cfg)   # cheap; always re-render (names change)
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
