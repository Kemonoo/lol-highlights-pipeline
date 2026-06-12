"""
train.py
--------
Reads all labeled dataset_*.json files produced by collect mode + review_clips.py,
trains a logistic regression, prints which features actually predict your picks,
and saves the model to model.json.

Run after you have at least ~50 labeled clips (ideally 3+ days of data).

Requirements:
    pip install scikit-learn

Usage:
    python train.py
"""

import json
import numpy as np
from pathlib import Path

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.metrics import classification_report
except ImportError:
    print("scikit-learn not installed.  pip install scikit-learn")
    raise


FEATURE_NAMES = [
    "audio_score",
    "motion_score",
    "keyword_match",
    "is_tournament",
    "log_view_count",
]


# ─── Load data ────────────────────────────────────────────────────────────────

def load_labeled_clips() -> list[dict]:
    """Load all clips that have been labeled via review_clips.py."""
    data_dir = Path(__file__).parent / "data"
    files = sorted(data_dir.glob("dataset_*.json")) if data_dir.exists() else []
    if not files:
        # Fall back to review_state.json
        for candidate in [data_dir / "review_state.json", Path("review_state.json")]:
            if candidate.exists():
                files = [candidate]
                break
    if not files:
        raise FileNotFoundError(
            "No dataset_*.json files found.\n"
            "Run twitch_top_clips.py in COLLECT_MODE=True, then review_clips.py to label clips."
        )
    clips = []
    for f in files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        for c in data.get("clips", []):
            if c.get("label") in ("accept", "reject"):
                clips.append(c)
    return clips


def build_features(clips: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for c in clips:
        X.append([
            float(c.get("audio_score",  0.3)),
            float(c.get("motion_score", 0.02)),
            1.0 if c.get("keyword_match") else 0.0,
            1.0 if c.get("is_tournament") else 0.0,
            float(np.log1p(c.get("view_count", 0)) / 15.0),
        ])
        y.append(1 if c["label"] == "accept" else 0)
    return np.array(X, dtype=float), np.array(y, dtype=int)


# ─── Train ────────────────────────────────────────────────────────────────────

def train(X: np.ndarray, y: np.ndarray) -> tuple:
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    model = LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced")
    model.fit(Xs, y)
    return model, scaler


def find_best_threshold(model, scaler, X: np.ndarray, y: np.ndarray) -> float:
    probs = model.predict_proba(scaler.transform(X))[:, 1]
    best_t, best_f1 = 0.5, 0.0
    for t in np.arange(0.15, 0.85, 0.05):
        preds = (probs >= t).astype(int)
        tp = np.sum((preds == 1) & (y == 1))
        fp = np.sum((preds == 1) & (y == 0))
        fn = np.sum((preds == 0) & (y == 1))
        if (tp + fp) > 0 and (tp + fn) > 0:
            p  = tp / (tp + fp)
            r  = tp / (tp + fn)
            f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
            if f1 > best_f1:
                best_f1, best_t = f1, t
    return float(best_t), float(best_f1)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    clips = load_labeled_clips()
    n_accept = sum(1 for c in clips if c["label"] == "accept")
    n_reject = sum(1 for c in clips if c["label"] == "reject")
    print(f"Loaded {len(clips)} labeled clips  ({n_accept} accepted, {n_reject} rejected)\n")

    if len(clips) < 20:
        print("⚠  Very few labeled clips — results will be unreliable.")
        print("   Aim for at least 50 labeled clips (3+ days of collect + review).\n")

    X, y = build_features(clips)
    model, scaler = train(X, y)

    # ── Cross-validation ──────────────────────────────────────────────────────
    if len(clips) >= 10:
        cv  = StratifiedKFold(n_splits=min(5, n_accept, n_reject), shuffle=True, random_state=42)
        cvs = cross_val_score(
            LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced"),
            scaler.transform(X), y, cv=cv, scoring="f1"
        )
        print(f"Cross-validation F1: {cvs.mean():.2f} ± {cvs.std():.2f}\n")

    # ── Feature importance ────────────────────────────────────────────────────
    print("Feature importance (how much each feature predicts acceptance):\n")
    coefs = list(zip(FEATURE_NAMES, model.coef_[0]))
    for name, c in sorted(coefs, key=lambda x: abs(x[1]), reverse=True):
        bar = ("▓" if c > 0 else "░") * min(int(abs(c) * 8), 30)
        sign = "+" if c > 0 else "-"
        print(f"  {sign} {name:<20s}  {c:+.3f}  {bar}")

    print()
    threshold, f1 = find_best_threshold(model, scaler, X, y)
    print(f"Suggested decision threshold: {threshold:.2f}  (F1={f1:.2f})")

    # ── Training set report ───────────────────────────────────────────────────
    preds = (model.predict_proba(scaler.transform(X))[:, 1] >= threshold).astype(int)
    print("\nTraining set performance:\n")
    print(classification_report(y, preds, target_names=["reject", "accept"]))

    # ── Show misses ───────────────────────────────────────────────────────────
    print("Clips you accepted that the model would have MISSED (false negatives):")
    for c, xi, pred in zip(clips, X, preds):
        if c["label"] == "accept" and pred == 0:
            print(f"  audio={xi[0]:.2f}  motion={xi[1]:.3f}  kw={int(xi[2])}  "
                  f"{c.get('title','')[:60]}")

    print("\nClips you rejected that the model would have PASSED (false positives):")
    for c, xi, pred in zip(clips, X, preds):
        if c["label"] == "reject" and pred == 1:
            print(f"  audio={xi[0]:.2f}  motion={xi[1]:.3f}  kw={int(xi[2])}  "
                  f"{c.get('title','')[:60]}")

    # ── Save model ────────────────────────────────────────────────────────────
    model_data = {
        "feature_names":      FEATURE_NAMES,
        "coefficients":       model.coef_[0].tolist(),
        "intercept":          float(model.intercept_[0]),
        "scaler_mean":        scaler.mean_.tolist(),
        "scaler_scale":       scaler.scale_.tolist(),
        "threshold":          threshold,
        "n_training_samples": len(clips),
        "training_f1":        round(f1, 3),
    }
    out = Path("model.json")
    with open(out, "w") as f:
        json.dump(model_data, f, indent=2)
    print(f"\n✅ Model saved → {out}")
    print("   Set COLLECT_MODE = False in twitch_top_clips.py to use it.")


if __name__ == "__main__":
    main()
