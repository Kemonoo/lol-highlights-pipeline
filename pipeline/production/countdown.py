"""Animated countdown badge (#N) as a transparent overlay.

Why this is not a drawtext filter any more: the badge used to be drawn straight into
the segment filtergraph with `drawtext`, using the bundled Montserrat-Bold.ttf. That
file is a VARIABLE font whose default weight is 100 — its style name is literally
"Montserrat Thin". PIL can set the weight axis (`set_variation_by_axes`, which is what
the thumbnail and the brand wordmark do, at 800), but ffmpeg's drawtext cannot, so it
rendered the badge in Thin at 118px. It looked spindly and nothing like the rest of the
brand, and no amount of border weight fixed it — a heavier outline on a hairline glyph
just reads as an outline.

Rendering the badge with PIL instead gets the real weight, and once frames are being
drawn anyway the position and the animation become free. Same transparent-qtrle-overlay
technique `nameplate.py` uses; assemble composites it the same way.

Cheap to cache: unlike a nameplate (per streamer, per avatar) there are only ever as
many badges as there are clips in a video, so the ~24 possible files are rendered once
and reused for the life of the project.
"""
import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.countdown")

_CACHE_VERSION = "v1"

# Fallbacks if the configured face is missing. Arial Black and Impact are both genuinely
# heavy at their default weight, so they survive the drawtext-style trap above.
_FALLBACKS = [
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(path: str, size: int, weight: int | None):
    from PIL import ImageFont
    cands = [path, *_FALLBACKS] if path else list(_FALLBACKS)
    for p in cands:
        if not p or not Path(p).exists():
            continue
        try:
            f = ImageFont.truetype(p, size)
        except OSError:
            continue
        if weight:
            try:
                f.set_variation_by_axes([weight])   # the whole point — see module docstring
            except Exception:
                pass                                # static font: already the right weight
        return f
    from PIL import ImageFont as IF
    return IF.load_default()


def _ease_out_back(x: float) -> float:
    """Ease-out with a small overshoot — the badge settles by springing back a little.

    Deliberately different from the nameplate's write-on: the owner asked for animation
    'similar, but not the same'. The nameplate slides in from the left and types itself
    on; the badge punches in from slightly too large and settles.
    """
    c1, c3 = 1.70158, 2.70158
    x = max(0.0, min(1.0, x))
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _render_frames(frames_dir: Path, rank: int, W: int, H: int, fps: int,
                   hold: float, y_frac: float, cd: dict) -> None:
    from PIL import Image, ImageDraw

    size = int(cd.get("size", 132))
    weight = cd.get("font_weight", 800)
    weight = int(weight) if weight else None
    fpath = str(cd.get("font", "")) or ""

    # The punch-in scales the type, so each frame needs its own size. Memoised because
    # a 4s badge is ~120 frames x 2 faces and truetype() is not cheap.
    _cache: dict[int, object] = {}

    def face(px: int):
        px = max(6, int(px))
        if px not in _cache:
            _cache[px] = _font(fpath, px, weight)
        return _cache[px]

    colour = tuple(cd.get("color", (255, 255, 255)))
    stroke = int(cd.get("stroke", 9))

    num = str(rank)
    right = W - int(cd.get("margin_x", 96))
    cy = int(H * y_frac)

    total = max(1, int((hold + 0.7) * fps))
    intro = max(1, int(0.42 * fps))          # punch-in
    outro_at = total - max(1, int(0.35 * fps))

    for i in range(total):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        t = i / fps

        if i < intro:
            p = _ease_out_back(i / intro)
            scale, alpha = 0.62 + 0.38 * p, min(1.0, i / max(intro * 0.5, 1))
        elif i >= outro_at:
            k = (i - outro_at) / max(total - outro_at, 1)
            scale, alpha = 1.0, max(0.0, 1.0 - k)
        else:
            # a very slow drift so it is not perfectly static while it holds
            scale, alpha = 1.0 + 0.012 * ((t - 0.42) % 2.0) / 2.0, 1.0

        s_num = face(size * scale)
        s_hash = face(size * 0.44 * scale)
        a = int(255 * alpha)
        if a <= 0:
            img.save(frames_dir / f"f_{i:05d}.png")
            continue

        nb = d.textbbox((0, 0), num, font=s_num)
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        hb = d.textbbox((0, 0), "#", font=s_hash)
        hw = hb[2] - hb[0]

        x = right - nw
        y = cy - nh // 2
        d.text((x - hw - int(size * 0.12), y + int(nh * 0.30)), "#", font=s_hash,
               fill=(*colour, int(a * 0.78)), stroke_width=max(2, stroke - 3),
               stroke_fill=(0, 0, 0, a))
        d.text((x, y), num, font=s_num, fill=(*colour, a),
               stroke_width=stroke, stroke_fill=(0, 0, 0, a))
        img.save(frames_dir / f"f_{i:05d}.png")


def build(cfg: dict, rank: int) -> Path | None:
    """Render (cached) a transparent countdown-badge .mov for one rank. None on failure."""
    v = cfg.get("video", {})
    cd = v.get("countdown", {}) or {}
    if not v.get("countdown_enabled", True) or not rank:
        return None
    try:
        W, H = v.get("width", 1920), v.get("height", 1080)
        fps = v.get("fps", 30)
        hold = float(cd.get("hold_seconds", 3.4))
        y_frac = float(cd.get("y_frac", 0.17))

        data = Path(cfg["paths"]["data_abs"])
        out_dir = data / "cache" / "countdown"
        out_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(
            f"{rank}|{W}x{H}|{fps}|{hold}|{y_frac}|{cd.get('font')}|"
            f"{cd.get('font_weight')}|{cd.get('size')}|{cd.get('color')}|"
            f"{cd.get('stroke')}|{cd.get('margin_x')}|{_CACHE_VERSION}".encode()
        ).hexdigest()[:16]
        out = out_dir / f"{key}.mov"
        if out.exists():
            return out

        with tempfile.TemporaryDirectory() as td:
            frames = Path(td)
            _render_frames(frames, rank, W, H, fps, hold, y_frac, cd)
            tmp = out.with_suffix(".tmp.mov")
            proc = subprocess.run(
                ["ffmpeg", "-y", "-framerate", str(fps),
                 "-i", str(frames / "f_%05d.png"),
                 "-c:v", "qtrle", "-pix_fmt", "argb", str(tmp)],
                capture_output=True, text=True)
            if proc.returncode != 0:
                log.warning("countdown encode failed for #%s: %s", rank, proc.stderr[-400:])
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(out)
        log.info("countdown badge -> %s (#%s)", out.name, rank)
        return out
    except Exception as e:
        log.warning("countdown build failed for #%s: %s", rank, e)
        return None
