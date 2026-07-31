"""Hardware detection - so a better machine actually buys you something.

Everything here is cached per process and degrades to a safe answer: if detection
fails for any reason we return the software/CPU path rather than raising, because a
misdetected GPU must never be the reason a run dies.

  ffmpeg_encoders()   available H.264 encoder names
  pick_encoder(cfg)   video.encoder resolved ("auto" -> best available)
  encoder_args(cfg)   ready-made ffmpeg flags for that encoder
  gpu()               (name, vram_mb) or None
  whisper_device(cfg) resolved (device, compute_type) for faster-whisper
"""
from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("pipeline.hardware")

# Best first. NVENC on any modern GeForce/Quadro, QSV on Intel iGPUs, VideoToolbox on
# macOS (Apple Silicon and recent Intel Macs).
_HW_ENCODERS = ["h264_nvenc", "h264_qsv", "h264_videotoolbox", "h264_amf"]

# Quality knobs differ per encoder - CRF is libx264-only. These target visually
# equivalent output to the libx264 crf=20 the pipeline used everywhere before.
_ENCODER_ARGS = {
    "libx264":            ["-crf", "20"],
    "h264_nvenc":         ["-rc", "vbr", "-cq", "20", "-b:v", "0"],
    "h264_qsv":           ["-global_quality", "20"],
    "h264_videotoolbox":  ["-q:v", "55"],
    "h264_amf":           ["-rc", "cqp", "-qp_i", "20", "-qp_p", "20"],
}

# `-preset` means different things per encoder, and passing an x264 preset to NVENC
# fails outright on older ffmpeg builds. Map the config value onto each family.
_NVENC_PRESETS = {"ultrafast": "p1", "superfast": "p1", "veryfast": "p2",
                  "faster": "p3", "fast": "p4", "medium": "p4", "slow": "p5",
                  "slower": "p6", "veryslow": "p7"}


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders() -> frozenset[str]:
    """H.264 encoders this ffmpeg build offers. Empty set if ffmpeg is missing."""
    if not have("ffmpeg"):
        return frozenset()
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("encoder probe failed: %s", e)
        return frozenset()
    found = set()
    for line in out.splitlines():
        parts = line.split()
        # rows look like:  " V....D h264_nvenc   NVIDIA NVENC H.264 encoder"
        if len(parts) >= 2 and parts[0].startswith("V"):
            if parts[1] == "libx264" or parts[1] in _HW_ENCODERS:
                found.add(parts[1])
    return frozenset(found)


def _encoder_works(name: str) -> bool:
    """Listed != usable - NVENC shows up on machines with no NVIDIA driver loaded.
    Encode one synthetic frame and see."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
             "-c:v", name, "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@functools.lru_cache(maxsize=8)
def pick_encoder(configured: str = "auto") -> str:
    """Resolve video.encoder. Falls back to libx264 whenever anything is uncertain."""
    available = ffmpeg_encoders()
    if configured and configured != "auto":
        if configured in available or not available:
            return configured
        log.warning("video.encoder '%s' is not in this ffmpeg build (%s) - using libx264",
                    configured, ", ".join(sorted(available)) or "none detected")
        return "libx264"
    for name in _HW_ENCODERS:
        if name in available and _encoder_works(name):
            log.info("hardware video encoding: %s", name)
            return name
    return "libx264"


def args_for_encoder(name: str, preset: str = "veryfast") -> list[str]:
    """ffmpeg flags for a named encoder: -c:v, quality, and a preset it accepts.

    Pure — no probing — so the mapping is testable on any machine."""
    args = ["-c:v", name, *_ENCODER_ARGS.get(name, _ENCODER_ARGS["libx264"])]
    if name in ("libx264", "h264_qsv"):
        args += ["-preset", preset]
    elif name == "h264_nvenc":
        # An x264 preset name is rejected outright by older NVENC builds.
        args += ["-preset", _NVENC_PRESETS.get(preset, "p4")]
    # videotoolbox / amf take no preset
    return args


def encoder_args(v: dict, threads: int = 0) -> list[str]:
    """ffmpeg flags for the `video` config section, resolving encoder: auto.

    Every segment in a run must go through this so the concat demuxer sees identical
    codec parameters. `threads` caps decode/filter threads (0 = ffmpeg's default)."""
    args = args_for_encoder(pick_encoder(str(v.get("encoder", "auto"))),
                            str(v.get("preset", "veryfast")))
    return [*args, "-threads", str(threads)] if threads else args


@functools.lru_cache(maxsize=1)
def gpu() -> tuple[str, int] | None:
    """(name, total VRAM in MB) for the first NVIDIA GPU, or None.

    nvidia-smi only - the point is picking model sizes for CUDA workloads (Ollama,
    faster-whisper), and those are CUDA-only anyway."""
    if not have("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    name, _, mem = out.splitlines()[0].partition(",")
    try:
        return name.strip(), int(mem.strip())
    except ValueError:
        return name.strip(), 0


_dll_dirs_registered = False


def register_cuda_dll_dirs() -> list[str]:
    """Windows: make pip-installed CUDA libraries loadable. Returns the dirs added.

    `pip install nvidia-cudnn-cu12` puts the DLLs in site-packages/nvidia/*/bin, which
    is NOT on the DLL search path. So `import nvidia.cudnn` succeeds while ctranslate2
    still dies hunting for cudnn64_9.dll — that is the "cudnnGetLibConfig, err 127"
    crash, and it is why installing the package alone never fixed it. os.add_dll_directory
    is the supported fix and, unlike editing PATH, is scoped to this process.

    Idempotent, and a no-op off Windows (those wheels carry usable rpaths).
    """
    global _dll_dirs_registered
    if _dll_dirs_registered or sys.platform != "win32":
        return []
    _dll_dirs_registered = True
    added = []
    try:
        import nvidia
    except ImportError:
        return []
    for root in nvidia.__path__:
        for sub in ("cudnn", "cublas", "cuda_runtime"):
            d = Path(root) / sub / "bin"
            if d.is_dir():
                try:
                    os.add_dll_directory(str(d))
                    added.append(str(d))
                except OSError:                      # pragma: no cover - path vanished
                    pass
    if added:
        log.debug("registered CUDA dll dirs: %s", added)
    return added


@functools.lru_cache(maxsize=1)
def cuda_ready() -> tuple[bool, str]:
    """Can faster-whisper actually use CUDA here? (ok, reason).

    The historical failure was a missing cuDNN 9 surfacing as a hard crash mid-run
    ("cudnnGetLibConfig", err 127) rather than a clean exception. So this does not ask
    whether the python package imports — it asks whether the DLL actually LOADS, which
    is the thing ctranslate2 will need a few seconds later."""
    if gpu() is None:
        return False, "no NVIDIA GPU detected"
    try:
        import ctranslate2
    except ImportError:
        return False, "ctranslate2 not installed (pip install faster-whisper)"
    try:
        if ctranslate2.get_cuda_device_count() < 1:
            return False, "ctranslate2 sees no CUDA device"
    except Exception as e:
        return False, f"ctranslate2 CUDA probe failed ({e})"
    try:
        import nvidia.cudnn  # noqa: F401
    except ImportError:
        return False, ("cuDNN 9 not found - fix: pip install nvidia-cudnn-cu12 "
                       "(without it CUDA transcription crashes mid-run)")
    register_cuda_dll_dirs()
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.WinDLL("cudnn_ops64_9.dll")
        except OSError as e:
            return False, (f"cuDNN 9 is installed but its DLL will not load ({e}) - "
                           f"the GPU path would crash mid-run, staying on CPU")
    return True, "CUDA available"


def whisper_device(cfg: dict) -> tuple[str, str]:
    """Resolve transcribe.device/compute_type, honouring explicit settings."""
    tc = cfg.get("transcribe", {})
    device = str(tc.get("device", "auto")).lower()
    compute = str(tc.get("compute_type", "auto")).lower()

    if device == "auto":
        ok, reason = cuda_ready()
        device = "cuda" if ok else "cpu"
        if not ok:
            log.info("transcribe: using CPU (%s)", reason)
    if compute == "auto":
        compute = "int8_float16" if device == "cuda" else "int8"
    return device, compute


def cpu_budget(cfg: dict) -> int:
    """How many threads CPU-bound work may use. 0 = unlimited (ffmpeg's own default)."""
    pct = int((cfg.get("resources") or {}).get("cpu_percent", 100))
    if pct >= 100:
        return 0
    cores = os.cpu_count() or 4
    return max(1, round(cores * pct / 100))


def apply_limits(cfg: dict) -> None:
    """Cap CPU-bound work and optionally de-prioritise the process.

    Called once at the top of a run, BEFORE the heavy libraries are imported — the
    thread-count environment variables are read by OpenMP/BLAS at import time, so
    setting them later has no effect.

    This throttles CPU only. Ollama and NVENC do their work on the GPU in another
    process; there is no portable way to cap that from here, but neither competes for
    the cores your foreground applications need."""
    res = cfg.get("resources") or {}
    threads = cpu_budget(cfg)

    # Stashed on the video config so assemble.enc() and the other ffmpeg call sites
    # inherit the cap without a parameter threaded through the whole render path.
    cfg.setdefault("video", {})["_cpu_threads"] = threads

    if threads:
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ.setdefault(var, str(threads))
        try:                                # opencv keeps its own pool
            import cv2
            cv2.setNumThreads(threads)
        except Exception:
            pass
        log.info("CPU budget: %d of %d cores (%s%%)",
                 threads, os.cpu_count() or 0, res.get("cpu_percent"))

    if res.get("low_priority", False):
        _lower_priority()


def _lower_priority() -> None:
    """Drop to below-normal scheduling priority. Child processes (ffmpeg) inherit it,
    so the whole run yields to whatever you're doing in the foreground."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            # argtypes/restype must be declared: GetCurrentProcess returns a HANDLE
            # (pointer-sized), and ctypes' default c_int return truncates it on 64-bit,
            # so SetPriorityClass gets a bogus handle and fails.
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.GetCurrentProcess.argtypes = []
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.SetPriorityClass.restype = wintypes.BOOL
            if not k32.SetPriorityClass(k32.GetCurrentProcess(),
                                        BELOW_NORMAL_PRIORITY_CLASS):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.nice(10)
        log.info("running at below-normal priority")
    except Exception as e:                 # never let a nicety stop the run
        log.warning("could not lower process priority: %s", e)


def summary() -> dict:
    """Everything detected, for the doctor report."""
    g = gpu()
    return {
        "ffmpeg": have("ffmpeg"),
        "ffprobe": have("ffprobe"),
        "encoders": sorted(ffmpeg_encoders()),
        "encoder_auto": pick_encoder("auto") if have("ffmpeg") else None,
        "gpu": g,
        "cuda": cuda_ready(),
    }
