"""Clip-to-clip transitions for the long-form video (owner picks, 2026-09-25).

Three kinds, all CUT-BASED: each boundary is split into an OUT half (the last frames of
clip N) and an IN half (the first frames of clip N+1) that meet at a hard cut. Nothing
overlaps, so every segment still renders on its own and concat keeps its `-c copy`.
  glitch    2 frames of red/blue split out, 3 frames in (no static, no noise)
  whip      the old clip blurs sideways and scrolls away; the new one arrives blurred
            and sharpens (the classic "whip cut" — both sides of a hard cut)
  pixelate  blocks grow to the cut and shrink after it

Pattern: a seeded weighted draw per boundary, never the same kind twice in a row. The
common editing advice is one "signature" transition with accents, so glitch weighs
double by default. Seeded by the date so a re-render (the bat's crash-retry, a --force)
picks the same transitions and cached segments stay valid.

Pure string builders — assemble applies them (tests/test_transitions.py).
"""
import random

FPS_FRAME = 1 / 30
DEFAULT_WEIGHTS = {"glitch": 2, "whip": 1, "pixelate": 1}
TAIL_S = {"glitch": 2 * FPS_FRAME, "whip": 0.2, "pixelate": 0.2}
HEAD_S = {"glitch": 3 * FPS_FRAME, "whip": 0.2, "pixelate": 0.2}
AUDIO_FADE_S = 0.05            # declick only; the cut itself is the transition


def plan(n_clips: int, seed: str, weights: dict | None = None) -> list[str]:
    """-> one kind per boundary (n_clips - 1), no kind twice in a row."""
    w = {k: float(v) for k, v in (weights or DEFAULT_WEIGHTS).items()
         if k in TAIL_S and float(v) > 0}
    if not w:
        return []
    rng = random.Random(f"transitions:{seed}")
    out: list[str] = []
    for _ in range(max(n_clips - 1, 0)):
        pool = [k for k in w if not out or k != out[-1]] or list(w)
        out.append(rng.choices(pool, weights=[w[k] for k in pool])[0])
    return out


def edges(kinds: list[str], i: int) -> tuple[str | None, str | None]:
    """(head, tail) kind of clip i: the boundary before it and the one after it."""
    head = kinds[i - 1] if 0 < i <= len(kinds) else None
    tail = kinds[i] if 0 <= i < len(kinds) else None
    return head, tail


def _steps(fmt: str, values: list, a: float, b: float) -> list[str]:
    """`fmt` with each value, each enabled for an equal slice of [a, b]."""
    n = len(values)
    out = []
    for k, val in enumerate(values):
        s, e = a + (b - a) * k / n, a + (b - a) * (k + 1) / n
        out.append(fmt.format(val) + f":enable='between(t,{s:.3f},{e:.3f})'")
    return out


def head_vf(kind: str | None) -> list[str]:
    """Filters for the first frames of a clip (t from 0)."""
    d = HEAD_S.get(kind or "", 0)
    if kind == "glitch":
        return [f"rgbashift=rh=16:bh=-16:enable='lte(t,{d:.3f})'"]
    if kind == "whip":
        return _steps("gblur=sigma={}:sigmaV=0.5:steps=2", [70, 35, 12], 0, d)
    if kind == "pixelate":
        return _steps("pixelize=w={0}:h={0}", [48, 24, 12, 6], 0, d)
    return []


def tail_vf(kind: str | None, dur: float) -> list[str]:
    """Filters for the last frames of a clip that lasts `dur` seconds."""
    d = TAIL_S.get(kind or "", 0)
    a = max(dur - d, 0)
    if kind == "glitch":
        return [f"rgbashift=rh=-16:bh=16:enable='gte(t,{a:.3f})'"]
    if kind == "whip":
        return ([f"scroll=h=-0.07:enable='gte(t,{a:.3f})'"]
                + _steps("gblur=sigma={}:sigmaV=0.5:steps=2", [12, 35, 70], a, dur + 0.01))
    if kind == "pixelate":
        return _steps("pixelize=w={0}:h={0}", [6, 12, 24, 48], a, dur + 0.01)
    return []
