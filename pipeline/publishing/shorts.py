"""Stage 11 — YouTube Shorts: 3-5 vertical clips posted daily.

Per clip:
  1. Trim to ≤55 s (centered on api_best_moment_s when the clip is longer).
  2. Face cam detection (9 frames, whole frame, CLAHE + frontal + profile cascades;
     a candidate must be live video with skin, see pick_facecam).
  3. One-pass ffmpeg render at 1080×1920 (blur-bg or split layout).
  4. Streamer speech → English via the shared enrichment.transcribe (faster-whisper),
     rendered as word-level captions at the BOTTOM of the frame (clear of the HUD + the
     Shorts UI). NO voiceover, NO AI overlay text — translate the speech and caption it.
  5. Upload as a YouTube Short — English title generated from the summary + speech
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
from typing import TYPE_CHECKING

# cv2 and numpy are imported lazily inside the detection functions (opencv is only
# needed when shorts.detect_facecam is on). This gives the quoted annotations below
# their names without importing at runtime.
if TYPE_CHECKING:
    import cv2
    import numpy as np

log = logging.getLogger("pipeline.shorts")

TARGET_W, TARGET_H = 1080, 1920
MAX_SHORT_S = 55  # under YouTube's 60 s limit with buffer

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


# ── text helpers ──────────────────────────────────────────────────────────────

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
                    cas_profile: "cv2.CascadeClassifier | None",
                    min_size: int = 40) -> list[tuple]:
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
                                   minSize=(min_size, min_size))
        if len(det):
            found.extend(det.tolist())
        # Horizontally flipped (catches right-facing profiles)
        det_f = cas.detectMultiScale(cv2.flip(enhanced, 1), scaleFactor=1.1,
                                     minNeighbors=5, minSize=(min_size, min_size))
        for fx, fy, fw2, fh2 in (det_f.tolist() if len(det_f) else []):
            found.append((sw - fx - fw2, fy, fw2, fh2))   # mirror x back

    return found


def pick_facecam(clusters: list[dict], min_votes: int = 4, min_live: float = 3.0,
                 min_skin: float = 0.12) -> dict | None:
    """Choose the webcam among face candidates. Pure (no cv2), so it is unit-tested.

    Each cluster is one on-screen position where faces were found:
      votes = in how many of the sampled frames, live = median frame-to-frame change of
      the box (0-255 mean abs diff), skin = median share of skin-coloured pixels.
    A webcam is live video of a person: it changes between frames and carries skin.
    The Haar cascade's false positives were HUD champion portraits, champion-select
    icons and minimap art: static pixels (live ~0-2.6) or no skin (~0-0.07). Kill-feed
    portraits DO change and can look skin-toned, but appear in fewer frames than a
    webcam (6 vs 9 on the clip that showed it), so the most-voted survivor wins; ties go
    to the livelier one. 4+ of 9 frames: 3-vote clusters were art on menu screens.
    """
    ok = [c for c in clusters
          if c["votes"] >= min_votes and c["live"] >= min_live and c["skin"] >= min_skin]
    return max(ok, key=lambda c: (c["votes"], c["live"])) if ok else None


def _detect_facecam(mp4: Path, duration: float, min_live: float = 3.0,
                    min_skin: float = 0.12) -> tuple | None:
    """Find the streamer's webcam: 9 frames across the clip, faces anywhere in frame.

    - Frontal + profile Haar cascades (both orientations), CLAHE for dark webcams, on a
      960-px-wide copy of each frame. Whole frame: webcams also sit top-left/right, and
      the old bottom-third-only search missed those.
    - Hits are clustered by position; each cluster is scored for votes, liveness and
      skin, and pick_facecam() decides (see its docstring for why).

    Measured 2026-09-24 on the 52 downloaded clips of 2026-09-22: the old detector was
    wrong or missed on 13 (HUD/minimap/portrait false positives on 7).

    Returns (x, y, w, h) crop region (face + padding) in source pixels, or None.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    cas_frontal = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cas_profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    if cas_profile.empty():
        cas_profile = None

    frames, src_w, src_h = [], 0, 0
    for i in range(1, 10):                         # t = 0.1, 0.2, ..., 0.9
        f = _extract_frame(mp4, min(duration * i / 10, duration - 0.5))
        if f is None:
            continue
        src_h, src_w = f.shape[:2]
        frames.append(cv2.resize(f, (960, max(1, round(960 * src_h / src_w)))))
    if len(frames) < 3:
        return None
    H, W = frames[0].shape[:2]
    k = src_w / W                                  # small-frame px -> source px

    hits = []                                      # (frame_index, x, y, w, h)
    for fi, f in enumerate(frames):
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        for x, y, w, h in _faces_in_strip(gray, cas_frontal, cas_profile, min_size=26):
            hits.append((fi, x, y, w, h))

    clusters: list[list] = []                      # group hits at the same position
    for hit in hits:
        cx, cy = hit[1] + hit[3] / 2, hit[2] + hit[4] / 2
        for cl in clusters:
            _, x0, y0, w0, h0 = cl[0]
            if abs(cx - (x0 + w0 / 2)) < 0.06 * W and abs(cy - (y0 + h0 / 2)) < 0.08 * H:
                cl.append(hit)
                break
        else:
            clusters.append([hit])

    scored = []
    for cl in clusters:
        votes = len({h[0] for h in cl})
        if votes < 3:
            continue
        x, y, w, h = (int(np.median([hit[j] for hit in cl])) for j in (1, 2, 3, 4))
        boxes = [f[y:y + h, x:x + w] for f in frames]
        live = float(np.median([np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()
                                for a, b in zip(boxes, boxes[1:])]))
        skins = []
        for bx in boxes:
            ycc = cv2.cvtColor(bx, cv2.COLOR_BGR2YCrCb)
            skins.append(float(((ycc[..., 1] > 133) & (ycc[..., 1] < 173)
                                & (ycc[..., 2] > 77) & (ycc[..., 2] < 127)).mean()))
        scored.append(dict(votes=votes, live=live, skin=float(np.median(skins)),
                           box=(x, y, w, h)))

    best = pick_facecam(scored, min_live=min_live, min_skin=min_skin)
    if best is None:
        if scored:
            log.info("  face cam: none of %d candidates is a live face (%s)", len(scored),
                     ", ".join(f"{c['votes']}v live {c['live']:.1f} skin {c['skin']:.2f}"
                               for c in scored))
        return None

    x, y, w, h = best["box"]
    fx, fy, rw, rh = int(x * k), int(y * k), int(w * k), int(h * k)
    pad = max(rw, rh)
    x0, y0 = max(0, fx - pad), max(0, fy - pad)
    x1, y1 = min(src_w, fx + rw + pad), min(src_h, fy + rh + pad)
    log.info("  face cam: %d/9 frames, live %.1f, skin %.2f, face=(%d,%d %dx%d) "
             "crop=(%d,%d %dx%d)", best["votes"], best["live"], best["skin"],
             fx, fy, rw, rh, x0, y0, x1 - x0, y1 - y0)
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


# ── TikTok-style captions (drawtext: pixel-precise y, renders without fontconfig) ──

_CAP_FONTS = ["C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/System/Library/Fonts/Supplemental/Arial Black.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def _cap_font() -> str:
    """A colon-escaped bold fontfile path for drawtext (Arial Black → Bold → DejaVu)."""
    for f in _CAP_FONTS:
        if Path(f).exists():
            return f.replace(":", r"\:")
    return _CAP_FONTS[-1].replace(":", r"\:")


def _caption_filters(words: list[dict], cap_mv: int, fontsize: int = 64,
                     v: dict | None = None) -> str:
    """One drawtext per caption PHRASE (only one visible at a time), centred, white with a
    black outline, its BOTTOM sitting `cap_mv` px above the frame bottom. Returns a
    comma-joined filterchain. drawtext+fontfile renders everywhere (no fontconfig/libass
    font matching).

    Grouping and the disjoint-window guarantee come from assemble._caption_groups — this
    had the same one-word-at-a-time flashing and the same overlap arithmetic as the
    long-form captions, so it gets the same fix and the same config keys."""
    from ..production.assemble import _caption_groups
    v = v or {}
    font = _cap_font()
    y = TARGET_H - cap_mv - fontsize
    seg = []
    for g in _caption_groups(words,
                             max_words=int(v.get("caption_max_words", 3)),
                             max_gap=float(v.get("caption_group_gap", 0.65)),
                             max_seconds=float(v.get("caption_max_seconds", 1.9)),
                             min_seconds=float(v.get("caption_min_seconds", 0.62))):
        text = _clean_overlay(g["text"].replace("'", "’")).upper()
        if not text:
            continue
        seg.append(
            f"drawtext=fontfile='{font}':text='{text}':fontsize={fontsize}:fontcolor=white:"
            f"borderw=7:bordercolor=black:x=(w-text_w)/2:y={y}:"
            f"enable='between(t,{g['start']:.2f},{g['end']:.2f})'")
    return ",".join(seg)


# ── single-pass render ────────────────────────────────────────────────────────

def _shorts_enc(cfg: dict) -> list[str]:
    """Encoder flags for a Short. Uses the machine's detected encoder (see
    pipeline/hardware.py) at a slightly lower quality target than the long-form —
    Shorts are standalone, so they never have to match anything for concat."""
    from ..hardware import pick_encoder
    v = cfg.get("video", {})
    name = pick_encoder(str(v.get("encoder", "auto")))
    if name == "h264_nvenc":
        return ["-c:v", name, "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    if name == "h264_qsv":
        return ["-c:v", name, "-global_quality", "23"]
    if name == "h264_videotoolbox":
        return ["-c:v", name, "-q:v", "50"]
    if name == "h264_amf":
        return ["-c:v", name, "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]


def _render_short(mp4: Path, out: Path, facecam: tuple | None,
                  caption_words: list[dict], cap_mv: int, game_h: int,
                  cfg: dict) -> None:
    """Render the final Short in ONE ffmpeg pass: a 1080x1920 vertical transform
    (blur-bg or split) + drawtext speech captions. Audio is the clip's own audio
    (no voiceover, no music)."""
    face_h = TARGET_H - game_h
    parts: list[str] = []

    if facecam:
        cx, cy, cw, ch = facecam
        parts += [
            "[0:v]split=2[vgame][vface]",
            f"[vgame]scale={TARGET_W}:{game_h}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{game_h}[game]",
            f"[vface]crop={cw}:{ch}:{cx}:{cy},"
            f"scale={TARGET_W}:{face_h}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{face_h}[face]",
            "[game][face]vstack=inputs=2[cur]",
        ]
    else:
        # No webcam: the gameplay sits centred over a blurred copy of itself. center_zoom
        # > 1 scales it up and crops the sides (the action is almost always mid-screen),
        # so a phone shows more of the fight and less of the HUD edges.
        zoom = max(1.0, float(cfg.get("shorts", {}).get("center_zoom", 1.0)))
        fg_w = int(TARGET_W * zoom) // 2 * 2
        parts += [
            "[0:v]split=2[vbg][vfg]",
            f"[vbg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},boxblur=20:5[bg]",
            f"[vfg]scale={fg_w}:-2,crop={TARGET_W}:ih[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[cur]",
        ]

    cur = "cur"
    if caption_words:
        dt = _caption_filters(caption_words, cap_mv)
        if dt:
            parts.append(f"[{cur}]{dt}[cap]")
            cur = "cap"

    cmd = [
        "ffmpeg", "-y", "-i", str(mp4),
        "-filter_complex", ";".join(parts),
        "-map", f"[{cur}]", "-map", "0:a?",
        # Shorts are standalone (never concatenated), so they can carry their own
        # slightly lower quality target — but still use the detected encoder.
        *_shorts_enc(cfg),
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr)


# ── title generation ──────────────────────────────────────────────────────────

_TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}


def _write_title(cfg: dict, prompt: str) -> str:
    """English Shorts title via the `commentary` role. '' on any failure - the caller
    has a template fallback, and a missing title must never block an upload."""
    from ..providers import ProviderUnavailable, get_provider
    try:
        provider = get_provider(cfg, "commentary")
    except ProviderUnavailable as e:
        log.info("  no title provider (%s) - using the template title", e)
        return ""
    try:
        r = provider.complete_json(prompt, schema=_TITLE_SCHEMA)
        return (r or {}).get("title", "") or ""
    except Exception as e:
        log.warning("  title generation failed: %s", e)
        return ""


# ── upload ────────────────────────────────────────────────────────────────────

def _upload(mp4: Path, title: str, description: str, tags: list,
            privacy: str, data: Path) -> str | None:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        from ..config import ROOT
        from .upload import _credentials
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
        log.info("shorts: no vlm_filtered.json - skipping")
        return work
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]

    count      = sh.get("count", 3)
    candidates = sorted(clips, key=lambda c: -c.get("api_rank_score", 0))[:count]

    privacy      = sh.get("privacy", "public")
    detect_face  = sh.get("detect_facecam", True)
    target_s     = min(float(sh.get("target_seconds", 32)), MAX_SHORT_S)  # punchy: 20-35 s wins
    pre_roll     = float(sh.get("pre_roll_s", 7))                          # build-up before the heat
    cap_margin   = int(sh.get("caption_margin_v", 430))                    # blur-bg: px above bottom
    cap_split_gap = int(sh.get("caption_split_gap", 190))                  # split: px above the facecam
                                                                            # (clears the in-game HUD too)

    done: dict = {}
    done_f = out_dir / "done.json"
    if done_f.exists():
        done = json.loads(done_f.read_text(encoding="utf-8"))

    for c in candidates:
        clip_id  = c["id"]
        if done.get(clip_id):
            log.info("short %s already done - skip", clip_id)
            continue

        mp4 = raw_dir / f"{clip_id}.mp4"
        if not mp4.exists():
            lp = c.get("local_path") or ""
            if lp:
                mp4 = Path(lp)
        if not mp4.exists():
            log.warning("short: %s mp4 missing - skip", clip_id)
            continue

        duration = float(c.get("duration", 30))
        streamer  = _ascii_name(c)   # ASCII-safe: login fallback for JP/KR/CN names
        summary   = c.get("vlm_summary") or c.get("title", "")
        log.info("short: %s - %s (%.0f s)", clip_id, streamer, duration)

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
                log.warning("  trim failed - using full clip: %s", e)
                trim_tmp = None

        # 2. Face cam detection (anywhere in frame; live + skin gates)
        facecam = (_detect_facecam(clip_src, min(duration, target_s),
                                   float(sh.get("facecam_min_live", 3.0)),
                                   float(sh.get("facecam_min_skin", 0.12)))
                   if detect_face else None)
        game_h  = int(TARGET_H * 0.60) if facecam else TARGET_H
        face_h  = TARGET_H - game_h

        # 3. Streamer speech → English captions (faster-whisper). No voiceover, no AI
        #    overlay text — per direction, we only translate the speech and caption it.
        short_s = min(duration, target_s)
        speech_words, speech_text = [], ""
        try:
            from ..enrichment.transcribe import transcribe as _transcribe
            tc = cfg.get("transcribe", {})
            tr = _transcribe(clip_src, tc.get("model", "small"), short_s,
                             tc.get("device", "cpu"), tc.get("compute_type", "int8"))
            speech_words, speech_text = tr["words"], tr["text"]
            if speech_text:
                log.info("  speech [%s]: %s", tr["lang"], speech_text[:80])
        except Exception as e:
            log.warning("  transcription failed: %s", e)

        # split layout: caption bottom sits above the facecam panel (over gameplay, off
        # the face + in-game HUD); blur-bg layout: a high lower-third position.
        cap_mv = (face_h + cap_split_gap) if facecam else cap_margin
        if speech_words:
            log.info("  captions: %d words @ %dpx from bottom", len(speech_words), cap_mv)

        # 4. Single-pass render (clip audio only, drawtext captions, no VO/music)
        v_final = out_dir / f"{clip_id}.mp4"
        try:
            _render_short(clip_src, v_final, facecam, speech_words, cap_mv, game_h, cfg)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[-500:]
            log.warning("  render failed for %s:\n%s", clip_id, stderr)
            v_final.unlink(missing_ok=True)
            continue
        finally:
            if trim_tmp:
                trim_tmp.unlink(missing_ok=True)

        log.info("  rendered -> %s", v_final.name)

        # 5. Upload
        if sh.get("upload", True):
            broadcaster_url = f"https://twitch.tv/{streamer.lower()}"
            # English title generated from the (English) summary + translated speech —
            # NEVER the raw Twitch clip title, which is often the streamer's own language.
            title_en = _write_title(cfg, _TITLE_PROMPT.format(
                streamer=streamer, summary=summary[:120],
                speech=speech_text[:200] or "(none)"))
            title_en = re.sub(r"[^\x00-\x7F]", "", title_en).strip().strip('"')
            if not title_en:
                title_en = f"{streamer} had to make this work"
            title = f"{title_en[:80]} #Shorts"
            description = (
                f"{title_en}\n\n"
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
