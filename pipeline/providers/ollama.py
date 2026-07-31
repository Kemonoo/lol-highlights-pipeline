"""Ollama provider - free local inference, and the backbone of the cost cascade.

The `vlm` role runs here by default. It sees roughly ten times as many clips as the paid
judge does, so keeping it local is what makes the whole pipeline cost pennies a day
rather than dollars. Treat "vlm stays free" as a design constraint, not a default.

Model selection: `model: auto` picks the best vision model already installed, preferring
larger ones that still plausibly fit consumer VRAM. Nothing is pulled automatically - a
pipeline run should never silently download 20GB.

Structured output: Ollama accepts a JSON Schema in `format`, which constrains decoding.
That matters much more for small local models than for frontier ones - a 4B model asked
for JSON in prose will happily emit prose about half the time.
"""
from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests

from .base import Provider, ProviderUnavailable, RoleConfig, _Retryable, raise_for_status

log = logging.getLogger("pipeline.providers.ollama")

DEFAULT_URL = "http://localhost:11434"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_autostart_tried = False


def _is_local(base: str) -> bool:
    return (urlparse(base).hostname or "").lower().strip("[]") in _LOCAL_HOSTS


def _start_server(base: str, timeout: float = 240.0) -> bool:
    """Launch `ollama serve` and wait for it to answer. True if it came up.

    Unattended runs are why this exists. A stock Ollama install registers no Windows
    service and no Run key, so it is only up while somebody has the tray app open —
    close it, or reboot and don't log in, and every 03:00 run dies at the preflight
    with "cannot reach Ollama". That failure cost three days of uploads and looks
    identical to a broken pipeline in the logs.

    Attempted once per process, and only for a local host: if the operator pointed the
    role at a server on another machine, that server not being up is their business
    and starting a local one would silently use the wrong models.
    """
    global _autostart_tried
    if _autostart_tried:
        return False
    _autostart_tried = True

    exe = shutil.which("ollama")
    if not exe:
        return False
    log.info("Ollama not responding at %s — starting it", base)

    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        # Detached, no console: the server has to outlive this run, and a console
        # window would block an unattended session.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([exe, "serve"], **kwargs)
    except OSError as e:
        log.warning("could not start ollama: %s", e)
        return False

    # Measured on the dev box: ~30s for a warm restart, but the first start after the
    # machine has been idle for days ran past 90s — it checks for an update first.
    # A generous ceiling costs nothing except in the case where it was never going to work.
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            if requests.get(f"{base}/api/tags", timeout=5).ok:
                log.info("Ollama is up (%.0fs)", time.time() - started)
                return True
        except requests.RequestException:
            pass
        waited = time.time() - started
        if waited > 30 and int(waited) % 30 < 2:      # don't look hung in the log
            log.info("  still waiting for Ollama (%.0fs)", waited)
    log.warning("started ollama but it did not answer within %.0fs", timeout)
    return False

# Best-first. Vision-capable models only - the vlm role is the reason this provider exists.
PREFERRED_VISION = [
    "qwen3-vl:8b", "qwen3-vl:4b", "qwen3-vl:2b",
    "qwen2.5vl:7b", "qwen2.5vl:3b", "qwen2.5-vl:7b",
    "llama3.2-vision:11b", "minicpm-v", "moondream",
    "llava:13b", "llava:7b", "llava",
]


class OllamaProvider(Provider):
    name = "ollama"
    supports_images = True
    supports_video = False            # no local model here ingests video directly
    supports_image_generation = False

    def __init__(self, rc: RoleConfig, *, vision: bool = False):
        super().__init__(rc)
        self.base = (rc.base_url or DEFAULT_URL).rstrip("/")
        self._vision = vision
        self._model = rc.model
        if not self._model or self._model == "auto":
            self._model = self._autoselect()
            log.info("role '%s': auto-selected local model %s", rc.role, self._model)

    def _tags(self) -> set[str]:
        try:
            resp = requests.get(f"{self.base}/api/tags", timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            # _start_server only ever fires once per process, so this recurses at most once.
            if self.rc.autostart and _is_local(self.base) and _start_server(self.base):
                return self._tags()
            raise ProviderUnavailable(
                f"cannot reach Ollama at {self.base} - is it running? Start the Ollama app "
                f"(or `ollama serve`), then re-run. ({e})"
            ) from e
        names = {m["name"] for m in resp.json().get("models", [])}
        return names | {n.split(":latest")[0] for n in names}

    def _autoselect(self) -> str:
        available = self._tags()
        if self._vision:
            for m in PREFERRED_VISION:
                if m in available:
                    return m
            raise ProviderUnavailable(
                f"no vision model installed in Ollama at {self.base}.\n"
                f"  installed: {sorted(available) or 'nothing'}\n"
                f"  fix: ollama pull qwen3-vl:4b     (~3GB, fits 4GB VRAM)"
            )
        if not available:
            raise ProviderUnavailable(
                f"no models installed in Ollama at {self.base}. Try: ollama pull qwen2.5:7b")
        for m in ("qwen2.5:7b", "llama3.1:8b", "mistral"):
            if m in available:
                return m
        return sorted(available)[0]

    def _complete(self, prompt: str, *, images, video, system, schema) -> str:
        if video:
            raise ProviderUnavailable("Ollama models cannot ingest video")

        text = self._chat(prompt, images=images, system=system, schema=schema)
        if not text.strip() and schema:
            # Constrained decoding produced nothing — retry ONCE, immediately,
            # unconstrained. (Ollama 0.32's legacy /api/generate did this for every
            # call that combined `format` with images.)
            log.warning("%s returned empty output under a schema - retrying without one",
                        self._model)
            text = self._chat(prompt, images=images, system=system, schema=None)

        # An empty answer is NOT retried further and NOT an error: for the kill-feed
        # crops it is the ordinary way of saying "nothing here", and small models emit
        # it often. Backing off 20s+ on each one stalls the stage for hours.
        # Callers decide what silence means — vlm_filter.detect() raises only when
        # *every* frame of a clip came back unusable.
        return text

    def _chat(self, prompt: str, *, images, system, schema) -> str:
        """One /api/chat call. NB: /api/chat, not the older /api/generate — the latter
        silently returns an empty string when `format` is used together with images."""
        message: dict = {"role": "user", "content": prompt}
        if images:
            message["images"] = [base64.b64encode(i).decode() for i in images]
        messages = ([{"role": "system", "content": system}] if system else []) + [message]

        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if schema:
            body["format"] = schema          # constrained decoding
        try:
            resp = requests.post(f"{self.base}/api/chat", json=body,
                                 timeout=max(self.rc.timeout_s, 600))
        except requests.RequestException as e:
            raise _Retryable(f"network error: {e}") from e
        raise_for_status(resp, f"ollama {self._model}")
        return (resp.json().get("message") or {}).get("content") or ""

    def health(self) -> tuple[bool, str]:
        try:
            available = self._tags()
        except ProviderUnavailable as e:
            return False, str(e).split("\n")[0]
        if self._model not in available:
            return False, (f"model '{self._model}' is not installed - "
                           f"fix: ollama pull {self._model}")
        return True, f"{self._model} installed at {self.base}"
