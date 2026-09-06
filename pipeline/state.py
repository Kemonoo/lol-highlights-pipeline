"""Tiny JSON state store: processed clips, streamer permissions, produced videos."""
import json
from datetime import datetime, timezone
from pathlib import Path


class State:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "state.json"
        if self.path.exists():
            self._d = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self._d = {"processed_clip_ids": [], "permissions": {}, "videos": [], "episodes": {}}

    # ── clips ──────────────────────────────────────────────────────────
    def is_processed(self, clip_id: str) -> bool:
        return clip_id in self._d["processed_clip_ids"]

    def mark_processed(self, clip_ids: list[str]) -> None:
        seen = set(self._d["processed_clip_ids"])
        self._d["processed_clip_ids"].extend(c for c in clip_ids if c not in seen)
        self.save()

    # ── permissions (Phase 3) ──────────────────────────────────────────
    def permission_status(self, broadcaster_login: str) -> str:
        """'approved' | 'denied' | 'pending' | 'unknown'"""
        return self._d["permissions"].get(broadcaster_login.lower(), {}).get("status", "unknown")

    def set_permission(self, broadcaster_login: str, status: str, note: str = "") -> None:
        self._d["permissions"][broadcaster_login.lower()] = {
            "status": status,
            "note": note,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        self.save()

    # ── episode numbering ─────────────────────────────────────────────
    def episode_number(self, date_label: str) -> int:
        """Sequential episode number for date_label, starting at 1 (assigned once,
        stable across reruns/retries of the same date)."""
        episodes = self._d.setdefault("episodes", {})
        if date_label in episodes:
            return episodes[date_label]
        n = (max(episodes.values()) if episodes else 0) + 1
        episodes[date_label] = n
        self.save()
        return n

    # ── videos ─────────────────────────────────────────────────────────
    def add_video(self, info: dict) -> None:
        self._d["videos"].append(info)
        self.save()

    def uploaded_id(self, date_label: str) -> str | None:
        """YouTube id already uploaded for this date, if any (upload idempotency)."""
        for v in self._d.get("videos", []):
            if v.get("date") == date_label and v.get("youtube_id"):
                return v["youtube_id"]
        return None

    def recent_titles(self, n: int = 3) -> list:
        """Titles of the last n uploads, newest first — so a new title can avoid
        opening with the same word as the one before it."""
        out = []
        for v in reversed(self._d.get("videos", [])):
            t = v.get("title")
            if t and t not in out:
                out.append(t)
            if len(out) >= n:
                break
        return out

    # ── API request budget ─────────────────────────────────────────────
    @staticmethod
    def quota_day() -> str:
        """Today's date on the clock the provider's daily quota resets on (Pacific).

        Not local midnight: Google's free-tier per-day quota rolls over at midnight
        America/Los_Angeles, and a 03:00 Europe/Amsterdam run is still in the PREVIOUS
        Pacific day — so it spends an allowance that anything run the afternoon before
        has already drawn from. Counting against local dates would silently double the
        budget on exactly the runs that matter.
        """
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

    def api_spend(self, bucket: str) -> int:
        """Requests already charged to `bucket` (e.g. a role name) this quota day."""
        return int(self._d.get("api_spend", {}).get(self.quota_day(), {}).get(bucket, 0))

    def record_api_spend(self, bucket: str, n: int = 1) -> None:
        spend = self._d.setdefault("api_spend", {})
        day = spend.setdefault(self.quota_day(), {})
        day[bucket] = int(day.get(bucket, 0)) + n
        for old in [k for k in spend if k < self.quota_day()]:   # keep the file small
            del spend[old]
        self.save()

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._d, indent=2, ensure_ascii=False), encoding="utf-8"
        )
