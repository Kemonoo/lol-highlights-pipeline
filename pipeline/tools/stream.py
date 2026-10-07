"""Long YouTube livestream of the finished daily videos, played back to back.

Owner idea (2026-10-07): long streams pull views, and the channel already has hours of
finished countdowns in data/output/. This plays them one after another as a live
stream via ffmpeg -> YouTube's RTMP ingest. Nothing is uploaded; YouTube records it.

  python -m pipeline.tools.stream [--config pipeline/config.kemono.yaml]
         [--hours 11.8] [--dry-run]                         (stream.bat does the same)

Setup (owner, once): YouTube Studio -> Create -> Go live -> Stream: copy the stream key
into .env as YT_STREAM_KEY=... . The key is never logged. Turn on "auto-start" there so
the stream goes live as soon as data arrives.

Decisions:
  - Re-encoded, not stream-copied: the masters' keyframes are far apart (encoder
    default), YouTube wants one every 2-4 s and a constant bitrate. NVENC when present,
    so a 12 h stream barely touches the CPU.
  - Default length 11.8 h: YouTube only keeps streams up to 12 h as a VOD.
  - The playlist repeats the available videos to fill the time (`order: newest` starts
    from the latest; `shuffle` is seeded by the date). More videos on disk = less
    repetition: `cleanup.keep_output_days` decides how many exist.
  - A dropped connection restarts ffmpeg from the video that was playing (up to
    `stream.max_restarts`), so one network blip doesn't end a 12 h stream.
  - Don't run it across 03:00: the nightly run needs the GPU and the upload line.
"""
import argparse
import logging
import os
import random
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

log = logging.getLogger("pipeline.stream")


def build_playlist(videos: list[tuple[str, float]], target_s: float, order: str = "newest",
                   seed: str = "") -> list[tuple[str, float]]:
    """(path, seconds) items repeated until they cover `target_s`. `videos` arrive
    oldest first (date-named files sort that way). Pure (tested)."""
    vids = [v for v in videos if v[1] > 0]
    if not vids or target_s <= 0:
        return []
    if order == "shuffle":
        vids = vids[:]
        random.Random(f"stream:{seed}").shuffle(vids)
    else:
        vids = vids[::-1]
    out, total, i = [], 0.0, 0
    while total < target_s:
        v = vids[i % len(vids)]
        out.append(v)
        total += v[1]
        i += 1
    return out


def resume_from(playlist: list[tuple[str, float]], elapsed_s: float) -> tuple[int, float]:
    """(index of the video playing at `elapsed_s`, seconds into it). Pure (tested)."""
    t = 0.0
    for i, (_, d) in enumerate(playlist):
        if elapsed_s < t + d:
            return i, elapsed_s - t
        t += d
    return len(playlist), 0.0


def playlist_text(items: list[tuple[str, float]], into: float = 0.0) -> str:
    """ffconcat lines; a reconnect starts the first file `into` seconds in (inpoint -
    an -ss on the whole concat input can't seek across files). Pure (tested)."""
    lines = []
    for k, (p, _) in enumerate(items):
        lines.append(f"file '{Path(p).as_posix()}'")
        if k == 0 and into > 0:
            lines.append(f"inpoint {into:.2f}")
    return "\n".join(lines)


def encode_args(encoder: str, kbps: int, fps: int) -> list[str]:
    """Constant-bitrate H.264 with a keyframe every 2 s. Pure (tested)."""
    gop = str(fps * 2)
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "cbr", *rate, "-g", gop, "-bf", "2"]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", *rate, "-g", gop,
                "-x264-params", "nal-hrd=cbr", "-pix_fmt", "yuv420p"]
    return ["-c:v", encoder, *rate, "-g", gop]


def _duration(p: Path) -> float:
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(p)], capture_output=True,
                                    text=True).stdout.strip() or 0)
    except (OSError, ValueError):
        return 0.0


def run_stream(cfg: dict, hours: float | None = None, dry_run: bool = False) -> int:
    from ..hardware import pick_encoder
    st = cfg.get("stream", {}) or {}
    out_dir = Path(cfg["paths"]["data_abs"]) / "output"
    videos = [(str(p), _duration(p)) for p in sorted(out_dir.glob("????-??-??.mp4"))]
    target = float(hours if hours is not None else st.get("hours", 11.8)) * 3600
    playlist = build_playlist(videos, target, st.get("order", "newest"), date.today().isoformat())
    if not playlist:
        log.error("no finished videos in %s - nothing to stream", out_dir)
        return 1
    uniq = len({p for p, _ in playlist})
    log.info("stream: %d videos on disk, %d plays, %.1f h (each video ~%.1fx)",
             len(videos), len(playlist), sum(d for _, d in playlist) / 3600,
             len(playlist) / max(uniq, 1))
    key = os.environ.get(str(st.get("key_env", "YT_STREAM_KEY")), "")
    if not key and not dry_run:
        log.error("no stream key: set %s in .env (YouTube Studio -> Go live -> Stream)",
                  st.get("key_env", "YT_STREAM_KEY"))
        return 1
    url = str(st.get("rtmp_url", "rtmp://a.rtmp.youtube.com/live2")).rstrip("/") + "/" + key
    fps = int(st.get("fps", 30))
    enc = encode_args(pick_encoder(str(st.get("encoder", "auto"))), int(st.get("bitrate_kbps", 6000)), fps)
    work = out_dir.parent / "work" / "_stream"
    work.mkdir(parents=True, exist_ok=True)

    started, restarts = time.time(), 0
    while True:
        i, into = resume_from(playlist, time.time() - started)
        if i >= len(playlist):
            log.info("stream: finished (%.1f h)", (time.time() - started) / 3600)
            return 0
        lst = work / "playlist.txt"
        lst.write_text(playlist_text(playlist[i:], into), encoding="utf-8")
        remaining = min(sum(d for _, d in playlist[i:]) - into, target - (time.time() - started))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-t", f"{remaining:.0f}",
               "-vf", f"scale=1920:1080:force_original_aspect_ratio=decrease,"
                      f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p",
               *enc, "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
               "-f", "flv", url]
        if dry_run:
            log.info("dry run: %s", " ".join(c if c != url else "<rtmp>/<key>" for c in cmd))
            return 0
        log.info("stream: ffmpeg from play %d/%d (+%.0f s), %.1f h left", i + 1,
                 len(playlist), into, remaining / 3600)
        rc = subprocess.run(cmd).returncode
        if rc == 0 and time.time() - started >= target - 60:
            log.info("stream: finished (%.1f h)", (time.time() - started) / 3600)
            return 0
        restarts += 1
        if restarts > int(st.get("max_restarts", 20)):
            log.error("stream: ffmpeg stopped %d times - giving up", restarts)
            return 1
        log.warning("stream: ffmpeg exited (%s) - reconnecting in 10 s (%d/%s)", rc,
                    restarts, st.get("max_restarts", 20))
        time.sleep(10)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=None, help="overlay yaml (e.g. pipeline/config.kemono.yaml)")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, don't go live")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    from ..config import load_config
    cfg = load_config(overlay=a.config) if a.config else load_config()
    return run_stream(cfg, a.hours, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
