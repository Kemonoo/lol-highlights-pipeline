"""Where are the streamers on screen? Their webcams, or the avatars that stand in for them.

Used by Shorts (split layout: gameplay above, streamer(s) below) and by the thumbnail's
reaction face. `find_all()` returns up to `shorts.facecam_max` boxes in source pixels
(empty = no streamer; Shorts then use the zoomed centre layout); `find()` returns the
largest one or None.

### Why the local vision model, and why still two steps

The Haar face detector (shorts._detect_facecam) finds FACES, which is the wrong question:
it boxed HUD champion portraits, minimap art and anime stickers, missed small or dark
webcams, and even when right it returned face + padding, not the webcam's frame. The
`vlm` role (Qwen3-VL, trained for grounding) answers the right question directly. On the
104 downloaded clips of 2026-09-23/24 it found ~17 webcams Haar missed and never boxed
game art (DEVLOG 2026-09-25).

Owner decisions (2026-09-25):
  * VTuber models (3D) and simple animated 2D avatars COUNT — they are the player's
    on-stream presence and they move. Not: champion portraits, items, the minimap,
    menus, stickers and other static pictures.
  * Duo streams are common (two webcams, e.g. Dantes on 09-24): account for two, at most
    three. The first version asked for "the" overlay and on duo clips boxed one webcam,
    or a box straddling both.

Step 1 asks for every overlay's box on three frames; an overlay is kept when two frames
agree on it (a small model occasionally boxes something different on one frame). The
agreed box is then SNAPPED to the overlay's real outline (see snap()). Step 2 asks a
narrower question about each crop alone — "streamer or part of the game/UI?" — which is
where a 4B model is reliable. Each step is one simple question (small models fail
compound ones).

No `vlm` role reachable -> the Haar detector, so a missing Ollama never costs a Short.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.streamer_cam")

LOCATE_PROMPT = (
    "This is a frame from a League of Legends Twitch stream. Streamers show themselves in "
    "overlays: a live webcam of a real person, OR an animated avatar that represents them "
    "(a VTuber model or a simple 2D character). Some streams show TWO streamers, each in "
    "their own overlay. Game elements do not count: champions, champion portraits, item "
    "icons, the minimap, menus, emotes and stickers. "
    "List the bounding box of EVERY streamer overlay (the whole overlay, not just the "
    "face). Answer ONLY JSON: "
    '{"streamers": [{"bbox_2d": [x1, y1, x2, y2]}, ...]} or {"streamers": []}'
)
CHECK_PROMPT = (
    "This image is cropped from a video game stream. Is it the streamer themselves — a "
    "live camera of a real person, or an animated avatar / VTuber character that "
    "represents them? Or is it part of the game or its interface (a champion, an icon, "
    "the map, a menu) or a static picture? "
    'Answer ONLY JSON: {"streamer": true} or {"streamer": false}'
)
# No `schema=` on these calls: with images, Ollama's constrained decoding often returns
# an empty answer from qwen3-vl, and the provider then asks again without it — every
# question paid twice (measured ~55 s vs ~20 s per clip). The prompts ask for JSON and
# parse_json reads it.


# ── pure helpers (tested) ─────────────────────────────────────────────────────

def to_pixels(box, w: int, h: int) -> tuple | None:
    """Qwen3-VL grounding box (0-1000 relative) -> (x, y, bw, bh) pixels, or None."""
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((max(0.0, min(1000.0, x1)), max(0.0, min(1000.0, x2))))
    y1, y2 = sorted((max(0.0, min(1000.0, y1)), max(0.0, min(1000.0, y2))))
    bw, bh = (x2 - x1) * w / 1000, (y2 - y1) * h / 1000
    if bw < 0.04 * w or bh < 0.04 * h:          # a sliver is not an overlay
        return None
    if bw > 0.9 * w and bh > 0.9 * h:           # "the whole frame" = no overlay
        return None
    return (int(x1 * w / 1000), int(y1 * h / 1000), int(bw), int(bh))


def parse_boxes(ans, w: int, h: int) -> list[tuple]:
    """Every box in a locate answer, in pixels. Tolerates the shapes a small model
    actually produces: {"streamers": [...]}, a bare list, or the old single-box
    {"streamer": true, "bbox_2d": [...]}."""
    if isinstance(ans, dict) and "bbox_2d" in ans:
        items = [ans] if ans.get("streamer", True) else []
    elif isinstance(ans, dict):
        items = ans.get("streamers") or []
    elif isinstance(ans, list):
        items = ans
    else:
        items = []
    out = []
    for it in items:
        raw = it.get("bbox_2d") if isinstance(it, dict) else it
        b = to_pixels(raw, w, h)
        if b:
            out.append(b)
    return out


def iou(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def _mean(boxes: list) -> tuple:
    return tuple(int(sum(v[i] for v in boxes) / len(boxes)) for i in range(4))


def agree(boxes: list, min_votes: int = 2, min_iou: float = 0.5) -> tuple | None:
    """The box most other frames agree with (IoU >= min_iou), averaged over its group.
    `boxes` holds one entry per sampled frame: a pixel box, or None."""
    found = consensus([[b] if b else [] for b in boxes], min_votes, min_iou, 1)
    return found[0] if found else None


def consensus(per_frame: list[list], min_votes: int = 2, min_iou: float = 0.5,
              max_n: int = 2) -> list[tuple]:
    """Overlays that at least `min_votes` frames agree on, most-voted first, at most max_n.

    per_frame[k] = the boxes frame k reported. A frame votes at most once per overlay.
    Overlays that overlap an already-kept one (IoU > 0.3) are the same overlay boxed
    differently, not a second streamer."""
    all_boxes = [(k, b) for k, bs in enumerate(per_frame) for b in bs]
    groups = []
    for _, b in all_boxes:
        members, frames = [], set()
        for k2, o in sorted(all_boxes, key=lambda kb: -iou(b, kb[1])):
            if k2 not in frames and iou(b, o) >= min_iou:
                members.append(o)
                frames.add(k2)
        if len(frames) >= min_votes:
            groups.append((len(frames), _mean(members)))
    groups.sort(key=lambda g: (-g[0], -(g[1][2] * g[1][3])))
    kept: list[tuple] = []
    for _, box in groups:
        if all(iou(box, k) <= 0.3 for k in kept):
            kept.append(box)
        if len(kept) >= max_n:
            break
    return kept


def _line_score(frames, axis: int, pos: int, lo: int, hi: int) -> float:
    """How much of the side's length (lo..hi) has a step edge at `pos` — median/min mix
    over frames, because an overlay's outline is there in EVERY frame and a gameplay
    edge is not. axis=1: vertical line at column pos; axis=0: horizontal at row pos."""
    import numpy as np
    scores = []
    for f in frames:
        if axis == 1:
            a, b = f[lo:hi, pos - 2], f[lo:hi, pos + 1]
        else:
            a, b = f[pos - 2, lo:hi], f[pos + 1, lo:hi]
        scores.append(float((np.abs(a - b) > 18).mean()))
    return float(np.median(scores)) * 0.5 + min(scores) * 0.5


def snap(frames, box: tuple, band: float = 0.18, min_score: float = 0.15) -> tuple:
    """Rough box -> the overlay's real outline. Pure numpy, so it is unit-tested.

    From the thumbnail session's prototype (docs/prototypes/cam_snap.py, 2026-09-25):
    the model's box is a few % off on every side (owner: cuts part of the webcam on one
    side, takes some gameplay on another). A webcam is a rectangle pasted over the game,
    so per side take the strongest persistent straight line within ±`band` of the rough
    edge. Dark webcam on dark game measured 0.20-0.40 (real); no line at all ~0.09, in
    which case the overlay runs off-screen if the frame border is near, else the rough
    edge is kept. No rectangle (green screen, VTuber) = nothing snaps = rough box.
    `frames` are same-size grayscale float arrays.
    """
    H, W = frames[0].shape
    x, y, w, h = box
    x0, y0, x1, y1 = x, y, x + w, y + h
    ry0, ry1 = int(y0 + 0.15 * h), int(y1 - 0.15 * h)   # middle 70% of each side
    rx0, rx1 = int(x0 + 0.15 * w), int(x1 - 0.15 * w)
    out = {}
    for side, axis, centre, span, lim in (
            ("l", 1, x0, (ry0, ry1), W), ("r", 1, x1, (ry0, ry1), W),
            ("t", 0, y0, (rx0, rx1), H), ("b", 0, y1, (rx0, rx1), H)):
        d = int(band * (w if axis == 1 else h))
        best = (0.0, centre)
        for pos in range(max(3, int(centre - d)), min(lim - 3, int(centre + d))):
            s = _line_score(frames, axis, pos, *span)
            s -= 0.10 * abs(pos - centre) / max(d, 1)   # ties -> closest to the model
            if s > best[0]:
                best = (s, pos)
        if best[0] >= min_score:
            out[side] = best[1]
        else:
            edge = 0 if side in ("l", "t") else lim
            out[side] = edge if abs(edge - centre) <= 2 * d else centre
    nx0, nx1, ny0, ny1 = out["l"], out["r"], out["t"], out["b"]
    nx0 = 0 if nx0 < 8 else nx0                     # a line hugging the frame border
    ny0 = 0 if ny0 < 8 else ny0                     # IS the border
    nx1 = W if W - nx1 < 8 else nx1
    ny1 = H if H - ny1 < 8 else ny1
    if nx1 - nx0 < 0.5 * w or ny1 - ny0 < 0.5 * h:   # snapping collapsed it: distrust
        return box
    return (int(nx0), int(ny0), int(nx1 - nx0), int(ny1 - ny0))


# ── frames ────────────────────────────────────────────────────────────────────

def _frame_jpg(mp4: Path, t: float, width: int = 1280) -> bytes | None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "f.jpg"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "3", str(out)],
                       capture_output=True)
        return out.read_bytes() if out.exists() else None


def _gray_frames(mp4: Path, times: list, w: int, h: int) -> list:
    import numpy as np
    from PIL import Image
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            p = Path(td) / f"{i}.png"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i",
                            str(mp4), "-frames:v", "1", "-vf", f"scale={w}:{h}", str(p)],
                           capture_output=True)
            if p.exists():
                out.append(np.asarray(Image.open(p).convert("L")).astype(np.float32))
    return out


def _size(mp4: Path) -> tuple[int, int] | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(mp4)],
                       capture_output=True, text=True)
    try:
        w, h = (int(v) for v in r.stdout.strip().split(",")[:2])
        return w, h
    except ValueError:
        return None


def _crop_jpg(jpg: bytes, box: tuple) -> bytes | None:
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    x, y, w, h = box
    ok, buf = cv2.imencode(".jpg", img[y:y + h, x:x + w])
    return buf.tobytes() if ok else None


# ── model calls ───────────────────────────────────────────────────────────────

def _is_streamer_crop(provider, jpg: bytes, small_box: tuple) -> bool:
    crop = _crop_jpg(jpg, small_box)
    verdict = (provider.complete_json(CHECK_PROMPT, images=[crop]) if crop else None) or {}
    return bool(verdict.get("streamer"))


def locate_vlm(mp4: Path, duration: float, provider, frames: int = 3,
               max_n: int = 2, snap_edges: bool = True) -> list[tuple]:
    """Two-step VLM location (see module docstring) -> up to max_n boxes in source px.
    Raises if the provider fails, so the caller can fall back."""
    size = _size(mp4)
    if not size:
        return []
    src_w, src_h = size
    small_h = round(1280 * src_h / src_w)
    times = [duration * (k + 1) / (frames + 1) for k in range(frames)]
    jpgs, per_frame = [], []
    for t in times:
        jpg = _frame_jpg(mp4, t)
        if jpg is None:
            continue
        ans = provider.complete_json(LOCATE_PROMPT, images=[jpg])
        jpgs.append(jpg)
        per_frame.append(parse_boxes(ans, 1280, small_h))
    agreed = consensus(per_frame, max_n=max_n)
    if not agreed:
        log.info("  streamer cam (vlm): none (%s)",
                 ", ".join(str(len(b)) for b in per_frame))
        return []

    k = src_w / 1280
    grays = (_gray_frames(mp4, [duration * (i + 1) / 7 for i in range(6)], src_w, src_h)
             if snap_edges else [])
    mid = jpgs[len(jpgs) // 2]
    boxes = []
    for small in agreed:
        box = tuple(int(v * k) for v in small)
        if grays:
            box = snap(grays, box)
        check = tuple(int(v / k) for v in box)
        if not _is_streamer_crop(provider, mid, check):
            log.info("  streamer cam (vlm): a box was found but its crop is not the streamer")
            continue
        x, y, w, h = box
        boxes.append((x, y, min(w, src_w - x), min(h, src_h - y)))
    log.info("  streamer cam (vlm): %d overlay(s) %s", len(boxes), boxes)
    return boxes


def is_streamer(provider, mp4: Path, duration: float, box: tuple) -> bool:
    """Step 2 alone, for a box found some other way (the Haar detector): is that crop
    the streamer? Asked on the middle frame."""
    size = _size(mp4)
    jpg = _frame_jpg(mp4, duration / 2) if size else None
    if not jpg:
        return False
    k = 1280 / size[0]
    return _is_streamer_crop(provider, jpg, tuple(int(v * k) for v in box))


def find_all(cfg: dict, mp4: Path, duration: float) -> list[tuple]:
    """The streamers' on-screen boxes for Shorts/thumbnails, per `shorts.facecam_method`.

    "haar": the face detector with the live-face gates (shorts._detect_facecam), 0-1 box.
    "vlm":  the local vision model locates and snaps the overlays (locate_vlm). When it
            finds none, the face detector's candidate gets a second chance, but only if
            the model confirms that crop is the streamer — on 2026-09-23 the model alone
            missed a few webcams the face detector had found. The vlm role being
            unreachable falls back to plain "haar".
    """
    from ..publishing.shorts import _detect_facecam
    sh = cfg.get("shorts", {})

    def haar():
        b = _detect_facecam(mp4, duration, float(sh.get("facecam_min_live", 3.0)),
                            float(sh.get("facecam_min_skin", 0.12)))
        return [b] if b else []

    if sh.get("facecam_method", "haar") != "vlm":
        return haar()
    try:
        from ..providers import get_provider
        provider = get_provider(cfg, "vlm", vision=True)
        boxes = locate_vlm(mp4, duration, provider, int(sh.get("facecam_vlm_frames", 3)),
                           int(sh.get("facecam_max", 2)), bool(sh.get("facecam_snap", True)))
        if boxes:
            return boxes
        cand = haar()
        if cand and is_streamer(provider, mp4, duration, cand[0]):
            log.info("  streamer cam: face detector's box confirmed by the vlm")
            return cand
        return []
    except Exception as e:                            # Ollama down, model missing, ...
        log.warning("  streamer cam: vlm unavailable (%s) - using the face detector", e)
        return haar()


def find(cfg: dict, mp4: Path, duration: float) -> tuple | None:
    """The largest streamer overlay (the thumbnail wants one face), or None."""
    boxes = find_all(cfg, mp4, duration)
    return max(boxes, key=lambda b: b[2] * b[3]) if boxes else None
