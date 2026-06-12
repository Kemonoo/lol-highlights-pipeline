"""Stage 1 — Fetch Twitch clips and download MP4s.

Two modes (config twitch.mode):
  top_clips    — top clips for the game in the time window (current default)
  broadcasters — clips per broadcaster login (the Challenger-list approach)

Outputs:
  data/raw/<date>/clips.json   metadata for every fetched clip
  data/raw/<date>/<clip_id>.mp4
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yt_dlp

log = logging.getLogger("pipeline.fetch")

HELIX = "https://api.twitch.tv/helix"


def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _headers(token: str, client_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Client-Id": client_id}


def _day_window(date_label: str, tz_name: str, window_hours: int) -> tuple[str, str]:
    tz = ZoneInfo(tz_name)
    start = datetime.strptime(date_label, "%Y-%m-%d").replace(tzinfo=tz)
    end = start + timedelta(hours=window_hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start.astimezone(timezone.utc).strftime(fmt),
            end.astimezone(timezone.utc).strftime(fmt))


def _get_user_ids(logins: list[str], token: str, client_id: str) -> dict[str, str]:
    ids = {}
    for i in range(0, len(logins), 100):
        resp = requests.get(
            f"{HELIX}/users",
            headers=_headers(token, client_id),
            params=[("login", l) for l in logins[i:i + 100]],
            timeout=15,
        )
        resp.raise_for_status()
        for u in resp.json().get("data", []):
            ids[u["login"]] = u["id"]
    return ids


def _fetch_page_loop(params: dict, token: str, client_id: str, limit: int) -> list[dict]:
    clips, cursor = [], None
    while len(clips) < limit:
        p = dict(params, first=min(100, limit - len(clips)))
        if cursor:
            p["after"] = cursor
        resp = requests.get(f"{HELIX}/clips", headers=_headers(token, client_id),
                            params=p, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("data", [])
        clips.extend(page)
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor or not page:
            break
    return clips


def fetch_metadata(cfg: dict, date_label: str, token: str) -> list[dict]:
    tw = cfg["twitch"]
    import os
    client_id = os.environ["TWITCH_CLIENT_ID"]
    started_at, ended_at = _day_window(date_label, tw["timezone"], tw["window_hours"])
    base = {"started_at": started_at, "ended_at": ended_at}

    if tw["mode"] == "broadcasters" and tw["broadcasters"]:
        ids = _get_user_ids(tw["broadcasters"], token, client_id)
        log.info("Resolved %d/%d broadcaster logins", len(ids), len(tw["broadcasters"]))
        clips = []
        per_streamer = max(5, tw["fetch_count"] // max(len(ids), 1))
        for login, bid in ids.items():
            clips.extend(_fetch_page_loop(dict(base, broadcaster_id=bid),
                                          token, client_id, per_streamer))
    else:
        clips = _fetch_page_loop(dict(base, game_id=tw["game_id"]),
                                 token, client_id, tw["fetch_count"])

    # de-dup, sort by views
    seen, out = set(), []
    for c in sorted(clips, key=lambda c: -c.get("view_count", 0)):
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


def download_clip(url: str, dest: Path, quality: str = "best") -> bool:
    """quality='worst' grabs a small low-res copy (scoring/judging);
    'best' is full quality (final video)."""
    if not url:
        return False
    fmt = "best[ext=mp4]/best" if quality == "best" else "worst[ext=mp4]/worst"
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": fmt,
        "outtmpl": str(dest),
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return dest.exists()
    except Exception as e:
        log.warning("download failed for %s: %s", url, e)
        return False


def run(cfg: dict, state, date_label: str) -> Path:
    """Fetch metadata + download new clips. Returns the day folder."""
    import os
    token = get_access_token(os.environ["TWITCH_CLIENT_ID"],
                             os.environ["TWITCH_CLIENT_SECRET"])

    day_dir = Path(cfg["paths"]["data_abs"]) / "raw" / date_label
    day_dir.mkdir(parents=True, exist_ok=True)

    clips = fetch_metadata(cfg, date_label, token)
    clips = [c for c in clips if not state.is_processed(c["id"])]

    if cfg["permissions"]["require_permission"]:
        clips = [c for c in clips
                 if state.permission_status(c.get("broadcaster_name", "")) == "approved"]

    log.info("Fetched %d new clips for %s (metadata only — downloads are on-demand: "
             "low-quality for scoring, full quality for filter survivors)",
             len(clips), date_label)

    for c in clips:
        dest = day_dir / f"{c['id']}.mp4"
        c["local_path"] = str(dest) if dest.exists() else ""

    (day_dir / "clips.json").write_text(
        json.dumps({"date": date_label, "clips": clips}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return day_dir
