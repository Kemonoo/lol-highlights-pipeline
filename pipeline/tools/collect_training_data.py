"""Standalone training-data collector for the prefilter classifier.

Fetches top LoL clips from Twitch for recent days, extracts audio/motion signals
using the same functions as pipeline/filtering/scoring.py, and saves
dataset_YYYY-MM-DD.json files to data/training/ for labeling.

Modes (set COLLECT_MODE at top of file):
  COLLECT_MODE = True  -- extract features for ALL clips, no filtering.
    Saves data/training/dataset_YYYY-MM-DD.json for labeling + training.
  COLLECT_MODE = False -- apply fixed thresholds to filter clips.
    Saves data/training/clips_YYYY-MM-DD.json (passing clips only).

Workflow:
  1. python -m pipeline.tools.collect_training_data   (COLLECT_MODE=True)
  2. python -m pipeline.tools.review_clips             (label Accept/Reject)
  3. python -m pipeline.tools.train_classifier         (print threshold suggestions)

Requirements:
    yt-dlp, librosa, opencv-python must be installed (already in requirements.txt).
    TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be in .env / environment.
    ffmpeg must be on PATH.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yt_dlp

from .scoring import (
    compute_audio_score,
    compute_motion_score,
    has_positive_keyword,
    is_tournament,
)


# ── Config ────────────────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

GAME_ID   = "21779"   # League of Legends
FETCH_N   = 200       # clips per day
DAYS_BACK = 8         # fetch 8 days; label days 1-5, test on 6-8
LOCAL_TZ  = ZoneInfo("Europe/Amsterdam")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "training"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Mode ──────────────────────────────────────────────────────────────────────
COLLECT_MODE = True   # True = collect for training; False = filter for review

# Audio scoring thresholds (mirrored from config.yaml — used only in filter mode)
AUDIO_EXCLUDE  = 0.22
AUDIO_PASS     = 0.52
MOTION_EXCLUDE = 0.015

# ── Optional Ollama descriptions ──────────────────────────────────────────────
OLLAMA_MODEL          = "qwen2.5:7b"
OLLAMA_URL            = "http://localhost:11434/api/generate"
GENERATE_DESCRIPTIONS = True


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET env vars.")
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── Date windows ──────────────────────────────────────────────────────────────

def get_day_windows(days_back: int) -> list[tuple[datetime, datetime, str]]:
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    windows = []
    for i in range(1, days_back + 1):
        day_start = today - timedelta(days=i)
        day_end   = day_start + timedelta(days=1)
        label     = day_start.strftime("%Y-%m-%d")
        windows.append((day_start.astimezone(timezone.utc),
                        day_end.astimezone(timezone.utc),
                        label))
    return windows


# ── Fetch ─────────────────────────────────────────────────────────────────────

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


# ── Downloads ─────────────────────────────────────────────────────────────────

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
        print(f"    audio download failed: {e}")
        return ""


def download_video(clip_url: str, clip_id: str) -> str:
    path = os.path.join(tempfile.gettempdir(), f"twclip_v_{clip_id}.mp4")
    ydl_opts = {"format": "worst[ext=mp4]/worst", "outtmpl": path,
                "quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clip_url])
        return path if os.path.exists(path) else ""
    except Exception as e:
        print(f"    video download failed: {e}")
        return ""


def _rm(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# ── Optional Ollama description ───────────────────────────────────────────────

def generate_clip_description(clip: dict) -> str:
    a = clip.get("audio_score", 0.0)
    m = clip.get("motion_score", 0.0)
    audio_label  = "excited" if a > 0.5 else "calm" if a < 0.20 else "moderate"
    motion_label = "very dynamic" if m > 0.05 else "mostly static" if m < 0.02 else "some movement"
    kw_line   = f"Keyword in title: \"{clip['keyword']}\".\n" if clip.get("keyword_match") else ""
    tour_line = "Note: title suggests tournament broadcast.\n" if clip.get("is_tournament") else ""
    prompt = (
        f"Twitch clip (League of Legends):\n"
        f"Title: \"{clip['title']}\"\n"
        f"Streamer: {clip.get('broadcaster_name', '?')}  |  "
        f"Views: {clip.get('view_count', 0):,}\n"
        f"{kw_line}{tour_line}"
        f"Audio: {audio_label} ({a:.2f}), Motion: {motion_label} ({m:.3f})\n\n"
        f"In one sentence, describe what probably happens in this clip and whether "
        f"it is likely a highlight or boring. Be direct and brief."
    )
    try:
        resp = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL,
                                               "prompt": prompt, "stream": False},
                             timeout=20)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception:
        return ""


# ── Save ──────────────────────────────────────────────────────────────────────

def _score_bar(score: float, width: int = 20) -> str:
    n = int(score * width)
    return "#" * n + "." * (width - n)


def save_dataset(clips: list[dict], date_label: str) -> None:
    path = DATA_DIR / f"dataset_{date_label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date_label, "mode": "collect",
                   "fetched_at": datetime.now(LOCAL_TZ).isoformat(),
                   "clips": clips},
                  f, indent=2, ensure_ascii=False)
    print(f"\n  Saved {len(clips)} clips -> {path}")
    print("  Run review_clips to label, then train_classifier to retrain.")


def save_clips(clips: list[dict], date_label: str) -> None:
    path = DATA_DIR / f"clips_{date_label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date_label,
                   "fetched_at": datetime.now(LOCAL_TZ).isoformat(),
                   "clips": clips},
                  f, indent=2, ensure_ascii=False)
    print(f"\n  Saved {len(clips)} clips -> {path}")


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_collect(clips: list[dict], date_label: str) -> None:
    print(f"  COLLECT MODE — {len(clips)} clips, no filtering.\n")
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
        if kw_match:              extras += f"  KW:{kw}"
        if clip["is_tournament"]: extras += "  TOURNAMENT"
        print(f"             audio=[{_score_bar(a)}] {a:.2f}  "
              f"motion=[{_score_bar(m)}] {m:.3f}{extras}")

        if GENERATE_DESCRIPTIONS:
            desc = generate_clip_description(clip)
            clip["description"] = desc
            if desc:
                print(f"             {desc[:100]}")
        print()

    save_dataset(clips, date_label)


def run_filter(clips: list[dict], date_label: str) -> None:
    passed, excluded = [], []

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
          f"{len(excluded)} tournament-excluded, {len(to_score)} to score.\n")

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
            print(f"             EXCL [{bar}] {score:.2f}\n")
            excluded.append(clip)
        elif score >= AUDIO_PASS:
            clip["filter_status"] = f"AUDIO_PASS ({score:.2f})"
            print(f"             PASS [{bar}] {score:.2f}\n")
            passed.append(clip)
        else:
            print(f"             CHCK [{bar}] {score:.2f} -> motion check\n")
            motion_needed.append(clip)

    for i, clip in enumerate(motion_needed, 1):
        print(f"  [{i:02d}/{len(motion_needed)}] {clip['title'][:65]}")
        mp4    = download_video(clip["url"], clip["id"])
        motion = compute_motion_score(mp4)
        _rm(mp4)
        clip["motion_score"] = round(motion, 4)
        if motion < MOTION_EXCLUDE:
            clip["filter_status"] = f"MOTION_EXCLUDE ({motion:.3f})"
            print(f"             EXCL motion={motion:.3f}\n")
            excluded.append(clip)
        else:
            clip["filter_status"] = f"MOTION_PASS ({motion:.3f})"
            print(f"             PASS motion={motion:.3f}\n")
            passed.append(clip)

    print(f"  DONE: {len(passed)} passed, {len(excluded)} excluded.")
    save_clips(passed, date_label)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode = "COLLECT" if COLLECT_MODE else "FILTER"
    print(f"Authenticating with Twitch...  (mode: {mode})")
    token = get_access_token()

    for started_at, ended_at, date_label in get_day_windows(DAYS_BACK):
        out_path = DATA_DIR / (
            f"dataset_{date_label}.json" if COLLECT_MODE else f"clips_{date_label}.json"
        )
        if out_path.exists():
            print(f"  {date_label} -- already processed, skipping.")
            continue
        print(f"\n{'='*60}")
        print(f"  {date_label}  [{mode} MODE]")
        print(f"{'='*60}\n")
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
