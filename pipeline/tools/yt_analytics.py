"""YouTube Analytics for our own uploads: impressions, CTR, retention, traffic sources.

The upload token (data/yt_token.json) is deliberately NOT reused: adding a scope to it
would make the unattended 03:00 upload stop and ask for browser consent. This tool keeps
its own read-only token (data/yt_analytics_token.json), created once interactively.

One-time setup (owner):
  1. Google Cloud console, the project that owns client_secret.json:
     APIs & Services -> Library -> "YouTube Analytics API" -> Enable.
  2. python -m pipeline.tools.yt_analytics      (opens a browser once for consent)

Usage:
  python -m pipeline.tools.yt_analytics [--days 60]      per-video table, newest first
  python -m pipeline.tools.yt_analytics --video <id>     traffic sources + daily views

Impressions / CTR (videoThumbnailImpressions[ClickRate]) exist in the API since
2026-01; Studio shows the same numbers with a ~2 day lag.
"""
import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("pipeline.yt_analytics")

SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly",
          "https://www.googleapis.com/auth/youtube.readonly"]
ROOT = Path(__file__).resolve().parents[2]
TOKEN = ROOT / "data" / "yt_analytics_token.json"


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / "client_secret.json"), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _services():
    from googleapiclient.discovery import build
    c = _creds()
    return (build("youtubeAnalytics", "v2", credentials=c, cache_discovery=False),
            build("youtube", "v3", credentials=c, cache_discovery=False))


def _rows(ya, **q) -> list[dict]:
    r = ya.reports().query(ids="channel==MINE", **q).execute()
    cols = [h["name"] for h in r.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in r.get("rows", [])]


def per_video(days: int) -> list[dict]:
    """One dict per video published in the window: title, views, impressions, CTR,
    average % viewed, subscribers gained."""
    ya, yt = _services()
    start, end = (date.today() - timedelta(days=days)).isoformat(), date.today().isoformat()
    base = dict(startDate=start, endDate=end, dimensions="video", sort="-views", maxResults=200)
    by = {r["video"]: r for r in _rows(ya, metrics="views,averageViewPercentage,"
                                                   "averageViewDuration,subscribersGained", **base)}
    try:                                 # reach metrics can't always share a query
        for r in _rows(ya, metrics="videoThumbnailImpressions,"
                                   "videoThumbnailImpressionsClickRate", **base):
            by.setdefault(r["video"], {"video": r["video"]}).update(r)
    except Exception as e:
        log.warning("impressions/CTR unavailable: %s", e)
    ids = list(by)
    for k in range(0, len(ids), 50):
        for v in yt.videos().list(part="snippet", id=",".join(ids[k:k + 50])).execute()["items"]:
            by[v["id"]].update(title=v["snippet"]["title"],
                               published=v["snippet"]["publishedAt"][:10])
    return sorted(by.values(), key=lambda r: r.get("published", ""), reverse=True)


def one_video(video_id: str, days: int) -> dict:
    ya, _ = _services()
    start, end = (date.today() - timedelta(days=days)).isoformat(), date.today().isoformat()
    f = f"video=={video_id}"
    return {
        "traffic": _rows(ya, startDate=start, endDate=end, filters=f, metrics="views",
                         dimensions="insightTrafficSourceType", sort="-views"),
        "daily": _rows(ya, startDate=start, endDate=end, filters=f,
                       metrics="views,videoThumbnailImpressions,"
                               "videoThumbnailImpressionsClickRate", dimensions="day"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--video")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.video:
        out = one_video(a.video, a.days)
        if a.json:
            print(json.dumps(out, indent=2))
            return
        for r in out["traffic"]:
            print(f"{r['insightTrafficSourceType']:<22} {r['views']:>7}")
        print()
        for r in out["daily"]:
            if r.get("views") or r.get("videoThumbnailImpressions"):
                print(r["day"], r.get("views"), r.get("videoThumbnailImpressions"),
                      f"{r.get('videoThumbnailImpressionsClickRate', 0):.2%}")
        return
    rows = per_video(a.days)
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    print(f"{'published':<11}{'views':>7}{'impr':>8}{'CTR':>7}{'%view':>7}  title")
    for r in rows:
        ctr = r.get("videoThumbnailImpressionsClickRate")
        print(f"{r.get('published', '?'):<11}{r.get('views', 0):>7}"
              f"{r.get('videoThumbnailImpressions', '-'):>8}"
              f"{(f'{ctr:.1%}' if ctr is not None else '-'):>7}"
              f"{r.get('averageViewPercentage', 0):>6.0f}%  {r.get('title', r['video'])[:60]}")


if __name__ == "__main__":
    main()
