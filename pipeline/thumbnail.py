"""Stage 9.5 — Cinematic thumbnail generator for the daily highlights video.

Composites a 1280×720 JPEG:
  - Background: champion splash art from Riot Data Dragon CDN (darkened, vignette,
    radial blur toward edges — free, no API key, Riot fan-content policy allows it)
  - Streamer face: circular portrait cropped from face-cam at or after the best moment
    (samples last 40% of clip, picks frame with the largest face — i.e. celebrating)
  - Achievement badge: PENTAKILL / QUADRA KILL / 1V5 OUTPLAY etc. (from vlm_summary)
  - Title text: "TOP N PLAYS" badge + hook line + streamer credits
  - Gameplay inset (bottom-left): best-moment frame when no face cam is found

Riot Data Dragon champion list + splash arts are cached in data/cache/ after first run.
Output: data/work/<date>/thumbnail.jpg
Called from run_daily as stage "thumbnail"; upload.py sets it as YouTube thumbnail.

AI enhancement (optional future step): pass the output JPEG through fal.ai / Replicate
img2img with prompt "cinematic action movie poster, dramatic lighting, epic fantasy" —
costs ~$0.02/image and adds dramatic flair on top of this compositing base.
"""
import json
import logging
import re
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("pipeline.thumbnail")

THUMB_W, THUMB_H = 1280, 720

# ── achievement detection ──────────────────────────────────────────────────────

_ACHIEVEMENTS = [
    (r"penta",                   "PENTAKILL"),
    (r"quadra\s*kill",           "QUADRA KILL"),
    (r"\b1\s*[vV]\s*5\b",        "1v5 OUTPLAY"),
    (r"\b1\s*[vV]\s*4\b",        "1v4 OUTPLAY"),
    (r"\b1\s*[vV]\s*3\b",        "1v3 OUTPLAY"),
    (r"triple\s*kill",           "TRIPLE KILL"),
    (r"\bsteal\b",               "OBJECTIVE STEAL"),
    (r"\bace\b",                 "TEAM ACE"),
    (r"\boutplay\b",             "OUTPLAYED"),
]

# (R, G, B) badge colors per achievement
_ACH_COLOR = {
    "PENTAKILL":        (170,  0, 240),
    "QUADRA KILL":      (220, 50,   0),
    "1v5 OUTPLAY":      (  0,160, 255),
    "1v4 OUTPLAY":      (  0,160, 255),
    "1v3 OUTPLAY":      (  0,160, 255),
    "TRIPLE KILL":      (200,140,   0),
    "OBJECTIVE STEAL":  (  0,200,  80),
    "TEAM ACE":         (200,  0,   0),
    "OUTPLAYED":        (220,140,   0),
}


def _achievement(summary: str) -> tuple[str, tuple] | None:
    text = (summary or "").lower()
    for pattern, label in _ACHIEVEMENTS:
        if re.search(pattern, text, re.I):
            return label, _ACH_COLOR.get(label, (200, 140, 0))
    return None


# ── Riot Data Dragon ───────────────────────────────────────────────────────────

_DDragon = "https://ddragon.leagueoflegends.com"


def _champion_map(cache_dir: Path) -> dict[str, str]:
    """Download + cache champion name → splash-key mapping from Data Dragon."""
    cached = cache_dir / "ddragon_champions.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    try:
        version = requests.get(f"{_DDragon}/api/versions.json", timeout=10).json()[0]
        champs  = requests.get(
            f"{_DDragon}/cdn/{version}/data/en_US/champion.json", timeout=10
        ).json()["data"]
        mapping = {v["name"].lower(): v["id"] for v in champs.values()}
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return mapping
    except Exception as e:
        log.warning("Could not fetch Data Dragon champion list: %s", e)
        return {}


def _detect_champion(summary: str, champ_map: dict[str, str]) -> str | None:
    text = (summary or "").lower()
    for name, champ_id in champ_map.items():
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return champ_id
    return None


def _fetch_splash(champ_id: str, cache_dir: Path) -> Optional["Image"]:
    from PIL import Image
    cached = cache_dir / f"splash_{champ_id}.jpg"
    if not cached.exists():
        url = f"{_DDragon}/cdn/img/champion/splash/{champ_id}_0.jpg"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            cached.write_bytes(r.content)
        except Exception as e:
            log.warning("Splash art fetch failed for %s: %s", champ_id, e)
            return None
    try:
        return Image.open(str(cached)).convert("RGB")
    except Exception:
        return None


# ── frame + face extraction ────────────────────────────────────────────────────

def _ffmpeg_frame(mp4: Path, t: float) -> Optional["Image"]:
    """Extract a single frame as a PIL Image at time t."""
    from PIL import Image
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(mp4),
             "-frames:v", "1", "-q:v", "2", str(tmp)],
            capture_output=True, check=True)
        return Image.open(str(tmp)).convert("RGB") if tmp.exists() else None
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _best_face_frame(mp4: Path, duration: float) -> tuple[Optional["Image"], Optional[tuple]]:
    """Sample the last 40% of the clip for a celebrating face cam.

    Returns (frame_image, face_box) where face_box is (x0,y0,x1,y1) in frame coords.
    Picks the frame with the largest detected face (most prominent reaction).
    Falls back to best-moment frame with no crop when no face is found.
    """
    try:
        import cv2
    except ImportError:
        return None, None

    from pipeline.shorts import _faces_in_strip

    cas_f = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile_xml = cv2.data.haarcascades + "haarcascade_profileface.xml"
    cas_p = cv2.CascadeClassifier(profile_xml)
    if cas_p.empty():
        cas_p = None

    best_frame, best_box, best_area = None, None, 0

    # Sample 8 frames from the last 40% (celebration window)
    start_frac = 0.60
    for i in range(8):
        t = duration * (start_frac + (1 - start_frac) * i / 7)
        t = min(t, duration - 0.5)
        frame = _ffmpeg_frame(mp4, t)
        if frame is None:
            continue

        import numpy as np
        arr    = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2GRAY)
        fh, fw = arr.shape
        strip_y = int(fh * 0.667)
        strip  = arr[strip_y:, :]
        dets   = _faces_in_strip(strip, cas_f, cas_p)
        if not dets:
            continue
        fx, fy, rw, rh = max(dets, key=lambda d: d[2] * d[3])
        fy += strip_y
        area = rw * rh
        if area > best_area:
            best_area  = area
            best_frame = frame
            # face + generous padding as the portrait crop
            pad   = int(max(rw, rh) * 1.3)
            cx, cy = fx + rw // 2, fy + rh // 2
            x0 = max(0,  cx - pad)
            y0 = max(0,  cy - pad)
            x1 = min(fw, cx + pad)
            y1 = min(fh, cy + pad)
            best_box = (x0, y0, x1, y1)

    return best_frame, best_box


# ── compositing helpers ────────────────────────────────────────────────────────

def _load_font(size: int) -> "ImageFont":
    from PIL import ImageFont
    for path in [
        r"C:\Windows\Fonts\Impact.ttf",
        r"C:\Windows\Fonts\ariblk.ttf",   # Arial Black
        r"C:\Windows\Fonts\arialbd.ttf",   # Arial Bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_text_outlined(draw: "ImageDraw", xy: tuple, text: str, font,
                        fill=(255, 255, 255), stroke_fill=(0, 0, 0),
                        stroke_width: int = 4, anchor: str = "mm") -> None:
    x, y = xy
    for dx in range(-stroke_width, stroke_width + 1):
        for dy in range(-stroke_width, stroke_width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=stroke_fill, anchor=anchor)
    draw.text((x, y), text, font=font, fill=fill, anchor=anchor)


def _vignette(size: tuple[int, int]) -> "Image":
    """Dark radial vignette (edges → black, center → transparent)."""
    from PIL import Image
    import numpy as np
    w, h   = size
    cx, cy = w / 2, h / 2
    ys, xs = np.mgrid[0:h, 0:w]
    dist   = np.sqrt(((xs - cx) / cx) ** 2 + ((ys - cy) / cy) ** 2)
    alpha  = np.clip((dist - 0.3) / 0.7, 0, 1) * 200   # max opacity 200/255
    arr    = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :, 3] = alpha.astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _circular_portrait(img: "Image", diameter: int, border_color=(220, 160, 0),
                        border_px: int = 6, glow_px: int = 14) -> "Image":
    """Square crop → circular portrait with glowing colored border."""
    from PIL import Image, ImageDraw, ImageFilter
    # Square-crop input
    w, h  = img.size
    side  = min(w, h)
    img   = img.crop(((w - side) // 2, (h - side) // 2,
                       (w + side) // 2, (h + side) // 2))
    img   = img.resize((diameter, diameter), Image.LANCZOS)

    # Glow ring (blurred larger circle in border_color)
    glow_d  = diameter + glow_px * 2
    glow    = Image.new("RGBA", (glow_d, glow_d), (0, 0, 0, 0))
    gd      = ImageDraw.Draw(glow)
    gd.ellipse((0, 0, glow_d - 1, glow_d - 1), fill=(*border_color, 200))
    glow    = glow.filter(ImageFilter.GaussianBlur(glow_px // 2))

    # Hard border ring
    ring_d = diameter + border_px * 2
    ring   = Image.new("RGBA", (ring_d, ring_d), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, ring_d - 1, ring_d - 1),
                                 fill=(*border_color, 255))

    # Circular mask for the portrait
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)

    # Compose: glow → ring → masked portrait
    out_size = (glow_d, glow_d)
    canvas   = Image.new("RGBA", out_size, (0, 0, 0, 0))
    gp       = (0, 0)
    canvas.paste(glow,   gp,   glow)
    rp       = ((glow_d - ring_d) // 2, (glow_d - ring_d) // 2)
    canvas.paste(ring,   rp,   ring)
    pp       = ((glow_d - diameter) // 2, (glow_d - diameter) // 2)
    canvas.paste(img.convert("RGBA"), pp, mask)
    return canvas


def _gameplay_inset(frame: "Image", w: int, h: int) -> "Image":
    """Scale + border the gameplay inset frame."""
    from PIL import Image, ImageDraw, ImageFilter
    frame = frame.copy()
    frame.thumbnail((w, h), Image.LANCZOS)
    fw, fh = frame.size

    # Soft edge: feathered vignette on the inset
    mask = Image.new("L", (fw, fh), 255)
    blur = 14
    for i in range(blur):
        frac = 255 * i // blur
        d = ImageDraw.Draw(mask)
        d.rectangle((i, i, fw - i - 1, fh - i - 1), outline=frac)
    mask = mask.filter(ImageFilter.GaussianBlur(blur // 2))

    result = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    result.paste(frame.convert("RGBA"), (0, 0))
    result.putalpha(mask)

    # Thin golden border
    bd = ImageDraw.Draw(result)
    bd.rectangle((0, 0, fw - 1, fh - 1), outline=(200, 160, 0, 200), width=2)
    return result


# ── main composition ───────────────────────────────────────────────────────────

def _compose(bg_img: Optional["Image"], face_img: Optional["Image"],
             gameplay_img: Optional["Image"],
             n_clips: int, hook: str,
             achievement: Optional[tuple[str, tuple]],
             streamers: list[str]) -> "Image":
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    # ── background ──────────────────────────────────────────────────────────
    if bg_img:
        bg = bg_img.convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS)
        # Desaturate → darken
        bg = ImageEnhance.Color(bg).enhance(0.55)
        bg = ImageEnhance.Brightness(bg).enhance(0.40)
        # Tint blue-purple (shadows)
        import numpy as np
        arr = np.array(bg, dtype=np.float32)
        arr[:, :, 2] = np.clip(arr[:, :, 2] * 1.3, 0, 255)   # lift blue
        arr[:, :, 0] = np.clip(arr[:, :, 0] * 0.85, 0, 255)  # reduce red
        bg = Image.fromarray(arr.astype(np.uint8), "RGB")
    else:
        # Dark navy fallback
        bg = Image.new("RGB", (THUMB_W, THUMB_H), (12, 10, 28))

    canvas = bg.convert("RGBA")

    # ── vignette ────────────────────────────────────────────────────────────
    canvas.alpha_composite(_vignette((THUMB_W, THUMB_H)))
    draw = ImageDraw.Draw(canvas)

    # ── face cam portrait (right side) ──────────────────────────────────────
    portrait_x = THUMB_W - 40  # right edge anchor (will be offset by portrait width)
    portrait_y = THUMB_H // 2  # vertical center

    has_face = face_img is not None
    if has_face:
        portrait_d  = 290
        portrait    = _circular_portrait(face_img, portrait_d,
                                         border_color=(220, 160, 0), border_px=7, glow_px=18)
        pw, ph      = portrait.size
        px          = THUMB_W - pw - 30
        py          = (THUMB_H - ph) // 2 + 20
        canvas.alpha_composite(portrait, (px, py))
        text_right  = px - 20                          # text stays left of portrait
    else:
        text_right  = THUMB_W - 40

    # ── gameplay inset (bottom-left, only when no face portrait) ────────────
    if gameplay_img and not has_face:
        inset = _gameplay_inset(gameplay_img, 430, 242)
        iw, ih = inset.size
        canvas.alpha_composite(inset, (30, THUMB_H - ih - 28))

    # ── "TOP N PLAYS" badge (top-right) ─────────────────────────────────────
    badge_font  = _load_font(34)
    badge_text  = f"TOP {n_clips} PLAYS"
    # badge pill background
    bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    btw  = bbox[2] - bbox[0] + 28
    bth  = bbox[3] - bbox[1] + 14
    bx   = THUMB_W - btw - 24
    by   = 22
    draw.rounded_rectangle((bx, by, bx + btw, by + bth),
                            radius=6, fill=(220, 160, 0, 240))
    draw.text((bx + 14, by + 7), badge_text, font=badge_font,
              fill=(15, 10, 5), anchor="lt")

    # ── achievement badge (center, large) ───────────────────────────────────
    text_cx = (text_right) // 2 if has_face else THUMB_W // 2
    y_cursor = 170

    if achievement:
        ach_label, ach_color = achievement
        ach_font  = _load_font(88)
        bbox = draw.textbbox((0, 0), ach_label, font=ach_font)
        atw  = bbox[2] - bbox[0]
        _draw_text_outlined(draw, (text_cx, y_cursor), ach_label, ach_font,
                            fill=(*ach_color, 255), stroke_fill=(0, 0, 0, 255),
                            stroke_width=5, anchor="mt")
        # shimmer underline
        ux = text_cx - atw // 2
        draw.rectangle((ux, y_cursor + 96, ux + atw, y_cursor + 100),
                        fill=(*ach_color, 180))
        y_cursor += 116

    # ── hook text ────────────────────────────────────────────────────────────
    hook_font  = _load_font(64)
    _draw_text_outlined(draw, (text_cx, y_cursor), hook.upper(), hook_font,
                        fill=(255, 255, 255, 255), stroke_fill=(0, 0, 0, 255),
                        stroke_width=4, anchor="mt")
    y_cursor += 80

    # ── streamer credits ─────────────────────────────────────────────────────
    if streamers:
        cr_text   = "ft. " + " · ".join(streamers[:4])
        cr_font   = _load_font(32)
        _draw_text_outlined(draw, (text_cx, y_cursor), cr_text, cr_font,
                            fill=(210, 210, 210, 230), stroke_fill=(0, 0, 0, 200),
                            stroke_width=3, anchor="mt")

    return canvas.convert("RGB")


# ── public API ────────────────────────────────────────────────────────────────

def generate(cfg: dict, date_label: str) -> Path | None:
    """Generate thumbnail.jpg for the given date. Returns path or None on failure."""
    th = cfg.get("thumbnail", {})
    if not th.get("enabled", True):
        log.info("thumbnail disabled")
        return None

    data      = Path(cfg["paths"]["data_abs"])
    work      = data / "work" / date_label
    raw_dir   = data / "raw"  / date_label
    cache_dir = data / "cache"
    out       = work / "thumbnail.jpg"

    # Load clip selection
    src = work / "vlm_filtered.json"
    if not src.exists():
        log.info("thumbnail: no vlm_filtered.json — skip")
        return None
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    if not clips:
        return None

    # Top clip drives the thumbnail
    top = max(clips, key=lambda c: c.get("api_rank_score", 0))
    duration = float(top.get("duration", 30))
    best_s   = float(top.get("api_best_moment_s") or duration * 0.6)

    mp4 = raw_dir / f"{top['id']}.mp4"
    if not mp4.exists():
        lp = top.get("local_path") or ""
        mp4 = Path(lp) if lp else mp4

    # Best-moment gameplay frame (always try, even if face also found)
    gameplay_frame = _ffmpeg_frame(mp4, best_s) if mp4.exists() else None

    # Face cam: search last 40% of clip for a celebrating face
    face_img = None
    if mp4.exists() and cfg.get("shorts", {}).get("detect_facecam", True):
        _, face_box = _best_face_frame(mp4, duration)
        if face_box and gameplay_frame:
            try:
                # Crop from the best-moment frame using the detected face region
                face_frame = _ffmpeg_frame(mp4, min(best_s + 2, duration - 0.5))
                if face_frame:
                    face_img = face_frame.crop(face_box)
            except Exception:
                pass

    # Champion from vlm_summary
    champ_map  = _champion_map(cache_dir)
    summary    = top.get("vlm_summary", "")
    champ_id   = _detect_champion(summary, champ_map)
    bg_img     = _fetch_splash(champ_id, cache_dir) if champ_id else None
    if not bg_img:
        log.info("  no champion detected from summary — using dark background")

    # Achievement badge
    achievement = _achievement(summary + " " + top.get("title", ""))

    # Hook text
    from .credits import _hook
    hook = _hook(clips)

    # Streamer credits (top 4, ASCII-safe names)
    from .commentary import _ascii_name
    ordered = sorted(clips, key=lambda c: -c.get("api_rank_score", 0))
    streamers = list(dict.fromkeys(_ascii_name(c) for c in ordered))[:4]

    # Compose
    try:
        img = _compose(
            bg_img       = bg_img,
            face_img     = face_img,
            gameplay_img = gameplay_frame,
            n_clips      = len(clips),
            hook         = hook,
            achievement  = achievement,
            streamers    = streamers,
        )
        img.save(str(out), "JPEG", quality=92)
        log.info("thumbnail -> %s  (%s  face=%s  champion=%s  achievement=%s)",
                 out.name, hook, "yes" if face_img else "no",
                 champ_id or "none", achievement[0] if achievement else "none")
        return out
    except Exception as e:
        log.warning("thumbnail composition failed: %s", e, exc_info=True)
        return None


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    generate(cfg, date_label)
    return data / "work" / date_label
