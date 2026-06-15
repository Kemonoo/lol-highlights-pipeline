"""Eval harness: filter decisions vs. your labels.

    python -m pipeline.eval_filter [--date YYYY-MM-DD] [--ok-is-good]

Reads data/work/<date>/labels.json (from label_clips.py) and vlm_scored.json, prints:
  - confusion matrix (KEEP/REJECT x good/ok/bad)
  - precision / recall of KEEP
  - every disagreement with the filter's reason, so we know what to fix next
"""
import argparse
import json
from pathlib import Path

from ..config import load_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=False)
    ap.add_argument("--ok-is-good", action="store_true",
                    help="count 'ok' labels as keep-worthy (default: ok = bad)")
    args = ap.parse_args()

    cfg = load_config()
    data = Path(cfg["paths"]["data_abs"])
    if args.date:
        date = args.date
    else:
        dates = sorted(p.name for p in (data / "work").iterdir() if p.is_dir())
        date = dates[-1]
    work = data / "work" / date

    labels = json.loads((work / "labels.json").read_text(encoding="utf-8"))
    scored = {c["id"]: c for c in
              json.loads((work / "vlm_scored.json").read_text(encoding="utf-8"))["clips"]}

    good_set = {"good", "ok"} if args.ok_is_good else {"good"}
    rows, missing = [], 0
    for cid, lab in labels.items():
        sc = scored.get(cid)
        if sc is None:
            missing += 1
            continue
        rows.append({
            "id": cid, "label": lab["verdict"], "tags": lab.get("tags", []),
            "note": lab.get("note", ""), "title": lab.get("title", ""),
            "decision": sc.get("decision"), "reason": sc.get("reason"),
            "keep_score": sc.get("keep_score"),
        })

    if not rows:
        raise SystemExit("No overlapping labeled+scored clips yet.")

    tp = sum(1 for r in rows if r["decision"] == "KEEP" and r["label"] in good_set)
    fp = sum(1 for r in rows if r["decision"] == "KEEP" and r["label"] not in good_set)
    fn = sum(1 for r in rows if r["decision"] != "KEEP" and r["label"] in good_set)
    tn = sum(1 for r in rows if r["decision"] != "KEEP" and r["label"] not in good_set)

    print(f"\n=== Eval {date} — {len(rows)} labeled clips "
          f"(good set = {sorted(good_set)}; {missing} labels without scores) ===")
    print(f"            label-good  label-bad")
    print(f"  KEEP      {tp:9d}  {fp:9d}")
    print(f"  REJECT    {fn:9d}  {tn:9d}")
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    print(f"\n  precision {prec:.2f}   recall {rec:.2f}\n")

    fps = [r for r in rows if r["decision"] == "KEEP" and r["label"] not in good_set]
    fns = [r for r in rows if r["decision"] != "KEEP" and r["label"] in good_set]
    if fps:
        print("-- FALSE KEEPS (boring clips that got in) --")
        for r in fps:
            print(f"  [{r['reason']}] {r['title'][:50]}  "
                  f"tags={','.join(r['tags'])} note={r['note'][:40]}")
    if fns:
        print("\n-- MISSED GOOD CLIPS --")
        for r in fns:
            print(f"  [{r['reason']}] {r['title'][:50]}  "
                  f"tags={','.join(r['tags'])} note={r['note'][:40]}")
    print()


if __name__ == "__main__":
    main()
