"""Stage-8 helper — animated PROJECT-style streamer nameplate (ffmpeg-native).

Reproduces the look of the HTML/CSS mock (write-on letters, cyan/orange glow,
underline swipe, framed avatar) WITHOUT a headless browser: PIL renders the
animation frame-by-frame, ffmpeg packs the PNG sequence into a transparent
QuickTime-RLE .mov, and assemble.py overlays that .mov bottom-left over each
clip's intro window (flush in → hold → fade out).

Why PIL+ffmpeg instead of Playwright/Chromium: it's robust for a daily
unattended run, fast (no browser, no per-frame screenshots), needs no extra
dependency, and keeps final compositing in ffmpeg (the pipeline's rule).

build(cfg, clip) -> Path | None
    Renders (and caches in data/cache/nameplates/) a full-frame alpha .mov for
    one streamer. Cache key covers name/avatar/style/size so a recurring
    streamer is rendered once. Returns None on any failure so the caller can
    fall back to the plain drawtext lower-third.
"""
import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.nameplate")

# accent palette: a = accent, b = accent-bright, o = secondary (RGB tuples)
ACCENTS = {
    "cyan":   {"a": (31, 214, 230),  "b": (169, 251, 255), "o": (255, 122, 31)},
    "orange": {"a": (255, 138, 42),  "b": (255, 214, 168), "o": (31, 214, 230)},
    "red":    {"a": (255, 70, 85),   "b": (255, 179, 186), "o": (255, 138, 42)},
    "white":  {"a": (223, 238, 245), "b": (255, 255, 255), "o": (255, 122, 31)},
}

# bold display fonts (CJK fallback for JP/KR/CN names), monospace for the eyebrow
_DISP = ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_DISP_CJK = ["C:/Windows/Fonts/msgothic.ttc", "C:/Windows/Fonts/YuGothB.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_MONO = ["C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf",
         "C:/Windows/Fonts/cour.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]

_CACHE_VERSION = "v2"


# ── easing ──────────────────────────────────────────────────────────────────

def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def _ease_out(x):          # cubic ease-out
    x = _clamp(x)
    return 1 - (1 - x) ** 3


def _ease_out_expo(x):
    x = _clamp(x)
    return 1.0 if x >= 1 else 1 - 2 ** (-10 * x)


def _font(cands, size):
    from PIL import ImageFont
    for p in cands:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _alpha_scaled(img, f):
    """Return a copy of an RGBA image with its alpha channel multiplied by f."""
    if f >= 1.0:
        return img
    a = img.split()[3].point(lambda v: int(v * f))
    out = img.copy()
    out.putalpha(a)
    return out


def _colorize(layer, rgb):
    """New RGBA image: solid rgb masked by layer's alpha (used for the glow)."""
    from PIL import Image
    out = Image.new("RGBA", layer.size, (*rgb, 0))
    out.putalpha(layer.split()[3])
    return out


# ── frame renderer ────────────────────────────────────────────────────────────

def _render_frames(frames_dir, name, eyebrow, pfp, accent, W, H, fps, hold, y_frac):
    """Render the full animation to frames_dir/f_%05d.png. Returns frame count."""
    from PIL import Image, ImageDraw, ImageFilter

    acc, bright, sec = accent["a"], accent["b"], accent["o"]
    name = (name or "").strip()[:18] or "streamer"
    chars = list(name)
    is_cjk = any(ord(c) > 0x2E80 for c in name)

    name_fs = round(H * 0.044)
    eye_fs = max(11, round(H * 0.0155))
    AV = round(H * 0.122)
    GAP = round(H * 0.016)
    PAD = round(H * 0.016)
    LM = round(W * 0.045)
    ULH = max(2, round(H * 0.0026))
    STROKE = max(1, name_fs // 22)
    CUT = round(H * 0.014)
    EXP = round(H * 0.022)         # padding around the card layer for glow bleed

    disp = _font(_DISP_CJK if is_cjk else _DISP, name_fs)
    mono = _font(_MONO, eye_fs)

    meas = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    name_w = int(meas.textlength(name, font=disp))
    char_x = [int(meas.textlength(name[:i], font=disp)) for i in range(len(chars))]
    eye_txt = (eyebrow or "LIVE").upper()
    eye_w = int(meas.textlength(eye_txt, font=mono))
    asc, desc = disp.getmetrics()
    name_h = asc + desc
    text_w = max(name_w, eye_w + eye_fs)

    # card geometry (full-canvas coords); vertical centre at y_frac of the height
    card_top = round(H * y_frac) - AV // 2
    card_bottom = card_top + AV
    info_x = LM + AV + GAP
    panel_l, panel_t = LM - PAD, card_top - PAD
    panel_r, panel_b = info_x + text_w + PAD, card_bottom + PAD

    ul_y = card_bottom - round(H * 0.012)
    name_top = ul_y - round(H * 0.010) - name_h
    eye_bottom = name_top - round(H * 0.004)
    eye_top = eye_bottom - eye_fs

    # local card layer origin (expanded for glow bleed)
    ox, oy = panel_l - EXP, panel_t - EXP
    lw, lh = (panel_r - panel_l) + 2 * EXP, (panel_b - panel_t) + 2 * EXP

    def L(x, y):                                  # canvas -> local
        return x - ox, y - oy

    # ── static drop shadow (full canvas, rendered once) ──
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = round(H * 0.006)
    sd.polygon([(panel_l, panel_t + off), (panel_r, panel_t + off),
                (panel_r, panel_b - CUT + off), (panel_r - CUT, panel_b + off),
                (panel_l, panel_b + off)], fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(H * 0.012)))

    # ── static chrome behind the avatar (panel + borders) ──
    chrome_back = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    cb = ImageDraw.Draw(chrome_back)
    pl, pt = L(panel_l, panel_t)
    pr, pb = L(panel_r, panel_b)
    poly = [(pl, pt), (pr, pt), (pr, pb - CUT), (pr - CUT, pb), (pl, pb)]
    cb.polygon(poly, fill=(7, 12, 17, 175))
    cb.line([(pl, pt), (pr, pt)], fill=(*acc, 255), width=max(2, round(H * 0.0018)))
    cb.line([(pl, pt), (pl, pb)], fill=(*acc, 90), width=1)
    cb.line([(pr - round(W * 0.018), pt), (pr, pt)], fill=(*sec, 255),
            width=max(2, round(H * 0.0018)))      # orange accent on the top-right

    # ── static chrome in front of the avatar (frame + brackets) ──
    chrome_front = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    cf = ImageDraw.Draw(chrome_front)
    ax0, ay0 = L(LM, card_top)
    ax1, ay1 = L(LM + AV, card_bottom)
    cf.rectangle([ax0, ay0, ax1 - 1, ay1 - 1], outline=(*acc, 255),
                 width=max(2, round(H * 0.0018)))
    bl = round(AV * 0.16)
    for (cx, cy, dx, dy) in [(ax0, ay0, 1, 1), (ax1, ay0, -1, 1)]:   # tl, tr brackets
        cf.line([(cx, cy), (cx + dx * bl, cy)], fill=(*bright, 255), width=2)
        cf.line([(cx, cy), (cx, cy + dy * bl)], fill=(*bright, 255), width=2)

    # ── avatar image (raw, no filter) prepared once ──
    av_img = None
    if pfp is not None:
        try:
            av_img = pfp.convert("RGB").resize((AV - 4, AV - 4), Image.LANCZOS).convert("RGBA")
        except Exception:
            av_img = None
    if av_img is None:                            # placeholder: initial on dark tile
        av_img = Image.new("RGBA", (AV - 4, AV - 4), (10, 16, 22, 255))
        pd = ImageDraw.Draw(av_img)
        ph_f = _font(_DISP_CJK if is_cjk else _DISP, round(AV * 0.5))
        ch0 = name[0].upper()
        w0 = pd.textlength(ch0, font=ph_f)
        pd.text(((AV - 4 - w0) / 2, (AV - 4) * 0.18), ch0, font=ph_f, fill=(*acc, 230))

    # ── timeline (seconds) ──
    T_PANEL = 0.32
    EYE_S, EYE_D = 0.30, 0.40
    UL_S, UL_D = 0.24, 0.85
    NAME_S, CHAR_STEP, CHAR_D = 0.60, 0.065, 0.16
    FADE = 0.35
    name_end = NAME_S + (len(chars) - 1) * CHAR_STEP + CHAR_D
    total = max(hold, name_end + 0.2) + FADE
    n_frames = int(round(total * fps)) + 1

    for fi in range(n_frames):
        t = fi / fps
        env_out = 1 - _clamp((t - hold) / FADE)
        if env_out <= 0:
            Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(frames_dir / f"f_{fi:05d}.png")
            continue

        panel_a = _ease_out(t / T_PANEL) * env_out
        local = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))

        # shadow + back chrome (panel)
        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if panel_a > 0:
            frame.alpha_composite(_alpha_scaled(shadow, panel_a))
        local.alpha_composite(_alpha_scaled(chrome_back, panel_a))

        # avatar (slides in from the left with the panel)
        slide = int((1 - _ease_out(t / T_PANEL)) * round(H * 0.012))
        avx, avy = L(LM + 2 - slide, card_top + 2)
        local.alpha_composite(_alpha_scaled(av_img, panel_a), (avx, avy))
        local.alpha_composite(_alpha_scaled(chrome_front, panel_a))

        # eyebrow: ▶ triangle + label
        eye_a = _ease_out((t - EYE_S) / EYE_D) * env_out
        if eye_a > 0:
            lay = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            ex, ey = L(info_x, eye_top)
            tri = eye_fs * 0.5
            d.polygon([(ex, ey + eye_fs * 0.18), (ex, ey + eye_fs * 0.82),
                       (ex + tri, ey + eye_fs * 0.5)], fill=(*sec, 255))
            d.text((ex + tri + eye_fs * 0.4, ey), eye_txt, font=mono, fill=(*acc, 255))
            local.alpha_composite(_alpha_scaled(lay, eye_a))

        # underline: cyan main + orange sub, swiping out to full width
        ul_p = _ease_out_expo((t - UL_S) / UL_D)
        ul_a = _clamp((t - UL_S) / 0.18) * env_out
        if ul_a > 0 and ul_p > 0:
            lay = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            ux, uy = L(info_x, ul_y)
            w1 = int(name_w * ul_p)
            d.rectangle([ux, uy, ux + w1, uy + ULH], fill=(*acc, 255))
            d.rectangle([ux, uy + ULH + 3, ux + int(w1 * 0.58), uy + ULH + 3 + max(1, ULH - 1)],
                        fill=(*sec, 235))
            if ul_p > 0.98:                       # end tick
                tx = ux + name_w
                d.polygon([(tx + 4, uy - 3), (tx + 4, uy + ULH + 3), (tx + 13, uy + ULH // 2)],
                          fill=(*sec, 255))
            local.alpha_composite(_alpha_scaled(lay, ul_a))

        # name: per-character write-on with cyan glow + edge
        nlay = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        nd = ImageDraw.Draw(nlay)
        nx, ny = L(info_x, name_top)
        wrote = -1
        for i, ch in enumerate(chars):
            ca = _ease_out((t - (NAME_S + i * CHAR_STEP)) / CHAR_D) * env_out
            if ca <= 0:
                continue
            wrote = i
            ai = int(255 * _clamp(ca))
            nd.text((nx + char_x[i], ny), ch, font=disp, fill=(255, 255, 255, ai),
                    stroke_width=STROKE, stroke_fill=(*acc, ai))
        if wrote >= 0:
            glow = _colorize(nlay, acc).filter(ImageFilter.GaussianBlur(max(2, name_fs * 0.16)))
            local.alpha_composite(_alpha_scaled(glow, 0.55))
            local.alpha_composite(nlay)
            # caret at the write head while typing
            if t < name_end + 0.18:
                cx = nx + (char_x[wrote] + meas.textlength(chars[wrote], font=disp))
                blink = 0.35 + 0.65 * abs(__import__("math").sin(t * 7))
                clay = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
                ImageDraw.Draw(clay).rectangle(
                    [cx + 2, ny + name_h * 0.06, cx + 2 + max(2, name_fs // 16), ny + name_h * 0.9],
                    fill=(*bright, 255))
                local.alpha_composite(_alpha_scaled(clay, blink * env_out))

        frame.alpha_composite(local, (ox, oy))
        frame.save(frames_dir / f"f_{fi:05d}.png")

    return n_frames


# ── public API ──────────────────────────────────────────────────────────────

def build(cfg, clip):
    """Render (cached) a transparent nameplate .mov for one clip. None on failure."""
    v = cfg.get("video", {})
    np_cfg = v.get("nameplate", {}) or {}
    if not np_cfg.get("enabled", False):
        return None
    try:
        from PIL import Image  # noqa: F401  (ensures Pillow present before work)
        from .thumbnail import _twitch_pfp

        W, H = v.get("width", 1920), v.get("height", 1080)
        fps = v.get("fps", 30)
        hold = float(np_cfg.get("hold_seconds", 5.0))
        accent_name = np_cfg.get("accent", "cyan")
        accent = ACCENTS.get(accent_name, ACCENTS["cyan"])
        eyebrow = np_cfg.get("eyebrow", "LIVE")
        y_frac = float(np_cfg.get("y_frac", 0.33))
        name = clip.get("broadcaster_name") or "streamer"
        bid = str(clip.get("broadcaster_id", ""))

        data = Path(cfg["paths"]["data_abs"])
        cache_dir = data / "cache"
        np_dir = cache_dir / "nameplates"
        np_dir.mkdir(parents=True, exist_ok=True)

        key = hashlib.md5(
            f"{name}|{bid}|{accent_name}|{eyebrow}|{W}x{H}|{fps}|{hold}|{y_frac}|{_CACHE_VERSION}"
            .encode("utf-8")).hexdigest()[:16]
        out = np_dir / f"{key}.mov"
        if out.exists():
            return out

        pfp = _twitch_pfp(bid, cache_dir) if bid else None

        with tempfile.TemporaryDirectory() as td:
            frames = Path(td)
            _render_frames(frames, name, eyebrow, pfp, accent, W, H, fps, hold, y_frac)
            tmp = out.with_suffix(".tmp.mov")
            proc = subprocess.run(
                ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(frames / "f_%05d.png"),
                 "-c:v", "qtrle", "-pix_fmt", "argb", str(tmp)],
                capture_output=True, text=True)
            if proc.returncode != 0:
                log.warning("nameplate encode failed for %s: %s", name, proc.stderr[-400:])
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(out)
        log.info("nameplate -> %s (%s, avatar=%s)", out.name, name, "yes" if pfp else "no")
        return out
    except Exception as e:
        log.warning("nameplate build failed for %s: %s", clip.get("broadcaster_name"), e)
        return None
