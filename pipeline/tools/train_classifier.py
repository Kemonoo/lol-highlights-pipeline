"""Train a logistic regression classifier on labeled LoL Twitch clips.

Reads labeled dataset_*.json files produced by collect_training_data.py +
review_clips.py, prints feature importance, and shows threshold suggestions
that can be set in pipeline/config.yaml (prefilter.audio_exclude, etc.).

Training data lives in data/training/ (gitignored).

Usage:
    python -m pipeline.tools.train_classifier
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

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "training"


# ── Load data ─────────────────────────────────────────────────────────────────

def load_labeled_clips() -> list[dict]:
    files = sorted(DATA_DIR.glob("dataset_*.json")) if DATA_DIR.exists() else []
    if not files:
        for candidate in [DATA_DIR / "review_state.json", Path("review_state.json")]:
            if candidate.exists():
                files = [candidate]
                break
    if not files:
        raise FileNotFoundError(
            f"No dataset_*.json files found in {DATA_DIR}.\n"
            "Run collect_training_data.py first, then review_clips.py to label them."
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


# ── Train ─────────────────────────────────────────────────────────────────────

def train(X: np.ndarray, y: np.ndarray):
    scaler = StandardScaler()
    Xs     = scaler.fit_transform(X)
    model  = LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced")
    model.fit(Xs, y)
    return model, scaler


def find_best_threshold(model, scaler, X: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    probs  = model.predict_proba(scaler.transform(X))[:, 1]
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    clips    = load_labeled_clips()
    n_accept = sum(1 for c in clips if c["label"] == "accept")
    n_reject = sum(1 for c in clips if c["label"] == "reject")
    print(f"Loaded {len(clips)} labeled clips  ({n_accept} accepted, {n_reject} rejected)\n")

    if len(clips) < 20:
        print("Very few labeled clips — results will be unreliable.")
        print("Aim for at least 50 labeled clips (3+ days of collect + review).\n")

    X, y = build_features(clips)
    model, scaler = train(X, y)

    if len(clips) >= 10:
        cv  = StratifiedKFold(n_splits=min(5, n_accept, n_reject), shuffle=True, random_state=42)
        cvs = cross_val_score(
            LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced"),
            scaler.transform(X), y, cv=cv, scoring="f1"
        )
        print(f"Cross-validation F1: {cvs.mean():.2f} +/- {cvs.std():.2f}\n")

    print("Feature importance:\n")
    for name, c in sorted(zip(FEATURE_NAMES, model.coef_[0]), key=lambda x: abs(x[1]), reverse=True):
        bar  = ("#" if c > 0 else ".") * min(int(abs(c) * 8), 30)
        print(f"  {'+'if c>0 else'-'} {name:<20s}  {c:+.3f}  {bar}")

    threshold, f1 = find_best_threshold(model, scaler, X, y)
    print(f"\nSuggested threshold: {threshold:.2f}  (F1={f1:.2f})")
    print("  -> Set prefilter.audio_exclude / audio_pass in pipeline/config.yaml")

    preds = (model.predict_proba(scaler.transform(X))[:, 1] >= threshold).astype(int)
    print("\nTraining set performance:\n")
    print(classification_report(y, preds, target_names=["reject", "accept"]))

    out = DATA_DIR / "model.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "feature_names":      FEATURE_NAMES,
        "coefficients":       model.coef_[0].tolist(),
        "intercept":          float(model.intercept_[0]),
        "scaler_mean":        scaler.mean_.tolist(),
        "scaler_scale":       scaler.scale_.tolist(),
        "threshold":          threshold,
        "n_training_samples": len(clips),
        "training_f1":        round(f1, 3),
    }, indent=2), encoding="utf-8")
    print(f"\nModel saved -> {out}")


if __name__ == "__main__":
    main()
