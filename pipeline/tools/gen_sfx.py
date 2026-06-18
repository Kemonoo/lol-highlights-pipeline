"""Generate a short, subtle UI 'notification' sound for the nameplate flush-in.

100% synthesized (numpy) -> zero licensing risk, no attribution, matches the
in-house-audio policy used by tools/gen_music.py. Swap the WAV freely if you
prefer a downloaded effect.

    python -m pipeline.tools.gen_sfx              # -> assets/sfx/nameplate.wav
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT = ROOT / "assets" / "sfx" / "nameplate.wav"
SR = 48000


def generate(out_path: Path = DEFAULT) -> Path:
    """Synthesize a soft two-pip 'tech blip' (~0.22s) and write it as a WAV."""
    import numpy as np
    import soundfile as sf

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dur = 0.22
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)

    def pip(f0, f1, start, length, gain):
        seg = np.zeros_like(t)
        m = (t >= start) & (t < start + length)
        tt = t[m] - start
        freq = f0 + (f1 - f0) * (tt / length)
        env = np.exp(-tt * 26) * np.sin(np.pi * np.clip(tt / length, 0, 1)) ** 0.4
        # gentle timbre: fundamental + soft 2nd harmonic
        tone = np.sin(2 * np.pi * freq * tt) + 0.25 * np.sin(4 * np.pi * freq * tt)
        seg[m] = tone * env * gain
        return seg

    sig = pip(720, 1180, 0.000, 0.085, 0.9) + pip(1180, 1480, 0.055, 0.090, 0.6)

    # tiny filtered-noise tick on the attack for a 'digital' edge
    tick = np.zeros_like(t)
    m = t < 0.006
    rng = np.random.default_rng(7)
    tick[m] = rng.standard_normal(m.sum()) * np.exp(-t[m] * 600) * 0.18
    sig += tick

    sig /= (np.max(np.abs(sig)) + 1e-9)
    sig *= 0.6                                   # leave headroom; assemble ducks further

    # subtle stereo width: ~0.4ms haas delay on the right channel
    d = int(SR * 0.0004)
    right = np.concatenate([np.zeros(d), sig])[:len(sig)]
    stereo = np.stack([sig, right], axis=1).astype(np.float32)

    sf.write(str(out_path), stereo, SR)
    return out_path


def ensure(path: Path = DEFAULT) -> Path | None:
    """Return path, generating the WAV if missing. None if synthesis unavailable."""
    path = Path(path)
    if path.exists():
        return path
    try:
        return generate(path)
    except Exception:
        return None


if __name__ == "__main__":
    print("wrote", generate())
