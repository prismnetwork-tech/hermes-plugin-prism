"""The confidential-inference provider, loaded the way an operator installs it.

Everything here runs against a throwaway HERMES_HOME laid out exactly as
``docs/confidential-side-models.md`` describes: the repository cloned to
``plugins/prism`` and ``plugins/prism-inference`` symlinked at its ``inference``
directory. Discovery is Hermes' own, so a layout that only works in a test
cannot pass. No wallet, no network: the gateway is mocked at ``urlopen``.
"""

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PLUGIN = "prism"
PROVIDER_PLUGIN = "prism-inference"
PROVIDER = "prism"
CHEAPEST = "openai/gpt-oss-20b"


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_serving(routes):
    """A urlopen stand-in answering *routes* (path suffix → payload)."""

    def _open(request, timeout=None):
        url = request.full_url
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _FakeResponse(payload)
        raise ConnectionRefusedError(url)

    return _open


def _urlopen_refusing(request, timeout=None):
    raise ConnectionRefusedError(request.full_url)


# Both payloads carry the numbers as strings, which is what the gateway sends.
HEALTHY = {
    "ok": True,
    "wallet": "0xEcaaE714912C38fA7e0dAF78afa7C54DbeD11039",
    "remaining_usdg": "0.944800",
    "require_attestation": True,
    "e2ee": True,
}

CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "qwen/qwen3.8-27b",
            "prism": {"tier": "confidential", "price_usdg_at_max": "0.025360"},
        },
        {
            "id": "llama3.1:8b",
            "prism": {"tier": "open", "price_usdg_at_max": "0.012144"},
        },
        {
            "id": CHEAPEST,
            "prism": {"tier": "confidential", "price_usdg_at_max": "0.011024"},
        },
    ],
}


@pytest.fixture(scope="module")
def hermes_home(tmp_path_factory):
    """A Hermes home with the backend installed and the provider symlinked."""
    home = tmp_path_factory.mktemp("hermes-home")
    plugins = home / "plugins"
    shutil.copytree(
        REPO_ROOT,
        plugins / BACKEND_PLUGIN,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.egg-info"),
    )
    (plugins / PROVIDER_PLUGIN).symlink_to(
        plugins / BACKEND_PLUGIN / "inference", target_is_directory=True
    )
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        f"    - {BACKEND_PLUGIN}\n"
        f"    - {PROVIDER_PLUGIN}\n"
        "auxiliary:\n"
        "  compression:\n"
        f"    provider: {PROVIDER}\n"
        f"    model: {CHEAPEST}\n"
        "    api_key: unused\n"
        "  memory_query_rewrite:\n"
        f"    provider: {PROVIDER}\n"
        f"    model: {CHEAPEST}\n"
        "    api_key: unused\n"
    )
    return home


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(scope="module")
def loaded(hermes_home, monkeypatch_module):
    """Discovery run against the throwaway home. Returns (manager, profile)."""
    monkeypatch_module.setenv("HERMES_HOME", str(hermes_home))
    # Provider discovery is lazy and one-shot per process, and these modules
    # cache the Hermes home at import. An earlier test module that resolved the
    # developer's real home would otherwise make every assertion below vacuous.
    for name in ("hermes_constants", "hermes_cli.config", "hermes_cli.plugins"):
        importlib.reload(importlib.import_module(name))

    import providers

    providers._REGISTRY.clear()
    providers._ALIASES.clear()
    providers._PROVIDER_LIST_CACHE = None
    providers._discovered = False
    for name in list(sys.modules):
        if name.startswith(("plugins.model_providers.", "_hermes_user_provider_")):
            del sys.modules[name]

    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)

    profile = providers.get_provider_profile(PROVIDER)
    assert profile is not None, "the prism profile did not register from the symlinked plugin"
    return manager, profile


@pytest.fixture
def gateway(loaded):
    """The plugin's gateway module, with its caches emptied."""
    _manager, profile = loaded
    module = sys.modules[type(profile).__module__].gateway
    module.reset_cache()
    return module


def test_both_plugins_install_side_by_side(loaded):
    manager, _profile = loaded
    backend = manager._plugins.get(BACKEND_PLUGIN)
    provider = manager._plugins.get(PROVIDER_PLUGIN)

    assert backend is not None, "the terminal backend was not discovered"
    assert backend.enabled, backend.error
    assert provider is not None, "the provider plugin was not discovered"
    assert provider.enabled, provider.error
    assert provider.manifest.kind == "model-provider"


def test_the_profile_describes_the_gateway(loaded):
    _manager, profile = loaded

    assert profile.name == PROVIDER
    assert profile.display_name == "Prism (confidential inference)"
    assert profile.base_url == "http://127.0.0.1:8787/v1"
    assert profile.auth_type == "api_key"
    assert profile.env_vars == ("PRISM_INFERENCE_API_KEY",)
    assert profile.default_max_tokens == 1024
    assert profile.supports_vision is False


def test_the_provider_reaches_the_model_picker(loaded):
    import hermes_cli.models

    models = importlib.reload(hermes_cli.models)
    entry = next((p for p in models.CANONICAL_PROVIDERS if p.slug == PROVIDER), None)

    assert entry is not None, "prism is missing from the picker's provider list"
    assert entry.label == "Prism (confidential inference)"


def test_the_catalog_is_confidential_and_cheapest_first(loaded, gateway):
    _manager, profile = loaded

    assert profile.default_aux_model == CHEAPEST
    assert profile.fallback_models[0] == CHEAPEST
    prices = [price for _model, price in gateway.CONFIDENTIAL_MODELS]
    assert prices == sorted(prices), "the catalog is not ordered cheapest first"
    assert len(profile.fallback_models) == len(set(profile.fallback_models))
    # The open tier has no confidentiality, so it must not be selectable here.
    assert not {"llama3.1:8b", "llama3.2:3b"} & set(profile.fallback_models)


def test_fetch_models_drops_the_open_tier(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    monkeypatch.setattr(gateway, "urlopen", _urlopen_serving({"/v1/models": CATALOG}))

    assert profile.fetch_models() == [CHEAPEST, "qwen/qwen3.8-27b"]


def test_fetch_models_returns_none_when_nothing_is_listening(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    monkeypatch.setattr(gateway, "urlopen", _urlopen_refusing)

    assert profile.fetch_models() is None


def test_the_cheap_model_tracks_the_live_catalog(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    monkeypatch.setattr(gateway, "urlopen", _urlopen_serving({"/v1/models": CATALOG}))

    assert profile.resolve_aux_model() == CHEAPEST
    assert profile.resolve_aux_model(vision=True) == "", "the enclave serves no vision model"


def test_doctor_rows_with_the_gateway_down(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    monkeypatch.setattr(gateway, "urlopen", _urlopen_refusing)
    monkeypatch.delenv("PRISM_AGENT_KEY", raising=False)
    monkeypatch.delenv("PRISM_HERMES_PORT", raising=False)

    rows = profile.doctor_checks()
    labels = [label for _ok, label, _detail in rows]

    assert labels == [
        "Prism inference gateway",
        "Prism wallet",
        "Prism attestation",
        "Prism daily budget",
    ]
    assert not any(ok for ok, _label, _detail in rows)
    assert "npx -y prism-hermes" in rows[0][2]
    assert "PRISM_AGENT_KEY" in rows[1][2]


def test_doctor_rows_with_the_gateway_up(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    monkeypatch.setattr(gateway, "urlopen", _urlopen_serving({"/healthz": HEALTHY}))
    monkeypatch.delenv("PRISM_HERMES_PORT", raising=False)

    rows = profile.doctor_checks()

    assert all(ok for ok, _label, _detail in rows), rows
    assert "0xEcaa…1039" in rows[1][2]
    assert "before it is sent" in rows[2][2]
    assert "0.9448 USDG" in rows[3][2]


def test_doctor_flags_attestation_turned_off(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    relaxed = dict(HEALTHY, require_attestation=False, remaining_usdg="0.000000")
    monkeypatch.setattr(gateway, "urlopen", _urlopen_serving({"/healthz": relaxed}))
    monkeypatch.delenv("PRISM_HERMES_PORT", raising=False)

    attestation, budget = profile.doctor_checks()[2:]

    assert attestation[0] is False
    assert "PRISM_HERMES_REQUIRE_ATTESTATION=0" in attestation[2]
    assert budget[0] is False
    assert "PRISM_HERMES_DAILY_USDG" in budget[2]


def test_doctor_names_a_port_the_profile_does_not_point_at(loaded, gateway, monkeypatch):
    _manager, profile = loaded
    monkeypatch.setattr(gateway, "urlopen", _urlopen_serving({"/healthz": HEALTHY}))
    monkeypatch.setenv("PRISM_HERMES_PORT", "9001")

    assert "http://127.0.0.1:9001/v1" in profile.doctor_checks()[0][2]


def test_side_model_config_keys_resolve_to_the_gateway(loaded):
    from agent.auxiliary_client import _get_auxiliary_task_config
    from hermes_cli.providers import get_provider

    for task in ("compression", "memory_query_rewrite"):
        config = _get_auxiliary_task_config(task)
        assert config.get("provider") == PROVIDER, task
        assert config.get("model") == CHEAPEST, task

    resolved = get_provider(PROVIDER, allow_network=False)
    assert resolved is not None, "prism did not resolve as a provider definition"
    assert resolved.base_url == "http://127.0.0.1:8787/v1"
    assert resolved.api_key_env_vars == ("PRISM_INFERENCE_API_KEY",)
    assert resolved.source == "plugin-profile"


def test_loopback_is_left_to_other_local_servers(loaded):
    _manager, profile = loaded
    import agent.model_metadata

    metadata = importlib.reload(agent.model_metadata)

    assert profile.get_hostname() == ""
    assert "127.0.0.1" not in metadata._URL_TO_PROVIDER
    # LM Studio's default port, which this profile must not answer for.
    assert metadata._infer_provider_from_url("http://127.0.0.1:1234/v1") != PROVIDER
