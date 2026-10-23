"""Stage 11 — YouTube Shorts: 3-5 vertical clips posted daily.

Per clip:
  1. Trim to ≤55 s (centered on api_best_moment_s when the clip is longer).
  2. Face cam detection (9 frames, whole frame, CLAHE + frontal + profile cascades;
     a candidate must be live video with skin, see pick_facecam).
  3. One-pass ffmpeg render at 1080×1920 (blur-bg or split layout).
  4. Streamer speech → English via the shared enrichment.transcribe (faster-whisper),
     rendered as word-level captions at the BOTTOM of the frame (clear of the HUD + the
     Shorts UI) — skipped when the source already burns in English captions (the same
     enrichment.burned_captions rule as the long-form video). NO voiceover, NO AI overlay text — translate the speech and caption it.
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
references what happens. No hashtags, no quotes, no emoji. Family friendly (YouTube ad
guidelines): no swearing, no "kill himself"/suicide wording; game violence is fine.{name_rule}
Also write HOOK: 2-4 words in ALL CAPS shown big over the first seconds of the video, so a
scrolling viewer knows why to stay (e.g. "WATCH THE FLASH", "1V3 IN BASE", "HE CALLED IT").
Streamer: {streamer}
What happens (English): {summary}
What the streamer said (English, may be empty): {speech}
Answer ONLY JSON: {{"title": "...", "hook": "..."}}"""

# owner 2026-10-21: Shorts search traffic is streamer names, so the title opens with one
_NAME_RULE = "\nSTART the title with the streamer's name exactly as given."
# `video.fame` (owner 2026-10-23): famous names lead; unknown streamers are described in
# words anyone gets and their name is appended (" | name") for search
_FAMOUS_RULE = ("\nThe streamer is FAMOUS: START the title with their name exactly as given, "
                "and the HOOK may use it too (e.g. \"BAUS 1V3 IN BASE\").")
_UNKNOWN_RULE = ("\nThe streamer is not famous: do NOT use their name in the title or HOOK. "
                 "Say who they are in words anyone understands (\"This Streamer\", "
                 "\"A Silver Player\", \"This Jungler\"). Keep the title under 50 characters.")


def name_suffix(title: str, name: str, limit: int = 80) -> str:
    """Append ' | name' for search unless the title already has it. Pure."""
    if not name or name.lower() in title.lower():
        return title[:limit]
    tail = f" | {name}"
    return title[:limit - len(tail)].rstrip() + tail


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


SPEAKER_COLORS = ["0xFFE600", "0x40E0D0", "0xFF9A00"]   # panel A, B, C (left to right)


def caption_groups(words: list[dict], v: dict | None = None) -> list[dict]:
    """The caption phrases of a Short (assemble._caption_groups with the video.* keys).
    Shared by the renderer and the speaker labelling so phrase i is the same in both."""
    from ..production.assemble import _caption_groups
    v = v or {}
    return _caption_groups(words,
                           max_words=int(v.get("caption_max_words", 3)),
                           max_gap=float(v.get("caption_group_gap", 0.65)),
                           max_seconds=float(v.get("caption_max_seconds", 1.9)),
                           min_seconds=float(v.get("caption_min_seconds", 0.62)),
                           mask=v.get("caption_mask_words") or (),
                           censor=str(v.get("caption_censor", "") or ""))


def caption_style(who: str | None, n_panels: int, cap_mv: int, fontsize: int) -> tuple:
    """(x expression, y, colour, fontsize) for one phrase. Pure (tested).

    No speaker info: centred, white, `cap_mv` above the bottom (as before). A duo Short
    (n_panels >= 2): a panel's speaker is drawn INSIDE their panel, near its bottom, in
    that panel's colour; "other" voices stay centred above the panels in white. One
    panel: the streamer stays white, other voices are teal — so a second voice reads as
    a second person."""
    y = TARGET_H - cap_mv - fontsize
    if who is None:
        return "(w-text_w)/2", y, "white", fontsize
    k = ord(who) - 65 if len(who) == 1 and who.isalpha() else -1
    if n_panels >= 2 and 0 <= k < n_panels:
        cell = TARGET_W // n_panels
        fs = min(fontsize, 50)
        return (f"'max(8,{k * cell}+({cell}-text_w)/2)'", TARGET_H - 64 - fs,
                SPEAKER_COLORS[k % len(SPEAKER_COLORS)], fs)
    if n_panels >= 2:
        return "(w-text_w)/2", y, "white", fontsize
    return "(w-text_w)/2", y, ("white" if k == 0 else "0x40E0D0"), fontsize


def _caption_filters(words: list[dict], cap_mv: int, fontsize: int = 64,
                     v: dict | None = None, speakers: list | None = None,
                     n_panels: int = 0) -> str:
    """One drawtext per caption PHRASE (only one visible at a time), centred, white with a
    black outline, its BOTTOM sitting `cap_mv` px above the frame bottom. Returns a
    comma-joined filterchain. drawtext+fontfile renders everywhere (no fontconfig/libass
    font matching).

    Grouping and the disjoint-window guarantee come from assemble._caption_groups — this
    had the same one-word-at-a-time flashing and the same overlap arithmetic as the
    long-form captions, so it gets the same fix and the same config keys.
    `speakers` (one label per phrase, enrichment/speakers) colour-codes and places each
    phrase per caption_style."""
    font = _cap_font()
    seg = []
    for i, g in enumerate(caption_groups(words, v)):
        text = _clean_overlay(g["text"].replace("'", "’")).upper()
        if not text:
            continue
        who = speakers[i] if speakers and i < len(speakers) else None
        x, y, color, fs = caption_style(who, n_panels, cap_mv, fontsize)
        seg.append(
            f"drawtext=fontfile='{font}':text='{text}':fontsize={fs}:fontcolor={color}:"
            f"borderw=7:bordercolor=black:x={x}:y={y}:"
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


def _render_short(mp4: Path, out: Path, facecam: "tuple | list | None",
                  caption_words: list[dict], cap_mv: int, game_h: int,
                  cfg: dict, total_h: int = TARGET_H,
                  speakers: list | None = None, hook: str = "",
                  name_tag: str = "") -> None:
    """Render the final Short in ONE ffmpeg pass: a 1080x1920 vertical transform
    (blur-bg or split) + drawtext speech captions. Audio is the clip's own audio
    (no voiceover, no music).

    `facecam` is one box or a list of them (duo streams: the streamers sit side by side
    in the lower panel, left to right as on the stream)."""
    face_h = total_h - game_h
    parts: list[str] = []
    cams = ([facecam] if isinstance(facecam, tuple) else list(facecam or []))[:3]
    cams.sort(key=lambda b: (b[0], b[1]))     # same order the speakers were labelled in

    if cams:
        n = len(cams)
        cell_w = TARGET_W // n // 2 * 2
        parts.append(f"[0:v]split={n + 1}[vgame]" + "".join(f"[vf{i}]" for i in range(n)))
        parts.append(f"[vgame]scale={TARGET_W}:{game_h}:force_original_aspect_ratio=increase,"
                     f"crop={TARGET_W}:{game_h}[game]")
        for i, (cx, cy, cw, ch) in enumerate(cams):
            parts.append(f"[vf{i}]crop={cw}:{ch}:{cx}:{cy},"
                         f"scale={cell_w}:{face_h}:force_original_aspect_ratio=increase,"
                         f"crop={cell_w}:{face_h}[face{i}]")
        if n == 1:
            parts.append("[game][face0]vstack=inputs=2[cur]")
        else:
            row = "".join(f"[face{i}]" for i in range(n))
            parts.append(f"{row}hstack=inputs={n},scale={TARGET_W}:{face_h}[faces]")
            parts.append("[game][faces]vstack=inputs=2[cur]")
    else:
        # No webcam: the gameplay sits centred over a blurred copy of itself. center_zoom
        # > 1 scales it up and crops the sides (the action is almost always mid-screen),
        # so a phone shows more of the fight and less of the HUD edges.
        zoom = max(1.0, float(cfg.get("shorts", {}).get("center_zoom", 1.0)))
        fg_w = int(TARGET_W * zoom) // 2 * 2
        parts += [
            "[0:v]split=2[vbg][vfg]",
            f"[vbg]scale={TARGET_W}:{total_h}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{total_h},boxblur=20:5[bg]",
            f"[vfg]scale={fg_w}:-2,crop={TARGET_W}:ih[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[cur]",
        ]

    cur = "cur"
    if caption_words:
        dt = _caption_filters(caption_words, cap_mv, v=cfg.get("video", {}),
                              speakers=speakers, n_panels=len(cams))
        if dt:
            parts.append(f"[{cur}]{dt}[cap]")
            cur = "cap"

    if name_tag:
        # `shorts.name_tag` (owner 2026-10-23): who this is, on screen the whole Short -
        # the same Twitch-purple tag the ranking Shorts put on the webcam panel
        from ..production.assemble import _font
        tag = _clean_overlay(name_tag)[:22]
        if tag:
            parts.append(
                f"[{cur}]drawtext=fontfile='{_font(tag, cfg.get('video', {}))}':text='{tag}':"
                f"fontsize=46:fontcolor=white:box=1:boxcolor=0x6441A5@0.85:boxborderw=14:"
                f"x=30:y=h-th-44[tag]")
            cur = "tag"

    if hook:
        # first-seconds hook (shorts.hook_text): big text over the top of the frame, gone
        # before the play, so a scrolling viewer knows why to stay
        from ..production.assemble import _esc, _font
        hk = cfg.get("shorts", {}).get("hook_text", {}) or {}
        secs = float(hk.get("seconds", 2.5))
        txt = _esc(hook)
        fs = int(min(104, 1000 / max(0.62 * len(txt), 1)))
        parts.append(
            f"[{cur}]drawtext=fontfile='{_font(txt, cfg.get('video', {}))}':text='{txt}':"
            f"fontsize={fs}:fontcolor=white:borderw=6:bordercolor=black:box=1:"
            f"boxcolor=black@0.45:boxborderw=22:x=(w-text_w)/2:y={int(hk.get('y', 210))}:"
            f"enable='lt(t,{secs:.2f})'[hook]")
        cur = "hook"

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

_TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"},
                                                 "hook": {"type": "string"}}}


def hook_variant(clip_id: str, share: float) -> bool:
    """A/B arm for the first-seconds hook text, stable per clip. Pure."""
    import hashlib
    return int(hashlib.md5(clip_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF < share


def clean_hook_text(text: str) -> str:
    """2-4 ASCII words, upper case, short enough for one line; '' if unusable. Pure."""
    t = re.sub(r"[^A-Z0-9 ?!'.*-]", "", (text or "").upper())
    t = re.sub(r"\s+", " ", t).strip()
    return t if t and len(t.split()) <= 4 and len(t) <= 22 else ""


def _write_title(cfg: dict, prompt: str) -> tuple[str, str]:
    """(title, hook) via the `commentary` role. ('', '') on any failure - the caller has
    a template fallback, and a missing title must never block an upload."""
    from ..providers import ProviderUnavailable, complete_json_chain
    try:
        r = complete_json_chain(cfg, "commentary", prompt, schema=_TITLE_SCHEMA) or {}
        from ..production.safe_text import mask as _safe
        return (_safe(r.get("title", "") or ""),
                clean_hook_text(_safe(r.get("hook", "") or "")))
    except ProviderUnavailable as e:
        log.info("  no title provider (%s) - using the template title", e)
        return "", ""
    except Exception as e:
        log.warning("  title generation failed: %s", e)
        return "", ""


# ── upload ────────────────────────────────────────────────────────────────────

def upscale(mp4: Path, cfg: dict) -> Path:
    """`shorts.upload_height` (owner 2026-10-19): the upload copy is upscaled once, as
    the long-form is (assemble.master) - YouTube encodes >1080p uploads with a better
    codec. Renders stay 1080x1920 (all overlay geometry is in those pixels). Returns the
    file to upload; the original on any failure or when off (0, the default)."""
    h = int((cfg.get("shorts", {}) or {}).get("upload_height", 0) or 0)
    if h <= TARGET_H:
        return mp4
    out = mp4.with_name(mp4.stem + f"_{h}p.mp4")
    if out.exists():
        return out
    w = int(round(TARGET_W * h / TARGET_H / 2)) * 2
    r = subprocess.run(["ffmpeg", "-y", "-i", str(mp4), "-vf",
                        f"scale={w}:{h}:flags=lanczos", *_shorts_enc(cfg),
                        "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)],
                       capture_output=True)
    if r.returncode != 0 or not out.exists():
        log.warning("  shorts: upscale failed, uploading 1080p: %s",
                    r.stderr[-200:].decode(errors="replace"))
        return mp4
    return out


def _upload(mp4: Path, title: str, description: str, tags: list,
            privacy: str, data: Path, publish_at=None, cfg: dict | None = None) -> str | None:
    """`publish_at`: the date's main video is scheduled (a catch-up night, see
    upload.publish_slot) - the Shorts go public with it, not before."""
    if cfg is not None:
        mp4 = upscale(mp4, cfg)
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        from ..config import ROOT
        from .upload import _credentials, status_for
        yt   = build("youtube", "v3", credentials=_credentials(ROOT, data))
        body = {
            "snippet": {
                "title":       title[:100],
                "description": description[:4900],
                "tags":        tags[:30],
                "categoryId":  "20",
            },
            "status": status_for(privacy, publish_at),
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
    rk = cfg.get("ranking_shorts", {})
    if rk.get("enabled", False):              # a ranking Short takes over these slots
        count = max(0, count - int(rk.get("replace_slots", 1)))
    candidates = sorted(clips, key=lambda c: -c.get("api_rank_score", 0))[:count]

    privacy      = sh.get("privacy", "public")
    from .upload import scheduled_time
    publish_at   = scheduled_time(state, date_label)   # catch-up night: wait for the video
    detect_face  = sh.get("detect_facecam", True)
    target_s     = min(float(sh.get("target_seconds", 32)), MAX_SHORT_S)  # punchy: 20-35 s wins
    pre_roll     = float(sh.get("pre_roll_s", 7))                          # build-up before the heat
    cap_margin   = int(sh.get("caption_margin_v", 430))                    # blur-bg: px above bottom
    cap_split_gap = int(sh.get("caption_split_gap", 190))                  # split: px above the facecam
                                                                            # (clears the in-game HUD too)

    # Same rule as the long-form video (assemble + enrichment/burned_captions): when the
    # streamer's own live-caption widget is baked into the clip and the speech is English,
    # ours would be a second copy of the same words. The detection is cached per clip in
    # work/<date>/burned_captions.json, which assemble already filled for these clips.
    burned: dict = {}
    try:
        from ..enrichment.burned_captions import detect as _detect_burned
        tr_path = work / "transcripts.json"
        tr_all = (json.loads(tr_path.read_text(encoding="utf-8").rstrip("\x00"))
                  if tr_path.exists() else {})
        burned = _detect_burned(cfg, date_label, candidates, tr_all)
    except Exception as e:                           # never let this block a Short
        log.warning("shorts: source-caption check failed (%s) - captioning all", e)

    done: dict = {}
    done_f = out_dir / "done.json"
    if done_f.exists():
        done = json.loads(done_f.read_text(encoding="utf-8"))

    from ..enrichment import fame as _fame
    fame = _fame.followers(cfg, candidates)
    use_fame = bool((cfg.get("video", {}).get("fame", {}) or {}).get("enabled", False))

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

        # 2. Where is the streamer (webcam or avatar)? enrichment/streamer_cam
        if detect_face:
            from ..enrichment.streamer_cam import find_all as _find_streamers
            facecam = _find_streamers(cfg, clip_src, min(duration, target_s)) or None
        else:
            facecam = None
        game_h  = int(TARGET_H * 0.60) if facecam else TARGET_H
        face_h  = TARGET_H - game_h

        # 3. Streamer speech → English captions (faster-whisper). No voiceover, no AI
        #    overlay text — per direction, we only translate the speech and caption it.
        short_s = min(duration, target_s)
        speech_words, speech_text = [], ""
        try:
            from ..enrichment.transcribe import Transcriber
            with Transcriber(cfg) as _transcribe:     # GPU crash = this clip on CPU
                tr = _transcribe(clip_src, short_s)
            speech_words, speech_text = tr["words"], tr["text"]
            if speech_text:
                log.info("  speech [%s]: %s", tr["lang"], speech_text[:80])
            from ..production.assemble import _is_english
            if speech_words and burned.get(clip_id) and _is_english(tr.get("lang")):
                log.info("  source already shows English captions - adding none")
                speech_words = []                    # speech_text still feeds the title
        except Exception as e:
            log.warning("  transcription failed: %s", e)

        # Colour-coded captions: who says each phrase (enrichment/speakers).
        speakers = None
        if facecam and speech_words and sh.get("speaker_colors", False):
            from ..enrichment.speakers import label_groups
            cams_sorted = sorted(facecam, key=lambda bx: (bx[0], bx[1]))
            speakers = label_groups(cfg, clip_src, caption_groups(speech_words,
                                                                  cfg.get("video", {})),
                                    cams_sorted)

        # split layout: caption bottom sits above the facecam panel (over gameplay, off
        # the face + in-game HUD); blur-bg layout: a high lower-third position.
        cap_mv = (face_h + cap_split_gap) if facecam else cap_margin
        if speech_words:
            log.info("  captions: %d words @ %dpx from bottom", len(speech_words), cap_mv)

        famous = _fame.is_famous(c.get("broadcaster_name", ""), fame, cfg)
        name_rule = ""
        if sh.get("title_names", False):
            name_rule = (_NAME_RULE if not use_fame else
                         _FAMOUS_RULE if famous else _UNKNOWN_RULE)

        # 4. Title + hook text first: the hook (A/B arm, shorts.hook_text) is burned in.
        #    English title from the (English) summary + translated speech - NEVER the raw
        #    Twitch clip title, which is often the streamer's own language.
        title_en, hook_en = _write_title(cfg, _TITLE_PROMPT.format(
            streamer=streamer, summary=summary[:120],
            speech=speech_text[:200] or "(none)",
            name_rule=name_rule))
        hk = sh.get("hook_text", {}) or {}
        arm = bool(hk.get("enabled", False)) and hook_variant(clip_id, float(hk.get("share", 0.5)))
        hook_used = hook_en if arm else ""
        if arm:
            log.info("  hook text (test arm): %s", hook_used or "(none written)")

        # 5. Single-pass render (clip audio only, drawtext captions, no VO/music)
        v_final = out_dir / f"{clip_id}.mp4"
        try:
            _render_short(clip_src, v_final, facecam, speech_words, cap_mv, game_h, cfg,
                          speakers=speakers, hook=hook_used,
                          name_tag=streamer if sh.get("name_tag", False) else "")
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[-500:]
            log.warning("  render failed for %s:\n%s", clip_id, stderr)
            v_final.unlink(missing_ok=True)
            continue
        finally:
            if trim_tmp:
                trim_tmp.unlink(missing_ok=True)

        log.info("  rendered -> %s", v_final.name)

        # 6. Upload
        # a hook-arm Short whose hook could not be written is a control in effect
        ab = {"hook_text": hook_used or None,
              "hook_arm": "hook" if hook_used else "control"} if hk.get("enabled") else {}
        if sh.get("upload", True):
            broadcaster_url = f"https://twitch.tv/{streamer.lower()}"
            title_en = re.sub(r"[^\x00-\x7F]", "", title_en).strip().strip('"')
            if not title_en:
                title_en = f"{streamer} had to make this work"
            if use_fame and sh.get("title_names", False) and not famous:
                title_en = name_suffix(title_en, streamer, 80)   # name kept for search
            title = f"{title_en[:80]} #Shorts"
            description = (
                f"{title_en}\n\n"
                f"Clip by: {streamer} — {broadcaster_url}\n"
                f"Full daily highlights on the channel!\n\n"
                f"#LeagueOfLegends #LoL #Shorts #TwitchClips"
            )
            tags = ["league of legends", "lol", "shorts", "twitch clips",
                    "lol highlights", streamer.lower()]
            vid_id = _upload(v_final, title, description, tags, privacy, data, publish_at,
                             cfg=cfg)
            if vid_id:
                url = f"https://youtube.com/shorts/{vid_id}"
                log.info("  uploaded: %s", url)
                done[clip_id] = {"youtube_id": vid_id, "url": url, "format": "clip", **ab}
            else:
                done[clip_id] = {"rendered": str(v_final), "format": "clip", **ab}
        else:
            done[clip_id] = {"rendered": str(v_final), "format": "clip", **ab}

    # The ranking Short (publishing/ranking_shorts): TOP N of one category from every
    # clip ever published. Its own state file makes it once-per-date.
    if rk.get("enabled", False) and "ranking" not in done:
        from . import ranking_shorts
        up = ((lambda mp4, title, desc, tags: _upload(mp4, title, desc, tags, privacy, data,
                                                      publish_at, cfg=cfg))
              if sh.get("upload", True) else None)
        try:
            entry = ranking_shorts.make(cfg, state, date_label, up)
        except Exception as e:                       # never costs the regular Shorts
            log.warning("ranking short failed: %s", e)
            entry = None
        if entry:
            done["ranking"] = entry

    done_f.write_text(json.dumps(done, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("shorts: %d processed", len(done))
    return work
