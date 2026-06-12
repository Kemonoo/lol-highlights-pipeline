"""Generate a copyright-free ambient-electronic background loop.

100% synthesized (no samples) -> zero licensing risk, no attribution needed.
Intended as a quiet bed at music_volume_db (-24 dB); replace with a licensed track in
assets/music/bg.mp3 whenever you prefer (see assets/music/README.md).

    python -m pipeline.gen_music [out.mp3] [--minutes 1.5]
"""
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 44100
BPM = 86.0
BEAT = 60.0 / BPM                       # 0.698s
BAR = 4 * BEAT

# A minor: Am - F - C - G, two bars each (lo-fi classic, hard to get wrong)
CHORDS = [
    [220.00, 261.63, 329.63],   # Am  (A3 C4 E4)
    [174.61, 220.00, 261.63],   # F   (F3 A3 C4)
    [196.00, 261.63, 329.63],   # C/G (G3 C4 E4)
    [196.00, 246.94, 293.66],   # G   (G3 B3 D4)
]
BASS = [110.00, 87.31, 130.81, 98.00]   # A2 F2 C3 G2


def _env(n: int, attack: float, release: float) -> np.ndarray:
    """Attack/release envelope over n samples."""
    e = np.ones(n)
    a = min(int(attack * SR), n // 2)
    r = min(int(release * SR), n // 2)
    e[:a] = np.linspace(0, 1, a)
    e[-r:] *= np.linspace(1, 0, r)
    return e


def _mellow(freq: float, dur: float, detune: float = 0.15) -> np.ndarray:
    """Soft pad voice: fundamental + weak odd harmonics, slight detune chorus."""
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for df in (-detune, 0.0, detune):
        f = freq + df
        out += (np.sin(2 * np.pi * f * t)
                + 0.18 * np.sin(2 * np.pi * 2 * f * t)
                + 0.06 * np.sin(2 * np.pi * 3 * f * t))
    return out / 3.0


def _kick(dur: float = 0.22) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    freq = 95 * np.exp(-t * 18) + 42
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 16)


def _hat(dur: float = 0.05, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    noise = rng.standard_normal(n)
    noise = np.diff(noise, prepend=0.0)          # crude highpass
    return noise * np.exp(-np.arange(n) / (0.012 * SR)) * 0.5


def render_loop() -> np.ndarray:
    """One 8-bar progression (~22.3s), seamless when repeated."""
    total = int(8 * BAR * SR)
    mix = np.zeros(total)

    # pads + bass, two bars per chord
    for i, (chord, bass) in enumerate(zip(CHORDS, BASS)):
        start = int(i * 2 * BAR * SR)
        dur = 2 * BAR + 0.6                       # overlap into next chord
        seg_n = min(int(dur * SR), total - start)
        env = _env(seg_n, attack=0.9, release=1.1)
        voice = sum(_mellow(f, dur)[:seg_n] for f in chord) / len(chord)
        mix[start:start + seg_n] += 0.32 * voice * env
        b = _mellow(bass, dur, detune=0.05)[:seg_n]
        mix[start:start + seg_n] += 0.22 * b * env

    # drums: kick on 1 and 3, hat on offbeats — very quiet, lo-fi
    kick, = (_kick(),)
    for bar in range(8):
        for beat in (0, 2):
            p = int((bar * BAR + beat * BEAT) * SR)
            mix[p:p + len(kick)] += 0.16 * kick[:max(0, min(len(kick), total - p))]
        for off in (0.5, 1.5, 2.5, 3.5):
            h = _hat(seed=bar * 7 + int(off * 2))
            p = int((bar * BAR + off * BEAT) * SR)
            mix[p:p + len(h)] += 0.05 * h[:max(0, min(len(h), total - p))]

    # gentle "breathing" volume (fake sidechain) once per bar
    t = np.arange(total) / SR
    lfo = 0.88 + 0.12 * (1 - np.exp(-np.mod(t, BAR) * 6))
    mix *= lfo

    mix /= np.max(np.abs(mix)) / 0.5              # peak -6 dBFS
    return mix


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("assets/music/bg.mp3")
    minutes = 1.5
    if "--minutes" in sys.argv:
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])

    loop = render_loop()
    reps = max(1, int(minutes * 60 / (len(loop) / SR)) + 1)
    audio = np.tile(loop, reps)
    fade = int(1.5 * SR)
    audio[-fade:] *= np.linspace(1, 0, fade)

    pcm = (audio * 32767).astype(np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-i", wav_path, "-af", "aecho=0.7:0.6:60:0.18",
                    "-b:a", "160k", str(out)], check=True, capture_output=True)
    Path(wav_path).unlink(missing_ok=True)
    print(f"wrote {out} ({len(audio)/SR:.1f}s)")


if __name__ == "__main__":
    main()
