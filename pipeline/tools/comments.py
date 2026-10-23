"""List the channel's comments and post a reply - by hand, never from the nightly run.

    python -m pipeline.tools.comments --list [--all]        # unanswered (or all) threads
    python -m pipeline.tools.comments --reply <thread_id> "text"

Owner rule (2026-10-23): replies are drafted for the owner and posted only after he
approves each one. Uses the upload OAuth token (data/yt_token.json, scope
youtube.force-ssl), so no new consent is needed.
"""
import argparse
import sys
from pathlib import Path


def _yt():
    from googleapiclient.discovery import build
    from ..config import ROOT, load_config
    from ..publishing.upload import _credentials
    cfg = load_config()
    return build("youtube", "v3", credentials=_credentials(ROOT, Path(cfg["paths"]["data_abs"])))


def threads(yt, limit: int = 50) -> list[dict]:
    me = yt.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    r = yt.commentThreads().list(part="snippet,replies", allThreadsRelatedToChannelId=me,
                                 maxResults=min(limit, 100), order="time").execute()
    out = []
    for it in r.get("items", []):
        top = it["snippet"]["topLevelComment"]["snippet"]
        replies = (it.get("replies") or {}).get("comments", [])
        answered = any(c["snippet"].get("authorChannelId", {}).get("value") == me
                       for c in replies)
        out.append({"id": it["id"], "video": it["snippet"]["videoId"],
                    "date": top["publishedAt"][:10], "author": top.get("authorDisplayName"),
                    "text": top["textOriginal"], "answered": answered})
    return out


def reply(yt, thread_id: str, text: str) -> str:
    r = yt.comments().insert(part="snippet", body={
        "snippet": {"parentId": thread_id, "textOriginal": text}}).execute()
    return r["id"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true", help="include answered threads")
    ap.add_argument("--reply", nargs=2, metavar=("THREAD_ID", "TEXT"))
    a = ap.parse_args()
    yt = _yt()
    if a.reply:
        print("posted", reply(yt, *a.reply))
        return
    for t in threads(yt):
        if a.all or not t["answered"]:
            print(f'{t["id"]}  {t["date"]}  youtu.be/{t["video"]}  {t["author"]}: '
                  f'{t["text"][:200]!r}')


if __name__ == "__main__":
    sys.exit(main())
