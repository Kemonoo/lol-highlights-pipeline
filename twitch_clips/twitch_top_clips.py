"""
twitch_top_clips.py
--------------------
Fetches top League of Legends Twitch clips and scores them by audio/motion
signal for manual labelling and classifier training.

Modes:
  COLLECT_MODE = True  → extract features for ALL clips, no filtering.
    Saves to data/dataset_YYYY-MM-DD.json for labelling + model training.
  COLLECT_MODE = False → use trained thresholds to filter clips.
    Saves only passing clips to data/clips_YYYY-MM-DD.json.

Pipeline (filter mode):
  1. Keyword match on title  → instant PASS  (specific LoL highlight terms)
  2. Tournament keyword       → instant EXCLUDE
  3. Audio download + score  → EXCLUDE if < AUDIO_EXCLUDE, PASS if > AUDIO_PASS
  4. Video download + motion → EXCLUDE if < MOTION_EXCLUDE, else PASS

Requirements:
    pip install requests tzdata yt-dlp librosa opencv-python
    ffmpeg must be in PATH
"""

import os
import json
import re
import tempfile
from pathlib import Path
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yt_dlp

try:
    import librosa
    import numpy as np
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False
    print("WARNING: librosa not installed — audio scoring disabled.  pip install librosa")

try:
    import cv2
    if not LIBROSA_AVAILABLE:
        import numpy as np
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    print("WARNING: opencv-python not installed — motion scoring disabled.  pip install opencv-python")


# ─── Config ───────────────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

GAME_ID   = "21779"   # League of Legends
FETCH_N   = 200       # clips per day
DAYS_BACK = 8         # fetch 8 days; label days 1-5, test on 6-8
LOCAL_TZ  = ZoneInfo("Europe/Amsterdam")

# Data folder is always next to this script, regardless of working directory.
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Mode ──────────────────────────────────────────────────────────────────────
COLLECT_MODE = True   # True = collect for training; False = filter for review

# Audio scoring thresholds (0–1).
# Calm talking clips typically score 0.05–0.20.
# Excitement / screaming clips typically score 0.45–0.90.
AUDIO_EXCLUDE = 0.22   # below → excluded (silent or calm consistent voice)
AUDIO_PASS    = 0.52   # above → passed directly (clearly excited audio)

# Motion scoring threshold (0–1 normalised frame diff).
MOTION_EXCLUDE = 0.015  # below → excluded (static image / loading screen)

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_MODEL          = "qwen2.5:7b"
OLLAMA_URL            = "http://localhost:11434/api/generate"
GENERATE_DESCRIPTIONS = True   # write a 1-sentence description per clip


# ─── Keywords ─────────────────────────────────────────────────────────────────

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

NVN_RE = re.compile(r'\b\d\s*v\s*\d+\b', re.IGNORECASE)   # 1v5, 2v9, etc.

TOURNAMENT_KEYWORDS = [
    " lcs ", " lck ", " lec ", " lpl ",
    "worlds 20", "spring split", "summer split",
    "grand final", "semifinals", "playoffs",
]


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


# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError(
            "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET env vars."
        )
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type":    "client_credentials",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ─── Date windows ─────────────────────────────────────────────────────────────

def get_day_windows(days_back: int) -> list[tuple[datetime, datetime, str]]:
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    windows = []
    for i in range(1, days_back + 1):
        day_start = today - timedelta(days=i)
        day_end   = day_start + timedelta(days=1)
        label     = day_start.strftime("%Y-%m-%d")
        windows.append((
            day_start.astimezone(timezone.utc),
            day_end.astimezone(timezone.utc),
            label,
        ))
    return windows


# ─── Fetch clips ──────────────────────────────────────────────────────────────

def fetch_clips(access_token: str, started_at: datetime, ended_at: datetime) -> list[dict]:
    clips, cursor = [], None
    while len(clips) < FETCH_N:
        params = {
            "game_id":    GAME_ID,
            "first":      min(100, FETCH_N - len(clips)),
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ended_at":   ended_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if cursor:
            params["after"] = cursor
        resp = requests.get(
            "https://api.twitch.tv/helix/clips",
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": CLIENT_ID},
            params=params,
        )
        resp.raise_for_status()
        data   = resp.json()
        page   = data.get("data", [])
        clips.extend(page)
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor or not page:
            break
    return clips


# ─── Audio download ───────────────────────────────────────────────────────────

def download_audio(clip_url: str, clip_id: str) -> str:
    base = os.path.join(tempfile.gettempdir(), f"twclip_a_{clip_id}")
    mp3  = base + ".mp3"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": base + ".%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3", "preferredquality": "96"}],
        "quiet": True, "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clip_url])
        return mp3 if os.path.exists(mp3) else ""
    except Exception as e:
        print(f"    ⚠  audio download failed: {e}")
        return ""


# ─── Audio scoring ────────────────────────────────────────────────────────────

def compute_audio_score(mp3_path: str) -> float:
    """
    Returns 0–1 excitement score. Four features designed to distinguish
    calm/consistent commentary (score ~0.05–0.20) from screaming excitement
    or dense combat audio (score ~0.45–0.90).

    Returns 0.30 on any failure → routes to motion check, not auto-pass.
    """
    _FALLBACK = 0.30

    if not LIBROSA_AVAILABLE:
        return _FALLBACK
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
        # Only counts sudden sound events during already-loud moments
        D          = np.abs(librosa.stft(y, hop_length=hop))
        onset_env  = librosa.onset.onset_strength(S=librosa.power_to_db(D ** 2), sr=sr)
        onset_norm = onset_env / (onset_env.max() + 1e-6)
        rms_at_onset = np.interp(
            np.arange(len(onset_env)) * hop / sr,
            np.arange(len(rms))      * hop / sr,
            rms,
        )
        loud_mask       = rms_at_onset > (mean_rms * 1.6)
        loud_spike_rate = np.sum((onset_norm > 0.45) & loud_mask) / (len(y) / sr)
        f4 = float(np.clip(loud_spike_rate / 5.0, 0.0, 1.0))

        score = 0.30 * f1 + 0.28 * f2 + 0.22 * f3 + 0.20 * f4
        return float(np.clip(score, 0.0, 1.0))

    except Exception as e:
        print(f"    ⚠  audio scoring failed: {e}")
        return _FALLBACK


# ─── Video download ───────────────────────────────────────────────────────────

def download_video(clip_url: str, clip_id: str) -> str:
    path = os.path.join(tempfile.gettempdir(), f"twclip_v_{clip_id}.mp4")
    ydl_opts = {
        "format": "worst[ext=mp4]/worst",
        "outtmpl": path,
        "quiet": True, "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clip_url])
        return path if os.path.exists(path) else ""
    except Exception as e:
        print(f"    ⚠  video download failed: {e}")
        return ""


# ─── Motion scoring ───────────────────────────────────────────────────────────

def compute_motion_score(video_path: str, n_samples: int = 8) -> float:
    if not OPENCV_AVAILABLE:
        return 0.5
    try:
        cap   = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or total < 2:
            cap.release(); return 0.5

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
        diffs = [np.mean(np.abs(frames[i] - frames[i-1])) / 255.0
                 for i in range(1, len(frames))]
        return float(np.mean(diffs))
    except Exception as e:
        print(f"    ⚠  motion scoring failed: {e}")
        return 0.5


# ─── Cleanup ──────────────────────────────────────────────────────────────────

def _rm(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try: os.remove(p)
            except OSError: pass


# ─── LLM description ──────────────────────────────────────────────────────────

def generate_clip_description(clip: dict) -> str:
    a = clip.get("audio_score", 0.0)
    m = clip.get("motion_score", 0.0)
    audio_label  = "excited/screaming" if a > 0.5 else "calm/quiet" if a < 0.20 else "moderate"
    motion_label = "very dynamic" if m > 0.05 else "mostly static" if m < 0.02 else "some movement"
    kw_line   = f"Keyword in title: \"{clip['keyword']}\".\n" if clip.get("keyword_match") else ""
    tour_line = "Note: title suggests a tournament broadcast.\n" if clip.get("is_tournament") else ""

    prompt = (
        f"Twitch clip (League of Legends):\n"
        f"Title: \"{clip['title']}\"\n"
        f"Streamer: {clip.get('broadcaster_name', '?')}  |  "
        f"Views: {clip.get('view_count', 0):,}\n"
        f"{kw_line}"
        f"{tour_line}"
        f"Audio signal: {audio_label} (score {a:.2f})\n"
        f"Motion signal: {motion_label} (score {m:.3f})\n\n"
        f"In one sentence, describe what probably happens in this clip and whether "
        f"it is likely a highlight (outplay, kill streak, exciting reaction) or a "
        f"boring clip (talking, spectating, loading screen). Be direct and brief."
    )
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception:
        return ""


# ─── Save ─────────────────────────────────────────────────────────────────────

def save_clips(clips: list[dict], date_label: str) -> None:
    path = DATA_DIR / f"clips_{date_label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date_label,
                   "fetched_at": datetime.now(LOCAL_TZ).isoformat(),
                   "clips": clips},
                  f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Saved {len(clips)} clips → {path}")


def save_dataset(clips: list[dict], date_label: str) -> None:
    path = DATA_DIR / f"dataset_{date_label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date_label,
                   "mode": "collect",
                   "fetched_at": datetime.now(LOCAL_TZ).isoformat(),
                   "clips": clips},
                  f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Dataset saved ({len(clips)} clips) → {path}")
    print(f"  Run review_clips.py to label them, then train.py to train the model.")


# ─── Score bar helper ─────────────────────────────────────────────────────────

def _score_bar(score: float, width: int = 20) -> str:
    n = int(score * width)
    return "█" * n + "░" * (width - n)


# ─── Collect mode ─────────────────────────────────────────────────────────────

def run_collect(clips: list[dict], date_label: str) -> None:
    """Extract features for ALL clips without filtering, for training data."""
    print(f"  COLLECT MODE — {len(clips)} clips, no filtering.\n"
          f"  Label with review_clips.py afterwards.\n")

    for i, clip in enumerate(clips, 1):
        title = clip["title"]
        print(f"  [{i:03d}/{len(clips)}] {title[:65]}")
        print(f"             {clip['url']}")

        kw_match, kw = has_positive_keyword(title)
        clip["keyword_match"] = kw_match
        clip["keyword"]       = kw if kw_match else ""
        clip["is_tournament"] = is_tournament(title)
        clip["transcript"]    = ""
        clip["label"]         = None
        clip["category"]      = None

        mp3 = download_audio(clip["url"], clip["id"])
        a   = compute_audio_score(mp3)
        _rm(mp3)
        clip["audio_score"] = round(a, 3)

        mp4 = download_video(clip["url"], clip["id"])
        m   = compute_motion_score(mp4)
        _rm(mp4)
        clip["motion_score"] = round(m, 4)

        extras = ""
        if kw_match:  extras += f"  KW:{kw}"
        if clip["is_tournament"]: extras += "  TOURNAMENT"
        print(f"             audio=[{_score_bar(a)}] {a:.2f}  "
              f"motion=[{_score_bar(m)}] {m:.3f}{extras}")

        if GENERATE_DESCRIPTIONS:
            desc = generate_clip_description(clip)
            clip["description"] = desc
            if desc:
                print(f"             💬 {desc[:100]}")
        print()

    save_dataset(clips, date_label)


# ─── Filter mode ──────────────────────────────────────────────────────────────

def run_filter(clips: list[dict], date_label: str) -> None:
    """Filter clips using fixed thresholds. Saves passing clips for review."""
    passed, excluded = [], []

    # Stage 1: keyword / tournament
    to_score = []
    for clip in clips:
        clip["transcript"] = ""
        kw_match, kw = has_positive_keyword(clip["title"])
        if kw_match:
            clip["filter_status"] = f"KEYWORD_PASS ({kw})"
            passed.append(clip)
        elif is_tournament(clip["title"]):
            clip["filter_status"] = "TOURNAMENT_EXCLUDE"
            excluded.append(clip)
        else:
            to_score.append(clip)

    print(f"  Stage 1: {len(passed)} keyword-passed, "
          f"{len(excluded)} tournament-excluded, "
          f"{len(to_score)} to score.\n")

    # Stage 2: audio
    if not LIBROSA_AVAILABLE:
        print("  ⚠  librosa missing — routing all to motion check.  pip install librosa\n")
        motion_needed = list(to_score)
    else:
        motion_needed = []
        for i, clip in enumerate(to_score, 1):
            print(f"  [{i:03d}/{len(to_score)}] {clip['title'][:65]}")
            mp3   = download_audio(clip["url"], clip["id"])
            score = compute_audio_score(mp3)
            _rm(mp3)
            clip["audio_score"] = round(score, 3)
            bar = _score_bar(score)
            if score < AUDIO_EXCLUDE:
                clip["filter_status"] = f"AUDIO_EXCLUDE ({score:.2f})"
                print(f"             ❌ [{bar}] {score:.2f}\n")
                excluded.append(clip)
            elif score >= AUDIO_PASS:
                clip["filter_status"] = f"AUDIO_PASS ({score:.2f})"
                print(f"             ✅ [{bar}] {score:.2f}\n")
                passed.append(clip)
            else:
                print(f"             🎬 [{bar}] {score:.2f} → motion check\n")
                motion_needed.append(clip)
        print(f"  Stage 2 done. {len(motion_needed)} clips need motion check.\n")

    # Stage 3: motion
    if not OPENCV_AVAILABLE:
        print("  ⚠  opencv missing — passing borderline clips.\n")
        for clip in motion_needed:
            clip["filter_status"] = "NO_OPENCV_PASS"
        passed.extend(motion_needed)
    else:
        for i, clip in enumerate(motion_needed, 1):
            print(f"  [{i:02d}/{len(motion_needed)}] {clip['title'][:65]}")
            mp4    = download_video(clip["url"], clip["id"])
            motion = compute_motion_score(mp4)
            _rm(mp4)
            clip["motion_score"] = round(motion, 4)
            if motion < MOTION_EXCLUDE:
                clip["filter_status"] = f"MOTION_EXCLUDE ({motion:.3f})"
                print(f"             ❌ motion={motion:.3f}\n")
                excluded.append(clip)
            else:
                clip["filter_status"] = f"MOTION_PASS ({motion:.3f})"
                print(f"             ✅ motion={motion:.3f}\n")
                passed.append(clip)

    print(f"\n  {'─'*56}")
    print(f"  DONE: {len(passed)} passed, {len(excluded)} excluded.")
    print(f"  {'─'*56}\n")
    for i, c in enumerate(passed, 1):
        print(f"  #{i:03d} [{c.get('filter_status','?'):30s}]  "
              f"{c['view_count']:>7,} views  {c['title'][:50]}")
    save_clips(passed, date_label)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    mode = "COLLECT" if COLLECT_MODE else "FILTER"
    print(f"Authenticating with Twitch...  (mode: {mode})")
    token = get_access_token()

    for started_at, ended_at, date_label in get_day_windows(DAYS_BACK):
        # Skip days that already have a dataset file
        out_path = DATA_DIR / (
            f"dataset_{date_label}.json" if COLLECT_MODE else f"clips_{date_label}.json"
        )
        if out_path.exists():
            print(f"  ⏭  {date_label} — already processed, skipping.")
            continue

        print(f"\n{'═'*60}")
        print(f"  {date_label}  [{mode} MODE]")
        print(f"{'═'*60}\n")

        print(f"  Fetching up to {FETCH_N} clips...")
        clips = fetch_clips(token, started_at, ended_at)
        print(f"  Got {len(clips)} clips.\n")

        if not clips:
            save_dataset([], date_label) if COLLECT_MODE else save_clips([], date_label)
            continue

        if COLLECT_MODE:
            run_collect(clips, date_label)
        else:
            run_filter(clips, date_label)

    print("\nAll days processed.")


if __name__ == "__main__":
    main()
