"""Ground-truth labeling tool.

    python -m pipeline.label_clips [--date YYYY-MM-DD] [--source prefiltered|raw] [--port 8765]

Opens a local web page (http://localhost:8765) that plays each clip and records your
verdict with one keypress:
    G = good (would belong in the video)    O = ok (borderline)    B = bad (boring/wrong)
Optional tags + a free-text note per clip. Labels save instantly to
data/work/<date>/labels.json — close the tab whenever, progress is kept.

The eval harness (pipeline/eval_filter.py) compares these labels against the filter's
decisions to measure precision/recall and find what to fix next.
"""
import argparse
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import load_config

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Clip labeling</title>
<style>
 body{font-family:system-ui;background:#111;color:#eee;margin:0;display:flex;flex-direction:column;height:100vh}
 #top{padding:10px 16px;background:#1a1a1a;display:flex;gap:16px;align-items:center}
 #vid{flex:1;display:flex;justify-content:center;align-items:center;background:#000}
 video{max-width:100%;max-height:100%}
 #bar{padding:12px 16px;background:#1a1a1a;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 button{font-size:15px;padding:8px 18px;border:none;border-radius:6px;cursor:pointer;color:#fff}
 .good{background:#2c7a3f}.ok{background:#8a6d1a}.bad{background:#8a2a2a}.nav{background:#333}
 label{margin-right:8px;user-select:none}
 #note{flex:1;min-width:160px;padding:6px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
 .done{color:#6c6}.pending{color:#999}
 #meta i{color:#9ab}
</style></head><body>
<div id="top"><b id="prog"></b><span id="meta"></span></div>
<div id="vid"><video id="player" controls autoplay></video></div>
<div id="bar">
 <button class="good" onclick="verdict('good')">G — Good</button>
 <button class="ok" onclick="verdict('ok')">O — Ok</button>
 <button class="bad" onclick="verdict('bad')">B — Bad</button>
 <span>
  <label><input type="checkbox" value="kills"> kills</label>
  <label><input type="checkbox" value="outplay"> outplay</label>
  <label><input type="checkbox" value="funny"> funny</label>
  <label><input type="checkbox" value="talk-only"> talk-only</label>
  <label><input type="checkbox" value="pro-play"> pro-play</label>
  <label><input type="checkbox" value="not-gameplay"> not-gameplay</label>
 </span>
 <input id="note" placeholder="optional note (what was in the clip?)">
 <button class="nav" onclick="step(-1)">⟨ prev</button>
 <button class="nav" onclick="step(1)">next ⟩</button>
</div>
<script>
let clips=[], labels={}, i=0;
async function init(){
  clips = await (await fetch('/clips.json')).json();
  labels = await (await fetch('/labels.json')).json();
  i = clips.findIndex(c => !labels[c.id]);
  if(i<0) i=0;
  show();
}
function show(){
  const c = clips[i];
  document.getElementById('player').src = '/video/'+c.id;
  const l = labels[c.id];
  document.getElementById('prog').textContent =
    `${Object.keys(labels).length}/${clips.length} labeled — clip ${i+1}`;
  document.getElementById('meta').innerHTML =
    `<b>${c.broadcaster_name}</b> — ${c.title} ` +
    `<i>(filter: ${c.decision||'?'} ${c.reason||''}, audio ${c.audio_score??'?'})</i>` +
    (l?` <span class="done">labeled: ${l.verdict}</span>`:' <span class="pending">unlabeled</span>');
  document.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = !!(l&&l.tags&&l.tags.includes(cb.value)));
  document.getElementById('note').value = (l&&l.note)||'';
}
async function verdict(v){
  const c = clips[i];
  const tags=[...document.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value);
  const note=document.getElementById('note').value;
  labels[c.id]={verdict:v,tags,note,title:c.title,broadcaster:c.broadcaster_name};
  await fetch('/label',{method:'POST',body:JSON.stringify({id:c.id,verdict:v,tags,note,
    title:c.title,broadcaster:c.broadcaster_name})});
  step(1);
}
function step(d){ i=Math.min(Math.max(i+d,0),clips.length-1); show(); }
document.addEventListener('keydown',e=>{
  if(e.target.id==='note') return;
  if(e.key==='g') verdict('good'); else if(e.key==='o') verdict('ok');
  else if(e.key==='b') verdict('bad');
  else if(e.key==='ArrowRight') step(1); else if(e.key==='ArrowLeft') step(-1);
});
init();
</script></body></html>"""


def make_handler(clips: list[dict], labels_path: Path, videos: dict[str, Path]):
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, HTML.encode())
            elif self.path == "/clips.json":
                self._send(200, json.dumps(clips).encode(), "application/json")
            elif self.path == "/labels.json":
                self._send(200, json.dumps(labels).encode(), "application/json")
            elif self.path.startswith("/video/"):
                vid = self.path.split("/video/", 1)[1]
                f = videos.get(vid)
                if not f or not f.exists():
                    self._send(404, b"not found", "text/plain")
                    return
                data = f.read_bytes()
                rng = self.headers.get("Range")
                if rng:
                    m = re.match(r"bytes=(\d+)-(\d*)", rng)
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else len(data) - 1
                    chunk = data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                else:
                    self._send(200, data, "video/mp4", {"Accept-Ranges": "bytes"})
            else:
                self._send(404, b"?", "text/plain")

        def do_POST(self):
            if self.path == "/label":
                n = int(self.headers.get("Content-Length", 0))
                rec = json.loads(self.rfile.read(n))
                labels[rec.pop("id")] = rec
                labels_path.write_text(json.dumps(labels, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
                self._send(200, b"{}", "application/json")
            else:
                self._send(404, b"?", "text/plain")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=False)
    ap.add_argument("--source", default="prefiltered", choices=["prefiltered", "raw"])
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    cfg = load_config()
    data = Path(cfg["paths"]["data_abs"])
    if args.date:
        date = args.date
    else:  # newest work dir
        dates = sorted(p.name for p in (data / "work").iterdir() if p.is_dir())
        if not dates:
            raise SystemExit("No work directories found — run the pipeline first.")
        date = dates[-1]

    if args.source == "prefiltered":
        src = data / "work" / date / "prefiltered.json"
    else:
        src = data / "raw" / date / "clips.json"
    clips = json.loads(src.read_text(encoding="utf-8"))["clips"]

    # merge filter decisions if available
    scored_f = data / "work" / date / "vlm_scored.json"
    if scored_f.exists():
        scored = {c["id"]: c for c in json.loads(scored_f.read_text(encoding="utf-8"))["clips"]}
        for c in clips:
            sc = scored.get(c["id"], {})
            c["decision"], c["reason"] = sc.get("decision"), sc.get("reason")

    raw_dir = data / "raw" / date
    videos = {}
    for c in clips:
        f = raw_dir / f"{c['id']}.mp4"
        if not f.exists() and c.get("local_path"):
            f = Path(c["local_path"])
        if f.exists():
            videos[c["id"]] = f
    clips = [c for c in clips if c["id"] in videos]
    labels_path = data / "work" / date / "labels.json"

    handler = make_handler(clips, labels_path, videos)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://localhost:{args.port}"
    print(f"Labeling {len(clips)} clips for {date} -> {labels_path}")
    print(f"Open {url}  (keys: G good / O ok / B bad, arrows to navigate). Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
