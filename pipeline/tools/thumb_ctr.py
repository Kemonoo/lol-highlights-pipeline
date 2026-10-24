"""Click-through rate per thumbnail style (owner, 2026-10-24) - run by hand.

    python -m pipeline.tools.thumb_ctr <studio_export.csv>

Thumbnail impressions and click-through rate are YouTube Studio only (the Analytics API
has neither). Export them from Studio -> Analytics -> Advanced mode -> Content (videos),
metrics "Thumbnail impressions" + "Thumbnail click-through rate", download as CSV - or let
Claude read the same table in Studio. Each upload's style is in
work/<date>/thumbnail_style.json (layout pip/bigface, palette, reacting face), and
state.json maps the date to its YouTube id. Grouped, impression-weighted: a style needs a
few thousand impressions before its CTR means anything.
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path


def _col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        low = h.lower()
        if all(n in low for n in needles):
            return i
    return None


def read_studio(path: Path) -> dict:
    """{video_id: (impressions, ctr %)} from a Studio Advanced-mode CSV export. Pure-ish."""
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    head = rows[0]
    vid = _col(head, "content") if _col(head, "content") is not None else 0
    imp = _col(head, "impressions")
    ctr = _col(head, "click-through")
    out = {}
    for r in rows[1:]:
        try:
            out[r[vid]] = (float(r[imp].replace(",", "")), float(r[ctr].replace(",", ".")))
        except (ValueError, IndexError, TypeError):
            continue                                   # the "Total" row and blanks
    return out


def group(styles: dict, studio: dict) -> dict:
    """{(layout, palette): [impressions, clicks, videos]} - CTR weighted by impressions."""
    g = defaultdict(lambda: [0.0, 0.0, 0])
    for vid, st in styles.items():
        if vid not in studio:
            continue
        imp, ctr = studio[vid]
        for key in ((st.get("layout"), "*"), ("*", st.get("palette")),
                    ("reacting" if st.get("reacting") else "not reacting/unknown", "*")):
            g[key][0] += imp
            g[key][1] += imp * ctr / 100
            g[key][2] += 1
    return g


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    from ..config import load_config
    cfg = load_config()
    data = Path(cfg["paths"]["data_abs"])
    state = json.loads((data / "state.json").read_text(encoding="utf-8"))
    ids = {v["date"]: v["youtube_id"] for v in state.get("videos", []) if v.get("youtube_id")}
    styles = {}
    for f in (data / "work").glob("*/thumbnail_style.json"):
        d = f.parent.name
        if d in ids:
            styles[ids[d]] = json.loads(f.read_text(encoding="utf-8"))
    for (a, b), (imp, clicks, n) in sorted(group(styles, read_studio(Path(sys.argv[1]))).items(),
                                           key=lambda kv: -kv[1][0]):
        label = a if b == "*" else b
        print(f"{label:24} {n:3d} videos  {imp:8.0f} impressions  CTR {100 * clicks / max(imp, 1):.2f}%")


if __name__ == "__main__":
    main()
