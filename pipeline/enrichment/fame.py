"""How well known a streamer is: Twitch follower counts (owner, 2026-10-23).

Naming policy for titles and hooks. A famous name sells the click on its own ("Thebausffs
...", like Ludwig's "Tyler1 challenged me"). An unknown one means nothing to a viewer, so
the hook uses a word anyone understands ("This Streamer", "A Diamond Jungler") and the
name rides along for search - the channel's organic traffic is name searches
(DEVLOG 2026-10-21), and featuring smaller streamers is the niche: one of them thanked
the channel in the comments for the Short.

`followers()` reads Helix /channels/followers (app token, `total` only), cached in
data/cache/twitch_followers.json for `video.fame.cache_days`. Any failure = {} (nobody is
famous -> descriptors everywhere, names still in descriptions/tags), never an error.
"""
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("pipeline.fame")

CACHE = "twitch_followers.json"


def tier(n: int | None, famous_at: int) -> str:
    """'famous' | 'known' | 'small' | 'unknown' (no data). Pure."""
    if n is None:
        return "unknown"
    if n >= famous_at:
        return "famous"
    return "known" if n >= famous_at // 25 else "small"


def is_famous(login: str, fame: dict, cfg: dict) -> bool:
    """Famous by followers, or listed in `video.fame.always` (owner pick, e.g. Baus)."""
    fc = (cfg.get("video", {}) or {}).get("fame", {}) or {}
    always = {x.lower() for x in fc.get("always", []) or []}
    always |= {x.lower() for x in (cfg.get("prefilter", {}).get("boost_streamers") or {})}
    if (login or "").lower() in always:
        return True
    return tier(fame.get((login or "").lower()), int(fc.get("famous_followers", 250_000))) \
        == "famous"


def followers(cfg: dict, clips: list[dict]) -> dict:
    """{login lower: follower count} for the clips' streamers (cached)."""
    fc = (cfg.get("video", {}) or {}).get("fame", {}) or {}
    if not fc.get("enabled", False):
        return {}
    path = Path(cfg["paths"]["data_abs"]) / "cache" / CACHE
    try:
        cache = json.loads(path.read_text(encoding="utf-8").rstrip("\x00"))
    except (OSError, ValueError):
        cache = {}
    stale = (date.today() - timedelta(days=int(fc.get("cache_days", 30)))).isoformat()
    want = {}
    for c in clips:
        login = (c.get("broadcaster_name") or "").lower()
        bid = c.get("broadcaster_id")
        if login and bid and (login not in cache or cache[login][1] < stale):
            want[login] = bid
    if want:
        try:
            import requests
            from ..ingestion.fetch import HELIX, _headers, get_access_token
            cid = os.environ["TWITCH_CLIENT_ID"]
            tok = get_access_token(cid, os.environ["TWITCH_CLIENT_SECRET"])
            for login, bid in want.items():
                r = requests.get(f"{HELIX}/channels/followers",
                                 params={"broadcaster_id": bid},
                                 headers=_headers(tok, cid), timeout=20)
                if r.ok and "total" in r.json():
                    cache[login] = [int(r.json()["total"]), date.today().isoformat()]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache), encoding="utf-8")
        except Exception as e:
            log.warning("follower counts unavailable (%s) - nobody counts as famous", e)
    return {k: v[0] for k, v in cache.items()}


def describe(login: str, fame: dict, cfg: dict) -> str:
    """'Thebausffs (1.6M followers, FAMOUS)' for prompts. Pure."""
    n = fame.get((login or "").lower())
    size = "" if n is None else (f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n // 1000}k"
                                  if n >= 1000 else str(n))
    tag = "FAMOUS" if is_famous(login, fame, cfg) else "not famous"
    return f"{login} ({size + ' followers, ' if size else ''}{tag})"
