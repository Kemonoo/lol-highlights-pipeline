"""OpenAI-compatible provider - one adapter, most of the ecosystem.

`/v1/chat/completions` is the de facto standard, so this single class covers OpenAI
itself plus OpenRouter, Groq, Together, DeepSeek, Fireworks, LM Studio, vLLM,
llama.cpp's server, and Ollama's own /v1 endpoint. Point `base_url` at whichever you
have access to:

    llm.roles.commentary:
      provider: openai
      model:    meta-llama/llama-3.3-70b-instruct
      base_url: https://openrouter.ai/api/v1
      api_key:  ${OPENROUTER_API_KEY}

    llm.roles.vlm:                       # a vision model on your own GPU box
      provider: openai
      model:    qwen2.5-vl-7b
      base_url: http://192.168.1.50:1234/v1
      api_key:  lm-studio

Vision goes through the standard image_url/data-URI part, which every serving stack
above implements. Structured output prefers `json_schema` and falls back to `json_object`
for older endpoints that only support the loose mode.
"""
from __future__ import annotations

import base64
import logging

import requests

from .base import (
    Provider,
    ProviderError,
    ProviderUnavailable,
    RoleConfig,
    _Retryable,
    raise_for_status,
)

log = logging.getLogger("pipeline.providers.openai")

DEFAULT_URL = "https://api.openai.com/v1"


class OpenAICompatProvider(Provider):
    name = "openai"
    supports_images = True
    supports_video = False
    supports_image_generation = False

    def __init__(self, rc: RoleConfig):
        super().__init__(rc)
        self.base = (rc.base_url or DEFAULT_URL).rstrip("/")
        self._local = any(h in self.base for h in ("localhost", "127.0.0.1", "0.0.0.0"))
        if not rc.api_key and not self._local:
            raise ProviderUnavailable(
                f"role '{rc.role}' uses provider: openai at {self.base} but no api_key is "
                "set. Add one in .env and reference it from llm.roles."
                f"{rc.role}.api_key (local servers usually accept any placeholder)."
            )
        if not rc.model:
            raise ProviderUnavailable(
                f"role '{rc.role}' uses provider: openai but no model id is set. "
                "Unlike Ollama, this provider cannot guess one.")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.rc.api_key:
            h["Authorization"] = f"Bearer {self.rc.api_key}"
        return h

    def _complete(self, prompt: str, *, images, video, system, schema) -> str:
        if video:
            raise ProviderUnavailable(
                "the OpenAI chat API does not accept video. The `judge` role needs "
                "provider: gemini, or the pipeline falls back to local scoring.")

        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images or []:
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}})

        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": content}]
        body: dict = {"model": self.rc.model, "messages": messages, "temperature": 0.0}
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": False, "schema": schema},
            }

        data = self._request(body)
        if data is None:                      # endpoint rejected json_schema - retry looser
            body["response_format"] = {"type": "json_object"}
            data = self._request(body, strict=True)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("no choices returned")
        return (choices[0].get("message") or {}).get("content") or ""

    def _request(self, body: dict, *, strict: bool = False) -> dict | None:
        try:
            resp = requests.post(f"{self.base}/chat/completions", json=body,
                                 headers=self._headers(), timeout=self.rc.timeout_s)
        except requests.RequestException as e:
            raise _Retryable(f"network error: {e}") from e
        # Older/simpler servers 400 on json_schema. Signal the caller to downgrade once
        # rather than failing a run over a response-format detail.
        if resp.status_code == 400 and not strict and "response_format" in body:
            log.debug("%s rejected json_schema - falling back to json_object", self.base)
            return None
        raise_for_status(resp, f"openai {self.rc.model} @ {self.base}")
        return resp.json()

    def health(self) -> tuple[bool, str]:
        try:
            resp = requests.get(f"{self.base}/models", headers=self._headers(), timeout=15)
        except requests.RequestException as e:
            return False, f"cannot reach {self.base} ({e})"
        if resp.status_code in (401, 403):
            return False, f"api key rejected by {self.base}"
        if resp.status_code >= 400:
            # /models is optional; a working /chat/completions is what matters.
            return True, f"{self.base} reachable (no /models listing)"
        try:
            ids = {m.get("id") for m in resp.json().get("data", [])}
            if ids and self.rc.model not in ids:
                return False, (f"model '{self.rc.model}' not offered by {self.base} "
                               f"({len(ids)} available)")
        except Exception:
            pass
        return True, f"{self.rc.model} available at {self.base}"
