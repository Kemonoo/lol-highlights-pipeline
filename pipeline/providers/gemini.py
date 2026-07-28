"""Google Gemini provider - text, images, native video, and image generation.

Gemini is the default for the `judge` role because it is the only widely-available API
that ingests a whole video with its audio and answers questions about it. That single
capability is what lets the pipeline ask "is this play actually good?" instead of
inferring entertainment from motion vectors.

Two upload paths, chosen by size:
  inline     base64 in the request body - cap raised from 20MB to 100MB in Jan 2026
  Files API  for anything larger (2GB cap); files auto-expire after 48h

Structured output: passing a schema sets responseMimeType/responseSchema, so the model
returns parseable JSON instead of prose we have to scrape.

Thinking: Gemini 3.x reasons before answering by default. For the judge that is mostly
wasted - measured on a scoring prompt, `low` cut 409 total tokens to 92 and 4.2s to 2.3s
with identical scores. Set per role via llm.roles.<role>.thinking.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import time
from pathlib import Path

import requests

from .base import (
    Provider,
    ProviderError,
    ProviderUnavailable,
    RoleConfig,
    _Retryable,
    raise_for_status,
)

log = logging.getLogger("pipeline.providers.gemini")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Gemini wants uppercase JSON-Schema-ish type names and rejects several standard keys
# (additionalProperties, $schema, ...). Translate rather than make callers write two
# schema dialects.
_TYPES = {"object": "OBJECT", "array": "ARRAY", "string": "STRING",
          "integer": "INTEGER", "number": "NUMBER", "boolean": "BOOLEAN"}
_ALLOWED = {"type", "properties", "items", "enum", "required", "description", "nullable"}

_THINKING = {"off": "low", "low": "low", "medium": "high", "high": "high"}


def to_gemini_schema(schema: dict) -> dict:
    """Translate a plain JSON Schema into Gemini's responseSchema dialect."""
    out: dict = {}
    for k, v in schema.items():
        if k not in _ALLOWED:
            continue
        if k == "type":
            out[k] = _TYPES.get(str(v).lower(), str(v).upper())
        elif k == "properties":
            out[k] = {pk: to_gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = to_gemini_schema(v)
        else:
            out[k] = v
    return out


class GeminiProvider(Provider):
    name = "gemini"
    supports_images = True
    supports_video = True
    supports_image_generation = True      # only true for *-image models; see _generate_image

    # Inline payload budget. Files API takes over above this.
    INLINE_MAX_MB = 90

    def __init__(self, rc: RoleConfig):
        super().__init__(rc)
        if not rc.api_key:
            raise ProviderUnavailable(
                f"role '{rc.role}' is set to provider: gemini but no API key was found. "
                "Set GEMINI_API_KEY in .env (get one at https://aistudio.google.com/apikey), "
                "or point the role at a local provider."
            )
        self._key = rc.api_key

    # ── request plumbing ──────────────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/models/{self.rc.model}:{method}?key={self._key}"

    def _post(self, method: str, body: dict, timeout: float | None = None) -> dict:
        try:
            resp = requests.post(self._url(method), json=body,
                                 timeout=timeout or self.rc.timeout_s)
        except requests.RequestException as e:
            raise _Retryable(f"network error: {e}") from e
        raise_for_status(resp, f"gemini {self.rc.model}")
        return resp.json()

    def _generation_config(self, schema: dict | None) -> dict:
        gc: dict = {}
        if schema:
            gc["responseMimeType"] = "application/json"
            gc["responseSchema"] = to_gemini_schema(schema)
        level = _THINKING.get((self.rc.thinking or "").lower())
        if level:
            gc["thinkingConfig"] = {"thinkingLevel": level}
        return gc

    @staticmethod
    def _text_of(data: dict) -> str:
        cands = data.get("candidates") or []
        if not cands:
            fb = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(f"no candidates returned{f' (blocked: {fb})' if fb else ''}")
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text and cand.get("finishReason") not in (None, "STOP"):
            raise ProviderError(f"empty response (finishReason: {cand['finishReason']})")
        return text

    # ── file upload ───────────────────────────────────────────────────────────

    def upload_file(self, path: Path, mime: str | None = None) -> str:
        """Resumable upload via the Files API. Returns a file URI usable in a request.

        Used for media too large to inline. Google deletes uploaded files after 48h, so
        there is nothing to clean up."""
        mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        start = requests.post(
            f"{API_ROOT}/files?key={self._key}",
            headers={"X-Goog-Upload-Protocol": "resumable",
                     "X-Goog-Upload-Command": "start",
                     "X-Goog-Upload-Header-Content-Length": str(size),
                     "X-Goog-Upload-Header-Content-Type": mime},
            json={"file": {"display_name": path.name}},
            timeout=self.rc.timeout_s,
        )
        raise_for_status(start, "gemini files.start")
        session = start.headers.get("X-Goog-Upload-URL")
        if not session:
            raise ProviderError("Files API did not return an upload URL")

        up = requests.post(
            session,
            headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                     "X-Goog-Upload-Command": "upload, finalize"},
            data=path.read_bytes(), timeout=max(self.rc.timeout_s, 300),
        )
        raise_for_status(up, "gemini files.upload")
        info = up.json().get("file", {})
        uri, name = info.get("uri"), info.get("name")
        if not uri:
            raise ProviderError("Files API returned no file URI")

        # Video must finish PROCESSING before it can be referenced.
        for _ in range(60):
            if info.get("state") == "ACTIVE":
                return uri
            if info.get("state") == "FAILED":
                raise ProviderError("Files API processing failed")
            time.sleep(2)
            got = requests.get(f"{API_ROOT}/{name}?key={self._key}", timeout=30)
            if got.status_code < 400:
                info = got.json()
        raise ProviderError("Files API processing timed out")

    def _media_part(self, blob: bytes, mime: str) -> dict:
        """Inline the blob, or push it through the Files API when it's too big."""
        if len(blob) <= self.INLINE_MAX_MB * 1024 * 1024:
            return {"inline_data": {"mime_type": mime,
                                    "data": base64.b64encode(blob).decode()}}
        import tempfile
        suffix = ".mp4" if mime.startswith("video/") else ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(blob)
            tmp = Path(fh.name)
        try:
            log.info("  %.0fMB exceeds the inline cap - using the Files API",
                     len(blob) / 1024 / 1024)
            return {"file_data": {"mime_type": mime, "file_uri": self.upload_file(tmp, mime)}}
        finally:
            tmp.unlink(missing_ok=True)

    # ── Provider hooks ────────────────────────────────────────────────────────

    def _complete(self, prompt: str, *, images, video, system, schema) -> str:
        parts: list[dict] = []
        for img in images or []:
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(img).decode()}})
        if video:
            parts.append(self._media_part(video, "video/mp4"))
        parts.append({"text": prompt})

        body: dict = {"contents": [{"parts": parts}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        gc = self._generation_config(schema)
        if gc:
            body["generationConfig"] = gc
        # Video judging is slow; give it room beyond the role's default timeout.
        timeout = max(self.rc.timeout_s, 300) if video else self.rc.timeout_s
        return self._text_of(self._post("generateContent", body, timeout))

    def _generate_image(self, prompt: str, *, images) -> bytes:
        if "image" not in self.rc.model:
            raise ProviderUnavailable(
                f"role '{self.rc.role}' uses model '{self.rc.model}', which is not an image "
                "generation model. Use e.g. gemini-3.1-flash-image."
            )
        parts: list[dict] = []
        for img in images or []:
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(img).decode()}})
        parts.append({"text": prompt})
        data = self._post("generateContent", {"contents": [{"parts": parts}]},
                          max(self.rc.timeout_s, 180))
        cands = data.get("candidates") or []
        for part in ((cands[0].get("content") or {}).get("parts") or []) if cands else []:
            blob = part.get("inline_data") or part.get("inlineData")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
        reason = cands[0].get("finishReason") if cands else "no candidates"
        # IMAGE_OTHER is usually the recitation filter: naming IP or feeding copyrighted
        # art. thumbnail.py deliberately keeps prompts generic for this reason.
        raise ProviderError(
            f"no image in response (finishReason: {reason}). If this is IMAGE_OTHER, the "
            "prompt likely tripped the recitation filter - keep prompts free of "
            "trademarked names and source art."
        )

    def health(self) -> tuple[bool, str]:
        try:
            resp = requests.get(f"{API_ROOT}/models/{self.rc.model}?key={self._key}", timeout=15)
        except requests.RequestException as e:
            return False, f"cannot reach the Gemini API ({e})"
        if resp.status_code == 404:
            return False, (f"model '{self.rc.model}' does not exist or was retired - "
                           "see https://ai.google.dev/gemini-api/docs/deprecations")
        if resp.status_code in (401, 403):
            return False, "API key rejected (check GEMINI_API_KEY)"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}"
        return True, f"{self.rc.model} reachable"
