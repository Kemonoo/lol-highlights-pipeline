"""Anthropic Claude provider - text and vision roles.

Usable for `commentary`, `feedback`, and `vlm`. NOT usable for `judge`, which needs
native video understanding (Gemini only, today) - configuring it there raises
ProviderUnavailable rather than silently degrading.

Unlike the other adapters here, this one uses the official `anthropic` SDK rather than
raw requests: it is the supported path, it handles retries/backoff and typed errors for
us, and it keeps structured-output and thinking parameters correct as the API evolves.
The import is lazy and the dependency is an extra (`pip install -e ".[anthropic]"`), so
nobody pays for it unless they point a role at Claude - same pattern the pipeline
already uses for kokoro and faster-whisper.

Retries are delegated to the SDK (it already backs off on 429/5xx/connection errors),
so this provider raises terminal errors only - wrapping it in the base class's retry
loop would multiply attempts.
"""
from __future__ import annotations

import base64
import logging

from .base import Provider, ProviderError, ProviderUnavailable, RoleConfig

log = logging.getLogger("pipeline.providers.anthropic")

DEFAULT_MODEL = "claude-opus-5"

# Roles here produce a JSON object or one or two sentences of commentary - never long
# prose - so a modest cap is deliberate rather than a lowball. Raise it in config if you
# repurpose a role for something longer.
DEFAULT_MAX_TOKENS = 4096

# The pipeline's generic `thinking` knob (off|low|medium|high) mapped onto Claude's
# effort levels. NB: on Claude Opus 5 thinking is ON by default and disabling it is only
# accepted at effort `high` or below - `_thinking_params` keeps that invariant.
_EFFORT = {"low": "low", "medium": "medium", "high": "high"}


class AnthropicProvider(Provider):
    name = "anthropic"
    supports_images = True
    supports_video = False            # the Messages API does not accept video
    supports_image_generation = False

    def __init__(self, rc: RoleConfig):
        super().__init__(rc)
        try:
            import anthropic
        except ImportError as e:
            raise ProviderUnavailable(
                f"role '{rc.role}' is set to provider: anthropic, but the SDK is not "
                'installed. Fix: pip install -e ".[anthropic]"'
            ) from e
        self._sdk = anthropic
        if not rc.api_key:
            raise ProviderUnavailable(
                f"role '{rc.role}' is set to provider: anthropic but no API key was "
                "found. Set ANTHROPIC_API_KEY in .env."
            )
        self._model = rc.model or DEFAULT_MODEL
        self._client = anthropic.Anthropic(
            api_key=rc.api_key,
            timeout=rc.timeout_s,
            max_retries=rc.max_retries,      # SDK owns backoff; see module docstring
        )

    def _thinking_params(self) -> dict:
        """Translate the generic `thinking` knob into thinking + effort."""
        level = (self.rc.thinking or "").lower()
        if level == "off":
            # Disabling thinking is rejected above `high` effort, so pin effort too.
            return {"thinking": {"type": "disabled"},
                    "output_config": {"effort": "low"}}
        effort = _EFFORT.get(level)
        if effort:
            return {"thinking": {"type": "adaptive"},
                    "output_config": {"effort": effort}}
        return {}

    def _complete(self, prompt: str, *, images, video, system, schema) -> str:
        if video:
            raise ProviderUnavailable(
                "the Anthropic Messages API does not accept video. The `judge` role "
                "needs provider: gemini, or the pipeline falls back to local scoring.")

        content: list[dict] = []
        for img in images or []:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.standard_b64encode(img).decode()}})
        content.append({"type": "text", "text": prompt})

        params: dict = {
            "model": self._model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": content}],
            **self._thinking_params(),
        }
        if system:
            params["system"] = system
        if schema:
            # Structured outputs require every object to be closed and its keys listed.
            params.setdefault("output_config", {})["format"] = {
                "type": "json_schema", "schema": _strict(schema)}

        try:
            response = self._client.messages.create(**params)
        except self._sdk.NotFoundError as e:
            raise ProviderError(
                f"model '{self._model}' not found - check the id in llm.roles."
                f"{self.rc.role}.model ({e})") from e
        except self._sdk.AuthenticationError as e:
            raise ProviderUnavailable(f"Anthropic API key rejected ({e})") from e
        except self._sdk.APIStatusError as e:
            raise ProviderError(f"anthropic {self._model}: HTTP {e.status_code} - {e}") from e
        except self._sdk.APIConnectionError as e:
            raise ProviderError(f"anthropic {self._model}: connection failed ({e})") from e

        # Safety classifiers can decline a request: HTTP 200, empty/partial content.
        # Check before indexing into content, or this raises IndexError instead of
        # something the caller can act on.
        if response.stop_reason == "refusal":
            raise ProviderError(
                f"anthropic {self._model} declined this request "
                f"(stop_reason: refusal). Nothing usable was returned.")
        return "".join(b.text for b in response.content if b.type == "text")

    def health(self) -> tuple[bool, str]:
        try:
            model = self._client.models.retrieve(self._model)
        except self._sdk.NotFoundError:
            return False, f"model '{self._model}' does not exist"
        except self._sdk.AuthenticationError:
            return False, "API key rejected (check ANTHROPIC_API_KEY)"
        except Exception as e:                      # network, permissions, ...
            return False, f"cannot reach the Anthropic API ({e})"
        return True, f"{model.id} reachable"


def _strict(schema: dict) -> dict:
    """Close every object in the schema - structured outputs reject open objects."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        out["additionalProperties"] = False
        props = out.get("properties") or {}
        out["properties"] = {k: _strict(v) for k, v in props.items()}
        out.setdefault("required", list(props))
    if "items" in out:
        out["items"] = _strict(out["items"])
    return out
