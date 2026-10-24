"""Post-run health check (owner, 2026-10-24) - plain code, no LLM, costs nothing.

    python -m pipeline.tools.health [--date YYYY-MM-DD] [--config overlay.yaml]
                                    [--data DIR] [--print] [--no-notify]

run_daily_auto.bat calls this after its date loop. It reads the LAST run block of
data/logs/auto_<date>.log (from the bat's "==== run started" marker on), writes
data/logs/health_<date>.md - finished/exit code, duration, video link + title, Shorts,
clips/minutes, warnings grouped by type - and, when something needs attention and
`health.notify` is on, shows a Windows toast (scripts/health_toast.ps1). The toast lands
in the Notification Center at 03:00 and is still there in the morning; it is a "reminder"
toast, so it stays on screen until dismissed once someone is at the PC.

Why: problems (a clip silently losing its captions to cp1252, 2026-10-24) were only seen
when someone happened to read the log. Flags are in `health:` (config.yaml). "New warning
type" compares against the previous `history_days` of auto_*.log files themselves, so it
works from the first night and the health files are only for people.

`--print` = report to stdout, write nothing, notify nobody (backtesting old logs).
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

RUN_STARTED = re.compile(r"^==== run started (.*?) ====", re.M)
RUN_FINISHED = re.compile(r"^==== run finished (.*?) \(exit (-?\d+)\) ====", re.M)
POSTPONED = re.compile(r"^==== postponed", re.M)
NOTHING = re.compile(r"^==== nothing to do", re.M)
DATES = re.compile(r"^==== dates:\s*(.*?)\s*====", re.M)
CLOCK = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
STAGE = re.compile(r"\[(\w+)\] starting")
SELECTION = re.compile(r"Selection: (\d+) clips, ([\d.]+) min")
ASSEMBLED = re.compile(r"Assembled (\S+?)\.mp4 \(([\d.]+) min, (\d+) clips\)")
UPLOADED = re.compile(r"Uploaded (\S+?)\.mp4 as (https://\S+)")
ALREADY_UP = re.compile(r"upload: (\S+?)(?:\.mp4)? already uploaded as (\S+)")
SHORT_UP = re.compile(r"(?<!ranking short )uploaded: (https://\S+)")
SHORT_SKIP = re.compile(r"short \S+ already done - skip")
RANKING_UP = re.compile(r"ranking short uploaded: (https://\S+)")
# "03:52:06 pipeline.providers.ollama WARNING msg" - searched, not anchored: yt-dlp's
# progress output has no newline, so a log line can start mid-line.
LOGGED = re.compile(r"\d\d:\d\d:\d\d (\S+)\s+(WARNING|ERROR|CRITICAL)\s+(.*)")
YTDLP_ERR = re.compile(r"^ERROR: (.*)")
TRANSCRIBE_FAIL = re.compile(r"transcribe failed for ")
JUDGE_LINE = re.compile(r"API judge: \d+ -> \d+ kept \(\d+ judged by API, (\d+) local\)")
NO_JUDGE = re.compile(r"no video judge available|JUDGE DEGRADED")

# Normalising a warning into its TYPE: whatever differs per clip / per night goes.
_NORMALISE = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b[A-Z][A-Za-z0-9]+-[A-Za-z0-9_-]{16}\b(?:\.mp4)?"), "<clip>"),
    (re.compile(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}"), "<char>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"[A-Za-z]:\\\S+|(?<!\w)/\S+/\S+"), "<path>"),
    (re.compile(r"\b\d+(?:\.\d+)*\b"), "N"),
    (re.compile(r"\s+"), " "),
]
TYPE_CHARS = 100


def normalise(msg: str) -> str:
    """Warning text -> its type: clip ids, urls, paths, numbers stripped; capped."""
    msg = msg.strip()
    for rx, sub in _NORMALISE:
        msg = rx.sub(sub, msg)
    return msg[:TYPE_CHARS].rstrip()


def last_run(text: str) -> str:
    """The log text from the LAST "run started" marker on (a day's log can hold a
    manual rerun appended after the nightly one). No marker = the whole text."""
    starts = list(RUN_STARTED.finditer(text))
    return text[starts[-1].start():] if starts else text


def _seconds(stamp: str) -> int | None:
    m = CLOCK.search(stamp or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) if m else None


def _tracebacks(lines: list[str]) -> list[str]:
    """The exception line ("ValueError: ...") ending each Traceback block."""
    out, inside = [], False
    for ln in lines:
        if ln.startswith("Traceback (most recent call last)"):
            inside = True
        elif inside and ln and not ln[0].isspace():
            out.append(ln)
            inside = False
    return out


def warnings_in(text: str) -> tuple[Counter, dict]:
    """({type: count}, {type: first raw message}) for every WARNING/ERROR/Traceback."""
    counts, example = Counter(), {}
    lines = text.splitlines()

    def add(kind: str, msg: str):
        msg_t = normalise(msg)
        # "transcribe: transcribe: ..." - most messages already name their stage
        t = msg_t if msg_t.startswith(kind.rsplit(".", 1)[-1] + ":") else f"{kind}: {msg_t}"
        counts[t] += 1
        example.setdefault(t, msg.strip()[:300])

    for ln in lines:
        m = LOGGED.search(ln)
        if m:
            add(m[1].removeprefix("pipeline."), m[3])
            continue
        m = YTDLP_ERR.match(ln)
        if m:
            add("yt-dlp", m[1])
    for exc in _tracebacks(lines):
        add("traceback", exc)
    return counts, example


def parse_run(text: str) -> dict:
    """Facts about one run block (see last_run). Pure."""
    started = RUN_STARTED.search(text)
    finished = list(RUN_FINISHED.finditer(text))
    fin = finished[-1] if finished else None
    dm = DATES.search(text)
    dates = dm[1].split() if dm else []
    t0 = _seconds(started[1]) if started else None
    t1 = _seconds(fin[1]) if fin else None
    if t0 is None or t1 is None:        # no bat markers: first/last logged clock time
        stamps = [s for s in (_seconds(m[0]) for m in
                              re.finditer(r"^\d\d:\d\d:\d\d", text, re.M)) if s is not None]
        t0 = t0 if t0 is not None else (stamps[0] if stamps else None)
        t1 = t1 if t1 is not None else (stamps[-1] if stamps else None)
    duration = None
    if t0 is not None and t1 is not None:
        duration = (t1 - t0) % 86400    # a run past midnight wraps; none takes a day
    uploads = {m[1]: m[2] for m in UPLOADED.finditer(text)}
    for m in ALREADY_UP.finditer(text):
        uploads.setdefault(m[1], m[2])
    counts, example = warnings_in(text)
    return {
        "started": started[1].strip() if started else None,
        "finished": fin[1].strip() if fin else None,
        "exit": int(fin[2]) if fin else None,
        "postponed": bool(POSTPONED.search(text)),
        "nothing_to_do": bool(NOTHING.search(text)),
        "dates": dates,
        "duration_s": duration,
        "stages": STAGE.findall(text),
        "selection": [(int(a), float(b)) for a, b in SELECTION.findall(text)],
        "assembled": {n: (float(mn), int(c)) for n, mn, c in ASSEMBLED.findall(text)},
        "uploads": uploads,
        "shorts": SHORT_UP.findall(text),
        "shorts_skipped": len(SHORT_SKIP.findall(text)),
        "ranking": RANKING_UP.findall(text),
        "warnings": counts,
        "examples": example,
        "transcribe_failures": len(TRANSCRIBE_FAIL.findall(text)),
        "judge_local": sum(int(n) for n in JUDGE_LINE.findall(text))
                       + len(NO_JUDGE.findall(text)),
    }


def expectations(cfg: dict) -> dict:
    """What a healthy night publishes, from the effective config."""
    up = bool(cfg.get("upload", {}).get("enabled"))
    sh = cfg.get("shorts", {})
    shorts = int(sh.get("count", 0)) if (up and sh.get("enabled", True)
                                         and sh.get("upload")) else 0
    return {"upload": up, "shorts": shorts}


def flags(run: dict, expect: dict, hc: dict, history: set) -> list[str]:
    """Everything that needs a human look, as short sentences. Empty = healthy. Pure."""
    if run["postponed"]:                # owner chose "Tonight" - nothing ran on purpose
        return []
    out = []
    if run["finished"] is None:
        out.append("run never reached 'run finished' (crashed, killed or still running)")
    elif run["exit"]:
        out.append(f"run exited with code {run['exit']}")
    dates = run["dates"]
    if expect["upload"]:
        missing = [d for d in dates if d not in run["uploads"]]
        if missing:
            out.append(f"no video uploaded for {', '.join(missing)}")
    if expect["shorts"] and dates:
        want = expect["shorts"] * len(dates)
        got = len(run["shorts"]) + len(run["ranking"]) + run["shorts_skipped"]
        if got < want:
            out.append(f"only {got} of {want} Shorts uploaded")
    max_h = float(hc.get("max_hours", 4))
    if run["duration_s"] is not None and run["duration_s"] > max_h * 3600:
        out.append(f"run took {run['duration_s'] / 3600:.1f} h (> {max_h:g} h)")
    min_m = float(hc.get("min_minutes", 6))
    for name, (mins, clips) in run["assembled"].items():
        if not name.startswith("weekly_") and mins < min_m:
            out.append(f"{name} is only {mins:.1f} min ({clips} clips, < {min_m:g} min)")
    if run["transcribe_failures"]:
        out.append(f"{run['transcribe_failures']} clip(s) failed to transcribe "
                   "(published without captions)")
    if run["judge_local"]:
        out.append(f"judge fell back to local_judge ({run['judge_local']} clip(s)/notice(s))")
    new = [t for t in run["warnings"] if t not in history]
    for t in new:
        out.append(f"new warning type: {t}")
    return out


def history_types(logs: Path, day: date, days: int) -> set:
    """Every warning type in the auto_*.log of the `days` days before `day`."""
    seen = set()
    for i in range(1, days + 1):
        p = logs / f"auto_{(day - timedelta(days=i)).isoformat()}.log"
        if p.exists():
            seen |= set(warnings_in(read_log(p))[0])
    return seen


def read_log(p: Path) -> str:
    # The bat appends child output in whatever code page it was written in; never fail
    # a health check on a stray byte. NUL padding: see CLAUDE.md "Read tolerance".
    return p.read_bytes().decode("utf-8", errors="replace").replace("\x00", "")


def _dur(s) -> str:
    if s is None:
        return "?"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def render(day: str, run: dict, fl: list[str], titles: dict) -> str:
    """The health_<date>.md text. Pure."""
    status = "OK" if not fl else f"{len(fl)} flag(s)"
    lines = [f"# KEMONO health {day} - {status}", ""]
    if run["finished"] is None:
        lines.append(f"- run: started {run['started'] or '?'}, NOT finished")
    else:
        lines.append(f"- run: exit {run['exit']}, {_dur(run['duration_s'])} "
                     f"({run['started']} -> {run['finished']})")
    if run["postponed"]:
        lines.append("- postponed to the next scheduled run")
    elif run["nothing_to_do"]:
        lines.append("- nothing to do (yesterday already finished)")
    for d in run["dates"]:
        url = run["uploads"].get(d)
        title = titles.get(d) or ""
        mins, clips = run["assembled"].get(d, (None, None))
        size = f"{clips} clips, {mins:.1f} min" if mins is not None else "not assembled"
        lines.append(f"- {d}: {url or 'NOT uploaded'}"
                     + (f' "{title}"' if title else "") + f" ({size})")
    for name, url in run["uploads"].items():
        if name not in run["dates"]:
            lines.append(f"- {name}: {url}" + (f' "{titles[name]}"' if titles.get(name) else ""))
    n_sh = len(run["shorts"]) + len(run["ranking"])
    lines.append(f"- Shorts: {n_sh} uploaded ({len(run['ranking'])} ranking)"
                 + (f", {run['shorts_skipped']} already done" if run["shorts_skipped"] else ""))
    if fl:
        lines += ["", "## Flags", *[f"- {f}" for f in fl]]
    if run["warnings"]:
        lines += ["", "## Warnings by type"]
        for t, n in run["warnings"].most_common():
            lines.append(f"- {n}x `{t}`")
    return "\n".join(lines) + "\n"


def notify(root: Path, day: str, fl: list[str], report: Path) -> None:
    """Windows toast via scripts/health_toast.ps1 (msg.exe fallback inside). Never raises."""
    if sys.platform != "win32":
        print("health: " + "; ".join(fl))
        return
    body = "; ".join(fl)
    if len(body) > 220:
        body = body[:217] + "..."
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(root / "scripts" / "health_toast.ps1"),
             "-Title", f"KEMONO {day}: {len(fl)} flag(s)", "-Body", body,
             "-Report", str(report)],
            timeout=60, check=False)
    except Exception as e:      # noqa: BLE001 - a notification must never fail the run
        print(f"health: toast failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="post-run health report for the nightly log")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="the log's date (auto_<date>.log = the night the bat ran)")
    ap.add_argument("--config", default="")
    ap.add_argument("--data", default="", help="data dir (default: paths.data)")
    ap.add_argument("--print", action="store_true", dest="dry",
                    help="print the report only: write nothing, notify nobody")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()

    from ..config import ROOT, load_config
    cfg = load_config(overlay=args.config or None)
    hc = cfg.get("health", {}) or {}
    if not hc.get("enabled", True):
        return 0
    data = Path(args.data or cfg["paths"]["data_abs"])
    logs = data / "logs"
    day = date.fromisoformat(args.date)
    log_path = logs / f"auto_{day.isoformat()}.log"
    if not log_path.exists():
        run = parse_run("")
        fl = [f"no log at {log_path.name} - the nightly run did not start"]
    else:
        run = parse_run(last_run(read_log(log_path)))
        hist = history_types(logs, day, int(hc.get("history_days", 14)))
        fl = flags(run, expectations(cfg), hc, hist)
    titles = {}
    for name in [*run["dates"], *run["uploads"]]:
        mp = data / "output" / f"{name}.meta.json"
        if mp.exists():
            try:
                titles[name] = json.loads(mp.read_text(encoding="utf-8").rstrip("\x00"))["title"]
            except (ValueError, KeyError):
                pass
    text = render(day.isoformat(), run, fl, titles)
    if args.dry:
        sys.stdout.buffer.write(text.encode("utf-8"))
        return 0
    report = logs / f"health_{day.isoformat()}.md"
    report.write_text(text, encoding="utf-8")
    print(f"health: {report.name} - " + ("OK" if not fl else f"{len(fl)} flag(s)"))
    if fl and hc.get("notify") and not args.no_notify:
        notify(ROOT, day.isoformat(), fl, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
