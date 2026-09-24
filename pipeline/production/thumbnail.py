"""Stage 9.5 — Cinematic thumbnail generator for the daily highlights video.

Composites a 1280×720 JPEG:
  - Background: champion splash art (Riot Data Dragon, free) with teal-shadow /
    warm-highlight cinematic grade + vignette + left-side text-legibility gradient
  - Streamer logo: Twitch profile_image_url in a gold Challenger-style ring border
    (cached per broadcaster_id in data/cache/pfp_*.jpg)
  - Achievement stamp: PENTAKILL / QUADRA KILL / 1v5 / etc. (from vlm_summary + title)
  - Hook text: from credits._hook() (best-clip title line)
  - Streamer credit line: "ft. Name1 · Name2 ..."

Riot Data Dragon assets cached in data/cache/; Twitch pfp cached in data/cache/pfp_*.
Output: data/work/<date>/thumbnail.jpg
upload.py sets it as the YouTube thumbnail after video upload.
"""

import json
import logging
import math
import re
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

# Pillow is imported lazily inside the functions that need it, so the pipeline can run
# stages that don't touch imaging without it. This block gives type checkers and
# linters the names used in the quoted annotations below without importing at runtime.
if TYPE_CHECKING:
    from PIL import Image, ImageFont

log = logging.getLogger("pipeline.thumbnail")

THUMB_W, THUMB_H = 1280, 720

# ── achievement detection ──────────────────────────────────────────────────────

_ACHIEVEMENTS = [
    (r"penta",          "PENTAKILL"),
    (r"quadra\s*kill",  "QUADRA KILL"),
    (r"\b1\s*v\s*5\b",  "1v5 OUTPLAY"),
    (r"\b1\s*v\s*4\b",  "1v4 OUTPLAY"),
    (r"\b1\s*v\s*3\b",  "1v3 OUTPLAY"),
    (r"triple\s*kill",  "TRIPLE KILL"),
    (r"\bsteal\b",      "OBJECTIVE STEAL"),
    (r"\bace\b",        "TEAM ACE"),
    (r"\boutplay\b",    "OUTPLAYED"),
]

_ACH_COLOR = {
    "PENTAKILL":       (255, 215,  50),
    "QUADRA KILL":     (255, 120,  20),
    "1v5 OUTPLAY":     ( 80, 200, 255),
    "1v4 OUTPLAY":     ( 80, 200, 255),
    "1v3 OUTPLAY":     ( 80, 200, 255),
    "TRIPLE KILL":     (255, 200,  40),
    "OBJECTIVE STEAL": ( 60, 230, 100),
    "TEAM ACE":        (255,  60,  60),
    "OUTPLAYED":       (255, 200,  60),
}


def _achievement(summary: str) -> tuple[str, tuple] | None:
    text = (summary or "").lower()
    for pattern, label in _ACHIEVEMENTS:
        if re.search(pattern, text, re.I):
            return label, _ACH_COLOR.get(label, (255, 215, 50))
    return None


# ── Riot Data Dragon ───────────────────────────────────────────────────────────

_DDRAGON = "https://ddragon.leagueoflegends.com"


def _champion_map(cache_dir: Path) -> dict[str, str]:
    cached = cache_dir / "ddragon_champions.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    try:
        version = requests.get(f"{_DDRAGON}/api/versions.json", timeout=10).json()[0]
        champs  = requests.get(
            f"{_DDRAGON}/cdn/{version}/data/en_US/champion.json", timeout=10
        ).json()["data"]
        mapping = {v["name"].lower(): v["id"] for v in champs.values()}
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return mapping
    except Exception as e:
        log.warning("champion map fetch failed: %s", e)
        return {}


def _detect_champion(summary: str, champ_map: dict[str, str]) -> str | None:
    text = (summary or "").lower()
    for name, champ_id in champ_map.items():
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            return champ_id
    return None


def _fetch_splash(champ_id: str, cache_dir: Path) -> Optional["Image.Image"]:
    from PIL import Image
    cached = cache_dir / f"splash_{champ_id}.jpg"
    if not cached.exists():
        url = f"{_DDRAGON}/cdn/img/champion/splash/{champ_id}_0.jpg"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            cached.write_bytes(r.content)
        except Exception as e:
            log.warning("splash fetch failed for %s: %s", champ_id, e)
            return None
    try:
        return Image.open(str(cached)).convert("RGB")
    except Exception:
        return None


# ── Twitch profile picture ─────────────────────────────────────────────────────

def _twitch_pfp(broadcaster_id: str, cache_dir: Path) -> Optional["Image.Image"]:
    """Fetch and cache the Twitch profile picture for a broadcaster."""
    import os

    from PIL import Image

    cached = cache_dir / f"pfp_{broadcaster_id}.jpg"
    if cached.exists():
        try:
            return Image.open(str(cached)).convert("RGB")
        except Exception:
            cached.unlink(missing_ok=True)

    client_id     = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.debug("TWITCH_CLIENT_ID/SECRET not set - skipping pfp")
        return None
    try:
        token = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": client_id, "client_secret": client_secret,
                    "grant_type": "client_credentials"},
            timeout=10,
        ).json()["access_token"]

        data = requests.get(
            "https://api.twitch.tv/helix/users",
            params={"id": broadcaster_id},
            headers={"Authorization": f"Bearer {token}", "Client-Id": client_id},
            timeout=10,
        ).json().get("data", [])

        if not data:
            return None

        url = data[0]["profile_image_url"].replace("-300x300", "-600x600")
        img_bytes = requests.get(url, timeout=10).content
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        cache_dir.mkdir(parents=True, exist_ok=True)
        img.save(str(cached), "JPEG", quality=92)
        return img
    except Exception as e:
        log.warning("pfp fetch failed for broadcaster %s: %s", broadcaster_id, e)
        return None


# ── Cinematic image processing ────────────────────────────────────────────────

def _cinematic_grade(img: "Image.Image") -> "Image.Image":
    """Teal-shadow / warm-highlight split tone + lifted S-curve contrast."""
    import numpy as np
    from PIL import Image
    arr = np.array(img).astype(np.float32) / 255.0
    arr = 0.5 + (arr - 0.5) * 1.30
    arr = np.clip(arr, 0, 1)
    lum       = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    shadow    = np.clip(1.0 - lum * 2.5, 0, 1)[:, :, np.newaxis]
    highlight = np.clip(lum * 2.0 - 1.1, 0, 1)[:, :, np.newaxis]
    arr[:, :, 0] -= shadow[:, :, 0] * 0.15
    arr[:, :, 1] += shadow[:, :, 0] * 0.04
    arr[:, :, 2] += shadow[:, :, 0] * 0.10
    arr[:, :, 0] += highlight[:, :, 0] * 0.10
    arr[:, :, 1] += highlight[:, :, 0] * 0.02
    arr[:, :, 2] -= highlight[:, :, 0] * 0.12
    return Image.fromarray(np.clip(arr * 255, 0, 255).astype(np.uint8))


def _vignette_overlay(img: "Image.Image", strength: float = 0.88) -> "Image.Image":
    import numpy as np
    from PIL import Image
    w, h   = img.size
    arr    = np.array(img).astype(np.float32)
    cx, cy = w / 2, h / 2
    Y, X   = np.ogrid[:h, :w]
    dist   = np.sqrt(((X - cx) / cx) ** 2 + ((Y - cy) / cy) ** 2)
    mask   = 1.0 - np.clip(dist * strength, 0, 1) ** 1.8
    arr   *= mask[:, :, np.newaxis]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _film_grain(img: "Image.Image", sigma: float = 4.0) -> "Image.Image":
    import numpy as np
    from PIL import Image
    arr   = np.array(img).astype(np.float32)
    grain = np.random.normal(0, sigma, arr.shape).astype(np.float32)
    return Image.fromarray(np.clip(arr + grain, 0, 255).astype(np.uint8))


# ── Challenger border ─────────────────────────────────────────────────────────

def _challenger_border(inner_d: int) -> "Image.Image":
    """RGBA image: gold ornate ring around a circle of diameter inner_d."""
    from PIL import Image, ImageDraw
    pad    = 48
    size   = inner_d + pad * 2
    cx = cy = size // 2
    r       = inner_d // 2

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(canvas)

    # Outer glow halo
    for i in range(28, 0, -1):
        a = int(120 * (1 - i / 28) ** 1.5)
        draw.ellipse([cx - r - i, cy - r - i, cx + r + i, cy + r + i],
                     outline=(255, 200, 60, a), width=3)

    # Dark backing ring (fill then cut interior to keep it ring-shaped)
    draw.ellipse([cx - r - 14, cy - r - 14, cx + r + 14, cy + r + 14],
                 fill=(15, 10, 4, 255))
    draw.ellipse([cx - r + 2, cy - r + 2, cx + r - 2, cy + r - 2],
                 fill=(0, 0, 0, 0))   # cut out interior

    # Gold rings
    draw.ellipse([cx - r - 13, cy - r - 13, cx + r + 13, cy + r + 13],
                 outline=(255, 220, 80, 255), width=4)
    draw.ellipse([cx - r - 7,  cy - r - 7,  cx + r + 7,  cy + r + 7],
                 outline=(200, 150, 30, 255), width=3)
    draw.ellipse([cx - r - 2,  cy - r - 2,  cx + r + 2,  cy + r + 2],
                 outline=(255, 240, 140, 255), width=2)

    # Cardinal ornament points
    for angle_deg in [0, 90, 180, 270]:
        rad = math.radians(angle_deg)
        bx  = cx + int((r + 22) * math.sin(rad))
        by  = cy - int((r + 22) * math.cos(rad))
        tip = (bx + int(10 * math.sin(rad)), by - int(10 * math.cos(rad)))
        perp = math.radians(angle_deg + 90)
        lx = bx + int(7 * math.sin(perp)); ly = by - int(7 * math.cos(perp))
        rx = bx - int(7 * math.sin(perp)); ry = by + int(7 * math.cos(perp))
        draw.polygon([tip, (lx, ly), (rx, ry)], fill=(255, 220, 80, 255))
        draw.ellipse([bx - 4, by - 4, bx + 4, by + 4], fill=(255, 240, 140, 255))

    # Diagonal diamonds
    for angle_deg in [45, 135, 225, 315]:
        rad  = math.radians(angle_deg)
        ox   = cx + int((r + 16) * math.sin(rad))
        oy   = cy - int((r + 16) * math.cos(rad))
        s    = 5
        perp = math.radians(angle_deg + 90)
        pts  = [
            (ox + int(s * math.sin(rad)),  oy - int(s * math.cos(rad))),
            (ox + int(s * math.sin(perp)), oy - int(s * math.cos(perp))),
            (ox - int(s * math.sin(rad)),  oy + int(s * math.cos(rad))),
            (ox - int(s * math.sin(perp)), oy + int(s * math.cos(perp))),
        ]
        draw.polygon(pts, fill=(200, 160, 40, 255))

    return canvas


def _circular_crop(img: "Image.Image", d: int) -> "Image.Image":
    from PIL import Image, ImageDraw
    img  = img.resize((d, d), Image.LANCZOS)
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
    out  = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), mask=mask)
    return out


# ── Text helpers ──────────────────────────────────────────────────────────────

def _font(size: int, bold: bool = True) -> "ImageFont.FreeTypeFont":
    from PIL import ImageFont
    candidates = [
        r"C:\Windows\Fonts\ariblk.ttf",   # Arial Black
        r"C:\Windows\Fonts\Impact.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",  # Arial Bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_outlined(draw, xy, text, font, fill, stroke, width):
    x, y = xy
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=stroke)
    draw.text(xy, text, font=font, fill=fill)


# ── Main composition ──────────────────────────────────────────────────────────

def _compose(
    bg_img:      Optional["Image.Image"],
    pfp_img:     Optional["Image.Image"],
    achievement: tuple[str, tuple] | None,
    hook:        str,
    stat_line:   str,
    streamer:    str,
) -> "Image.Image":
    from PIL import Image, ImageDraw

    # ── 1. Background: splash art, fitted + cinematic grade ───────────────────
    if bg_img:
        sw, sh = bg_img.size
        scale  = max(THUMB_W / sw, THUMB_H / sh)
        bg     = bg_img.resize((int(sw * scale), int(sh * scale)), Image.LANCZOS)
        ox     = (bg.width  - THUMB_W) // 2
        oy     = (bg.height - THUMB_H) // 2
        bg     = bg.crop((ox, oy, ox + THUMB_W, oy + THUMB_H))
    else:
        bg = Image.new("RGB", (THUMB_W, THUMB_H), (12, 8, 25))

    bg     = _cinematic_grade(bg)
    bg     = _vignette_overlay(bg, strength=0.88)

    # ── 2. Left-side legibility gradient ─────────────────────────────────────
    grad   = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(grad)
    fade_w = int(THUMB_W * 0.62)
    for x in range(fade_w):
        a = int(180 * (1 - x / fade_w) ** 1.4)
        g_draw.line([(x, 0), (x, THUMB_H)], fill=(0, 0, 0, a))
    canvas = bg.convert("RGBA")
    canvas.alpha_composite(grad)

    # ── 3. Streamer profile picture + Challenger border (right) ───────────────
    pfp_d = 280
    border  = _challenger_border(pfp_d)
    b_size  = border.size[0]
    pad     = (b_size - pfp_d) // 2
    bx      = THUMB_W - b_size - 55
    by      = (THUMB_H - b_size) // 2

    if pfp_img is not None:
        # Use the raw profile picture as-is (no brightness/color/contrast filter)
        pfp_circ = _circular_crop(pfp_img, pfp_d)
    else:
        # Fallback: simple dark circle so the border still reads
        pfp_circ = Image.new("RGBA", (pfp_d, pfp_d), (0, 0, 0, 0))
        ImageDraw.Draw(pfp_circ).ellipse([0, 0, pfp_d - 1, pfp_d - 1],
                                         fill=(30, 25, 15, 255))

    # Warm outer glow behind the border
    glow_d = pfp_d + 80
    glow   = Image.new("RGBA", (glow_d, glow_d), (0, 0, 0, 0))
    g2     = ImageDraw.Draw(glow)
    for i in range(1, 41):
        a = int(100 * ((i - 1) / 40) ** 2)
        rg = glow_d // 2
        g2.ellipse([rg - pfp_d // 2 - i, rg - pfp_d // 2 - i,
                    rg + pfp_d // 2 + i, rg + pfp_d // 2 + i],
                   outline=(255, 200, 80, a), width=2)
    gx = bx + pad - (glow_d - pfp_d) // 2
    gy = by + pad - (glow_d - pfp_d) // 2
    canvas.alpha_composite(glow,     (gx, gy))
    canvas.alpha_composite(pfp_circ, (bx + pad, by + pad))
    canvas.alpha_composite(border,   (bx, by))

    # ── 4. Text ───────────────────────────────────────────────────────────────
    draw     = ImageDraw.Draw(canvas)
    left_pad = 64

    # "TOP PLAYS OF THE DAY" tag
    font_tag = _font(28)
    _text_outlined(draw, (left_pad, 38), "TOP PLAYS OF THE DAY", font_tag,
                   fill=(255, 160, 40), stroke=(0, 0, 0), width=3)

    # Achievement stamp (big gold text)
    ach_y = THUMB_H // 2 - 130
    if achievement:
        ach_label, ach_color = achievement
        font_ach = _font(108)
        _text_outlined(draw, (left_pad, ach_y), ach_label, font_ach,
                       fill=ach_color, stroke=(10, 5, 0), width=6)
        ach_y += 118

    # Hook / stat line (omitted when it would just repeat the achievement)
    if stat_line:
        font_hook = _font(50)
        _text_outlined(draw, (left_pad, ach_y), stat_line.upper(), font_hook,
                       fill=(240, 240, 240), stroke=(0, 0, 0), width=4)

    # Streamer credit
    font_cr = _font(34)
    _text_outlined(draw, (left_pad, THUMB_H - 90), f"ft. {streamer}", font_cr,
                   fill=(180, 220, 255), stroke=(0, 0, 0), width=3)

    # ── 5. Film grain ─────────────────────────────────────────────────────────
    return _film_grain(canvas.convert("RGB"), sigma=4.0)


# ── Public API ────────────────────────────────────────────────────────────────

# ── CTR-optimized variants (face-forward, ≤2 text lines, A/B set) ──────────────

def _resolve_mp4(clip: dict, raw_dir: Path) -> Path | None:
    p = raw_dir / f"{clip.get('id','')}.mp4"
    if p.exists():
        return p
    lp = clip.get("local_path") or ""
    return Path(lp) if lp and Path(lp).exists() else None


def _expression_scores(crops: list) -> list:
    """Score face crops by how far each is from this streamer's RESTING face.

    The neutral face is, by construction, the one the clip spends most of its time in —
    so the per-pixel median across sampled frames approximates it, and the frame that
    deviates most from it is the animated one (mouth open, eyes wide, leaning in).
    Sharpness breaks ties away from motion-blurred frames, which read as smears at
    feed size. No landmark model, no extra dependency: this runs on the cv2 already
    used for facecam detection.
    """
    import cv2
    import numpy as np

    small = [cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), (48, 48)).astype("float32")
             for c in crops]
    neutral = np.median(np.stack(small), axis=0)
    out = []
    for g in small:
        # Normalize per frame so a lighting/exposure shift can't masquerade as emotion.
        d = g - g.mean() - (neutral - neutral.mean())
        motion = float(np.abs(d).mean())
        sharp = float(cv2.Laplacian(g, cv2.CV_32F).var())
        out.append(motion + 0.02 * min(sharp, 300.0))
    return out


def _reaction_face(clip: dict, mp4: Path, best_s: float,
                   samples: int = 9) -> "Image.Image | None":
    """Pick the streamer's most EXPRESSIVE facecam frame near the peak moment.

    Was: one frame at best_s. That is whatever expression happened at that instant, and
    on real output it was routinely a flat or averted face — the single biggest CTR
    problem in the local design, since the face is the largest element on the canvas.
    Now: sample a window around the peak and rank by _expression_scores().
    """
    try:
        import cv2
        from PIL import Image

        from ..publishing.shorts import _detect_facecam, _extract_frame
        dur = float(clip.get("duration", 30) or 30)
        region = _detect_facecam(mp4, dur)
        if not region:
            return None
        x, y, w, h = (int(v) for v in region)
        x, y = max(0, x), max(0, y)

        # Reactions land ON and just AFTER the play, so weight the window that way.
        peak = best_s if best_s and best_s > 0 else dur * 0.6
        lo, hi = max(0.0, peak - 2.0), min(dur - 0.3, peak + 5.0)
        if hi <= lo:
            lo, hi = 0.0, max(dur - 0.3, 0.5)
        times = [lo + (hi - lo) * i / max(samples - 1, 1) for i in range(samples)]

        crops = []
        for t in times:
            frame = _extract_frame(mp4, t)
            if frame is None:
                continue
            fh, fw = frame.shape[:2]
            crop = frame[y:min(y + h, fh), x:min(x + w, fw)]
            if crop.size:
                crops.append(crop)
        if not crops:
            return None
        if len(crops) == 1:
            best = crops[0]
        else:
            scores = _expression_scores(crops)
            best = crops[max(range(len(crops)), key=lambda i: scores[i])]
        return Image.fromarray(cv2.cvtColor(best, cv2.COLOR_BGR2RGB))
    except Exception as e:
        log.debug("reaction face failed: %s", e)
        return None


def _prep_bg(img: "Image.Image", blur: int = 0, darken: float = 0.0,
             zoom: float = 1.0, focus_x: float = 0.5) -> "Image.Image":
    """Fit an image to the thumbnail canvas; brightened + punchier, no darkening grade.

    The previous cinematic-grade + strong vignette treatment read as too dark (owner
    feedback) - a vignette in particular crushes exactly the corners/edges a thumbnail
    needs to stay legible at YouTube's small feed size. Plain brightness/saturation/
    contrast keeps the real gameplay frame recognizable instead."""
    from PIL import Image, ImageEnhance, ImageFilter
    img = img.convert("RGB")
    sw, sh = img.size
    scale = max(THUMB_W / sw, THUMB_H / sh) * max(1.0, zoom)
    img = img.resize((int(sw * scale), int(sh * scale)), Image.LANCZOS)
    # focus_x = where on the canvas the source's horizontal centre should land (0.5 =
    # centred). Needs zoom > 1 to have any room to move.
    ox = min(max(0, int(img.width / 2 - focus_x * THUMB_W)), img.width - THUMB_W)
    # splash arts put the champion's head high; when zoomed, keep more of the top
    oy = int((img.height - THUMB_H) * (0.3 if zoom > 1 else 0.5))
    img = img.crop((ox, oy, ox + THUMB_W, oy + THUMB_H))
    img = ImageEnhance.Brightness(img).enhance(1.2)
    img = ImageEnhance.Color(img).enhance(1.35)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if darken:
        img = ImageEnhance.Brightness(img).enhance(1 - darken)
    return img.convert("RGBA")


def _ask(hook: str) -> str:
    """`hook` + "?!", unless it already ends in punctuation (else: "HE DID WHAT?!?!")."""
    hook = (hook or "").strip()
    return hook if hook[-1:] in "?!." else f"{hook}?!"


def _clip_frame_bg(mp4: Path, best_s: float, blur: int = 0) -> "Image.Image | None":
    """A still from the actual peak moment — authentic-action background.

    `blur` exists because a raw gameplay frame carries the full HUD, minimap, health
    bars and often the streamer's chat overlay. At the ~320px the browse feed actually
    renders, all of that competes with the headline for the same few pixels. A small
    blur removes the fine detail while keeping the frame readable as *this* fight —
    deliberately blur and NOT darkening, since the owner already rejected the darker
    cinematic grade for crushing the edges (see _prep_bg).
    """
    try:
        import cv2
        from PIL import Image

        from ..publishing.shorts import _extract_frame
        frame = _extract_frame(mp4, best_s if best_s and best_s > 0 else 2.0)
        if frame is None:
            return None
        return _prep_bg(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                        blur=blur)
    except Exception:
        return None


def _color_ring(inner_d: int, accent: tuple) -> tuple:
    """Glowing accent ring sized to surround a circle of inner_d. Returns (ring, pad)."""
    from PIL import Image, ImageDraw
    pad = 40
    size = inner_d + pad * 2
    c = size // 2
    r = inner_d // 2
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring)
    for i in range(30, 0, -1):
        d.ellipse([c - r - i, c - r - i, c + r + i, c + r + i],
                  outline=(*accent, int(110 * (1 - i / 30) ** 1.6)), width=3)
    d.ellipse([c - r - 12, c - r - 12, c + r + 12, c + r + 12], fill=(10, 10, 12, 255))
    d.ellipse([c - r + 2, c - r + 2, c + r - 2, c + r - 2], fill=(0, 0, 0, 0))
    d.ellipse([c - r - 11, c - r - 11, c + r + 11, c + r + 11], outline=(*accent, 255), width=6)
    d.ellipse([c - r - 3, c - r - 3, c + r + 3, c + r + 3], outline=(255, 255, 255, 235), width=3)
    return ring, pad


def _circle_crop_any(img: "Image.Image", d: int) -> "Image.Image":
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    return _circular_crop(img, d)


def _compose_ctr(bg: "Image.Image | None", face: "Image.Image | None", big_text: str,
                 accent: tuple, streamer: str, face_scale: float = 0.84) -> "Image.Image":
    from PIL import Image, ImageDraw, ImageFilter
    W, H = THUMB_W, THUMB_H
    canvas = (bg.copy() if bg is not None else Image.new("RGBA", (W, H), (12, 8, 25, 255))).convert("RGBA")

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    fade = int(W * 0.72)
    for x in range(fade):
        # 140 (was 210): enough to hold the headline, light enough that the champion
        # splash behind it stays visible
        gd.line([(x, 0), (x, H)], fill=(0, 0, 0, int(140 * (1 - x / fade) ** 1.25)))
    canvas.alpha_composite(grad)

    face_left = W - 40
    if face is not None:
        d = int(H * face_scale)
        circle = _circle_crop_any(face, d)
        ring, pad = _color_ring(d, accent)
        ix, iy = W - d - 46, H - d + int(H * 0.06)
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse([ix - 8, iy - 8, ix + d + 8, iy + d + 8], fill=(0, 0, 0, 160))
        canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(22)))
        canvas.alpha_composite(circle, (ix, iy))
        canvas.alpha_composite(ring, (ix - pad, iy - pad))
        face_left = ix - pad   # keep the text clear of the glow ring, not just the circle

    left = 64
    lines = _hook_lines(big_text)
    palette = _palette_for(accent)
    font = _fit_title(lines, max(face_left - left - 70, 200), int(H * 0.46), start=170)
    block = _title_block(lines, font, palette)
    y0 = (H - block.height) // 2
    canvas.alpha_composite(block, (left, y0))

    if streamer:
        _name_badge(canvas, f"ft. {streamer}", palette)
    _red_border(canvas, color=accent, radius=40, border=14, glow=12, glow_alpha=140)
    return canvas.convert("RGB")


def generate_variants(cfg: dict, date_label: str, n: int = 3,
                      out_dir: Path | None = None) -> list:
    """Render up to n CTR-optimized thumbnail variants (variant 1 = primary)."""
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    cache_dir = data / "cache"
    raw_dir = data / "raw" / date_label
    out_dir = out_dir or work

    src = work / "vlm_filtered.json"
    if not src.exists():
        return []
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    if not clips:
        return []

    top = max(clips, key=lambda c: c.get("api_rank_score", 0))
    summary = top.get("vlm_summary", "") + " " + top.get("title", "")
    best_s = float(top.get("api_best_moment_s", 0) or 0)
    from .commentary import _ascii_name
    from .credits import _hook
    hook = _hook(clips, date_label)
    streamer = _ascii_name(top)

    th = cfg.get("thumbnail", {})
    champ_id = _detect_champion(summary, _champion_map(cache_dir))
    splash = _fetch_splash(champ_id, cache_dir) if champ_id else None
    # Splash arts centre their champion, which is exactly where the big streamer circle
    # sits. Zoom in a little and slide the champion into the open area left of it.
    splash_bg = (_prep_bg(splash, zoom=float(th.get("splash_zoom", 1.35)),
                          focus_x=float(th.get("splash_focus_x", 0.24)))
                 if splash else None)

    mp4 = _resolve_mp4(top, raw_dir)
    face = (_reaction_face(top, mp4, best_s, int(th.get("face_samples", 9)))
            if mp4 else None)
    used_facecam = face is not None
    if face is None and top.get("broadcaster_id"):
        face = _twitch_pfp(top["broadcaster_id"], cache_dir)
    frame_bg = _clip_frame_bg(mp4, best_s, int(th.get("background_blur", 2))) if mp4 else None

    # Circle sizes per variant as before 2026-09-24: the owner tried 0.95 on all three for
    # one night and preferred the slightly smaller avatar. What stayed from that change:
    # the champion splash is shown SHARP (zoomed, slid beside the circle) instead of the
    # blurred, darkened variant-3 background, which hid which champion the clip is about.
    specs = [
        dict(bg=splash_bg or frame_bg, text=_ask(hook), accent=(255, 60, 60), scale=0.86),
        dict(bg=frame_bg or splash_bg, text=hook, accent=(255, 205, 60), scale=0.82),
        dict(bg=splash_bg or frame_bg, text="INSANE!", accent=(31, 214, 230), scale=0.95),
    ][:max(1, n)]

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, sp in enumerate(specs):
        try:
            img = _compose_ctr(sp["bg"], face, sp["text"], sp["accent"], streamer, sp["scale"])
            p = out_dir / ("thumbnail.jpg" if i == 0 else f"thumbnail_{i + 1}.jpg")
            img.save(str(p), "JPEG", quality=92)
            paths.append(p)
        except Exception as e:
            log.warning("thumbnail variant %d failed: %s", i + 1, e)
    log.info("thumbnails -> %d variants (champion=%s, face=%s)",
             len(paths), champ_id or "none", "facecam" if used_facecam else ("pfp" if face else "none"))
    return paths


# ── Gemini AI reaction thumbnail (Nano Banana / gemini-2.5-flash-image) ────────
#
# Approach (owner-chosen style): the model ONLY enhances the streamer's real facecam
# into an over-the-top excited/shocked reaction on a green screen — we chroma-key it out
# and composite it over the REAL clip gameplay (slight blur), then overlay a centred
# metallic announcement (PENTAKILL / QUADRA KILL) in Montserrat Bold + a radiating red border.
#
# Why not have the model build the whole scene? Naming the IP, or feeding it several
# copyrighted Riot frames, trips its recitation guardrail (finishReason IMAGE_OTHER, no
# image). Enhancing just the face is reliable; the real gameplay frame supplies context.
_CUT_PROMPT = (
    "Head and shoulders portrait of the SAME person in this image — keep their exact "
    "identity and likeness. Give them an extreme, over-the-top EXCITED and SHOCKED "
    "reaction: wide eyes, mouth wide open screaming with hype, pure adrenaline. Dramatic "
    "cinematic rim lighting on hair and shoulders. Place them on a COMPLETELY UNIFORM "
    "solid chroma-key green background (#00b140), nothing else in frame, no text.")

# announcement palettes: (gradient top, gradient bottom, glow)
_ANN_GOLD = ((255, 240, 170), (208, 138, 28), (255, 168, 40))
_ANN_RED = ((255, 184, 160), (198, 28, 28), (255, 70, 40))
_ANN_CYAN = ((210, 250, 255), (20, 150, 180), (90, 220, 255))

# maps the flat accent RGB used by the local CTR variants to a metallic text palette
_PALETTE_FOR_ACCENT = {
    (255, 60, 60):  _ANN_RED,
    (255, 205, 60): _ANN_GOLD,
    (31, 214, 230): _ANN_CYAN,
}


def _palette_for(accent: tuple) -> tuple:
    return _PALETTE_FOR_ACCENT.get(accent, _ANN_GOLD)

_FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"


def _clip_frame_raw(mp4: Path, best_s: float) -> "Image.Image | None":
    """Ungraded gameplay still at the peak moment — fed to Gemini as scene reference."""
    try:
        import cv2
        from PIL import Image

        from ..publishing.shorts import _extract_frame
        f = _extract_frame(mp4, best_s if best_s and best_s > 0 else 2.0)
        return None if f is None else Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
    except Exception:
        return None


def _best_with_face(clips: list, raw_dir: Path, top_n: int) -> tuple | None:
    """Highest-ranked clip that has a detectable facecam → (clip, mp4, face, best_s)."""
    ranked = sorted(clips, key=lambda c: c.get("api_rank_score", 0), reverse=True)
    for c in ranked[:max(1, top_n)]:
        mp4 = _resolve_mp4(c, raw_dir)
        if not mp4:
            continue
        best_s = float(c.get("api_best_moment_s", 0) or 0)
        face = _reaction_face(c, mp4, best_s)
        if face is not None:
            return c, mp4, face, best_s
    return None


def _fit_1280(img: "Image.Image") -> "Image.Image":
    from PIL import Image
    img = img.convert("RGB")
    sw, sh = img.size
    scale = max(THUMB_W / sw, THUMB_H / sh)
    img = img.resize((int(sw * scale), int(sh * scale)), Image.LANCZOS)
    ox, oy = (img.width - THUMB_W) // 2, (img.height - THUMB_H) // 2
    return img.crop((ox, oy, ox + THUMB_W, oy + THUMB_H))


def _afont(name: str, size: int, wght: int | None = None):
    """Load a bundled display font (assets/fonts); fall back to system Arial Black."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(str(_FONTS_DIR / name), size)
        if wght:
            try:
                f.set_variation_by_axes([wght])
            except Exception:
                pass
        return f
    except OSError:
        return _font(size)


def _green_key(img: "Image.Image") -> "Image.Image":
    """Chroma-key a green-screen portrait → RGBA cutout with green-spill suppression."""
    import numpy as np
    from PIL import Image, ImageFilter
    rgb = np.array(img.convert("RGB"))
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    green = (g > 90) & (g > r * 1.12) & (g > b * 1.12)
    out = rgb.copy()
    spill = (g > np.maximum(r, b)) & ~green
    out[..., 1] = np.where(spill, np.maximum(r, b), out[..., 1]).astype("uint8")
    alpha = np.where(green, 0, 255).astype("uint8")
    im = Image.fromarray(np.dstack([out, alpha]).astype("uint8"), "RGBA")
    im.putalpha(im.split()[3].filter(ImageFilter.MinFilter(3)))   # erode 1px green rim
    return im


_TITLE_FONT = "Montserrat-Bold.ttf"


def _gameplay_bg(frame: "Image.Image", blur: int = 5, darken: float = 0.34) -> "Image.Image":
    """Real clip frame, only slightly blurred + darkened (trailer style — still legible)."""
    from PIL import ImageEnhance, ImageFilter
    bg = _fit_1280(frame).filter(ImageFilter.GaussianBlur(blur))
    bg = ImageEnhance.Color(bg).enhance(1.18)
    bg = ImageEnhance.Brightness(bg).enhance(1 - darken)
    return bg.convert("RGBA")


def _metallic_line(text: str, fnt, palette) -> "Image.Image":
    """One headline line: vertical metallic gradient + dark stroke + outer glow (RGBA)."""
    from PIL import Image, ImageDraw, ImageFilter
    top, bot, glow = palette
    sw = max(5, fnt.size // 15)
    pad = int(fnt.size * 0.24) + 16
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    bb = probe.textbbox((0, 0), text, font=fnt, stroke_width=sw)
    cw, ch = (bb[2] - bb[0]) + pad * 2, (bb[3] - bb[1]) + pad * 2
    ox, oy = pad - bb[0], pad - bb[1]

    gl = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ImageDraw.Draw(gl).text((ox, oy), text, font=fnt, fill=(*glow, 255),
                            stroke_width=sw, stroke_fill=(*glow, 255))
    gl = gl.filter(ImageFilter.GaussianBlur(18))

    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).text((ox, oy), text, font=fnt, fill=255)
    grad = Image.new("RGB", (cw, ch))
    g = ImageDraw.Draw(grad)
    for y in range(ch):
        t = y / max(1, ch - 1)
        g.line([(0, y), (cw, y)], fill=tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    fill = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    fill.paste(grad, mask=mask)

    dark = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ImageDraw.Draw(dark).text((ox, oy), text, font=fnt, fill=(0, 0, 0, 0),
                              stroke_width=sw, stroke_fill=(12, 7, 0, 255))
    return Image.alpha_composite(Image.alpha_composite(gl, dark), fill)


def _title_block(lines: list, fnt, palette, line_gap: float = -0.5) -> "Image.Image":
    """Stack headline lines, each horizontally centred and identically shaded."""
    from PIL import Image
    layers = [_metallic_line(l, fnt, palette) for l in lines]
    bw = max(l.width for l in layers)
    gap = int(fnt.size * line_gap)
    bh = sum(l.height for l in layers) + gap * (len(layers) - 1)
    block = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    y = 0
    for l in layers:
        block.alpha_composite(l, ((bw - l.width) // 2, y))
        y += l.height + gap
    return block


def _hook_lines(hook: str) -> list:
    w = hook.split()
    return [hook] if len(w) <= 1 else (
        w if len(w) == 2 else [" ".join(w[:len(w) // 2]), " ".join(w[len(w) // 2:])])


def _fit_title(lines: list, max_w: int, max_h: int, start: int = 170):
    """Largest Montserrat-Bold size whose centred block fits the headline area."""
    from PIL import Image, ImageDraw
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    size = start
    while size > 64:
        f = _afont(_TITLE_FONT, size, 800)
        sw = max(5, size // 15)
        bbs = [probe.textbbox((0, 0), l, font=f, stroke_width=sw) for l in lines]
        w = max(b[2] - b[0] for b in bbs)
        h = sum(b[3] - b[1] for b in bbs) + int(size * -0.16) * (len(lines) - 1)
        if w <= max_w and h <= max_h:
            return f
        size -= 6
    return _afont(_TITLE_FONT, 64, 800)


def _red_border(canvas, color=(230, 28, 28), border: int = 10, glow: int = 10,
                glow_alpha: int = 125, radius: int = 26) -> None:
    """Picture-frame border: solid color flush to the literal canvas edge everywhere,
    with a rounded window cut into it toward the image, plus a short glow bleeding
    inward from that window edge.

    Earlier version painted only the band `0 <= depth < border` (depth = distance
    inward from a rounded boundary sitting at the canvas edge) - that left the four
    corner notches (where the round cut recedes from the literal square canvas corner)
    completely unpainted, so bare background showed through in a triangle at each
    corner. Dropping the `depth >= 0` floor fills that notch with the same solid color
    too: everywhere outside the rounded window, all the way to the physical edge, is
    now frame. Nothing changes along the straight edges (depth can't go negative there -
    array bounds already cap it at 0), so this only affects the four corners. Net
    effect: the frame's outer edge is always the literal image boundary - there is
    nothing beyond it but the rest of the page - so it's automatically immune to
    whatever corner-radius YouTube's own UI crops thumbnails to: the pixels it would
    clip are flat color, not content, so the clip is invisible either way."""
    import numpy as np
    from PIL import Image
    W, H = canvas.size
    ax = np.abs(np.arange(W) - (W - 1) / 2)[None, :] - (W / 2 - radius)
    ay = np.abs(np.arange(H) - (H - 1) / 2)[:, None] - (H / 2 - radius)
    outside = np.sqrt(np.maximum(ax, 0) ** 2 + np.maximum(ay, 0) ** 2) - radius
    inside = np.minimum(np.maximum(ax, ay), 0)
    depth = -(outside + inside)               # distance inward from the rounded edge
    a = np.zeros((H, W), float)
    a[depth < border] = 255                   # solid frame: corner notch through to the band
    g = (depth >= border) & (depth < border + glow)
    a[g] = glow_alpha * (1 - (depth[g] - border) / glow) ** 1.7
    ov = np.zeros((H, W, 4), "uint8")
    ov[..., 0], ov[..., 1], ov[..., 2] = color
    ov[..., 3] = a.astype("uint8")
    canvas.alpha_composite(Image.fromarray(ov, "RGBA"))


def _name_badge(canvas, text: str, accent) -> None:
    """Highlighted streamer badge (dark pill + accent border), bottom-left."""
    from PIL import Image, ImageDraw
    f = _afont(_TITLE_FONT, 38, 800)
    d = ImageDraw.Draw(canvas)
    tb = d.textbbox((0, 0), text, font=f)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    px, py = 22, 12
    x0, y0 = 48, THUMB_H - th - py * 2 - 44
    x1, y1 = x0 + tw + px * 2, y0 + th + py * 2
    pill = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle([x0, y0, x1, y1], radius=14, fill=(10, 12, 18, 215),
                                           outline=(*accent[2], 255), width=4)
    canvas.alpha_composite(pill)
    d.text((x0 + px - tb[0], y0 + py - tb[1]), text, font=f, fill=(245, 248, 255),
           stroke_width=2, stroke_fill=(0, 0, 0))


def _reaction_cutout(provider, face) -> "Image.Image | None":
    """One paid image call: enhance the facecam into an excited green-screen reaction.

    Deliberately narrow — see CLAUDE.md: asking the model to build the whole scene
    trips the recitation filter and flakes. We key this out and composite the rest
    ourselves."""
    import io

    from PIL import Image
    blob = provider.generate_image(_CUT_PROMPT, images=[_img_bytes(face)])
    if not blob:
        return None
    return _green_key(Image.open(io.BytesIO(blob)).convert("RGB"))


def _img_bytes(im) -> bytes:
    import io
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _compose_reaction(frame, cutout, hook, streamer, accent) -> "Image.Image":
    """Default style: slightly-blurred gameplay + keyed reaction face (right) + centred
    metallic announcement (left) + highlighted name badge + radiating red border."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = THUMB_W, THUMB_H
    canvas = _gameplay_bg(frame)
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    fade = int(W * 0.5)
    for x in range(fade):
        gd.line([(x, 0), (x, H)], fill=(0, 0, 0, int(140 * (1 - x / fade) ** 1.3)))
    canvas.alpha_composite(grad)

    if cutout is not None:
        d = int(H * 0.98)
        cw = int(cutout.width * d / cutout.height)
        c = cutout.resize((cw, d), Image.LANCZOS)
        x, y = W - cw + int(cw * 0.06), H - d + int(H * 0.02)
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rectangle([x + 18, y + 18, x + cw, y + d], fill=(0, 0, 0, 130))
        canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(28)))
        # white sticker outline so the player pops off the background
        ow = max(6, cw // 110)
        rim_mask = c.split()[3].filter(ImageFilter.MaxFilter(ow * 2 + 1))
        rim = Image.new("RGBA", c.size, (0, 0, 0, 0))
        rim.paste((245, 246, 250, 255), mask=rim_mask)
        canvas.alpha_composite(rim, (x, y))
        canvas.alpha_composite(c, (x, y))

    lines = _hook_lines(hook)
    block = _title_block(lines, _fit_title(lines, int(W * 0.52), int(H * 0.6)), accent)
    canvas.alpha_composite(block, (int(W * 0.30 - block.width / 2),
                                   int(H * 0.46 - block.height / 2)))

    if streamer:
        _name_badge(canvas, streamer, accent)
    _red_border(canvas)
    return canvas.convert("RGB")


def generate_gemini(cfg: dict, date_label: str) -> Path | None:
    """AI reaction thumbnail: enhanced facecam keyed over the real blurred gameplay."""
    th = cfg.get("thumbnail", {})
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    raw_dir = data / "raw" / date_label
    from ..providers import ProviderUnavailable, get_provider
    try:
        provider = get_provider(cfg, "thumbnail_image")
        provider.require(image_generation=True)
    except ProviderUnavailable as e:
        log.info("AI thumbnail unavailable (%s) — using the local design", e)
        return None

    src = work / "vlm_filtered.json"
    if not src.exists():
        return None
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    if not clips:
        return None

    picked = _best_with_face(clips, raw_dir, int(th.get("face_search_top", 6)))
    if not picked:
        log.info("gemini thumb: no clip with a detectable facecam - fallback")
        return None
    clip, mp4, face, best_s = picked

    from .commentary import _ascii_name
    from .credits import _hook
    hook = _hook(clips, date_label)
    streamer = _ascii_name(clip)
    frame = _clip_frame_raw(mp4, best_s)
    if frame is None:
        log.info("gemini thumb: no gameplay frame - fallback")
        return None

    cutout = _reaction_cutout(provider, face)
    if cutout is None:
        log.warning("gemini thumb: reaction cutout failed - fallback")
        return None

    accent = _ANN_GOLD   # gold text reads best against the always-red border
    work.mkdir(parents=True, exist_ok=True)
    out = work / "thumbnail.jpg"
    _compose_reaction(frame, cutout, hook, streamer, accent).save(str(out), "JPEG", quality=92)
    log.info("gemini thumbnail -> %s (streamer=%s hook=%s)", out.name, streamer, hook)
    return out


def generate(cfg: dict, date_label: str) -> Path | None:
    th = cfg.get("thumbnail", {})
    if not th.get("enabled", True):
        log.info("thumbnail disabled")
        return None

    if th.get("provider", "local") == "gemini":
        try:
            p = generate_gemini(cfg, date_label)
            if p:
                return p
        except Exception as e:
            log.warning("gemini thumbnail failed (%s) — using local design", e)

    try:
        paths = generate_variants(cfg, date_label, n=int(th.get("variants", 3)))
        if paths:
            return paths[0]
    except Exception as e:
        log.warning("variant thumbnails failed (%s) — using legacy design", e)
    return _generate_legacy(cfg, date_label)


def _generate_legacy(cfg: dict, date_label: str) -> Path | None:
    th = cfg.get("thumbnail", {})
    if not th.get("enabled", True):
        log.info("thumbnail disabled")
        return None

    data      = Path(cfg["paths"]["data_abs"])
    work      = data / "work" / date_label
    cache_dir = data / "cache"
    out       = work / "thumbnail.jpg"

    src = work / "vlm_filtered.json"
    if not src.exists():
        log.info("thumbnail: no vlm_filtered.json - skip")
        return None
    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    if not clips:
        return None

    # Best clip drives everything
    top     = max(clips, key=lambda c: c.get("api_rank_score", 0))
    summary = top.get("vlm_summary", "") + " " + top.get("title", "")

    # Champion splash art
    champ_map = _champion_map(cache_dir)
    champ_id  = _detect_champion(summary, champ_map)
    bg_img    = _fetch_splash(champ_id, cache_dir) if champ_id else None
    if not champ_id:
        log.info("  no champion detected - using dark background")

    # Twitch profile picture
    broadcaster_id = top.get("broadcaster_id", "")
    pfp_img = _twitch_pfp(broadcaster_id, cache_dir) if broadcaster_id else None
    if pfp_img is None:
        log.info("  no pfp for broadcaster %s - using fallback", broadcaster_id)

    # Achievement + hook
    achievement = _achievement(summary)
    from .credits import _hook
    hook = _hook(clips, date_label)

    # Stat line: rank + hook — but never repeat the achievement. The best clip's title
    # drives both the achievement stamp and the hook, so e.g. a "quadra" clip yields
    # achievement == hook == "QUADRA KILL"; showing both renders the text twice.
    ach_label = achievement[0] if achievement else ""
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    rank = top.get("rank") or top.get("broadcaster_rank") or ""
    stat_parts = []
    if rank:
        stat_parts.append(str(rank).upper())
    if hook and not (ach_label and (_norm(ach_label) in _norm(hook)
                                    or _norm(hook) in _norm(ach_label))):
        stat_parts.append(hook[:40] if not achievement else hook[:50])
    stat_line = "  ·  ".join(stat_parts)

    # Streamer name (ASCII-safe)
    from .commentary import _ascii_name
    streamer = _ascii_name(top)

    try:
        img = _compose(
            bg_img      = bg_img,
            pfp_img     = pfp_img,
            achievement = achievement,
            hook        = hook,
            stat_line   = stat_line,
            streamer    = streamer,
        )
        work.mkdir(parents=True, exist_ok=True)
        img.save(str(out), "JPEG", quality=92)
        log.info("thumbnail -> %s  (champion=%s  achievement=%s  pfp=%s)",
                 out.name, champ_id or "none",
                 achievement[0] if achievement else "none",
                 "yes" if pfp_img else "no")
        return out
    except Exception as e:
        log.warning("thumbnail failed: %s", e, exc_info=True)
        return None


def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    generate(cfg, date_label)
    return data / "work" / date_label
