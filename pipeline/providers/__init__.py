"""Provider registry — resolves llm.roles.<role> from config into a Provider.

    from ..providers import get_provider, ProviderUnavailable

    try:
        vlm = get_provider(cfg, "vlm", vision=True)
    except ProviderUnavailable as e:
        log.warning("vlm unavailable (%s) — skipping", e)

Stages ask for a role and never name a vendor. Instances are cached per role so a model
is picked (and Ollama probed) once per run rather than once per clip.
"""
from __future__ import annotations

import logging

from .base import Provider, ProviderError, ProviderUnavailable, RoleConfig, parse_json

log = logging.getLogger("pipeline.providers")

_cache: dict[tuple[str, bool], Provider] = {}


def role_config(cfg: dict, role: str) -> RoleConfig:
    llm = cfg.get("llm") or {}
    defaults = llm.get("defaults") or {}
    roles = llm.get("roles") or {}
    entry = roles.get(role)
    if entry is None:
        raise ProviderUnavailable(
            f"no llm.roles.{role} entry in config.yaml — add one, or leave the stage "
            f"disabled. See config.yaml for the shape.")

    provider = str(entry.get("provider", "none")).lower()
    # `auto` = use the API model when a key is present, else the local fallback. This is
    # what makes a keyless clone still produce a video instead of failing.
    if provider == "auto":
        if entry.get("api_key"):
            provider = "gemini"
        else:
            fb = entry.get("fallback") or {}
            if not fb:
                raise ProviderUnavailable(
                    f"llm.roles.{role} is 'auto' with no api_key and no fallback block.")
            log.info("role '%s': no API key - falling back to %s:%s",
                     role, fb.get("provider"), fb.get("model"))
            entry = {**fb}
            provider = str(entry.get("provider", "none")).lower()

    return RoleConfig(
        role=role,
        provider=provider,
        model=str(entry.get("model") or ""),
        api_key=str(entry.get("api_key") or ""),
        base_url=str(entry.get("base_url") or ""),
        thinking=str(entry.get("thinking") or ""),
        timeout_s=float(entry.get("timeout_s", defaults.get("timeout_s", 120))),
        max_retries=int(entry.get("max_retries", defaults.get("max_retries", 4))),
        retry_base_s=float(entry.get("retry_base_s", defaults.get("retry_base_s", 20))),
        autostart=bool(entry.get("autostart", defaults.get("autostart", True))),
        fallback=entry.get("fallback"),
    )


def build(rc: RoleConfig, *, vision: bool = False) -> Provider:
    """Instantiate a provider from a resolved role config (no caching)."""
    if rc.provider in ("none", "off", "disabled"):
        raise ProviderUnavailable(f"role '{rc.role}' is disabled (provider: {rc.provider})")
    if rc.provider == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(rc)
    if rc.provider == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(rc, vision=vision)
    if rc.provider in ("openai", "openai_compat"):
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(rc)
    if rc.provider == "anthropic":
        from .anthropic_api import AnthropicProvider
        return AnthropicProvider(rc)
    raise ProviderUnavailable(
        f"role '{rc.role}': unknown provider '{rc.provider}'. "
        f"Valid: gemini | ollama | openai | anthropic | auto | none")


def get_provider(cfg: dict, role: str, *, vision: bool = False) -> Provider:
    """Resolve and cache the provider serving `role`.

    Raises ProviderUnavailable when the role can't run — stages are expected to catch
    that and fall back to their local path rather than abort the pipeline."""
    key = (role, vision)
    if key not in _cache:
        provider = build(role_config(cfg, role), vision=vision)
        log.info("role '%s' -> %s", role, provider.rc.describe())
        _cache[key] = provider
    return _cache[key]


def get_fallback(cfg: dict, role: str, *, vision: bool = False) -> Provider | None:
    """The role's `fallback` block as a Provider, or None if it has none / can't run.

    This is the *runtime* fallback — used when the primary starts failing mid-run
    (quota exhausted), which is different from the `auto` resolution in role_config()
    that picks a provider up front based on whether a key exists at all."""
    rc = role_config(cfg, role)
    if not rc.fallback:
        return None
    fb = RoleConfig(
        role=f"{role}:fallback",
        provider=str(rc.fallback.get("provider", "ollama")).lower(),
        model=str(rc.fallback.get("model") or ""),
        api_key=str(rc.fallback.get("api_key") or ""),
        base_url=str(rc.fallback.get("base_url") or ""),
        timeout_s=rc.timeout_s,
        max_retries=1,          # a fallback that also stalls just wastes the run
        retry_base_s=rc.retry_base_s,
    )
    try:
        return build(fb, vision=vision)
    except ProviderUnavailable as e:
        log.info("no fallback available for role '%s' (%s)", role, e)
        return None


def reset_cache() -> None:
    """Drop cached providers (tests, and config reloads inside one process)."""
    _cache.clear()


__all__ = ["get_provider", "get_fallback", "role_config", "build", "reset_cache",
           "Provider", "ProviderError", "ProviderUnavailable", "RoleConfig",
           "parse_json"]
