"""Where is the streamer on screen? Their webcam, or the avatar that stands in for them.

Used by Shorts (split layout: gameplay above, streamer below) and by the thumbnail's
reaction face. Returns a crop box in source pixels, or None (Shorts then use the zoomed
centre layout).

### Why the local vision model, and why still two steps

The Haar face detector (shorts._detect_facecam) finds FACES, which is the wrong question:
it boxed HUD champion portraits, minimap art and anime stickers, missed small or dark
webcams, and even when right it returned face + padding, not the webcam's frame. The
`vlm` role (Qwen3-VL, trained for grounding) answers the right question directly and
returns the whole overlay rectangle. Measured 2026-09-24 on 9 hard clips from 09-23:
it boxed all 5 webcams including 4 the Haar detector missed, and said "none" on the
menu screen and the no-webcam clip.

Owner decision (2026-09-25): VTuber models (3D) and simple animated 2D avatars COUNT —
they are the player's on-stream presence and they move, which is what the lower panel
is for. What does not count: champion portraits, items, the minimap, menus, stickers
and other static pictures.

Step 1 asks for the box on three frames and keeps it only when two agree (a small model
occasionally boxes something different on one frame). Step 2 asks a narrower question
about the crop alone — "streamer or part of the game/UI?" — which is where a 4B model
is reliable. Both steps are one simple question each (small models fail compound ones).

No `vlm` role reachable -> the Haar detector, so a missing Ollama never costs a Short.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("pipeline.streamer_cam")

LOCATE_PROMPT = (
    "This is a frame from a League of Legends Twitch stream. Streamers usually show "
    "themselves in an overlay: a live webcam of the real person, OR an animated avatar "
    "that represents them (a VTuber model or a simple 2D character). "
    "Is there such an overlay of the streamer? Game elements do not count: champions, "
    "champion portraits, item icons, the minimap, menus, emotes and stickers. "
    "If yes, give the bounding box of the whole overlay. Answer ONLY JSON: "
    '{"streamer": true, "bbox_2d": [x1, y1, x2, y2]} or {"streamer": false}'
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


def iou(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def agree(boxes: list, min_votes: int = 2, min_iou: float = 0.5) -> tuple | None:
    """The box most other frames agree with (IoU >= min_iou), averaged over its group.

    `boxes` holds one entry per sampled frame: a pixel box, or None for "no streamer".
    """
    real = [b for b in boxes if b]
    best: list = []
    for b in real:
        group = [o for o in real if iou(b, o) >= min_iou]
        if len(group) > len(best):
            best = group
    if len(best) < min_votes:
        return None
    return tuple(int(sum(v[i] for v in best) / len(best)) for i in range(4))


# ── model calls ───────────────────────────────────────────────────────────────

def _frame_jpg(mp4: Path, t: float, width: int = 1280) -> bytes | None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "f.jpg"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "3", str(out)],
                       capture_output=True)
        return out.read_bytes() if out.exists() else None


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


def locate_vlm(mp4: Path, duration: float, provider, frames: int = 3) -> tuple | None:
    """Two-step VLM location (see module docstring). Raises if the provider fails, so
    the caller can fall back; returns None when there is no streamer overlay."""
    size = _size(mp4)
    if not size:
        return None
    src_w, src_h = size
    jpgs, boxes = [], []
    for k in range(frames):
        t = duration * (k + 1) / (frames + 1)
        jpg = _frame_jpg(mp4, t)
        if jpg is None:
            continue
        ans = provider.complete_json(LOCATE_PROMPT, images=[jpg]) or {}
        box = to_pixels(ans.get("bbox_2d"), 1280, round(1280 * src_h / src_w)) \
            if ans.get("streamer") else None
        jpgs.append(jpg)
        boxes.append(box)
    small = agree(boxes)
    if small is None:
        log.info("  streamer cam (vlm): none (%s)",
                 ", ".join("-" if b is None else "box" for b in boxes))
        return None
    # step 2 on the frame whose box is closest to the agreed one
    i = max((j for j, b in enumerate(boxes) if b), key=lambda j: iou(boxes[j], small))
    crop = _crop_jpg(jpgs[i], small)
    verdict = (provider.complete_json(CHECK_PROMPT, images=[crop])
               if crop else None) or {}
    if not verdict.get("streamer"):
        log.info("  streamer cam (vlm): box found but the crop is not the streamer")
        return None
    k = src_w / 1280
    x, y, w, h = (int(v * k) for v in small)
    log.info("  streamer cam (vlm): %d/%d frames agree, box=(%d,%d %dx%d)",
             sum(1 for b in boxes if b and iou(b, small) >= 0.5), len(boxes), x, y, w, h)
    return (x, y, min(w, src_w - x), min(h, src_h - y))


def is_streamer(provider, mp4: Path, duration: float, box: tuple) -> bool:
    """Step 2 alone, for a box found some other way (the Haar detector): is that crop
    the streamer? Asked on the middle frame."""
    size = _size(mp4)
    jpg = _frame_jpg(mp4, duration / 2) if size else None
    if not jpg:
        return False
    k = 1280 / size[0]
    crop = _crop_jpg(jpg, tuple(int(v * k) for v in box))
    verdict = (provider.complete_json(CHECK_PROMPT, images=[crop]) if crop else None) or {}
    return bool(verdict.get("streamer"))


def find(cfg: dict, mp4: Path, duration: float) -> tuple | None:
    """The streamer's on-screen box for Shorts/thumbnails, per `shorts.facecam_method`.

    "haar": the face detector with the live-face gates (shorts._detect_facecam).
    "vlm":  the local vision model locates the overlay (locate_vlm). When it finds none,
            the face detector's candidate gets a second chance, but only if the model
            confirms that crop is the streamer — on 2026-09-23 the model alone missed a
            2D avatar and two webcams the face detector had found. The vlm role being
            unreachable falls back to plain "haar".
    """
    from ..publishing.shorts import _detect_facecam
    sh = cfg.get("shorts", {})

    def haar():
        return _detect_facecam(mp4, duration, float(sh.get("facecam_min_live", 3.0)),
                               float(sh.get("facecam_min_skin", 0.12)))

    if sh.get("facecam_method", "haar") != "vlm":
        return haar()
    try:
        from ..providers import get_provider
        provider = get_provider(cfg, "vlm", vision=True)
        box = locate_vlm(mp4, duration, provider, int(sh.get("facecam_vlm_frames", 3)))
        if box:
            return box
        cand = haar()
        if cand and is_streamer(provider, mp4, duration, cand):
            log.info("  streamer cam: face detector's box confirmed by the vlm")
            return cand
        return None
    except Exception as e:                            # Ollama down, model missing, ...
        log.warning("  streamer cam: vlm unavailable (%s) - using the face detector", e)
        return haar()
