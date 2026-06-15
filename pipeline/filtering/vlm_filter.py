"""Stage 3 — Stepwise VLM filter, v4.

Detection (model calls, cached per clip in vlm_partial_v3.json):
  Step 1  gameplay check    3 frames, binary, majority vote
  Step 2  pro-play check    1 frame, binary
  Step 3  kill detection    kill-feed / announcement / event-log crops (kill_detect.py)

Decision (pure code, recomputed from cache every run -> tuning rules is FREE):
  REJECT  blacklisted broadcaster (label finding: JP event/custom-tournament channels)
  REJECT  no gameplay / pro-play UI
  KEEP    title keyword ("penta", "1v5", ...) — viewer-written titles are high precision
  KEEP    multikill announcement (banner or event log, any language)
  KEEP    kills confirmed (>=2 crops or event-log backup) AND audio >= kill_audio_min
  KEEP    audio >= hype_only_min (not available for Japanese-title clips)
  else REJECT

Outputs in data/work/<date>/: vlm_scored.json, vlm_filtered.json, thumbs/, crops/, report.html
"""
import base64
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import requests

from . import kill_detect

log = logging.getLogger("pipeline.vlm_filter")

PREFERRED_OLLAMA_MODELS = [
    "qwen3-vl:8b", "qwen3-vl:4b", "qwen3-vl:2b", "qwen2.5vl:7b", "qwen2.5vl:3b",
    "qwen2.5-vl:7b", "llava:13b", "llava:7b", "llava",
]

GAMEPLAY_PROMPT = """Is this screenshot actual in-game League of Legends gameplay — 3D champions on the map with the game HUD (ability bar at the bottom, minimap, health bars)?
Loading screens, champion select, lobby/queue screens, scoreboards, menus, websites, other games, or mostly-webcam/just-chatting shots do NOT count.
Answer ONLY JSON: {"gameplay": true}"""

PRO_PLAY_PROMPT = """Is this frame from a professional esports BROADCAST (spectator UI: team names and score bar across the top, caster webcams, tournament overlay, side-by-side team gold/kill totals) rather than a regular streamer playing their own game (own-perspective HUD, ability bar bottom-center)?
Answer ONLY JSON: {"esports_broadcast": false}"""


def _has_japanese(text: str) -> bool:
    """Hiragana/katakana imply Japanese (CJK ideographs alone could be Chinese)."""
    return any("぀" <= ch <= "ヿ" for ch in text)


# ── frame sampling ────────────────────────────────────────────────────────────

def sample_frames(mp4: Path, n: int) -> list[bytes]:
    frames = []
    duration = kill_detect.probe_duration(mp4)
    if duration <= 0:
        return frames
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = duration * (i + 0.5) / n
            out = Path(td) / f"f{i}.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(mp4),
                 "-frames:v", "1", "-q:v", "4", "-vf", "scale=768:-1", str(out)],
                capture_output=True,
            )
            if out.exists():
                frames.append(out.read_bytes())
    return frames


# ── providers ─────────────────────────────────────────────────────────────────

def pick_ollama_model(vf: dict) -> str:
    configured = vf["ollama_model"]
    try:
        resp = requests.get(f"{vf['ollama_url']}/api/tags", timeout=10)
        resp.raise_for_status()
        available = {m["name"] for m in resp.json().get("models", [])}
        available |= {a.split(":latest")[0] for a in available}
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach Ollama at {vf['ollama_url']} — is Ollama running? "
            f"Start the Ollama app (or `ollama serve`), then re-run. ({e})"
        )
    if configured != "auto":
        return configured
    for m in PREFERRED_OLLAMA_MODELS:
        if m in available:
            return m
    raise RuntimeError(
        f"No vision model found in Ollama. Available: {sorted(available)}. "
        f"Try: ollama pull qwen3-vl:4b"
    )


def _parse_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def make_asker(vf: dict):
    """Return ask(images, prompt) -> dict | None for the configured provider."""
    if vf["provider"] == "gemini":
        def ask(images: list[bytes], prompt: str) -> dict | None:
            key = vf["gemini_api_key"]
            if not key:
                raise RuntimeError("provider=gemini but GEMINI_API_KEY not set")
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{vf['gemini_model']}:generateContent?key={key}")
            parts = [{"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(f).decode()}}
                     for f in images]
            parts.append({"text": prompt})
            resp = requests.post(url, json={"contents": [{"parts": parts}]}, timeout=60)
            resp.raise_for_status()
            return _parse_json(
                resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        return ask

    model = pick_ollama_model(vf)
    log.info("Local VLM: %s", model)

    def ask(images: list[bytes], prompt: str) -> dict | None:
        resp = requests.post(
            f"{vf['ollama_url']}/api/generate",
            json={"model": model, "prompt": prompt,
                  "images": [base64.b64encode(f).decode() for f in images],
                  "stream": False, "options": {"temperature": 0.0}},
            timeout=600,
        )
        resp.raise_for_status()
        return _parse_json(resp.json().get("response", ""))
    return ask


# ── detection (model calls) ───────────────────────────────────────────────────

def detect(clip: dict, mp4: Path, ask, vf: dict, crops_dir: Path | None) -> None:
    """Steps 1-3: attach detection fields to the clip (no decision here)."""
    frames = sample_frames(mp4, vf.get("gameplay_frames", 3))
    if not frames:
        clip["vlm_gameplay"] = False
        clip["vlm_summary"] = "no frames extractable"
        return

    votes = []
    for f in frames:
        r = ask([f], GAMEPLAY_PROMPT)
        votes.append(bool(r.get("gameplay", False)) if r else False)
    clip["vlm_gameplay"] = sum(votes) > len(votes) / 2
    clip["gameplay_votes"] = f"{sum(votes)}of{len(votes)}"
    if not clip["vlm_gameplay"]:
        clip["vlm_summary"] = "not in-game gameplay"
        return

    r = ask([frames[len(frames) // 2]], PRO_PLAY_PROMPT)
    clip["vlm_pro_play"] = bool(r.get("esports_broadcast", False)) if r else False
    if clip["vlm_pro_play"]:
        clip["vlm_summary"] = "esports broadcast UI"
        return

    kd = kill_detect.analyze(mp4, ask, vf, crops_dir)
    clip.update(kills_max=kd["kills_max"], kill_frames=kd["kill_frames"],
                multikill=kd["multikill"], announcements=kd["announcements"],
                eventlog=kd["eventlog"], eventlog_kill=kd["eventlog_kill"])
    clip["vlm_summary"] = (
        "In-game LoL gameplay"
        + (f", kill notifications in {kd['kill_frames']} sampled frame(s)"
           if kd["kill_frames"] else ", no kills detected")
        + (f", announcement: {kd['announcements'][0]}" if kd["announcements"] else "")
        + (f", event log: {kd['eventlog'][0]}" if kd["eventlog"] else "")
    )


CACHE_KEYS = ("vlm_gameplay", "gameplay_votes", "vlm_pro_play", "kills_max",
              "kill_frames", "multikill", "announcements", "eventlog",
              "eventlog_kill", "vlm_summary")


# ── decision (pure code — recomputed from cache, tuning is free) ──────────────

def decide(clip: dict, vf: dict, blacklist: frozenset = frozenset()) -> None:
    audio = clip.get("audio_score", 0.0)
    title = clip.get("title", "")

    if clip.get("broadcaster_name", "").lower() in blacklist:
        clip.update(decision="REJECT", reason="BLACKLIST", keep_score=0.0)
        return
    if not clip.get("vlm_gameplay"):
        clip.update(decision="REJECT",
                    reason=f"NO_GAMEPLAY_{clip.get('gameplay_votes', '?')}",
                    keep_score=0.0)
        return
    if clip.get("vlm_pro_play"):
        clip.update(decision="REJECT", reason="PRO_PLAY_UI", keep_score=0.0)
        return

    kills_max = clip.get("kills_max", 0)
    kill_frames = clip.get("kill_frames", 0)
    announcements = [str(a) for a in clip.get("announcements", [])]
    eventlog = [str(m) for m in clip.get("eventlog", [])]
    multikill = bool(clip.get("multikill"))
    eventlog_kill = (bool(clip.get("eventlog_kill"))
                     or kill_detect._contains(eventlog, kill_detect.KILL_WORDS))
    kills_confirmed = (kill_frames >= 2 or multikill
                       or (kills_max >= 1 and eventlog_kill))
    clip["kills_confirmed"] = kills_confirmed

    jp = _has_japanese(title)
    clip["title_japanese"] = jp
    hype_allowed = not (jp and vf.get("japanese_needs_kills", True))
    keyword = clip.get("keyword", "")
    m = __import__("re").fullmatch(r"(\d+)\s*v\s*(\d+)", keyword.strip(), __import__("re").I)
    if m and m.group(1) == m.group(2):
        keyword = ""   # "1v1"/"2v2" = a format, not an outplay claim

    ks = round(min(1.0,
                   (0.30 * min(kills_max, 3) / 3) * (1.0 if kills_confirmed else 0.3)
                   + (0.25 if multikill else 0.0)
                   + 0.35 * audio
                   + (0.10 if (announcements or eventlog_kill) else 0.0)
                   + (0.20 if keyword else 0.0)), 3)
    clip["keep_score"] = ks

    if keyword:
        # viewer wrote "penta"/"1v5"/... in the title — high-precision signal
        clip.update(decision="KEEP", reason=f"TITLE_KEYWORD_{keyword}")
    elif multikill:
        clip.update(decision="KEEP", reason="MULTIKILL")
    elif kills_confirmed and audio >= vf.get("kill_audio_min", 0.30):
        clip.update(decision="KEEP",
                    reason=f"KILLS_CONFIRMED_{kill_frames}f_HYPE_{audio:.2f}")
    elif hype_allowed and audio >= vf.get("hype_only_min", 0.55):
        clip.update(decision="KEEP", reason=f"HYPE_ONLY_{audio:.2f}")
    else:
        why = "JP_TITLE_" if (jp and audio >= vf.get("hype_only_min", 0.55)) else ""
        clip.update(decision="REJECT",
                    reason=f"{why}UNCONFIRMED_k{kills_max}x{kill_frames}_a{audio:.2f}")


# ── report ────────────────────────────────────────────────────────────────────

def write_report(work: Path, date_label: str, clips: list[dict]) -> None:
    rows = []
    for c in sorted(clips, key=lambda c: -c.get("keep_score", 0)):
        badge = "KEEP" if c.get("decision") == "KEEP" else "REJ"
        rows.append(f"""
<tr class="{'keep' if c.get('decision') == 'KEEP' else 'rej'}">
 <td><a href="{c.get('url', '#')}" target="_blank"><img src="thumbs/{c['id']}.jpg" width="240"></a></td>
 <td><b>{badge} {c.get('reason', '')}</b><br>
     keep={c.get('keep_score', 0):.2f} audio={c.get('audio_score', 0):.2f}
     kills={c.get('kills_max', '-')}x{c.get('kill_frames', '-')}f
     multikill={'y' if c.get('multikill') else 'n'}<br>
     announcements: {', '.join(c.get('announcements', [])) or '—'}<br>
     eventlog: {'; '.join(c.get('eventlog', [])[:2]) or '—'}</td>
 <td><b>{c.get('broadcaster_name', '')}</b> · {c.get('view_count', 0):,} views<br>
     {c.get('title', '')}<br><i>{c.get('vlm_summary', '')}</i></td>
</tr>""")
    html = f"""<!doctype html><meta charset="utf-8"><title>Filter report {date_label}</title>
<style>
 body{{font-family:system-ui;background:#111;color:#eee;margin:20px}}
 table{{border-collapse:collapse;width:100%}} td{{border-bottom:1px solid #333;padding:8px;vertical-align:top}}
 tr.keep{{background:#15241a}} tr.rej{{opacity:.55}} a{{color:#7bf}} img{{border-radius:6px}}
</style>
<h2>Filter report — {date_label} ({sum(1 for c in clips if c.get('decision') == 'KEEP')}/{len(clips)} kept)</h2>
<table>{''.join(rows)}</table>"""
    (work / "report.html").write_text(html, encoding="utf-8")


# ── stage entry ───────────────────────────────────────────────────────────────

def run(cfg: dict, state, date_label: str) -> Path:
    data = Path(cfg["paths"]["data_abs"])
    work = data / "work" / date_label
    clips = json.loads((work / "prefiltered.json").read_text(encoding="utf-8"))["clips"]
    vf = cfg["vlm_filter"]
    blacklist = frozenset(b.lower() for b in cfg.get("blacklist", {}).get("broadcasters", []))

    thumbs = work / "thumbs"
    thumbs.mkdir(exist_ok=True)
    crops_dir = None
    if vf.get("save_crops", True):
        crops_dir = work / "crops"
        crops_dir.mkdir(exist_ok=True)

    partial_path = work / "vlm_partial_v3.json"
    cache = (json.loads(partial_path.read_text(encoding="utf-8"))
             if partial_path.exists() else {})

    from ..ingestion.fetch import download_clip
    raw_dir = data / "raw" / date_label
    ask = None
    scored = []
    for c in clips:
        hq = raw_dir / f"{c['id']}.mp4"
        lq = raw_dir / "lq" / f"{c['id']}.mp4"    # prefilter's small scoring copy
        lp = c.get("local_path") or ""            # NB: Path("") is "." (exists!)
        mp4 = hq if hq.exists() else (lq if lq.exists()
                                      else (Path(lp) if lp else None))
        if mp4 is None or not mp4.exists():
            continue
        thumb = thumbs / f"{c['id']}.jpg"
        if not thumb.exists():
            f1 = sample_frames(mp4, 1)
            if f1:
                thumb.write_bytes(f1[0])

        cached = cache.get(c["id"])
        if cached:
            c.update(cached)
            decide(c, vf, blacklist)   # rules re-applied fresh — tuning is free
            scored.append(c)
            log.info("%-7s %-30s %s (cached)", c["decision"], c["reason"],
                     c.get("title", "")[:46])
            continue

        # blacklist check before spending model time
        if c.get("broadcaster_name", "").lower() in blacklist:
            decide(c, vf, blacklist)
            scored.append(c)
            log.info("%-7s %-30s %s", c["decision"], c["reason"], c.get("title", "")[:46])
            continue

        # full quality for detection: kill-feed crops need the resolution, and
        # any clip that reaches this point may end up in the final video
        if not hq.exists() and download_clip(c.get("url", ""), hq):
            pass
        if hq.exists():
            mp4 = hq

        try:
            if ask is None:
                ask = make_asker(vf)
            detect(c, mp4, ask, vf, crops_dir)
        except Exception as e:
            log.warning("detection failed for %s: %s — deciding on audio only", c["id"], e)
            c["vlm_gameplay"] = True  # benefit of the doubt on gameplay
            decide(c, vf, blacklist)
            c["reason"] += "_VLM_FAILED"
            scored.append(c)          # failures NOT cached -> retried next run
            continue

        cache[c["id"]] = {k: c[k] for k in CACHE_KEYS if k in c}
        partial_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        decide(c, vf, blacklist)
        scored.append(c)
        log.info("%-7s %-30s %s", c["decision"], c["reason"], c.get("title", "")[:46])

    kept = sorted((c for c in scored if c["decision"] == "KEEP"),
                  key=lambda c: -c.get("keep_score", 0))[: vf["max_keep"]]

    (work / "vlm_scored.json").write_text(
        json.dumps({"date": date_label, "clips": scored}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    (work / "vlm_filtered.json").write_text(
        json.dumps({"date": date_label, "clips": kept}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    write_report(work, date_label, scored)

    log.info("Filter: %d -> %d kept. Review: %s", len(scored), len(kept),
             work / "report.html")
    return work
