"""Daily review queue: spot-check the filter's decisions a few clips at a time.

    python -m pipeline.tools.review_queue [--days 14] [--port 8766]
    python -m pipeline.tools.review_queue --stats

Why this exists: the filter makes ~200 decisions a night and nobody watches the rejects,
so "the filtering seems fine" was a guess. This samples a handful of clips from EVERY
gate (too quiet, too still, not gameplay, pro broadcast, dropped by the judge, ...) plus a
few at random, shows why the pipeline decided what it did, and records two answers:

  * was the pipeline's stated reason TRUE?  (Y / N)  -> is the DETECTOR wrong?
  * how good is the clip actually?          (G / O / B) -> is the RULE wrong?

"Reason true, clip good" is the interesting case: the detector did its job and the rule
threw away a highlight anyway (the silent-pentakill pattern). "Reason false" means the
model or the measurement misread the clip.

Clips play from the local MP4 while it still exists (raw files are pruned after a day)
and from the Twitch embed after that, so any day with a clips.json can be reviewed —
the queue is built from the work files the pipeline already writes, nothing new has to
run at night. Reviews append to data/reviews/reviews.jsonl with a snapshot of every fact
the pipeline had about the clip, so they stay useful after work/ files change or vanish.
The newest line per clip wins. Everything here except serve() is pure and tested.
"""
import argparse
import json
import random
import re
import time
import webbrowser
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import load_config
from ..filtering.scoring import is_tournament

# gate -> (where it sits in the funnel, plain-language label). Order = funnel order.
GATES = {
    "blacklist":       ("Before download", "Streamer is on the blacklist"),
    "language":        ("Before download", "Channel language not accepted"),
    "tournament":      ("Before download", "Title looks like a tournament"),
    "quiet":           ("Sound and motion", "Too quiet"),
    "static":          ("Sound and motion", "Too still"),
    "rank_cap":        ("Sound and motion", "Passed, but ranked below the cut"),
    "vlm_no_gameplay": ("Vision model", "Not League gameplay"),
    "vlm_pro":         ("Vision model", "Looks like a pro broadcast"),
    "vlm_weak":        ("Vision model", "No confirmed kill, not loud enough"),
    "vlm_cap":         ("Vision model", "Kept, but ranked below the cut"),
    "judge_drop":      ("Gemini judge", "Gemini scored it too low"),
    "trimmed":         ("Selection", "Good enough, cut to fit the video"),
    "selected":        ("Selection", "In the video"),
}

TAGS = ["kill / outplay", "multikill", "funny", "hype reaction", "talk only",
        "nothing happens", "not LoL gameplay", "pro broadcast", "caption widget",
        "great for a Short"]


def _read(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8").rstrip("\x00"))
    except (ValueError, OSError):
        return None


def _clips(obj) -> list:
    if obj is None:
        return []
    return obj["clips"] if isinstance(obj, dict) and "clips" in obj else obj


def _pct(x) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "?"


def plain_vlm(reason: str) -> str:
    """vlm_filter.decide() reason code -> words."""
    r = reason or ""
    m = re.search(r"(\d)of(\d)", r)
    if r.startswith("NO_GAMEPLAY"):
        return f"not gameplay ({m.group(1)} of {m.group(2)} frames said gameplay)" if m else "not gameplay"
    if r.startswith("PRO_PLAY"):
        return "looked like a pro broadcast"
    if r.startswith("TITLE_KEYWORD_"):
        return f"title says “{r.split('_', 2)[2]}”"
    if r.startswith("MULTIKILL"):
        return "multikill banner seen"
    if r.startswith("KILLS_CONFIRMED"):
        return "kills seen on screen" + (f", audio {r.rsplit('_', 1)[1]}" if "HYPE" in r else "")
    if r.startswith("HYPE_ONLY"):
        return f"loud reaction (audio {r.rsplit('_', 1)[1]}), no kill seen"
    if r.startswith("UNCONFIRMED"):
        return "a kill maybe, not confirmed, and not loud enough"
    if r == "BLACKLIST":
        return "streamer is blacklisted"
    return r.lower().replace("_", " ")


def load_day(data: Path, date: str) -> dict:
    """Everything the pipeline wrote about one day, keyed the way trace() wants it."""
    work, raw = data / "work" / date, data / "raw" / date
    return {
        "raw": _clips(_read(raw / "clips.json")),
        "prefilter": _read(work / "prefilter_partial.json") or {},
        "prefiltered": [c["id"] for c in _clips(_read(work / "prefiltered.json"))],
        "vlm": {c["id"]: c for c in _clips(_read(work / "vlm_scored.json"))},
        "judged": {c["id"]: c for c in _clips(_read(work / "api_scored.json"))},
        "final": {c["id"]: c for c in _clips(_read(work / "vlm_filtered.json"))},
    }


def trace(day: dict, cfg: dict, date: str) -> list[dict]:
    """One record per fetched clip: the gate that stopped it (or 'selected'), the claim
    that gate made about the clip, and the path of checks it went through."""
    pf = cfg.get("prefilter", {})
    include = {x.lower() for x in pf.get("include_languages", [])}
    exclude = {x.lower() for x in pf.get("exclude_languages", [])}
    blacklist = {b.lower() for b in cfg.get("blacklist", {}).get("broadcasters", [])}
    audio_min = pf.get("audio_exclude", 0.22)
    motion_min = pf.get("motion_exclude", 0.015)
    cap_pf, cap_vlm = len(day["prefiltered"]), None
    judged_ids = set(day["judged"])
    if day["vlm"]:
        cap_vlm = len(judged_ids) or None

    # prefilter rank among the clips that passed sound+motion, same sort as prefilter.py
    passed = [c for c in day["raw"]
              if str(day["prefilter"].get(c["id"], {}).get("prefilter_status", "")).endswith("PASS")]
    passed.sort(key=lambda c: (
        day["prefilter"][c["id"]]["prefilter_status"] != "KEYWORD_PASS",
        -day["prefilter"][c["id"]].get("audio_score", 0), -c.get("view_count", 0)))
    pf_rank = {c["id"]: i + 1 for i, c in enumerate(passed)}
    vlm_keeps = sorted((v for v in day["vlm"].values() if v.get("decision") == "KEEP"),
                       key=lambda v: -v.get("keep_score", 0))
    vlm_rank = {v["id"]: i + 1 for i, v in enumerate(vlm_keeps)}

    out = []
    for c in day["raw"]:
        cid = c["id"]
        lang = (c.get("language") or "").lower()
        p = day["prefilter"].get(cid, {})
        v = day["vlm"].get(cid)
        j = day["judged"].get(cid)
        path, gate, claim = [], None, None

        def step(name, ok, detail=""):
            path.append({"step": name, "ok": ok, "detail": detail})

        # 1. before download
        if c.get("broadcaster_name", "").lower() in blacklist:
            gate = "blacklist"
            claim = "This channel's clips never belong in the video (event / custom games)."
            step("Streamer allowed", False, "on the blacklist")
        elif (include and lang not in include) or (not include and lang in exclude):
            gate = "language"
            claim = f"The streamer isn't speaking an accepted language (Twitch tag: {lang or '?'})."
            step("Channel language", False, f"tagged “{lang or '?'}”")
        elif p.get("prefilter_status") == "TOURNAMENT_EXCLUDE" or (not p and is_tournament(c.get("title", ""))):
            gate = "tournament"
            claim = "This is a tournament or event clip."
            step("Title", False, "looks like a tournament")
        else:
            step("Channel language", True, lang or "?")

        # 2. sound and motion
        if gate is None:
            st = p.get("prefilter_status")
            a, m = p.get("audio_score"), p.get("motion_score")
            detail = f"audio {_pct(a)}, motion {_pct(m)}"
            if st is None or st == "NO_FILE":
                continue                           # never measured: nothing to review
            if st == "AUDIO_EXCLUDE":
                gate = "quiet"
                claim = f"Nothing loud happens in this clip (audio {_pct(a)}, needs {audio_min})."
                step("Loud enough", False, detail)
            elif st == "MOTION_EXCLUDE":
                gate = "static"
                claim = f"Hardly anything moves on screen (motion {m:.3f}, needs {motion_min})."
                step("Moving enough", False, detail)
            else:
                why = f"title keyword “{p.get('keyword')}”" if st == "KEYWORD_PASS" else detail
                step("Sound and motion", True, why)
                if cid not in day["prefiltered"]:
                    gate = "rank_cap"
                    step(f"Ranked into the top {cap_pf}", False,
                         f"ranked {pf_rank.get(cid, '?')} of {len(passed)}")
                else:
                    step(f"Ranked into the top {cap_pf}", True, f"ranked {pf_rank.get(cid, '?')}")

        # 3. vision model
        if gate is None:
            if v is None:
                continue                           # VLM never ran on it (crash / old day)
            reason = v.get("reason", "")
            if v.get("decision") != "KEEP":
                if reason.startswith("NO_GAMEPLAY"):
                    gate = "vlm_no_gameplay"
                    claim = f"This isn't League of Legends gameplay ({v.get('gameplay_votes', '?')} frames said it was)."
                elif reason.startswith("PRO_PLAY"):
                    gate = "vlm_pro"
                    claim = "This is a professional tournament broadcast, not a streamer."
                elif reason == "BLACKLIST":
                    gate = "blacklist"
                    claim = "This channel's clips never belong in the video."
                else:
                    gate = "vlm_weak"
                    claim = (f"No kill is confirmed on screen and the reaction isn't loud "
                             f"(kills seen {v.get('kills_max', 0)}, audio {_pct(v.get('audio_score'))}).")
                step("Vision model", False, plain_vlm(reason))
            else:
                step("Vision model", True, plain_vlm(reason))
                if cid not in judged_ids:
                    gate = "vlm_cap"
                    step(f"Ranked into the top {cap_vlm or '?'}", False,
                         f"ranked {vlm_rank.get(cid, '?')} of {len(vlm_keeps)}")

        # 4. judge and selection
        if gate is None and j is not None:
            desc = (j.get("api_what_happens") or "").strip()
            scores = (f"entertainment {j.get('api_entertainment', '?')}/10, "
                      f"play {j.get('api_play_quality', '?')}/10, {j.get('api_focus', '?')}")
            said = f"Gemini: “{desc}” ({scores})" if desc else f"Gemini scored it {scores}."
            reason = j.get("api_reason", "")
            final = day["final"].get(cid)
            if final is not None or j.get("api_decision") == "KEEP":
                gate = "selected"
                claim = said
                rank = (final or j).get("countdown_rank")
                step("Gemini judge", True, scores + (" (filler)" if "FILLER" in reason else ""))
                step("In the video", True, f"#{rank}" if rank else "")
            elif "OVER_" in reason:
                gate = "trimmed"
                claim = said
                step("Gemini judge", True, scores)
                step("In the video", False, "cut to fit")
            else:
                gate = "judge_drop"
                claim = said
                step("Gemini judge", False, scores)
        if gate is None:
            continue

        out.append({
            "id": cid, "date": date, "gate": gate, "claim": claim, "path": path,
            "url": c.get("url") or f"https://clips.twitch.tv/{cid}",
            "who": c.get("broadcaster_name", ""), "title": c.get("title", ""),
            "lang": lang, "views": c.get("view_count", 0),
            "duration": c.get("duration", 0), "created_at": c.get("created_at", ""),
            "facts": {k: x for k, x in {**p, **(v or {}), **(j or {})}.items()
                      if k.startswith(("api_", "vlm_", "audio", "motion", "prefilter",
                                       "reason", "decision", "keep_score", "kills",
                                       "kill_", "multikill", "announcements", "keyword",
                                       "gameplay_votes", "countdown_rank"))},
        })
    return out


def sample(records: list[dict], reviewed: set, per_gate: int, n_random: int,
           seed: str) -> list[dict]:
    """A few unreviewed clips from every gate, plus a few from anywhere. Stable for a
    given seed so reopening the page shows the same day's picks."""
    rng = random.Random(seed)
    pool = [r for r in records if r["id"] not in reviewed]
    by_gate = defaultdict(list)
    for r in pool:
        by_gate[r["gate"]].append(r)
    picked = []
    for gate in GATES:
        items = sorted(by_gate.get(gate, []), key=lambda r: r["id"])
        rng.shuffle(items)
        for r in items[:per_gate]:
            picked.append(dict(r, why_picked=f"{GATES[gate][1].lower()} (gate sample)"))
    rest = sorted((r for r in pool if r["id"] not in {p["id"] for p in picked}),
                  key=lambda r: r["id"])
    rng.shuffle(rest)
    picked += [dict(r, why_picked="random pick") for r in rest[:n_random]]
    rng.shuffle(picked)
    return picked


def load_reviews(path: Path) -> dict:
    """{clip_id: newest review}."""
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            out[r["id"]] = r
    return out


def stats(reviews: dict) -> list[dict]:
    """Per gate: how often its stated reason was true, and how many good clips it lost."""
    rows = []
    for gate, (where, label) in GATES.items():
        rs = [r for r in reviews.values() if r.get("gate") == gate]
        if not rs:
            continue
        judged = [r for r in rs if r.get("claim_ok") is not None]
        q = Counter(r.get("quality") for r in rs)
        rows.append({
            "gate": gate, "where": where, "label": label, "n": len(rs),
            "claim_true": sum(1 for r in judged if r["claim_ok"]),
            "claim_n": len(judged),
            "good": q.get("good", 0), "ok": q.get("ok", 0), "bad": q.get("bad", 0),
        })
    return rows


def build_queue(cfg: dict, reviews: dict, days: int) -> list[dict]:
    rv = cfg.get("review", {})
    data = Path(cfg["paths"]["data_abs"])
    dates = sorted((d.name for d in (data / "work").iterdir()
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
                    and (data / "raw" / d.name / "clips.json").exists()
                    and (d / "api_scored.json").exists()),        # finished nights only
                   reverse=True)[:days]
    queue = []
    for date in dates:
        recs = trace(load_day(data, date), cfg, date)
        for r in sample(recs, set(reviews), rv.get("per_gate", 1), rv.get("random", 4), date):
            # full-quality file when it still exists; otherwise the Twitch embed. Not the
            # prefilter's LQ copy: Twitch now serves that as a 360x640 portrait frame.
            mp4 = data / "raw" / date / f"{r['id']}.mp4"
            r["local"] = f"/media/{date}/{r['id']}.mp4" if mp4.exists() else None
            queue.append(r)
    return queue


# ── server ────────────────────────────────────────────────────────────────────

def serve(cfg: dict, days: int, port: int, open_browser: bool = True) -> None:
    data = Path(cfg["paths"]["data_abs"])
    store = data / "reviews" / "reviews.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    html = (Path(__file__).with_name("review_ui.html")).read_text(encoding="utf-8")
    state = {"queue": build_queue(cfg, load_reviews(store), days)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", code)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/queue":
                reviews = load_reviews(store)
                return self._json({"queue": state["queue"], "tags": TAGS,
                                   "gates": {k: list(v) for k, v in GATES.items()},
                                   "reviewed": {k: reviews[k] for k in reviews
                                                if any(q["id"] == k for q in state["queue"])},
                                   "total_reviews": len(reviews)})
            if self.path == "/api/stats":
                return self._json(stats(load_reviews(store)))
            m = re.fullmatch(r"/media/(\d{4}-\d{2}-\d{2})/((?:lq/)?[\w-]+\.mp4)", self.path)
            if m:
                return self._media(data / "raw" / m.group(1) / m.group(2))
            self._send(b"not found", "text/plain", 404)

        def _media(self, f: Path):
            # Range support, or the browser can't seek (and Chrome won't even start some files)
            if not f.exists():
                return self._send(b"gone", "text/plain", 404)
            size = f.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = size - int(m.group(2))
            end = min(end, size - 1)
            with f.open("rb") as fh:
                fh.seek(start)
                body = fh.read(end - start + 1)
            self._send(body, "video/mp4", 206 if m else 200,
                       {"Accept-Ranges": "bytes",
                        **({"Content-Range": f"bytes {start}-{end}/{size}"} if m else {})})

        def do_POST(self):
            if self.path != "/api/review":
                return self._send(b"not found", "text/plain", 404)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            q = next((x for x in state["queue"] if x["id"] == body.get("id")), None)
            if q is None:
                return self._json({"error": "unknown clip"}, 400)
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "id": q["id"], "date": q["date"], "gate": q["gate"], "claim": q["claim"],
                "claim_ok": body.get("claim_ok"), "quality": body.get("quality"),
                "tags": [t for t in body.get("tags", []) if t in TAGS],
                "note": (body.get("note") or "").strip(),
                "clip": {k: q[k] for k in ("url", "who", "title", "lang", "views",
                                           "duration", "created_at", "path", "facts",
                                           "why_picked")},
            }
            with store.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._json({"ok": True})

    srv = ThreadingHTTPServer(("localhost", port), H)
    url = f"http://localhost:{port}"
    print(f"Review queue: {len(state['queue'])} clips across {days} day(s) -> {url}")
    print(f"Reviews are saved to {store}. Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def print_stats(cfg: dict) -> None:
    store = Path(cfg["paths"]["data_abs"]) / "reviews" / "reviews.jsonl"
    rows = stats(load_reviews(store))
    if not rows:
        print("No reviews yet.")
        return
    print(f"{'gate':34} {'n':>4}  {'reason true':>11}  {'good':>4} {'ok':>4} {'bad':>4}")
    for r in rows:
        true = f"{r['claim_true']}/{r['claim_n']}" if r["claim_n"] else "-"
        print(f"{r['where'] + ': ' + r['label']:34.34} {r['n']:>4}  {true:>11}  "
              f"{r['good']:>4} {r['ok']:>4} {r['bad']:>4}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--days", type=int, default=None, help="how many recent days to sample")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--stats", action="store_true", help="print per-gate results and exit")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    a = ap.parse_args()
    cfg = load_config(overlay=a.config)
    rv = cfg.get("review", {})
    if a.stats:
        return print_stats(cfg)
    serve(cfg, a.days or rv.get("days", 7), a.port or rv.get("port", 8766),
          open_browser=not a.no_browser)


if __name__ == "__main__":
    main()
