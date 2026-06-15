"""Demo thumbnail generator — run standalone to preview the design.

Layout: champion splash art background (cinematic graded) + streamer logo
in a Challenger-rank border (right side) + big achievement text (left side).

Usage:
    venv\Scripts\python.exe demo_thumbnail.py
Output:
    demo_thumbnail.jpg  (open in any image viewer)
"""

import io
import math
import os
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720


# ── Network ───────────────────────────────────────────────────────────────────

def _get(url: str) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


# ── Cinematic grade ───────────────────────────────────────────────────────────

def cinematic_grade(img: Image.Image) -> Image.Image:
    """Teal-shadow / warm-highlight split tone + lifted contrast."""
    arr = np.array(img).astype(np.float32) / 255.0

    # Boost contrast (S-curve approximation)
    arr = 0.5 + (arr - 0.5) * 1.30
    arr = np.clip(arr, 0, 1)

    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    shadow    = np.clip(1.0 - lum * 2.5, 0, 1)[:, :, np.newaxis]
    highlight = np.clip(lum * 2.0 - 1.1, 0, 1)[:, :, np.newaxis]

    # Shadows → teal (pull R down, B up)
    arr[:, :, 0] -= shadow[:, :, 0] * 0.15
    arr[:, :, 1] += shadow[:, :, 0] * 0.04
    arr[:, :, 2] += shadow[:, :, 0] * 0.10

    # Highlights → orange (push R up, pull B down)
    arr[:, :, 0] += highlight[:, :, 0] * 0.10
    arr[:, :, 1] += highlight[:, :, 0] * 0.02
    arr[:, :, 2] -= highlight[:, :, 0] * 0.12

    return Image.fromarray(np.clip(arr * 255, 0, 255).astype(np.uint8))


def vignette(img: Image.Image, strength: float = 0.80) -> Image.Image:
    w, h = img.size
    arr  = np.array(img).astype(np.float32)
    cx, cy = w / 2, h / 2
    Y, X   = np.ogrid[:h, :w]
    dist   = np.sqrt(((X - cx) / cx) ** 2 + ((Y - cy) / cy) ** 2)
    mask   = 1.0 - np.clip(dist * strength, 0, 1) ** 1.8
    arr   *= mask[:, :, np.newaxis]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def film_grain(img: Image.Image, sigma: float = 5.0) -> Image.Image:
    arr   = np.array(img).astype(np.float32)
    grain = np.random.normal(0, sigma, arr.shape).astype(np.float32)
    return Image.fromarray(np.clip(arr + grain, 0, 255).astype(np.uint8))


# ── Challenger border ─────────────────────────────────────────────────────────

def challenger_border(inner_d: int) -> Image.Image:
    """RGBA image: gold ornate ring sized to surround a circle of inner_d px."""
    pad   = 48          # extra space around the circle for ornaments
    size  = inner_d + pad * 2
    cx = cy = size // 2
    r  = inner_d // 2

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(canvas)

    # ── Glow halo ─────────────────────────────────────────────────────────────
    for i in range(28, 0, -1):
        a = int(120 * (1 - i / 28) ** 1.5)
        draw.ellipse([cx - r - i, cy - r - i, cx + r + i, cy + r + i],
                     outline=(255, 200, 60, a), width=3)

    # ── Outer dark backing ring (ring only — interior must stay transparent) ──
    draw.ellipse([cx - r - 14, cy - r - 14, cx + r + 14, cy + r + 14],
                 fill=(15, 10, 4, 255))
    # Cut the interior back out so pfp shows through
    draw.ellipse([cx - r + 2, cy - r + 2, cx + r - 2, cy + r - 2],
                 fill=(0, 0, 0, 0))

    # ── Main gold rings ───────────────────────────────────────────────────────
    draw.ellipse([cx - r - 13, cy - r - 13, cx + r + 13, cy + r + 13],
                 outline=(255, 220, 80, 255), width=4)
    draw.ellipse([cx - r - 7,  cy - r - 7,  cx + r + 7,  cy + r + 7],
                 outline=(200, 150, 30, 255), width=3)
    draw.ellipse([cx - r - 2,  cy - r - 2,  cx + r + 2,  cy + r + 2],
                 outline=(255, 240, 140, 255), width=2)

    # ── Cardinal ornaments (crown-like points) ────────────────────────────────
    for angle_deg in [0, 90, 180, 270]:
        rad  = math.radians(angle_deg)
        bx   = cx + int((r + 22) * math.sin(rad))
        by   = cy - int((r + 22) * math.cos(rad))
        tip  = (bx + int(10 * math.sin(rad)), by - int(10 * math.cos(rad)))
        perp = math.radians(angle_deg + 90)
        lx   = bx + int(7 * math.sin(perp))
        ly   = by - int(7 * math.cos(perp))
        rx   = bx - int(7 * math.sin(perp))
        ry   = by + int(7 * math.cos(perp))
        draw.polygon([tip, (lx, ly), (rx, ry)], fill=(255, 220, 80, 255))
        draw.ellipse([bx - 4, by - 4, bx + 4, by + 4], fill=(255, 240, 140, 255))

    # ── Diagonal diamonds ─────────────────────────────────────────────────────
    for angle_deg in [45, 135, 225, 315]:
        rad = math.radians(angle_deg)
        ox  = cx + int((r + 16) * math.sin(rad))
        oy  = cy - int((r + 16) * math.cos(rad))
        s   = 5
        perp = math.radians(angle_deg + 90)
        pts  = [
            (ox + int(s * math.sin(rad)),  oy - int(s * math.cos(rad))),
            (ox + int(s * math.sin(perp)), oy - int(s * math.cos(perp))),
            (ox - int(s * math.sin(rad)),  oy + int(s * math.cos(rad))),
            (ox - int(s * math.sin(perp)), oy + int(s * math.cos(perp))),
        ]
        draw.polygon(pts, fill=(200, 160, 40, 255))

    return canvas


def circular_crop(img: Image.Image, d: int) -> Image.Image:
    img  = img.resize((d, d), Image.LANCZOS)
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d, d], fill=255)
    out  = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), mask=mask)
    return out


# ── Placeholder profile picture ───────────────────────────────────────────────

def make_placeholder_pfp(size: int = 300) -> Image.Image:
    """A stylised logo stand-in until we pull Twitch profile images."""
    img  = Image.new("RGB", (size, size), (8, 5, 20))
    draw = ImageDraw.Draw(img)
    # Radial gradient approximation
    for i in range(size, 0, -4):
        t = i / size
        r = int(15 + 80 * t)
        g = int(5  + 30 * t)
        b = int(50 + 140 * t)
        cx = size // 2
        half = i // 2
        draw.ellipse([cx - half, cx - half, cx + half, cx + half], fill=(r, g, b))
    # Hexagon shape (rank badge feel)
    cx = size // 2
    pts = [(cx + int(size * 0.38 * math.sin(math.radians(a))),
            cx - int(size * 0.38 * math.cos(math.radians(a))))
           for a in range(0, 360, 60)]
    draw.polygon(pts, fill=(100, 200, 255), outline=(200, 230, 255))
    # Inner star
    inner = [(cx + int(size * 0.18 * math.sin(math.radians(a))),
              cx - int(size * 0.18 * math.cos(math.radians(a))))
             for a in range(0, 360, 72)]
    draw.polygon(inner, fill=(255, 230, 80))
    return img


# ── Text helper ───────────────────────────────────────────────────────────────

def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def text_outlined(draw, xy, text, font, fill, stroke, width):
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx or dy:
                draw.text((xy[0] + dx, xy[1] + dy), text, font=font, fill=stroke)
    draw.text(xy, text, font=font, fill=fill)


# ── Compose ───────────────────────────────────────────────────────────────────

def compose(
    champion: str   = "Jinx",
    achievement: str = "PENTAKILL",
    streamer: str   = "SolarbacCA",
    stat_line: str  = "5 KILLS  ·  CHALLENGER",
    pfp_img: Image.Image | None = None,
) -> Image.Image:

    # ── 1. Background splash art ──────────────────────────────────────────────
    splash_url = (
        f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{champion}_0.jpg"
    )
    print(f"Fetching splash art for {champion}...")
    try:
        bg = _get(splash_url)
    except Exception as e:
        print(f"  splash fetch failed ({e}), using solid color")
        bg = Image.new("RGB", (W, H), (20, 10, 40))

    # Fit to 1280×720
    sw, sh = bg.size
    scale  = max(W / sw, H / sh)
    bg     = bg.resize((int(sw * scale), int(sh * scale)), Image.LANCZOS)
    ox     = (bg.width - W) // 2
    oy     = (bg.height - H) // 2
    bg     = bg.crop((ox, oy, ox + W, oy + H))

    # ── 2. Cinematic grade + vignette ─────────────────────────────────────────
    bg = cinematic_grade(bg)
    bg = vignette(bg, strength=0.90)

    # ── 3. Left-side dark gradient (text legibility) ──────────────────────────
    grad   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(grad)
    fade_w = int(W * 0.65)
    for x in range(fade_w):
        a = int(185 * (1 - x / fade_w) ** 1.4)
        g_draw.line([(x, 0), (x, H)], fill=(0, 0, 0, a))
    canvas = bg.convert("RGBA")
    canvas.alpha_composite(grad)

    # ── 4. Profile picture + Challenger border (right side) ───────────────────
    if pfp_img is None:
        pfp_img = make_placeholder_pfp(300)

    pfp_d  = 280
    # Boost pfp so it pops against the dark cinematic background
    from PIL import ImageEnhance
    pfp_img = ImageEnhance.Brightness(pfp_img).enhance(1.9)
    pfp_img = ImageEnhance.Color(pfp_img).enhance(1.5)
    pfp_img = ImageEnhance.Contrast(pfp_img).enhance(1.2)
    pfp_circ = circular_crop(pfp_img, pfp_d)
    border   = challenger_border(pfp_d)
    b_size   = border.size[0]
    pad      = (b_size - pfp_d) // 2

    # Right-center position
    bx = W - b_size - 55
    by = (H - b_size) // 2

    # Warm backing glow so dark logos don't disappear into the vignette
    glow_size = pfp_d + 80
    glow = Image.new("RGBA", (glow_size, glow_size), (0, 0, 0, 0))
    for i in range(40, 0, -1):
        a = int(90 * (1 - i / 40) ** 2)
        r_g = glow_size // 2
        ImageDraw.Draw(glow).ellipse(
            [r_g - pfp_d // 2 - i, r_g - pfp_d // 2 - i,
             r_g + pfp_d // 2 + i, r_g + pfp_d // 2 + i],
            fill=(255, 200, 80, a))
    gx = bx + pad - (glow_size - pfp_d) // 2
    gy = by + pad - (glow_size - pfp_d) // 2
    canvas.alpha_composite(glow, (gx, gy))

    # Warm amber backing — frames any pfp with contrast
    backing = Image.new("RGBA", (pfp_d, pfp_d), (0, 0, 0, 0))
    ImageDraw.Draw(backing).ellipse([0, 0, pfp_d, pfp_d], fill=(120, 80, 20, 255))
    canvas.alpha_composite(backing, (bx + pad, by + pad))

    canvas.alpha_composite(pfp_circ, (bx + pad, by + pad))
    canvas.alpha_composite(border,   (bx, by))

    # ── 5. Text ───────────────────────────────────────────────────────────────
    draw = ImageDraw.Draw(canvas)

    font_xl  = _font("C:/Windows/Fonts/ariblk.ttf", 110)  # achievement
    font_lg  = _font("C:/Windows/Fonts/ariblk.ttf",  52)  # stat line
    font_sm  = _font("C:/Windows/Fonts/arialbd.ttf",  34)  # streamer name
    font_tag = _font("C:/Windows/Fonts/arialbd.ttf",  28)  # top tag

    left_pad = 64

    # Top tag
    text_outlined(draw, (left_pad, 36), "TOP PLAYS OF THE DAY", font_tag,
                  fill=(255, 160, 40), stroke=(0, 0, 0), width=3)

    # Big achievement
    text_outlined(draw, (left_pad, H // 2 - 120), achievement, font_xl,
                  fill=(255, 215, 50), stroke=(10, 5, 0), width=6)

    # Stat line
    text_outlined(draw, (left_pad, H // 2 + 18), stat_line, font_lg,
                  fill=(240, 240, 240), stroke=(0, 0, 0), width=4)

    # Streamer name
    text_outlined(draw, (left_pad, H - 90), f"ft. {streamer}", font_sm,
                  fill=(180, 220, 255), stroke=(0, 0, 0), width=3)

    # ── 6. Subtle film grain ──────────────────────────────────────────────────
    result = film_grain(canvas.convert("RGB"), sigma=4.0)

    return result


# ── Twitch profile picture fetch ─────────────────────────────────────────────

def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def fetch_twitch_pfp(login: str) -> Image.Image | None:
    client_id     = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("  TWITCH_CLIENT_ID/SECRET not set — skipping profile picture")
        return None
    try:
        import requests as req
        token_resp = req.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": client_id, "client_secret": client_secret,
            "grant_type": "client_credentials",
        })
        token = token_resp.json()["access_token"]
        user_resp = req.get("https://api.twitch.tv/helix/users",
                            params={"login": login.lower()},
                            headers={"Authorization": f"Bearer {token}",
                                     "Client-Id": client_id})
        data = user_resp.json().get("data", [])
        if not data:
            print(f"  Twitch user '{login}' not found")
            return None
        pfp_url = data[0]["profile_image_url"].replace("-300x300", "-600x600")
        print(f"  Profile picture: {pfp_url}")
        return _get(pfp_url)
    except Exception as e:
        print(f"  pfp fetch failed: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _load_env()

    streamer = "SolarbacCA"
    print(f"Fetching Twitch profile picture for {streamer}...")
    pfp = fetch_twitch_pfp(streamer)
    if pfp is None:
        print("  Falling back to placeholder")

    img = compose(
        champion    = "Gangplank",
        achievement = "PENTAKILL",
        streamer    = streamer,
        stat_line   = "5 KILLS  ·  CHALLENGER",
        pfp_img     = pfp,
    )
    out = "demo_thumbnail.jpg"
    img.save(out, quality=94)
    print(f"\nSaved -> {out}")
    import subprocess
    subprocess.Popen(["explorer", out])
