"""Stage 10 — YouTube upload via Data API v3 (resumable, OAuth desktop flow).

One-time setup:
  1. console.cloud.google.com -> create a project -> enable "YouTube Data API v3"
  2. OAuth consent screen: External, add your Google account as a test user
  3. Credentials -> Create credentials -> OAuth client ID -> Desktop app
     -> download the JSON and save it as  client_secret.json  in the repo root
  4. pip install google-api-python-client google-auth-oauthlib   (in requirements.txt)
  5. set  upload.enabled: true  in pipeline/config.yaml

The first run opens a browser to authorize; the refresh token is cached at
data/yt_token.json so every later run is fully unattended.

Quota: videos.insert costs 1,600 of the default 10,000 daily units (~6 uploads/day).
Videos upload as `upload.privacy` (default private) — review, then publish manually,
or set privacy to unlisted/public once you trust the output.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("pipeline.upload")

# upload + force-ssl: comment endpoints (commentThreads) accept ONLY force-ssl —
# youtube.readonly does not cover comments. Scope changes here trigger automatic
# re-authorization (the scope check below), no manual token deletion needed.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]


def _credentials(root: Path, data: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_f = data / "yt_token.json"
    creds = None
    if token_f.exists():
        # check GRANTED scopes from the file itself — from_authorized_user_file()
        # overwrites creds.scopes with the REQUESTED ones, so it can't be trusted,
        # and refreshing an old token with new scopes fails with invalid_scope.
        try:
            granted = set(json.loads(token_f.read_text(encoding="utf-8"))
                          .get("scopes") or [])
        except Exception:
            granted = set()
        if granted and not set(SCOPES).issubset(granted):
            log.info("cached token is missing scopes %s — re-authorizing in browser",
                     sorted(set(SCOPES) - granted))
            token_f.unlink()
        else:
            creds = Credentials.from_authorized_user_file(str(token_f), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = root / "client_secret.json"
            if not secret.exists():
                raise RuntimeError(
                    "client_secret.json not found in the repo root — "
                    "see pipeline/upload.py docstring for the 5-step setup.")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
            creds = flow.run_local_server(port=0)
        token_f.write_text(creds.to_json(), encoding="utf-8")
    return creds


def run(cfg: dict, state, date_label: str) -> None:
    up = cfg["upload"]
    if not up.get("enabled", False):
        log.info("upload disabled — video left in data/output/ "
                 "(enable in config after OAuth setup, see pipeline/upload.py)")
        return

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        raise RuntimeError(
            "pip install google-api-python-client google-auth-oauthlib")

    from .config import ROOT
    data = Path(cfg["paths"]["data_abs"])
    video = data / "output" / f"{date_label}.mp4"
    meta_f = data / "output" / f"{date_label}.meta.json"
    if not video.exists():
        raise FileNotFoundError(f"{video} — run assemble first")
    if not meta_f.exists():
        raise FileNotFoundError(f"{meta_f} — run credits first")
    meta = json.loads(meta_f.read_text(encoding="utf-8").rstrip("\x00"))

    yt = build("youtube", "v3", credentials=_credentials(ROOT, data))
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"][:4900],
            "tags": meta.get("tags", []),
            "categoryId": up.get("category_id", "20"),
        },
        "status": {
            "privacyStatus": up.get("privacy", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("uploading… %d%%", int(status.progress() * 100))

    vid_id = response["id"]
    url    = f"https://youtu.be/{vid_id}"
    log.info("Uploaded %s as %s (%s)", video.name, url, body["status"]["privacyStatus"])
    state.add_video({"date": date_label, "youtube_id": vid_id, "url": url,
                     "title": meta["title"]})

    # Set thumbnail if one was generated by the thumbnail stage
    thumb = data / "work" / date_label / "thumbnail.jpg"
    if thumb.exists():
        try:
            yt.thumbnails().set(
                videoId=vid_id,
                media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg"),
            ).execute()
            log.info("thumbnail set for %s", vid_id)
        except Exception as e:
            log.warning("thumbnail upload failed (channel may need verification): %s", e)
