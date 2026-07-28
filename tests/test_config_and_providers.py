"""Config resolution and provider selection — the flexibility surface.

The promise these tests protect is "point a role at any backend without touching
code". They construct config dicts directly and never make a network call.
"""
import pytest

from pipeline.config import _deep_merge, _migrate_legacy_keys, load_config
from pipeline.hardware import args_for_encoder, encoder_args
from pipeline.providers import ProviderUnavailable, build, reset_cache, role_config


@pytest.fixture(autouse=True)
def _clean_provider_cache():
    reset_cache()
    yield
    reset_cache()


def cfg_with(**roles) -> dict:
    return {"llm": {"defaults": {"timeout_s": 30, "max_retries": 2, "retry_base_s": 1},
                    "roles": roles}}


# ── overlay merging ───────────────────────────────────────────────────────────

def test_overlay_merges_nested_keys_without_dropping_siblings():
    base = {"video": {"width": 1920, "height": 1080, "brand": {"name": "A", "enabled": True}}}
    over = {"video": {"brand": {"name": "B"}}}
    out = _deep_merge(base, over)
    assert out["video"]["brand"]["name"] == "B"
    assert out["video"]["brand"]["enabled"] is True     # sibling survives
    assert out["video"]["width"] == 1920                # untouched section survives


def test_overlay_replaces_lists_rather_than_appending():
    """A half-overridden music_tracks/blacklist is never what anyone means."""
    base = {"blacklist": {"broadcasters": ["a", "b", "c"]}}
    out = _deep_merge(base, {"blacklist": {"broadcasters": ["z"]}})
    assert out["blacklist"]["broadcasters"] == ["z"]


# ── legacy key migration ──────────────────────────────────────────────────────

def test_legacy_provider_keys_move_into_llm_roles():
    cfg = {"api_judge": {"model": "gemini-x", "api_key": "k", "enabled": True},
           "vlm_filter": {"ollama_model": "llava:7b", "max_keep": 18}}
    _migrate_legacy_keys(cfg)
    roles = cfg["llm"]["roles"]
    assert roles["judge"]["model"] == "gemini-x"
    assert roles["judge"]["api_key"] == "k"
    assert roles["vlm"]["model"] == "llava:7b"
    # Behavioural keys stay where they are.
    assert cfg["vlm_filter"]["max_keep"] == 18
    assert cfg["api_judge"]["enabled"] is True
    # Migrated keys are removed so nothing reads a stale value.
    assert "model" not in cfg["api_judge"]


def test_migration_is_a_noop_for_a_current_config():
    cfg = {"llm": {"roles": {"judge": {"provider": "gemini", "model": "m"}}},
           "api_judge": {"enabled": True}}
    _migrate_legacy_keys(cfg)
    assert cfg["llm"]["roles"]["judge"]["model"] == "m"


# ── role resolution ───────────────────────────────────────────────────────────

def test_role_inherits_defaults_and_reports_itself():
    cfg = cfg_with(judge={"provider": "gemini", "model": "gemini-3.6-flash",
                          "api_key": "x"})
    rc = role_config(cfg, "judge")
    assert rc.timeout_s == 30 and rc.max_retries == 2
    assert "gemini-3.6-flash" in rc.describe()


def test_role_can_override_a_default():
    cfg = cfg_with(judge={"provider": "gemini", "model": "m", "api_key": "x",
                          "timeout_s": 300})
    assert role_config(cfg, "judge").timeout_s == 300


def test_auto_uses_the_api_provider_when_a_key_is_present():
    cfg = cfg_with(commentary={"provider": "auto", "model": "gemini-x", "api_key": "k",
                               "fallback": {"provider": "ollama", "model": "qwen2.5:7b"}})
    assert role_config(cfg, "commentary").provider == "gemini"


def test_auto_falls_back_to_local_when_no_key_is_set():
    """This is what makes a keyless clone still produce a video."""
    cfg = cfg_with(commentary={"provider": "auto", "model": "gemini-x", "api_key": "",
                               "fallback": {"provider": "ollama", "model": "qwen2.5:7b"}})
    rc = role_config(cfg, "commentary")
    assert rc.provider == "ollama"
    assert rc.model == "qwen2.5:7b"


def test_unknown_role_raises_something_actionable():
    with pytest.raises(ProviderUnavailable, match="llm.roles.nope"):
        role_config(cfg_with(), "nope")


def test_unknown_provider_names_the_valid_options():
    cfg = cfg_with(judge={"provider": "hal9000", "model": "m"})
    with pytest.raises(ProviderUnavailable, match="gemini"):
        build(role_config(cfg, "judge"))


def test_gemini_without_a_key_explains_the_fix():
    cfg = cfg_with(judge={"provider": "gemini", "model": "m", "api_key": ""})
    with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
        build(role_config(cfg, "judge"))


def test_openai_provider_requires_an_explicit_model():
    cfg = cfg_with(commentary={"provider": "openai", "model": "",
                               "base_url": "https://openrouter.ai/api/v1",
                               "api_key": "k"})
    with pytest.raises(ProviderUnavailable, match="model"):
        build(role_config(cfg, "commentary"))


def test_local_openai_endpoint_does_not_require_a_key():
    """LM Studio / llama.cpp / vLLM accept any placeholder, so don't block on it."""
    cfg = cfg_with(vlm={"provider": "openai", "model": "qwen2.5-vl",
                        "base_url": "http://localhost:1234/v1", "api_key": ""})
    provider = build(role_config(cfg, "vlm"))
    assert provider.supports_images


# ── capability guards ─────────────────────────────────────────────────────────

def test_non_gemini_providers_refuse_video_rather_than_failing_obscurely():
    cfg = cfg_with(judge={"provider": "openai", "model": "gpt-x",
                          "base_url": "https://api.openai.com/v1", "api_key": "k"})
    provider = build(role_config(cfg, "judge"))
    with pytest.raises(ProviderUnavailable, match="video"):
        provider.require(video=True)


def test_ollama_refuses_image_generation():
    cfg = cfg_with(thumbnail_image={"provider": "ollama", "model": "llava",
                                    "base_url": "http://localhost:11434"})
    # Constructed lazily: an explicit model means no /api/tags probe at build time.
    provider = build(role_config(cfg, "thumbnail_image"))
    with pytest.raises(ProviderUnavailable, match="image generation"):
        provider.require(image_generation=True)


# ── shipped config sanity ─────────────────────────────────────────────────────

def test_shipped_config_loads_and_is_safe_by_default():
    """A fresh clone must never publish to someone's channel on the first run."""
    cfg = load_config()
    assert cfg["upload"]["enabled"] is False
    assert cfg["upload"]["privacy"] == "private"
    assert cfg["shorts"]["upload"] is False
    assert cfg["shorts"]["privacy"] == "private"


def test_shipped_config_defines_every_role_the_stages_ask_for():
    roles = load_config()["llm"]["roles"]
    for name in ("judge", "vlm", "commentary", "feedback", "thumbnail_image"):
        assert name in roles, f"llm.roles.{name} missing from config.yaml"


# ── encoder argument construction ─────────────────────────────────────────────
# args_for_encoder is pure (no ffmpeg probing), so these assertions hold on any
# machine regardless of which encoders it actually has.

def test_libx264_gets_crf_and_its_own_preset():
    args = args_for_encoder("libx264", "medium")
    assert args[:2] == ["-c:v", "libx264"]
    assert "-crf" in args
    assert args[args.index("-preset") + 1] == "medium"


def test_nvenc_gets_an_nvenc_preset_not_an_x264_one():
    """Passing 'veryfast' to NVENC is rejected by older ffmpeg builds."""
    args = args_for_encoder("h264_nvenc", "veryfast")
    assert args[args.index("-preset") + 1] == "p2"
    assert "-crf" not in args        # CRF is libx264-only


def test_videotoolbox_gets_no_preset_flag():
    assert "-preset" not in args_for_encoder("h264_videotoolbox", "veryfast")


def test_unknown_preset_falls_back_to_a_valid_nvenc_one():
    args = args_for_encoder("h264_nvenc", "nonsense")
    assert args[args.index("-preset") + 1] == "p4"


def test_encoder_args_resolves_from_the_video_config():
    args = encoder_args({"encoder": "libx264", "preset": "slow"})
    assert args[:2] == ["-c:v", "libx264"]
    assert args[args.index("-preset") + 1] == "slow"
