"""Claude via the Claude Code CLI (`claude -p`) - the owner's subscription, not the API.

Owner 2026-10-18: the Anthropic API credits ran out, but the Claude subscription that
Claude Code logs into keeps working. Headless `claude -p` answers on that login, so a
role pointed here costs subscription usage and never API credits.

Images go in as base64 content blocks over `--input-format stream-json` (no files, no
tools: `--tools ""`). ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are stripped from the
child's environment ON PURPOSE - when set, Claude Code bills the API key instead of the
subscription, which is exactly what this provider exists to avoid. It runs from a temp
directory so no project CLAUDE.md is loaded into the prompt.

A usage-limit or login failure is a ProviderError with status 429/401, which callers
treat as "stop asking today" - never retried, the CLI's limit does not refill in seconds.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile

from .base import Provider, ProviderError, ProviderUnavailable, RoleConfig, parse_json

log = logging.getLogger("pipeline.providers.claude_code")

_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def _media_type(b: bytes) -> str:
    return "image/png" if b[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


class ClaudeCodeProvider(Provider):
    name = "claude_code"
    supports_images = True
    supports_video = False
    supports_image_generation = False

    def __init__(self, rc: RoleConfig):
        super().__init__(rc)
        self.exe = shutil.which(rc.base_url or "claude")
        if not self.exe:
            raise ProviderUnavailable(
                f"role '{rc.role}' is set to provider: claude_code but the `claude` CLI is "
                "not on PATH. Install Claude Code and log in once (`claude`).")

    def _complete(self, prompt, *, images, video, system, schema) -> str:
        content = [{"type": "text", "text": prompt}]
        for img in images or []:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": _media_type(img),
                "data": base64.b64encode(img).decode()}})
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        cmd = [self.exe, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--system-prompt", system or "You are a precise assistant. Answer in JSON."]
        if self.rc.model:
            cmd += ["--model", self.rc.model]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        try:
            r = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True,
                               text=True, encoding="utf-8", errors="replace", env=env,
                               cwd=tempfile.gettempdir(), timeout=self.rc.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"claude -p timed out after {self.rc.timeout_s:.0f}s",
                                status=408) from e
        result = None
        for line in r.stdout.splitlines():
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            if j.get("type") == "result":
                result = j
        if result is None:
            raise ProviderError(f"claude -p gave no result (exit {r.returncode}): "
                                f"{(r.stderr or r.stdout)[-300:]}")
        text = str(result.get("result") or "")
        if result.get("is_error"):
            low = text.lower()
            status = (429 if ("limit" in low or "usage" in low) else
                      401 if ("login" in low or "auth" in low) else None)
            raise ProviderError(f"claude -p: {text[:300]}", status=status)
        structured = result.get("structured_output")
        if structured is not None:
            return json.dumps(structured)
        return text

    def complete_json(self, prompt, *, schema=None, **kw):
        # no retry ladder: a CLI usage limit doesn't clear in 20 s, and each attempt
        # spends subscription usage
        return parse_json(self._complete(prompt, images=kw.get("images"), video=None,
                                         system=kw.get("system", ""), schema=schema))

    def health(self) -> tuple[bool, str]:
        try:
            v = subprocess.run([self.exe, "--version"], capture_output=True, text=True,
                               timeout=30).stdout.strip()
            return True, f"claude CLI {v} (subscription login; API key stripped)"
        except Exception as e:
            return False, f"claude CLI not runnable: {e}"
