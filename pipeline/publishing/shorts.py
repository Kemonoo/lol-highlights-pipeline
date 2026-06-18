"""Stage 11 — YouTube Shorts: 3-5 vertical clips posted daily.

Per clip:
  1. Trim to ≤55 s (centered on api_best_moment_s when the clip is longer).
  2. Face cam detection (9 frames, majority vote, CLAHE + frontal + profile cascades).
  3. One-pass ffmpeg render at 1080×1920 (blur-bg or split layout).
  4. Streamer speech → English via the shared enrichment.transcribe (faster-whisper):
     drives both the English captions and the VO/title context.
  5. Informative English VO line synthesised by the same TTS provider as the main video.
  6. English speech captions (word-level, TikTok style, positioned at the BOTTOM of the
     frame so they do not overlap LoL HUD ability icons).
  7. Upload as a YouTube Short — English title generated from the summary + speech
     (never the raw, often-native Twitch clip title).

Output:  data/work/<date>/shorts/<clip_id>.mp4
Sentinel: data/work/<date>/shorts/done.json
"""
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.shorts")

TARGET_W, TARGET_H = 1080, 1920
MAX_SHORT_S = 55  # under YouTube's 60 s limit with buffer

_VO_PROMPT = """Write ONE voiceover sentence introducing this League of Legends clip as a YouTube Short.
Tone: sports commentator — factual, curious, builds anticipation. No Gen Z slang.
Good examples (do NOT copy verbatim, vary the structure):
  "Watch {streamer} get caught in a 1v3 — this might actually work though."
  "Caught out of position, {streamer} has to think fast here."
  "{streamer} going full send — let's see if the read was right."
  "No flash, one HP, three enemies. This is going to be interesting."
Keep under 15 words. Reference what actually happens. No hype words like "insane".
English only. Clip: {streamer} — {summary}
What the streamer said (English, may be empty): {speech}
Answer ONLY JSON: {{"line": "..."}}"""

_OVERLAY_PROMPT = """Write 2-4 words of punchy on-screen caption text for this League of Legends clip.
React to the moment — like a meme caption that makes someone pause on scroll.
Examples: NOT LOOKING GOOD, SOMEHOW WORKED, THE AUDACITY, CALCULATED, CLASSIC
No emoji. No punctuation beyond ellipsis (...). Capital letters OK.
Clip: {streamer} — {summary}
Answer ONLY JSON: {{"text": "..."}}"""

_TITLE_PROMPT = """Write a punchy ENGLISH YouTube Shorts title for a League of Legends clip.
<= 70 characters. English ONLY (never the streamer's native language). Clickbait but honest,
references what happens. No hashtags, no quotes, no emoji.
Streamer: {streamer}
What happens (English): {summary}
What the streamer said (English, may be empty): {speech}
Answer ONLY JSON: {{"title": "..."}}"""


# ── name helper ───────────────────────────────────────────────────────────────

def _ascii_name(c: dict) -> str:
    """Return a TTS-safe streamer name.

    Twitch display names can contain CJK characters (JP/KR/CN). TTS engines
    cannot pronounce them, so fall back to broadcaster_login which is always
    ASCII-only per Twitch's rules.
    """
    name = c.get("broadcaster_name", "")
    if name.isascii():
        return name or "the streamer"
    return c.get("broadcaster_login", "") or "the streamer"


# ── path helpers ──────────────────────────────────────────────────────────────

def _ass_esc(path: Path) -> str:
    """Escape a Windows absolute path for use inside a ffmpeg filter option value."""
    return str(path).replace("\\", "/").replace(":", "\\:")


def _clean_overlay(text: str) -> str:
    """Strip emoji, ASS control chars, and ffmpeg drawtext-unsafe chars."""
    text = re.sub(r"[^\x00-\x7F]", "", text)   # drop everything non-ASCII (emoji etc.)
    for ch in "{}\\:,=[];'\"<>":
        text = text.replace(ch, "")
    return text.strip()


# ── face cam detection ────────────────────────────────────────────────────────

def _extract_frame(mp4: Path, t: float) -> "np.ndarray | None":
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    try:
        subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", str(mp4),
             "-frames:v", "1", "-q:v", "2", str(tmp)],
            capture_output=True, check=True,
        )
        import cv2
        return cv2.imread(str(tmp)) if tmp.exists() else None
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _faces_in_strip(strip_gray: "np.ndarray",
                    cas_frontal: "cv2.CascadeClassifier",
                    cas_profile: "cv2.CascadeClassifier | None") -> list[tuple]:
    """Return all face detections from one strip using frontal + profile cascades.

    Applies CLAHE first to handle dark or low-contrast webcam overlays.
    Profile cascade runs twice (normal + flipped) to catch both orientations.
    """
    import cv2

    # Adaptive contrast enhancement: lifts dark webcam feeds
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(strip_gray)
    sh, sw = enhanced.shape

    found: list[tuple] = []

    for cas in ([cas_frontal] + ([cas_profile] if cas_profile else [])):
        # Normal orientation
        det = cas.detectMultiScale(enhanced, scaleFactor=1.1, minNeighbors=5,
                                   minSize=(40, 40))
        if len(det):
            found.extend(det.tolist())
        # Horizontally flipped (catches right-facing profiles)
        det_f = cas.detectMultiScale(cv2.flip(enhanced, 1), scaleFactor=1.1,
                                     minNeighbors=5, minSize=(40, 40))
        for fx, fy, fw2, fh2 in (det_f.tolist() if len(det_f) else []):
            found.append((sw - fx - fw2, fy, fw2, fh2))   # mirror x back

    return found


def _detect_facecam(mp4: Path, duration: float) -> tuple | None:
    """Multi-frame face cam detection: 9 frames across the full clip, majority vote.

    Combines:
    - CLAHE preprocessing for dark webcam overlays
    - Frontal + profile cascades (both orientations) to catch 3/4-view faces
    - 9 evenly-spaced frames (10%–90% of clip) for temporal coverage
    - Majority vote: ≥3/9 frames agreeing at the same position → confirmed

    Returns (x, y, w, h) crop region (face + padding) in source pixels, or None.
    """
    try:
        import cv2
    except ImportError:
        return None

    cas_frontal = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    profile_xml = cv2.data.haarcascades + "haarcascade_profileface.xml"
    cas_profile = cv2.CascadeClassifier(profile_xml) if profile_xml else None
    if cas_profile and cas_profile.empty():
        cas_profile = None

    TOLERANCE = 120   # px — face cam widget doesn't move between frames
    MIN_VOTES = 3     # need agreement in at least 3 out of 9 frames

    all_hits: list[tuple] = []   # (cx, cy, fx, fy, rw, rh, fw, fh)
    for i in range(1, 10):       # t = 0.1, 0.2, …, 0.9
        t     = min(duration * i / 10, duration - 0.5)
        frame = _extract_frame(mp4, t)
        if frame is None:
            continue
        import cv2 as _cv2
        fh, fw  = frame.shape[:2]
        gray    = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)
        strip_y = int(fh * 0.667)
        strip   = gray[strip_y:, :]

        detections = _faces_in_strip(strip, cas_frontal, cas_profile)
        if not detections:
            continue
        fx, fy, rw, rh = max(detections, key=lambda f: f[2] * f[3])
        fy += strip_y
        all_hits.append((fx + rw // 2, fy + rh // 2, fx, fy, rw, rh, fw, fh))

    if len(all_hits) < MIN_VOTES:
        return None

    # Vote: position supported by the most frames wins
    best, best_votes = None, 0
    for cx1, cy1, fx, fy, rw, rh, fw, fh in all_hits:
        votes = sum(
            1 for cx2, cy2, *_ in all_hits
            if abs(cx1 - cx2) < TOLERANCE and abs(cy1 - cy2) < TOLERANCE
        )
        if votes > best_votes:
            best_votes, best = votes, (fx, fy, rw, rh, fw, fh)

    if best_votes < MIN_VOTES:
        return None

    fx, fy, rw, rh, fw, fh = best
    pad = max(rw, rh)
    x0  = max(0,  fx - pad)
    y0  = max(0,  fy - pad)
    x1  = min(fw, fx + rw + pad)
    y1  = min(fh, fy + rh + pad)
    log.info("  face cam: %d/9 frames agree face=(%d,%d %dx%d) crop=(%d,%d %dx%d)",
             best_votes, fx, fy, rw, rh, x0, y0, x1 - x0, y1 - y0)
    return (x0, y0, x1 - x0, y1 - y0)


# ── clip trimming ─────────────────────────────────────────────────────────────

def _trim_clip(mp4: Path, out: Path, duration: float, best_s: float,
               length: float, pre_roll: float) -> Path:
    """Stream-copy trim to `length` seconds with only `pre_roll` s of build-up before the
    best moment — Shorts live or die in the first ~2 s, so lead with the heat, not the lull."""
    start = max(0.0, best_s - pre_roll)
    end   = min(duration, start + length)
    start = max(0.0, end - length)                # re-anchor when near the end
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(mp4),
         "-t", f"{end - start:.3f}", "-c", "copy", str(out)],
        capture_output=True, check=True,
    )
    return out


# ── TikTok-style ASS captions ─────────────────────────────────────────────────

def _write_tiktok_ass(words: list[dict], path: Path, margin_v: int = 80) -> None:
    """One word per cue, Arial Black 64 px, white with thick black outline.

    margin_v: pixels from the BOTTOM of the 1920 px frame (default 80).
    Positioned at the very bottom so captions never overlap the LoL HUD.
    """
    def ts(s: float) -> str:
        h, rem = divmod(s, 3600)
        m, s2  = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s2:06.3f}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {TARGET_W}\n"
        f"PlayResY: {TARGET_H}\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        # Arial Black, size 64, white fill (&H00FFFFFF), yellow karaoke (&H0000FFFF),
        # black 6 px outline (&H00000000), bold (-1), bottom-center (Alignment=2)
        f"Style: Cap,Arial Black,64,&H00FFFFFF,&H0000FFFF,&H00000000,"
        f"&H00000000,-1,0,0,0,100,110,2,0,1,6,0,2,60,60,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    events = []
    for w in words:
        word = (w["word"]
                .replace("{", "").replace("}", "").replace("\\", "")
                .replace("<", "").replace(">", "")
                .strip())
        if not word:
            continue
        t0 = w["start"]
        t1 = max(t0 + 0.15, w["end"])
        events.append(f"Dialogue: 0,{ts(t0)},{ts(t1)},Cap,,0,0,0,,{word.upper()}")

    path.write_text(header + "\n".join(events), encoding="utf-8-sig")


# ── single-pass render ────────────────────────────────────────────────────────

def _render_short(mp4: Path, out: Path, facecam: tuple | None,
                  vo: Path | None, ass: Path | None,
                  overlay_text: str, game_h: int) -> None:
    """Render the final Short in ONE ffmpeg pass.

    filter_complex contains:
      - 1080x1920 vertical transform (blur-bg or split)
      - ASS caption overlay (TikTok style, if words exist)
      - Meme-text drawtext (if overlay_text is set)
      - Audio: clip audio ducked under VO for VO duration, clip audio alone after
    """
    face_h = TARGET_H - game_h

    # ── video filter graph ──────────────────────────────────────────
    parts: list[str] = []

    if facecam:
        cx, cy, cw, ch = facecam
        parts += [
            # Split into gameplay stream and face cam stream
            "[0:v]split=2[vgame][vface]",
            # Gameplay: scale to fill the top panel (center-crop to 1080 x game_h)
            f"[vgame]scale={TARGET_W}:{game_h}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{game_h}[game]",
            # Face cam: crop the detected corner region, scale to fill bottom panel
            f"[vface]crop={cw}:{ch}:{cx}:{cy},"
            f"scale={TARGET_W}:{face_h}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{face_h}[face]",
            # Stack: gameplay top, face cam bottom
            "[game][face]vstack=inputs=2[cur]",
        ]
    else:
        parts += [
            # Split into background (blurred fill) and foreground (pillar-boxed)
            "[0:v]split=2[vbg][vfg]",
            f"[vbg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},boxblur=20:5[bg]",
            f"[vfg]scale={TARGET_W}:-2[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[cur]",
        ]

    cur = "cur"

    # ── ASS caption overlay ─────────────────────────────────────────
    if ass and ass.exists():
        nxt = "subs"
        parts.append(f"[{cur}]ass='{_ass_esc(ass)}'[{nxt}]")
        cur = nxt

    # ── meme/hook text drawtext ─────────────────────────────────────
    if overlay_text:
        safe = _clean_overlay(overlay_text)
        if safe:
            # TOP third — sound-off hook readable in the first frame, clear of YouTube's
            # top UI and of the bottom speech captions (research: hook text, top third).
            nxt = "ovr"
            parts.append(
                f"[{cur}]drawtext=text='{safe}'"
                f":fontsize=66:fontcolor=white"
                f":bordercolor=black:borderw=6"
                f":box=1:boxcolor=black@0.40:boxborderw=14"
                f":x=(w-text_w)/2:y=h*0.12[{nxt}]"
            )
            cur = nxt

    video_graph = ";".join(parts)

    # ── audio ───────────────────────────────────────────────────────
    vo_input:    list[str] = []
    audio_parts: list[str] = []
    audio_map:   list[str]

    if vo and vo.exists():
        vo_input    = ["-i", str(vo)]
        audio_parts = [
            "[0:a]volume=0.25[ga]",
            "[1:a]volume=1.0[va]",
            # duration=first: mix runs for clip's full length;
            # VO plays until it ends, then only the (ducked) clip audio continues.
            "[ga][va]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        ]
        audio_map = ["-map", "[aout]"]
    else:
        audio_map = ["-map", "0:a?"]

    full_graph = ";".join(parts + audio_parts)

    cmd = [
        "ffmpeg", "-y", "-i", str(mp4), *vo_input,
        "-filter_complex", full_graph,
        "-map", f"[{cur}]", *audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr)


# ── Gemini helper ─────────────────────────────────────────────────────────────

def _gemini_call(prompt: str, cm: dict, key: str) -> str:
    from ..production.commentary import _gemini_text
    try:
        r = _gemini_text(prompt, cm)
        return (r or {}).get(key, "") or ""
    except Exception as e:
        log.warning("  Gemini call failed: %s", e)
        return ""


# ── upload ────────────────────────────────────────────────────────────────────

def _upload(mp4: Path, title: str, description: str, tags: list,
            privacy: str, data: Path) -> str | None:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from .upload import _credentials
        from ..config import ROOT
        yt   = build("youtube", "v3", credentials=_credentials(ROOT, data))
        body = {
            "snippet": {
                "title":       title[:100],
                "description": description[:4900],
                "tags":        tags[:30],
                "categoryId":  "20",
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(mp4), chunksize=8 * 1024 * 1024, resumable=True)
        req   = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp  = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]
    except Exception as e:
        log.warning("Short upload failed: %s", e)
        return None


# ── stage entry ───────────────────────────────────────────────────────────────

def run(cfg: dict, state, date_label: str) -> Path:
    sh = cfg.get("shorts", {})
    if not sh.get("enabled", False):
        log.info("shorts disabled")
        return Path(cfg["paths"]["data_abs"])

    data    = Path(cfg["paths"]["data_abs"])
    work    = data / "work" / date_label
    raw_dir = data / "raw"  / date_label
    out_dir = work / "shorts"
    out_dir.mkdir(exist_ok=True)

    src = work / "vlm_filtered.json"
    if not src.exists():
        log.info("shorts: no vlm_filtered.json — skipping")
        return work
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]

    count      = sh.get("count", 3)
    candidates = sorted(clips, key=lambda c: -c.get("api_rank_score", 0))[:count]

    cm = {
        "gemini_model":   cfg.get("commentary", {}).get("gemini_model", "gemini-2.0-flash-lite"),
        "gemini_api_key": cfg.get("api_judge",  {}).get("api_key", ""),
    }
    tts_cfg      = cfg.get("tts", {})
    tts_provider = tts_cfg.get("provider", "edge-tts")
    tts_voice    = tts_cfg.get("voice", "male-us")
    privacy      = sh.get("privacy", "public")
    detect_face  = sh.get("detect_facecam", True)
    target_s     = min(float(sh.get("target_seconds", 32)), MAX_SHORT_S)  # punchy: 20-35 s wins
    pre_roll     = float(sh.get("pre_roll_s", 7))                          # build-up before the heat

    done: dict = {}
    done_f = out_dir / "done.json"
    if done_f.exists():
        done = json.loads(done_f.read_text(encoding="utf-8"))

    for c in candidates:
        clip_id  = c["id"]
        if done.get(clip_id):
            log.info("short %s already done — skip", clip_id)
            continue

        mp4 = raw_dir / f"{clip_id}.mp4"
        if not mp4.exists():
            lp = c.get("local_path") or ""
            if lp:
                mp4 = Path(lp)
        if not mp4.exists():
            log.warning("short: %s mp4 missing — skip", clip_id)
            continue

        duration = float(c.get("duration", 30))
        streamer  = _ascii_name(c)   # ASCII-safe: login fallback for JP/KR/CN names
        summary   = c.get("vlm_summary") or c.get("title", "")
        log.info("short: %s — %s (%.0f s)", clip_id, streamer, duration)

        # 1. Trim to a punchy length, leading close to the action (Short retention)
        clip_src = mp4
        trim_tmp: Path | None = None
        if duration > target_s:
            trim_tmp = out_dir / f"{clip_id}_trim.mp4"
            best_s   = float(c.get("api_best_moment_s") or duration / 2)
            try:
                clip_src = _trim_clip(mp4, trim_tmp, duration, best_s, target_s, pre_roll)
                log.info("  trimmed %.0f s -> %.0f s (best moment %.1f s, pre-roll %.0f s)",
                         duration, target_s, best_s, pre_roll)
            except subprocess.CalledProcessError as e:
                log.warning("  trim failed — using full clip: %s", e)
                trim_tmp = None

        # 2. Face cam detection (bottom corners only)
        facecam = _detect_facecam(clip_src, min(duration, target_s)) if detect_face else None
        game_h  = int(TARGET_H * 0.60) if facecam else TARGET_H
        face_h  = TARGET_H - game_h

        # 3. Streamer speech → English (shared faster-whisper): drives captions + context
        short_s = min(duration, target_s)
        speech_words, speech_text = [], ""
        try:
            from ..enrichment.transcribe import transcribe as _transcribe
            tr = _transcribe(clip_src, cfg.get("transcribe", {}).get("model", "small"), short_s)
            speech_words, speech_text = tr["words"], tr["text"]
            if speech_text:
                log.info("  speech [%s]: %s", tr["lang"], speech_text[:80])
        except Exception as e:
            log.warning("  transcription failed: %s", e)

        # 4. Informative English VO line (Gemini → simple fallback so captions always exist)
        vo_line = ""
        if cm["gemini_api_key"]:
            vo_line = _gemini_call(
                _VO_PROMPT.format(streamer=streamer, summary=summary[:120],
                                  speech=speech_text[:200] or "(none)"), cm, "line")
        if not vo_line:
            vo_line = f"Here's {streamer} — let's see how this plays out."

        # 5. TTS synthesis -> MP3
        vo_path: Path | None = None
        if vo_line:
            from ..production.tts import synthesize
            vo_mp3 = out_dir / f"{clip_id}_vo.mp3"
            try:
                synthesize(vo_line, vo_mp3, tts_voice, provider=tts_provider)
                vo_path = vo_mp3
                log.info("  VO: %s", vo_line[:80])
            except Exception as e:
                log.warning("  short TTS failed: %s — no VO for this clip", e)

        # 6. English captions from the translated speech (bottom — never over the HUD)
        ass_path: Path | None = None
        if speech_words:
            ass_path = out_dir / f"{clip_id}.ass"
            _write_tiktok_ass(speech_words, ass_path, margin_v=80)
            log.info("  captions: %d words", len(speech_words))

        # 7. Meme text overlay
        overlay_text = ""
        if cm["gemini_api_key"]:
            raw = _gemini_call(
                _OVERLAY_PROMPT.format(streamer=streamer, summary=summary[:80]),
                cm, "text")
            overlay_text = _clean_overlay(raw)

        # 8. Single-pass render
        v_final = out_dir / f"{clip_id}.mp4"
        try:
            _render_short(clip_src, v_final, facecam, vo_path,
                          ass_path, overlay_text, game_h)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[-500:]
            log.warning("  render failed for %s:\n%s", clip_id, stderr)
            v_final.unlink(missing_ok=True)
            continue
        finally:
            if trim_tmp:
                trim_tmp.unlink(missing_ok=True)
            if ass_path:
                ass_path.unlink(missing_ok=True)

        log.info("  rendered -> %s", v_final.name)

        # 9. Upload
        if sh.get("upload", True):
            broadcaster_url = f"https://twitch.tv/{streamer.lower()}"
            # English title generated from the (English) summary + translated speech —
            # NEVER the raw Twitch clip title, which is often the streamer's own language.
            title_en = ""
            if cm["gemini_api_key"]:
                title_en = _gemini_call(
                    _TITLE_PROMPT.format(streamer=streamer, summary=summary[:120],
                                         speech=speech_text[:200] or "(none)"), cm, "title")
            title_en = re.sub(r"[^\x00-\x7F]", "", title_en).strip().strip('"')
            if not title_en:
                title_en = f"{streamer} had to make this work"
            title = f"{title_en[:80]} #Shorts"
            description = (
                f"{vo_line or summary}\n\n"
                f"Clip by: {streamer} — {broadcaster_url}\n"
                f"Full daily highlights on the channel!\n\n"
                f"#LeagueOfLegends #LoL #Shorts #TwitchClips"
            )
            tags = ["league of legends", "lol", "shorts", "twitch clips",
                    "lol highlights", streamer.lower()]
            vid_id = _upload(v_final, title, description, tags, privacy, data)
            if vid_id:
                url = f"https://youtube.com/shorts/{vid_id}"
                log.info("  uploaded: %s", url)
                done[clip_id] = {"youtube_id": vid_id, "url": url}
            else:
                done[clip_id] = {"rendered": str(v_final)}
        else:
            done[clip_id] = {"rendered": str(v_final)}

    done_f.write_text(json.dumps(done, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("shorts: %d processed", len(done))
    return work
