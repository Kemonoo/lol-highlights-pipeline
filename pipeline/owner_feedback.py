"""Owner feedback web UI — localhost interface for leaving notes on pipeline videos.

Saved to data/feedback/owner_notes.jsonl; read by feedback.py at the start of every
pipeline run. Owner notes bypass the min_agreement viewer-comment threshold — one
owner note is always ACTIONABLE.

Usage:
    python -m pipeline.owner_feedback          # start server, print URL
    python -m pipeline.owner_feedback --open   # also open browser automatically
"""
import json
import sys
import webbrowser
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Timer

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Pipeline Feedback</title>
  <meta name="viewport" content="width=device-width">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#111;color:#ddd;font-family:system-ui,-apple-system,sans-serif;
         max-width:680px;margin:0 auto;padding:32px 20px;line-height:1.5}
    h1{font-size:1.25rem;color:#fff;margin-bottom:24px}
    .field{margin-bottom:16px}
    label{display:block;font-size:.75rem;color:#777;text-transform:uppercase;
          letter-spacing:.06em;margin-bottom:6px}
    select,textarea{width:100%;background:#1b1b1b;color:#ddd;border:1px solid #2d2d2d;
                    border-radius:6px;padding:10px 12px;font-size:.95rem;outline:none;
                    font-family:inherit}
    select:focus,textarea:focus{border-color:#444}
    textarea{height:110px;resize:vertical}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
    button{padding:9px 18px;border:none;border-radius:6px;font-size:.9rem;
           cursor:pointer;font-family:inherit}
    #btn-save{background:#2563eb;color:#fff}
    #btn-save:hover{background:#1d4ed8}
    #btn-mic{background:#222;color:#bbb;border:1px solid #333}
    #btn-mic.on{background:#7f1d1d;border-color:#991b1b;color:#fca5a5}
    .toast{font-size:.85rem;color:#4ade80;opacity:0;transition:opacity .3s}
    .toast.show{opacity:1}
    hr{border:none;border-top:1px solid #1e1e1e;margin:28px 0}
    .card{background:#181818;border:1px solid #252525;border-radius:6px;
          padding:12px 14px;margin-bottom:10px}
    .card-meta{font-size:.75rem;color:#555;margin-bottom:4px}
    .card-text{font-size:.9rem;color:#ccc}
    .empty{color:#444;font-size:.85rem}
    .hint{font-size:.8rem;color:#555;margin-top:6px}
  </style>
</head>
<body>
  <h1>📋 Pipeline Feedback</h1>

  <div class="field">
    <label>Video</label>
    <select id="sel"></select>
  </div>

  <div class="field">
    <label>Your note</label>
    <textarea id="txt"
      placeholder="e.g. Clip 3 — the commentary mentioned &quot;kills detected&quot; which sounds weird. The intro feels robotic. Clip at 2:30 was great, keep this type."></textarea>
    <p class="hint">Mention clip numbers, timestamps, or topics freely — Gemini will extract the structure.</p>
  </div>

  <div class="row">
    <button id="btn-save">Save note</button>
    <button id="btn-mic">🎤 Speak</button>
    <span class="toast" id="toast">✓ Saved</span>
  </div>

  <hr>
  <label>Recent notes</label>
  <div id="list"></div>

  <script>
    const VIDEOS = __VIDEOS_JSON__;
    const sel = document.getElementById('sel');
    sel.innerHTML = '<option value="">— no specific video —</option>' +
      VIDEOS.map(v => `<option value='${JSON.stringify({d:v.date,y:v.youtube_id})}'>${v.date}${v.title?' · '+v.title.slice(0,45):''}</option>`).join('');
    if (VIDEOS.length) sel.selectedIndex = 1;

    // Speech recognition (Chrome/Edge)
    const mic = document.getElementById('btn-mic');
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    let recog = null, listening = false, base = '';
    if (SR) {
      recog = new SR();
      recog.continuous = true;
      recog.interimResults = true;
      recog.lang = 'en-US';
      recog.onresult = e => {
        let interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++)
          e.results[i].isFinal ? (base += e.results[i][0].transcript + ' ') : (interim += e.results[i][0].transcript);
        document.getElementById('txt').value = base + interim;
      };
      recog.onend = () => { listening = false; mic.textContent = '🎤 Speak'; mic.classList.remove('on'); };
      mic.onclick = () => {
        if (listening) { recog.stop(); return; }
        base = document.getElementById('txt').value.trimEnd() + (document.getElementById('txt').value ? ' ' : '');
        recog.start(); listening = true;
        mic.textContent = '⏹ Stop'; mic.classList.add('on');
      };
    } else {
      mic.textContent = '🎤 (Chrome/Edge only)'; mic.disabled = true;
    }

    // Save
    document.getElementById('btn-save').onclick = async () => {
      const note = document.getElementById('txt').value.trim();
      if (!note) return;
      const v = sel.value ? JSON.parse(sel.value) : {};
      const r = await fetch('/submit', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({note, video_date: v.d || '', youtube_id: v.y || ''})
      });
      if (r.ok) {
        document.getElementById('txt').value = ''; base = '';
        const t = document.getElementById('toast');
        t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2000);
        load();
      }
    };

    async function load() {
      const r = await fetch('/notes');
      const notes = await r.json();
      document.getElementById('list').innerHTML = notes.length
        ? notes.map(n => `<div class="card">
            <div class="card-meta">${n.when.slice(0,16).replace('T',' ')} UTC · ${n.video_date||'no video'}</div>
            <div class="card-text">${n.note.replace(/</g,'&lt;')}</div></div>`).join('')
        : '<p class="empty">No notes yet.</p>';
    }
    load();
  </script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, fb_dir: Path, videos: list, *a, **kw):
        self._fb_dir = fb_dir
        self._videos = videos
        super().__init__(*a, **kw)

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path in ("/", ""):
            page = _HTML.replace("__VIDEOS_JSON__",
                                 json.dumps(self._videos, ensure_ascii=False))
            self._send(200, "text/html; charset=utf-8", page.encode())
        elif self.path == "/notes":
            notes = self._recent()
            self._send(200, "application/json",
                       json.dumps(notes, ensure_ascii=False).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/submit":
            self.send_error(404); return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        text = body.get("note", "").strip()
        if not text:
            self.send_error(400); return
        record = {
            "when": datetime.now(timezone.utc).isoformat(),
            "video_date": body.get("video_date", ""),
            "youtube_id": body.get("youtube_id", ""),
            "note": text,
            "source": "owner",
        }
        dest = self._fb_dir / "owner_notes.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._send(200, "application/json", b'{"ok":true}')

    def _recent(self) -> list:
        f = self._fb_dir / "owner_notes.jsonl"
        if not f.exists():
            return []
        out = []
        for line in f.read_text(encoding="utf-8").splitlines()[-20:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out[::-1]

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def _state_videos(data: Path) -> list:
    f = data / "state.json"
    if not f.exists():
        return []
    try:
        st = json.loads(f.read_text(encoding="utf-8").rstrip("\x00"))
        return [
            {"date": v.get("date", ""), "youtube_id": v.get("youtube_id", ""),
             "title": v.get("title", "")}
            for v in reversed(st.get("videos", [])[-10:])
            if v.get("youtube_id")
        ]
    except Exception:
        return []


def main():
    from .config import load_config
    cfg = load_config()
    data = Path(cfg["paths"]["data_abs"])
    fb_dir = data / "feedback"
    videos = _state_videos(data)

    port = 7863
    handler = partial(_Handler, fb_dir, videos)
    httpd = HTTPServer(("127.0.0.1", port), handler)
    url = f"http://localhost:{port}"
    print(f"Owner feedback: {url}  (Ctrl+C to stop)")
    print("Notes saved to data/feedback/owner_notes.jsonl")
    print("They will be processed at the next pipeline run (or: schtasks /run /tn \"LoL Daily Highlights\")")

    if "--open" in sys.argv:
        Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
