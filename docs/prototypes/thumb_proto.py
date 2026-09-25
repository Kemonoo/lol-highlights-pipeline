"""Prototype thumbnails in the style of the channels that rank for 'lol moments'.

Standalone: reads the pipeline's work files and the streamer_cam locator, writes into
the scratchpad. Nothing in pipeline/ is modified.
"""
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

REPO = Path(r"C:\Users\micha\Documents\code\Clips")
sys.path.insert(0, str(REPO))
from pipeline.config import load_config  # noqa: E402
from pipeline.enrichment import streamer_cam  # noqa: E402

OUT = Path(__file__).parent / "thumbs"
OUT.mkdir(exist_ok=True)
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-24"
W, H = 1280, 720
IMPACT = "C:/Windows/Fonts/impact.ttf"

cfg = load_config(overlay=[str(REPO / "pipeline" / "config.kemono.yaml")])
cfg.setdefault("shorts", {})["facecam_method"] = "vlm"
clips = json.loads((REPO / f"data/work/{DATE}/api_scored.json").read_text(encoding="utf-8"))["clips"]
clips = sorted(clips, key=lambda c: -c.get("api_rank_score", 0))
raw = REPO / f"data/raw/{DATE}"
cache_p = OUT / f"cams_{DATE}.json"
cams = json.loads(cache_p.read_text()) if cache_p.exists() else {}


def frame(mp4, t):
    p = OUT / "_f.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(mp4),
                    "-frames:v", "1", str(p)], check=True)
    return Image.open(p).convert("RGB").resize((1920, 1080))


def cam_box(c, mp4):
    if c["id"] not in cams:
        b = streamer_cam.find(cfg, mp4, float(c.get("duration", 30)))
        cams[c["id"]] = list(b) if b else None
        cache_p.write_text(json.dumps(cams))
    b = cams[c["id"]]
    if not b:
        return None
    # the locator works on the 1280-wide sample frame or source px; normalise to 1920
    return b


def action_centre(img, box=None):
    """Where the fight is: the most saturated+bright area, HUD/minimap/webcam masked."""
    import numpy as np
    small = img.resize((192, 108)).convert("HSV")
    a = np.asarray(small).astype(float)
    m = (a[..., 1] / 255) * (a[..., 2] / 255)
    m[int(108 * 0.80):] = 0            # bottom HUD
    m[:int(108 * 0.08)] = 0            # score bar
    m[:, int(192 * 0.80):] = 0         # minimap/scoreboard/chat column
    m[:, :int(192 * 0.08)] = 0
    if box:
        x, y, bw, bh = (v / 10 for v in box)
        m[int(y):int(y + bh) + 1, int(x):int(x + bw) + 1] = 0
    from PIL import ImageFilter as F
    mi = Image.fromarray((m * 255).astype("uint8")).filter(F.GaussianBlur(12))
    b = np.asarray(mi).astype(float)
    yy, xx = np.unravel_index(b.argmax(), b.shape)
    return xx / 192, yy / 108


def best_frame(mp4, t, dur, box):
    """Of 5 frames around the judge's peak, the one with the most on-screen action."""
    import numpy as np
    best = None
    for dt in (-1.5, -0.75, 0, 0.75, 1.5):
        tt = min(max(t + dt, 0.2), max(dur - 0.3, 0.2))
        im = frame(mp4, tt)
        a = np.asarray(im.resize((192, 108)).convert("HSV")).astype(float)
        sc = ((a[..., 1] / 255) * (a[..., 2] / 255))[10:86, 15:153].mean()
        if best is None or sc > best[0]:
            best = (sc, im)
    return best[1]


def zoom(img, z, cx=0.5, cy=0.5):
    w, h = img.size
    cw, ch = w / z, h / z
    x0 = min(max(cx * w - cw / 2, 0), w - cw)
    y0 = min(max(cy * h - ch / 2, 0), h - ch)
    return img.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((W, H), Image.LANCZOS)


def pop(img):
    img = ImageEnhance.Color(img).enhance(1.35)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    return img.filter(ImageFilter.UnsharpMask(2, 80, 2))


def text(draw, xy, s, size, fill=(255, 230, 0), stroke=10, anchor="la"):
    f = ImageFont.truetype(IMPACT, size)
    draw.text(xy, s, font=f, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0), anchor=anchor)
    return f


def fit(s, max_w, start):
    size = start
    while size > 40 and ImageFont.truetype(IMPACT, size).getlength(s) > max_w:
        size -= 4
    return size


def border(img, color, px=12):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W - 1, H - 1), outline=color, width=px)
    return img


def cam_panel(src, box, w_out, edge=(255, 255, 255)):
    x, y, bw, bh = box
    cam = src.crop((int(x), int(y), int(x + bw), int(y + bh)))
    h_out = int(w_out * bh / bw)
    cam = pop(cam.resize((w_out, h_out), Image.LANCZOS))
    framed = Image.new("RGB", (w_out + 12, h_out + 12), edge)
    framed.paste(cam, (6, 6))
    return framed


def arrow(img, tip, tail, color=(235, 20, 20)):
    """Fat hand-drawn-looking arrow from tail to tip."""
    import math
    d = ImageDraw.Draw(img)
    ang = math.atan2(tip[1] - tail[1], tip[0] - tail[0])
    L = math.dist(tip, tail)
    head = 70
    body_end = (tip[0] - head * 0.8 * math.cos(ang), tip[1] - head * 0.8 * math.sin(ang))
    for wdt, col in ((34, (0, 0, 0)), (22, color)):
        d.line([tail, body_end], fill=col, width=wdt)
    def tri(sz):
        a1, a2 = ang + 2.6, ang - 2.6
        return [tip, (tip[0] + sz * math.cos(a1), tip[1] + sz * math.sin(a1)),
                (tip[0] + sz * math.cos(a2), tip[1] + sz * math.sin(a2))]
    d.polygon(tri(head + 14), fill=(0, 0, 0))
    d.polygon(tri(head), fill=color)
    return img


def style_daily(c, src, box, word):
    """'LoL Daily Moments': sharp gameplay, webcam picture-in-picture, 1-3 words."""
    cx, cy = action_centre(src, box)
    cx, cy = 0.5 + (cx - 0.5) * 0.6, 0.5 + (cy - 0.5) * 0.6
    img = pop(zoom(src, 1.25, cx, cy))
    left = False
    if box:
        panel = cam_panel(src, box, 450)
        left = box[0] + box[2] / 2 < 960        # the webcam's own side of the frame
        px = 22 if left else W - panel.width - 22
        img.paste(panel, (px, H - panel.height - 22))
    d = ImageDraw.Draw(img)
    size = fit(word, 800, 150)
    text(d, (40, 26), word, size)
    return border(img, (40, 230, 60))


def style_arrow(c, src, box, word):
    """Close-up on the fight + red arrow + big word, no face (Protatomonster-ish)."""
    cx, cy = action_centre(src, box)
    img = pop(zoom(src, 1.7, cx, cy))
    d = ImageDraw.Draw(img)
    size = fit(word, 1100, 170)
    text(d, (W / 2, 20), word, size, fill=(255, 255, 255), stroke=12, anchor="ma")
    return border(img, (230, 30, 30))


def style_split(c, src, box, word):
    """Reaction-forward: gameplay left 60%, big webcam right, word across the bottom."""
    cx, cy = action_centre(src, box)
    img = pop(zoom(src, 1.35, 0.5 + (cx - 0.5) * 0.6 - 0.12, 0.5 + (cy - 0.5) * 0.6))
    if box:
        x, y, bw, bh = box
        cam = src.crop((int(x), int(y), int(x + bw), int(y + bh)))
        pw = 500
        from PIL import ImageOps
        panel = pop(ImageOps.fit(cam, (pw, H), Image.LANCZOS, centering=(0.5, 0.4)))
        img.paste(panel, (W - pw, 0))
        ImageDraw.Draw(img).rectangle((W - pw - 8, 0, W - pw, H), fill=(255, 210, 0))
    d = ImageDraw.Draw(img)
    size = fit(word, 720, 140)
    text(d, (36, H - 24), word, size, anchor="ld")
    return border(img, (255, 210, 0), 10)


def word_for(c):
    t = (c.get("api_what_happens") or c.get("vlm_summary") or "").lower()
    ti = c["title"].lower()
    if "penta" in ti and "quadra" in t:
        return "PENTA STOLEN?!"
    t = t + " " + ti
    for k, wd in [("penta", "PENTAKILL?!"), ("quadra", "QUADRA KILL"), ("1v3", "1V3 OUTPLAY"),
                  ("one shot", "ONE SHOT"), ("dodge", "200 IQ DODGE"), ("triple", "TRIPLE KILL"),
                  ("outplay", "INSANE OUTPLAY")]:
        if k in t:
            return wd
    return "INSANE"


if __name__ == "__main__":
    picks = clips[:int(sys.argv[2]) if len(sys.argv) > 2 else 3]
    for c in picks:
        mp4 = raw / f"{c['id']}.mp4"
        if not mp4.exists():
            print("missing", c["id"]); continue
        t = float(c.get("api_best_moment_s") or 0)
        box = cam_box(c, mp4)
        # locator box is in source pixels; our src is resized to 1920x1080
        if box:
            iw, ih = streamer_cam._size(mp4) or (1920, 1080)
            sx, sy = 1920 / iw, 1080 / ih
            box = (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)
        src = best_frame(mp4, t, float(c.get("duration", 30)), box)
        word = word_for(c)
        name = c["broadcaster_name"]
        for fn in (style_daily, style_arrow, style_split):
            fn(c, src, box, word).save(OUT / f"{DATE}_{name}_{fn.__name__}.jpg", quality=92)
        print(name, word, box)
