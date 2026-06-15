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
from typing import Optional

import requests

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
    from PIL import Image
    import os

    cached = cache_dir / f"pfp_{broadcaster_id}.jpg"
    if cached.exists():
        try:
            return Image.open(str(cached)).convert("RGB")
        except Exception:
            cached.unlink(missing_ok=True)

    client_id     = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.debug("TWITCH_CLIENT_ID/SECRET not set — skipping pfp")
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
    w, h   = img.size
    arr    = np.array(img).astype(np.float32)
    cx, cy = w / 2, h / 2
    Y, X   = np.ogrid[:h, :w]
    dist   = np.sqrt(((X - cx) / cx) ** 2 + ((Y - cy) / cy) ** 2)
    mask   = 1.0 - np.clip(dist * strength, 0, 1) ** 1.8
    arr   *= mask[:, :, np.newaxis]
    return img.__class__.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _film_grain(img: "Image.Image", sigma: float = 4.0) -> "Image.Image":
    import numpy as np
    arr   = np.array(img).astype(np.float32)
    grain = np.random.normal(0, sigma, arr.shape).astype(np.float32)
    return img.__class__.fromarray(np.clip(arr + grain, 0, 255).astype(np.uint8))


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
    achievement: Optional[tuple[str, tuple]],
    hook:        str,
    stat_line:   str,
    streamer:    str,
) -> "Image.Image":
    from PIL import Image, ImageDraw, ImageEnhance

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
        # Brighten so it pops against the dark cinematic background
        pfp_img = ImageEnhance.Brightness(pfp_img).enhance(1.9)
        pfp_img = ImageEnhance.Color(pfp_img).enhance(1.5)
        pfp_img = ImageEnhance.Contrast(pfp_img).enhance(1.2)
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

    # Hook / stat line
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

def generate(cfg: dict, date_label: str) -> Path | None:
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
        log.info("thumbnail: no vlm_filtered.json — skip")
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
        log.info("  no champion detected — using dark background")

    # Twitch profile picture
    broadcaster_id = top.get("broadcaster_id", "")
    pfp_img = _twitch_pfp(broadcaster_id, cache_dir) if broadcaster_id else None
    if pfp_img is None:
        log.info("  no pfp for broadcaster %s — using fallback", broadcaster_id)

    # Achievement + hook
    achievement = _achievement(summary)
    from .credits import _hook
    hook = _hook(clips)

    # Stat line: rank info from top clip if available
    rank = top.get("rank") or top.get("broadcaster_rank") or ""
    stat_parts = []
    if rank:
        stat_parts.append(rank.upper())
    stat_parts.append(hook[:40] if not achievement else hook[:50])
    stat_line = "  ·  ".join(stat_parts) if stat_parts else hook

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
