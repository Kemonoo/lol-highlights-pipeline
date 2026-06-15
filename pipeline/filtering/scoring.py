"""Audio/motion scoring and keyword filters for the prefilter stage.

Scoring functions were originally trained on ~200+ labeled LoL Twitch clips
using logistic regression. The thresholds (AUDIO_EXCLUDE etc.) live in
pipeline/config.yaml under the `prefilter` section so they can be tuned
without touching code. These functions only implement the signal extraction.

To retrain or re-evaluate thresholds:
    python -m pipeline.tools.train_classifier
    python -m pipeline.tools.eval_filter
To collect new training data:
    python -m pipeline.tools.collect_training_data
"""
import re

import numpy as np


# ── Keyword lists ─────────────────────────────────────────────────────────────

POSITIVE_KEYWORDS = [
    "pentakill", "penta",
    "quadrakill", "quadra",
    "triple kill", "triplekill",
    "outplay", "outplayed",
    "solobolo", "solo kill",
    "smite steal", "baron steal", "dragon steal", "objective steal",
    "perfect smite",
    "backdoor",
    "clutch",
    "one shot", "oneshot",
]

NVN_RE = re.compile(r"\b\d\s*v\s*\d+\b", re.IGNORECASE)   # 1v5, 2v9, etc.

TOURNAMENT_KEYWORDS = [
    " lcs ", " lck ", " lec ", " lpl ",
    "worlds 20", "spring split", "summer split",
    "grand final", "semifinals", "playoffs",
]


# ── Keyword scoring ───────────────────────────────────────────────────────────

def has_positive_keyword(title: str) -> tuple[bool, str]:
    if m := NVN_RE.search(title):
        return True, m.group()
    t = title.lower()
    for kw in POSITIVE_KEYWORDS:
        if kw in t:
            return True, kw
    return False, ""


def is_tournament(title: str) -> bool:
    t = " " + title.lower() + " "
    return any(kw in t for kw in TOURNAMENT_KEYWORDS)


# ── Audio excitement scoring ──────────────────────────────────────────────────

def compute_audio_score(mp3_path: str) -> float:
    """Return 0–1 excitement score from an MP3 file.

    Four signal features distinguish calm commentary (0.05–0.20) from
    screaming/combat audio (0.45–0.90). Returns 0.30 on failure so the
    clip gets routed to the motion check rather than auto-passing or
    auto-failing.
    """
    _FALLBACK = 0.30
    try:
        import librosa
    except ImportError:
        return _FALLBACK

    import os
    if not mp3_path or not os.path.exists(mp3_path):
        return _FALLBACK
    try:
        y, sr = librosa.load(mp3_path, sr=16000, mono=True)
        if len(y) < sr:
            return 0.15
        if np.max(np.abs(y)) < 0.002:
            return 0.05

        hop      = 512
        rms      = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
        mean_rms = np.mean(rms)
        if mean_rms < 0.003:
            return 0.08

        # Feature 1: Peak-to-median energy ratio
        # Calm voice: 1.5–2.5  |  Screaming: 4.0–9.0
        peak_median = np.percentile(rms, 92) / (np.percentile(rms, 50) + 1e-6)
        f1 = float(np.clip((peak_median - 1.5) / 5.0, 0.0, 1.0))

        # Feature 2: Energy variability (coefficient of variation)
        # Calm: CV 0.3–0.6  |  Excited: CV 0.9–2.0
        cv = np.std(rms) / (mean_rms + 1e-6)
        f2 = float(np.clip((cv - 0.4) / 1.3, 0.0, 1.0))

        # Feature 3: Fraction of frames in high-energy state
        # Calm: ~0.03–0.08  |  Excited: ~0.12–0.35
        f3 = float(np.clip((np.mean(rms > mean_rms * 2.5) - 0.03) / 0.22, 0.0, 1.0))

        # Feature 4: Loud onset spike density
        D         = np.abs(librosa.stft(y, hop_length=hop))
        onset_env = librosa.onset.onset_strength(S=librosa.power_to_db(D ** 2), sr=sr)
        onset_norm = onset_env / (onset_env.max() + 1e-6)
        rms_at_onset = np.interp(
            np.arange(len(onset_env)) * hop / sr,
            np.arange(len(rms))      * hop / sr,
            rms,
        )
        loud_mask      = rms_at_onset > (mean_rms * 1.6)
        loud_spike_rate = np.sum((onset_norm > 0.45) & loud_mask) / (len(y) / sr)
        f4 = float(np.clip(loud_spike_rate / 5.0, 0.0, 1.0))

        score = 0.30 * f1 + 0.28 * f2 + 0.22 * f3 + 0.20 * f4
        return float(np.clip(score, 0.0, 1.0))
    except Exception:
        return _FALLBACK


# ── Motion scoring ────────────────────────────────────────────────────────────

def compute_motion_score(video_path: str, n_samples: int = 8) -> float:
    """Return 0–1 motion score from sampled frames of an MP4.

    Low score (~0.005–0.015) → loading screen / static image.
    High score (>0.05) → dynamic gameplay.
    Returns 0.5 on failure (neutral — do not auto-exclude).
    """
    try:
        import cv2
    except ImportError:
        return 0.5
    try:
        cap   = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or total < 2:
            cap.release()
            return 0.5

        frames = []
        for idx in [int(i * total / n_samples) for i in range(n_samples)]:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                frames.append(
                    cv2.cvtColor(cv2.resize(frame, (160, 90)),
                                 cv2.COLOR_BGR2GRAY).astype(np.float32)
                )
        cap.release()
        if len(frames) < 2:
            return 0.5
        diffs = [np.mean(np.abs(frames[i] - frames[i - 1])) / 255.0
                 for i in range(1, len(frames))]
        return float(np.mean(diffs))
    except Exception:
        return 0.5
