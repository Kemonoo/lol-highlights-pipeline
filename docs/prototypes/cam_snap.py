"""Snap a rough webcam box to the overlay's real edges.

A webcam overlay is a rectangle pasted over the game. Its outline is a straight line that
sits in the SAME place in every frame, while the gameplay around it moves. So for each side,
search a band around the rough edge for the row/column whose gradient is strong along
most of the side's length, consistently over several frames, and snap to it.
"""
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


def gray_frames(mp4, times, w=1920, h=1080):
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            p = Path(td) / f"{i}.png"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i",
                            str(mp4), "-frames:v", "1", "-vf", f"scale={w}:{h}", str(p)])
            if p.exists():
                out.append(np.asarray(Image.open(p).convert("L")).astype(np.float32))
    return out


def _line_score(frames, axis, pos, lo, hi):
    """Share of the side's length (lo..hi) where a step edge sits at `pos`, min over frames.

    axis=1: vertical line at column pos (compare columns pos-1 and pos+1 along rows lo..hi).
    axis=0: horizontal line at row pos.
    """
    scores = []
    for f in frames:
        if axis == 1:
            a, b = f[lo:hi, pos - 2], f[lo:hi, pos + 1]
        else:
            a, b = f[pos - 2, lo:hi], f[pos + 1, lo:hi]
        scores.append(float((np.abs(a - b) > 18).mean()))
    # the overlay edge is there in EVERY frame; a gameplay edge is not
    return float(np.median(scores)) * 0.5 + min(scores) * 0.5


def snap(frames, box, band=0.18, min_score=0.15):
    """box=(x,y,w,h) rough -> tightened box.

    Per side: the strongest persistent line near the rough edge if it scores >= min_score.
    A dark webcam on dark gameplay gives a FAINT but real line (0.2-0.4 measured), so the
    bar is low; a side with no line at all (<0.15, measured ~0.09) is either off-screen
    (frame border nearby -> use it) or unknown (keep the rough edge)."""
    H, W = frames[0].shape
    x, y, w, h = box
    x0, y0, x1, y1 = x, y, x + w, y + h
    out = {}
    # trim the along-side span to the middle 70% so corners/rounded ends don't dilute it
    ry0, ry1 = int(y0 + 0.15 * h), int(y1 - 0.15 * h)
    rx0, rx1 = int(x0 + 0.15 * w), int(x1 - 0.15 * w)
    for side, axis, centre, span, lim in (
            ("l", 1, x0, (ry0, ry1), W), ("r", 1, x1, (ry0, ry1), W),
            ("t", 0, y0, (rx0, rx1), H), ("b", 0, y1, (rx0, rx1), H)):
        size = w if axis == 1 else h
        d = int(band * size)
        best = (0.0, centre)
        for pos in range(max(3, int(centre - d)), min(lim - 3, int(centre + d))):
            s = _line_score(frames, axis, pos, *span)
            # prefer the line closest to the model's edge when scores tie
            s -= 0.10 * abs(pos - centre) / max(d, 1)
            if s > best[0]:
                best = (s, pos)
        if best[0] >= min_score:
            out[side] = best[1]
        else:
            # no line: if the frame border is near, the overlay runs off-screen there
            edge = 0 if side in ("l", "t") else lim
            out[side] = edge if abs(edge - centre) <= 2 * d else centre
        out[side + "_score"] = round(best[0], 2)
    nx0, nx1, ny0, ny1 = out["l"], out["r"], out["t"], out["b"]
    # a line that hugs the frame border means the overlay runs to the edge: use the edge
    # (a found line within a few px of the frame border is the border itself)
    if nx0 < 8: nx0 = 0
    if ny0 < 8: ny0 = 0
    if W - nx1 < 8: nx1 = W
    if H - ny1 < 8: ny1 = H
    if nx1 - nx0 < 0.5 * w or ny1 - ny0 < 0.5 * h:      # snapping collapsed it: distrust
        return box, out
    return (nx0, ny0, nx1 - nx0, ny1 - ny0), out
