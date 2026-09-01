"""Provider interface - one shape for every model call in the pipeline.

Stages ask for a ROLE ("judge", "vlm", "commentary", ...) and get back something that
can answer prompts. Which vendor actually serves that role is a config decision
(llm.roles.<role> in config.yaml), not a code decision, so swapping Gemini for a local
model - or for OpenRouter, Groq, LM Studio, vLLM - never touches a stage.

Two capabilities matter and not every provider has them:
    supports_images           frames in  (vlm role)
    supports_video            whole clip in  (judge role - Gemini only, today)
    supports_image_generation image out (thumbnail role)

Stages must degrade rather than crash when a capability is missing: the pipeline is
designed to produce a video with no API keys at all, just a less well-curated one.
Call `require()` to check up front and handle ProviderUnavailable.
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("pipeline.providers")


class ProviderError(RuntimeError):
    """A call failed after exhausting retries.

    Carries the HTTP status and the provider's own refusal reason where there was one,
    so callers can tell "this key is dead" from "this one clip was refused" without
    grepping the message text. api_judge needs that distinction: it stops calling the
    API after repeated failures, and a per-clip refusal must not trigger that.
    """

    def __init__(self, msg: str, *, status: int | None = None, reason: str = "",
                 quota_id: str = ""):
        super().__init__(msg)
        self.status = status      # HTTP status, when the failure came from a response
        self.reason = reason      # provider finishReason, e.g. RECITATION
        self.quota_id = quota_id  # e.g. GenerateRequestsPerDayPerProjectPerModel-FreeTier


class ProviderUnavailable(ProviderError):
    """The provider cannot run at all: no key, unreachable host, missing capability.

    Stages should catch this and fall back to their local path rather than abort."""


@dataclass
class RoleConfig:
    """Resolved llm.roles.<role> entry, with llm.defaults filled in."""
    role: str
    provider: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    thinking: str = ""
    timeout_s: float = 120.0
    max_retries: int = 4
    retry_base_s: float = 20.0
    autostart: bool = True            # local backends only — see ollama._start_server
    fallback: dict | None = field(default=None, repr=False)
    # Extra model ids to move to when this one's DAILY quota is spent. Gemini's free
    # tier is metered PerProjectPerModel, so a second model id is a second allowance —
    # verified: with gemini-3.6-flash 429ing on quota, gemini-3.1-flash-lite and
    # gemini-flash-latest both still answered video on the same key.
    overflow_models: list = field(default_factory=list)

    def describe(self) -> str:
        where = f" @ {self.base_url}" if self.base_url else ""
        return f"{self.provider}:{self.model or 'auto'}{where}"


# Retry only on transient conditions. A 400 (bad request) or 404 (retired model) will
# never succeed on retry, and burning four 180s timeouts on a typo'd model id is the
# kind of thing that makes a pipeline feel broken rather than misconfigured.
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def retrying(fn, rc: RoleConfig, what: str):
    """Run fn(), retrying transient failures with linear backoff."""
    last: Exception | None = None
    for attempt in range(1, rc.max_retries + 1):
        try:
            return fn()
        except ProviderUnavailable:
            raise
        except _Retryable as e:
            last = e.__cause__ or e
            if attempt == rc.max_retries:
                break
            wait = rc.retry_base_s * attempt
            log.info("  %s: %s - retry %d/%d in %.0fs",
                     what, e, attempt, rc.max_retries, wait)
            time.sleep(wait)
        except Exception as e:                       # non-transient: fail fast
            raise ProviderError(f"{what}: {e}", status=getattr(e, "status", None),
                                reason=getattr(e, "reason", ""),
                                quota_id=getattr(e, "quota_id", "")) from e
    # Preserve the cause's status/reason: callers decide whether a dead key, a rate
    # limit, or a single refused clip should stop them, and cannot if we flatten it.
    raise ProviderError(f"{what}: giving up after {rc.max_retries} attempts ({last})",
                        status=getattr(last, "status", None),
                        reason=getattr(last, "reason", ""),
                        quota_id=getattr(last, "quota_id", ""))


class _Retryable(Exception):
    """Internal marker: this failure is worth another attempt."""

    def __init__(self, msg: str, *, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _quota_id(resp) -> str:
    """QuotaFailure.violations[].quotaId from a 429 body, or "" if absent.

    Google puts the only reliable per-day/per-minute discriminator here; the human
    message and the RetryInfo look identical for both.
    """
    try:
        for d in (resp.json().get("error", {}) or {}).get("details", []) or []:
            for v in d.get("violations", []) or []:
                if v.get("quotaId"):
                    return str(v["quotaId"])
    except Exception:
        pass
    return ""


def raise_for_status(resp, what: str) -> None:
    """Turn an HTTP response into either nothing, a _Retryable, or a hard error."""
    if resp.status_code < 400:
        return
    detail = ""
    try:
        body = resp.json()
        detail = (body.get("error", {}) or {}).get("message", "") if isinstance(body, dict) else ""
    except Exception:
        detail = (resp.text or "")[:200]
    msg = f"HTTP {resp.status_code}" + (f" - {detail}" if detail else "")

    # A PER-DAY quota is not a transient condition, however much the response looks like
    # one: Google returns 429 with a RetryInfo of ~20s next to a QuotaFailure whose
    # quotaId says PerDay. Taking that retryDelay at face value spends three more of an
    # allowance that will not refill for hours — on a 20/day free tier that is 15% of the
    # budget burned proving the budget is gone.
    quota_id = _quota_id(resp) if resp.status_code == 429 else ""
    if "PerDay" in quota_id:
        raise ProviderError(f"{what}: {msg}", status=429, quota_id=quota_id)
    if resp.status_code in RETRY_STATUS:
        raise _Retryable(msg, status=resp.status_code)
    if resp.status_code == 404:
        raise ProviderError(
            f"{what}: {msg}\n"
            "  A 404 here almost always means the model id was retired. Model ids live "
            "in llm.roles.* in config.yaml; check "
            "https://ai.google.dev/gemini-api/docs/deprecations for Gemini."
        , status=404)
    raise ProviderError(f"{what}: {msg}", status=resp.status_code)


def parse_json(text: str) -> dict | None:
    """Best-effort JSON extraction, for providers without structured-output support.

    Providers that DO support it (all of them, currently) pass a schema instead and
    never reach this. Kept because local models via Ollama still wrap JSON in prose or
    markdown fences often enough to matter."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


class Provider(ABC):
    """Base class. Subclasses implement _complete / _generate_image as they can."""

    name = "base"
    supports_images = False
    supports_video = False
    supports_image_generation = False

    def __init__(self, rc: RoleConfig):
        self.rc = rc

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.rc.describe()}>"

    # ── capability guard ──────────────────────────────────────────────────────

    def require(self, *, images: bool = False, video: bool = False,
                image_generation: bool = False) -> None:
        """Raise ProviderUnavailable unless this provider can do what's asked."""
        missing = []
        if images and not self.supports_images:
            missing.append("image input")
        if video and not self.supports_video:
            missing.append("video input")
        if image_generation and not self.supports_image_generation:
            missing.append("image generation")
        if missing:
            raise ProviderUnavailable(
                f"role '{self.rc.role}' is configured as {self.rc.describe()}, which does "
                f"not support {' and '.join(missing)}."
            )

    # ── the interface stages actually use ─────────────────────────────────────

    def complete(self, prompt: str, *, images: list[bytes] | None = None,
                 video: bytes | None = None, system: str = "",
                 schema: dict | None = None) -> str:
        """Prompt in, text out."""
        if images:
            self.require(images=True)
        if video:
            self.require(video=True)
        return retrying(
            lambda: self._complete(prompt, images=images, video=video,
                                   system=system, schema=schema),
            self.rc, f"{self.rc.role} ({self.rc.describe()})")

    def complete_json(self, prompt: str, *, schema: dict | None = None,
                      **kw) -> dict | None:
        """Prompt in, parsed JSON object out (None if the model produced nothing usable).

        Passing a schema is strongly preferred - every provider here maps it onto native
        structured output, which removes a whole class of "model wrote prose" failures."""
        text = self.complete(prompt, schema=schema, **kw)
        if isinstance(text, dict):
            return text
        return parse_json(text)

    def generate_image(self, prompt: str, *, images: list[bytes] | None = None) -> bytes:
        """Prompt (+ optional reference images) in, PNG/JPEG bytes out."""
        self.require(image_generation=True)
        return retrying(lambda: self._generate_image(prompt, images=images),
                        self.rc, f"{self.rc.role} image ({self.rc.describe()})")

    # ── subclass hooks ────────────────────────────────────────────────────────

    @abstractmethod
    def _complete(self, prompt: str, *, images, video, system, schema) -> str:
        ...

    def _generate_image(self, prompt: str, *, images) -> bytes:
        raise ProviderUnavailable(f"{self.name} cannot generate images")

    def health(self) -> tuple[bool, str]:
        """(ok, message) for `python -m pipeline.doctor`. Cheap checks only - no
        billable calls."""
        return True, "no health check implemented"
