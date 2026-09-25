"""clip_log: one record per published clip, idempotent per date (no network/ffmpeg)."""
import json

from pipeline.publishing.clip_log import build_entries, upsert

CHAPTERS = [
    {"clip_id": "A", "start": 2.8, "broadcaster": "Dantes", "title": "lol", "rank": 2,
     "broadcaster_url": "https://twitch.tv/Dantes"},
    {"clip_id": "B", "start": 30.0, "broadcaster": "Kdrama", "title": "PENTA", "rank": 1,
     "broadcaster_url": "https://twitch.tv/Kdrama"},
]
CLIPS = [{"id": "B", "url": "https://clips.twitch.tv/B", "title": "HUBRIS PENTA KILL",
          "broadcaster_name": "Kdrama", "api_play_quality": 9, "local_path": "C:/x.mp4"}]


def test_one_record_per_chapter_in_video_order():
    ents = build_entries("2026-09-24", CHAPTERS, CLIPS,
                         transcripts={"B": {"lang": "en", "text": "no way"}},
                         shorts_done={"B": {"youtube_id": "s1"}},
                         video={"youtube_id": "v1", "title": "T"}, episode=31)
    assert [e["clip_id"] for e in ents] == ["A", "B"]
    b = ents[1]
    assert b["countdown_rank"] == 1 and b["position"] == 2
    assert b["twitch_title"] == "HUBRIS PENTA KILL"
    assert b["filter"]["api_play_quality"] == 9
    assert "local_path" not in b["filter"]            # machine-local, meaningless elsewhere
    assert b["speech_en"] == "no way" and b["short_youtube_id"] == "s1"
    assert b["video_youtube_id"] == "v1" and b["episode"] == 31


def test_clip_missing_from_filter_data_is_still_logged():
    a = build_entries("d", CHAPTERS, CLIPS)[0]
    assert a["twitch_title"] == "lol" and a["broadcaster"] == "Dantes"
    assert a["clip_url"] == "https://clips.twitch.tv/A"


def test_upsert_replaces_the_date_and_keeps_others(tmp_path):
    p = tmp_path / "clip_log.jsonl"
    upsert(p, "2026-09-23", build_entries("2026-09-23", CHAPTERS[:1], []))
    upsert(p, "2026-09-24", build_entries("2026-09-24", CHAPTERS, CLIPS))
    upsert(p, "2026-09-24", build_entries("2026-09-24", CHAPTERS, CLIPS))  # crash-retry
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]
    assert [(r["date"], r["clip_id"]) for r in rows] == [
        ("2026-09-23", "A"), ("2026-09-24", "A"), ("2026-09-24", "B")]


def test_rebuild_keeps_short_id_after_cleanup_deleted_its_record(tmp_path):
    p = tmp_path / "clip_log.jsonl"
    upsert(p, "d", build_entries("d", CHAPTERS, CLIPS, shorts_done={"B": {"youtube_id": "s1"}}))
    upsert(p, "d", build_entries("d", CHAPTERS, CLIPS))          # shorts/done.json gone
    rows = {json.loads(ln)["clip_id"]: json.loads(ln) for ln in p.read_text().splitlines()}
    assert rows["B"]["short_youtube_id"] == "s1"
