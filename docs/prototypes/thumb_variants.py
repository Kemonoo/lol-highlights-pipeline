"""Style A variations: border reds/widths, text colours, fonts. Scratch only."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.argv = [sys.argv[0], "2026-09-24"]
import thumb_proto as tp  # noqa: E402

OUT = tp.OUT / "variants"
OUT.mkdir(exist_ok=True)
W, H = tp.W, tp.H

REDS = {"pure #FF0000": (255, 0, 0), "torero #E10600": (225, 6, 0),
        "YouTube #FF0033": (255, 0, 51), "deep #C8102E": (200, 16, 46)}

FONTS = {
    "Impact": ("C:/Windows/Fonts/impact.ttf", None),
    "Montserrat Black": (str(tp.REPO / "assets/fonts/Montserrat-Bold.ttf"), b"Black"),
    "Arial Black": ("C:/Windows/Fonts/ariblk.ttf", None),
    "Segoe UI Black": ("C:/Windows/Fonts/seguibl.ttf", None),
    "Bahnschrift Bold Cond.": ("C:/Windows/Fonts/bahnschrift.ttf", b"Bold Condensed"),
    "Verdana Bold": ("C:/Windows/Fonts/verdanab.ttf", None),
}

# (fill, stroke) text colour schemes
SCHEMES = {
    "yellow / black edge": ((255, 230, 0), (0, 0, 0)),
    "yellow / red edge": ((255, 230, 0), (225, 6, 0)),
    "white / red edge": ((255, 255, 255), (225, 6, 0)),
    "red / white edge": ((225, 6, 0), (255, 255, 255)),
    "white / black edge": ((255, 255, 255), (0, 0, 0)),
    "yellow on red box": ((255, 230, 0), None),
}


def font(name, size):
    path, var = FONTS[name]
    f = ImageFont.truetype(path, size)
    if var:
        f.set_variation_by_name(var)
    return f


def fit(name, s, max_w, start):
    size = start
    while size > 40 and font(name, size).getlength(s) > max_w:
        size -= 4
    return size


def base(c, src, box):
    cx, cy = tp.action_centre(src, box)
    cx, cy = 0.5 + (cx - 0.5) * 0.6, 0.5 + (cy - 0.5) * 0.6
    img = tp.pop(tp.zoom(src, 1.25, cx, cy))
    if box:
        panel = tp.cam_panel(src, box, 450)
        left = box[0] + box[2] / 2 < 960
        img.paste(panel, (22 if left else W - panel.width - 22, H - panel.height - 22))
    return img


def render(img, word, *, red=(225, 6, 0), width=16, fname="Impact",
           scheme="yellow / black edge", glow=False, label=""):
    img = img.copy()
    d = ImageDraw.Draw(img)
    fill, stroke = SCHEMES[scheme]
    size = fit(fname, word, 800, 150)
    f = font(fname, size)
    sw = max(6, size // 14)
    if stroke is None:                                   # solid red box behind
        x0, y0, x1, y1 = d.textbbox((40, 30), word, font=f)
        d.rectangle((x0 - 18, y0 - 12, x1 + 18, y1 + 14), fill=red)
        d.text((40, 30), word, font=f, fill=fill, stroke_width=4, stroke_fill=(0, 0, 0))
    else:
        if stroke != (0, 0, 0):                          # thin black rim outside the colour edge
            d.text((40, 30), word, font=f, fill=fill, stroke_width=sw + 5, stroke_fill=(0, 0, 0))
        d.text((40, 30), word, font=f, fill=fill, stroke_width=sw, stroke_fill=stroke)
    if glow:                                             # soft red bleed inside the border
        g = Image.new("L", (W, H), 0)
        ImageDraw.Draw(g).rectangle((0, 0, W - 1, H - 1), outline=255, width=width + 26)
        g = g.filter(ImageFilter.GaussianBlur(14))
        img = Image.composite(Image.new("RGB", (W, H), red), img, g.point(lambda v: int(v * 0.8)))
    ImageDraw.Draw(img).rectangle((0, 0, W - 1, H - 1), outline=red, width=width)
    if label:
        lf = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 30)
        ImageDraw.Draw(img).text((W - 20, 20), label, font=lf, fill=(255, 255, 255),
                                 anchor="ra", stroke_width=3, stroke_fill=(0, 0, 0))
    return img


def sheet(tiles, name, cols=3):
    rows = (len(tiles) + cols - 1) // cols
    s = Image.new("RGB", (cols * 650 + 10, rows * 370 + 10), (30, 30, 30))
    for i, t in enumerate(tiles):
        s.paste(t.resize((640, 360), Image.LANCZOS), (10 + (i % cols) * 650, 10 + (i // cols) * 370))
    s.save(OUT / name, quality=90)


if __name__ == "__main__":
    import json
    picks = {c["broadcaster_name"]: c for c in tp.clips[:6]}
    cams = json.loads(tp.cache_p.read_text())
    imgs = {}
    for nm in ("GinLaBg", "KeshaEuw", "LivinhaSayuri"):
        c = picks[nm]
        mp4 = tp.raw / f"{c['id']}.mp4"
        box = cams.get(c["id"])
        if box:
            iw, ih = tp.streamer_cam._size(mp4) or (1920, 1080)
            box = (box[0] * 1920 / iw, box[1] * 1080 / ih, box[2] * 1920 / iw, box[3] * 1080 / ih)
        src = tp.best_frame(mp4, float(c.get("api_best_moment_s") or 0), float(c["duration"]), box)
        imgs[nm] = (base(c, src, box), tp.word_for(c))

    g, gw = imgs["GinLaBg"]
    # 1) border: 4 reds x 3 widths (+glow on the thickest)
    t = []
    for rn, rc in REDS.items():
        for w in (10, 20, 30):
            t.append(render(g, gw, red=rc, width=w, glow=(w == 30), label=f"{rn} {w}px{' +glow' if w == 30 else ''}"))
    sheet(t, "1_borders.jpg")
    # 2) text colours (torero red border 20px), two clips
    t = []
    for sc in SCHEMES:
        for nm in ("GinLaBg", "KeshaEuw"):
            im, wd = imgs[nm]
            t.append(render(im, wd, width=20, scheme=sc, label=sc))
    sheet(t, "2_text_colours.jpg", cols=4)
    # 3) fonts (yellow / black edge, torero 20px), two clips
    t = []
    for fn in FONTS:
        for nm in ("LivinhaSayuri", "KeshaEuw"):
            im, wd = imgs[nm]
            t.append(render(im, wd, width=20, fname=fn, label=fn))
    sheet(t, "3_fonts.jpg", cols=4)
    print("ok")
