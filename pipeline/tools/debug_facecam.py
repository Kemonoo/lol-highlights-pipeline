"""Debug face cam detection — shows exactly what the detector sees.

Usage:
    python -m pipeline.debug_facecam              # uses latest work dir
    python -m pipeline.debug_facecam 2026-06-13   # specific date

Saves annotated frames to data/work/<date>/facecam_debug/
Each image shows:
  BLUE  rect  — the two corner search regions
  GREEN rect  — the detected face within the region
  RED   rect  — the final crop region (face + padding) passed to ffmpeg
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


def _extract_frame(mp4: Path, t: float) -> np.ndarray | None:
    tmp = Path(tempfile.mktemp(suffix=".jpg"))
    try:
        subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", str(mp4),
             "-frames:v", "1", "-q:v", "2", str(tmp)],
            capture_output=True, check=True,
        )
        return cv2.imread(str(tmp)) if tmp.exists() else None
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


TOLERANCE = 120
MIN_VOTES = 3
PANEL_W   = 480   # width of each frame panel in the composite image


def detect_and_annotate(mp4: Path, duration: float) -> tuple[np.ndarray | None, dict]:
    """9-frame detection with majority vote — mirrors shorts.py exactly.

    Returns a 3x3 composite image (9 annotated frames) + info dict.
    Blue line  = bottom-third search boundary
    Green rect = largest detected face in this frame
    Red rect   = final crop (shown on all frames if vote succeeded)
    """
    from pipeline.publishing.shorts import _faces_in_strip

    cas_frontal = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    profile_xml = cv2.data.haarcascades + "haarcascade_profileface.xml"
    cas_profile = cv2.CascadeClassifier(profile_xml)
    if cas_profile.empty():
        cas_profile = None

    all_hits: list[tuple] = []
    frame_data: list[tuple] = []

    for i in range(1, 10):
        t_frac = i / 10
        t      = min(duration * t_frac, duration - 0.5)
        frame  = _extract_frame(mp4, t)
        face   = None
        if frame is not None:
            fh, fw  = frame.shape[:2]
            gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            strip_y = int(fh * 0.667)
            strip   = gray[strip_y:, :]
            dets    = _faces_in_strip(strip, cas_frontal, cas_profile)
            if dets:
                fx, fy, rw, rh = max(dets, key=lambda f: f[2] * f[3])
                fy += strip_y
                face = (fx, fy, rw, rh, fw, fh)
                all_hits.append((fx + rw // 2, fy + rh // 2, fx, fy, rw, rh, fw, fh))
        frame_data.append((frame, t_frac, face))

    # Vote (same logic as shorts.py)
    final_crop = None
    best_votes = 0
    if len(all_hits) >= MIN_VOTES:
        best = None
        for cx1, cy1, fx, fy, rw, rh, fw, fh in all_hits:
            votes = sum(1 for cx2, cy2, *_ in all_hits
                        if abs(cx1 - cx2) < TOLERANCE and abs(cy1 - cy2) < TOLERANCE)
            if votes > best_votes:
                best_votes, best = votes, (fx, fy, rw, rh, fw, fh)
        if best_votes >= MIN_VOTES:
            fx, fy, rw, rh, fw, fh = best
            pad = max(rw, rh)
            x0  = max(0,  fx - pad)
            y0  = max(0,  fy - pad)
            x1  = min(fw, fx + rw + pad)
            y1  = min(fh, fy + rh + pad)
            final_crop = (x0, y0, x1 - x0, y1 - y0)

    result = {"found": bool(final_crop), "crop": final_crop,
              "hits": len(all_hits), "votes": best_votes}

    # ── build 3×3 composite ──────────────────────────────────────────────────
    panels = []
    for frame, t_frac, face in frame_data:
        if frame is None:
            blank = np.zeros((270, PANEL_W, 3), dtype=np.uint8)
            cv2.putText(blank, "MISSING", (10, 135),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (80, 80, 80), 2)
            panels.append(blank)
            continue

        fh, fw  = frame.shape[:2]
        scale   = PANEL_W / fw
        ann     = cv2.resize(frame, (PANEL_W, int(fh * scale)))
        ah, aw  = ann.shape[:2]
        sy      = int(ah * 0.667)

        # Blue search-zone line
        cv2.line(ann, (0, sy), (aw, sy), (255, 80, 0), 2)

        # Detected face this frame (green)
        if face is not None:
            fx2, fy2, rw2, rh2, fw2, fh2 = face
            sx = scale; sy2 = scale
            x1p, y1p = int(fx2 * sx), int(fy2 * sy2)
            x2p, y2p = int((fx2 + rw2) * sx), int((fy2 + rh2) * sy2)
            cv2.rectangle(ann, (x1p, y1p), (x2p, y2p), (0, 220, 0), 2)

        # Final crop overlay (red) if vote succeeded
        if final_crop:
            x0c, y0c, cwc, chc = final_crop
            cv2.rectangle(ann,
                          (int(x0c * scale), int(y0c * scale)),
                          (int((x0c + cwc) * scale), int((y0c + chc) * scale)),
                          (0, 0, 220), 2)

        hit_label = "HIT" if face is not None else "---"
        cv2.putText(ann, f"t={t_frac:.0%} {hit_label}", (4, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 220, 0) if face else (100, 100, 100), 1)
        panels.append(ann)

    # Arrange 3 rows × 3 cols
    ph = max(p.shape[0] for p in panels)
    rows = []
    for r in range(3):
        row_panels = []
        for c in range(3):
            p = panels[r * 3 + c]
            if p.shape[0] < ph:
                p = np.pad(p, ((0, ph - p.shape[0]), (0, 0), (0, 0)))
            row_panels.append(p)
        rows.append(np.concatenate(row_panels, axis=1))
    composite = np.concatenate(rows, axis=0)

    # Verdict bar at bottom
    bar = np.zeros((50, composite.shape[1], 3), dtype=np.uint8)
    verdict = (f"FACE FOUND  ({best_votes}/9 votes)" if final_crop
               else f"NO FACE  ({len(all_hits)} hits, {best_votes} max votes, need {MIN_VOTES})")
    color = (0, 220, 0) if final_crop else (0, 80, 220)
    cv2.putText(bar, verdict, (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    composite = np.concatenate([composite, bar], axis=0)

    return composite, result

    label = "FACE FOUND" if result["found"] else "NO FACE"
    color = (0, 200, 0) if result["found"] else (0, 0, 220)
    cv2.putText(annotated, label, (20, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 2, color, 4)

    return annotated, result


def main():
    from ..config import load_config
    cfg     = load_config()
    data    = Path(cfg["paths"]["data_abs"])
    work_root = data / "work"

    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        dates = sorted(p.name for p in work_root.iterdir() if p.is_dir())
        if not dates:
            print("No work dirs found.")
            return
        date = dates[-1]

    print(f"Date: {date}")
    work    = work_root / date
    raw_dir = data / "raw" / date
    out_dir = work / "facecam_debug"
    out_dir.mkdir(exist_ok=True)

    src = work / "vlm_filtered.json"
    if not src.exists():
        print("vlm_filtered.json not found — run at least up to api_judge first.")
        return

    clips = json.loads(src.read_text(encoding="utf-8").rstrip("\x00"))["clips"]
    clips = clips[:10]  # at most 10

    print(f"Testing {len(clips)} clips -> {out_dir}\n")

    for c in clips:
        clip_id  = c["id"]
        streamer = c.get("broadcaster_name", "?")
        duration = float(c.get("duration", 30))

        mp4 = raw_dir / f"{clip_id}.mp4"
        if not mp4.exists():
            lp = c.get("local_path") or ""
            mp4 = Path(lp) if lp else mp4
        if not mp4.exists():
            print(f"  {clip_id[:30]}: MP4 missing — skip")
            continue

        frame, info = detect_and_annotate(mp4, duration)
        if frame is None:
            print(f"  {clip_id[:30]}: frame extraction failed")
            continue

        out_path = out_dir / f"{clip_id[:40]}.jpg"
        cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

        status = f"FACE crop={info['crop']}" if info["found"] else "no face"
        print(f"  {streamer:20s}  {status}")
        print(f"    -> {out_path.name}")

    print(f"\nDone. Open {out_dir} to review.")


if __name__ == "__main__":
    main()
