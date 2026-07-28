"""Channel branding — the logo card + animated intro sting.

Identity is entirely config-driven (video.brand): a champion splash backdrop pulled from
Riot's Data Dragon CDN (video.brand.splash_champion / splash_skin), or your own artwork
via video.brand.splash_path, behind a gold wordmark (video.brand.name). The wordmark
reuses the thumbnail's gold metallic treatment so the brand reads consistently across
logo/intro/thumbnails.

  build_logo(cfg)        -> 1920x1080 logo PNG (cached splash, recomposited each call)
  build_intro(cfg, out)  -> short animated sting (slow push-in + fades), encoded to match
                            assemble's concat format so it drops straight into the video.

Set video.brand.splash_path to a local file to avoid depending on Data Dragon at all —
recommended for a real channel, since champion art is Riot's, not yours.
"""
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("pipeline.brand")

# The intro is concatenated with the segments, so it MUST be encoded identically —
# hence the shared helper rather than a second copy of the flags.
from .assemble import enc as _enc  # noqa: E402


def _ff(args: list) -> None:
    r = subprocess.run(["ffmpeg", "-y", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-600:])


def _cover(img, w: int, h: int):
    from PIL import Image
    img = img.convert("RGB")
    sw, sh = img.size
    s = max(w / sw, h / sh)
    img = img.resize((int(sw * s), int(sh * s)), Image.LANCZOS)
    ox, oy = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((ox, oy, ox + w, oy + h))


def _splash(cfg: dict, cache: Path):
    """Brand backdrop: your own artwork if set, else a Data Dragon champion splash.

    Returns a PIL RGB image, or None on failure (build_logo then falls back to a flat
    dark card, so a missing/offline splash never breaks a run)."""
    from PIL import Image

    from ..config import ROOT
    b = cfg.get("video", {}).get("brand", {})
    override = b.get("splash_path", "")
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = ROOT / override
        if p.exists():
            return Image.open(p).convert("RGB")
        log.warning("brand.splash_path %s not found - using Data Dragon splash", p)
    champion = str(b.get("splash_champion", "LeeSin") or "LeeSin")
    skin = int(b.get("splash_skin", 0))
    f = cache / f"{champion.lower()}_{skin}.jpg"
    if not f.exists():
        import requests
        url = (f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/"
               f"{champion}_{skin}.jpg")
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            f.write_bytes(r.content)
        except Exception as e:
            log.warning("brand splash download failed (%s_%s): %s — using a plain card. "
                        "Champion ids are case-sensitive, e.g. LeeSin, MissFortune, KaiSa.",
                        champion, skin, e)
            return None
    return Image.open(f).convert("RGB")


def build_logo(cfg: dict) -> Path:
    """Composite the 1920x1080 brand logo card → data/cache/brand/logo.png."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    from .thumbnail import _ANN_GOLD, _afont, _cinematic_grade, _metallic_line, _vignette_overlay
    v = cfg["video"]
    b = v.get("brand", {})
    W, H = v["width"], v["height"]
    cache = Path(cfg["paths"]["data_abs"]) / "cache" / "brand"
    cache.mkdir(parents=True, exist_ok=True)

    splash = _splash(cfg, cache)
    if splash is not None:
        bg = _vignette_overlay(_cinematic_grade(_cover(splash, W, H)), 0.92)
        bg = ImageEnhance.Brightness(bg).enhance(0.6)        # dim so the wordmark pops
    else:
        bg = Image.new("RGB", (W, H), (11, 14, 20))
    canvas = bg.convert("RGBA")

    # soft dark band behind the wordmark for legibility over the art
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(scrim).rectangle([0, int(H * 0.30), W, int(H * 0.74)], fill=(0, 0, 0, 110))
    canvas.alpha_composite(scrim.filter(ImageFilter.GaussianBlur(70)))

    name = (b.get("name") or "DAILY HIGHLIGHTS").upper()
    word = _metallic_line(name, _afont("Montserrat-Bold.ttf", 210, 800), _ANN_GOLD)
    canvas.alpha_composite(word, ((W - word.width) // 2, int(H * 0.33)))

    tag = b.get("tagline", "LEAGUE OF LEGENDS")
    if tag:
        tf = _afont("Montserrat-Bold.ttf", 46, 600)
        d = ImageDraw.Draw(canvas)
        tb = d.textbbox((0, 0), tag, font=tf)
        d.text(((W - (tb[2] - tb[0])) // 2, int(H * 0.625)), tag, font=tf,
               fill=(236, 239, 246), stroke_width=3, stroke_fill=(0, 0, 0))

    out = cache / "logo.png"
    canvas.convert("RGB").save(out)
    return out


def build_intro(cfg: dict, out: Path) -> Path:
    """Render the animated intro sting (slow push-in + fade in/out, silent audio track)."""
    v = cfg["video"]
    b = v.get("brand", {})
    W, H, fps = v["width"], v["height"], v["fps"]
    dur = float(b.get("intro_seconds", 2.8))
    logo = build_logo(cfg)

    frames = max(1, int(dur * fps))
    zoom = (f"scale={W*2}:-1,zoompan=z='min(zoom+0.0012,1.10)':d={frames}:"
            f"s={W}x{H}:fps={fps}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
    vf = (f"{zoom},fade=t=in:st=0:d=0.4,fade=t=out:st={dur-0.5:.2f}:d=0.5,"
          f"format=yuv420p")
    _ff(["-loop", "1", "-i", str(logo),
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.2f}",
         "-t", f"{dur:.2f}", "-filter_complex", f"[0:v]{vf}[v]",
         "-map", "[v]", "-map", "1:a",
         *_enc(v), str(out)])
    return out
