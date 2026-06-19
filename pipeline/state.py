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
            self._d = {"processed_clip_ids": [], "permissions": {}, "videos": []}

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

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._d, indent=2, ensure_ascii=False), encoding="utf-8"
        )
