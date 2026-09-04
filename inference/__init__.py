"""Prism confidential inference as a Hermes provider profile.

Hermes sends more of a conversation to its side models than to the main one:
compression reads whole trajectories, memory retrieval reads what the agent
remembered, session search reads history. This profile points those tasks at a
GPU enclave instead of an account in someone's name.

The wire is the prism-hermes gateway on loopback. It speaks OpenAI chat
completions, pays for each generation from a wallet under its own per-call and
daily ceilings, and verifies the enclave's attestation before it hands an answer
back. Hermes needs no patch and holds no provider key.
"""

from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

from . import gateway
from .gateway import CHEAPEST_MODEL, CONFIDENTIAL_MODELS, DEFAULT_BASE_URL

__all__ = ["gateway", "prism", "PrismInferenceProfile"]


class PrismInferenceProfile(ProviderProfile):
    """A provider whose endpoint is a paying gateway on this machine."""

    def get_hostname(self) -> str:
        """No hostname. Deliberately.

        ``agent/model_metadata.py`` builds a hostname → provider map from every
        profile and matches it by substring against a base URL's authority.
        Derived from a loopback base URL this profile would claim ``127.0.0.1``
        and with it every other local server the user runs: LM Studio, vLLM,
        Ollama, a proxy on 8080. There is nothing to win here either, since
        models.dev has no entry for Prism to look up.
        """
        return ""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Confidential models only, cheapest first.

        The gateway also serves an open tier where the host operator can read
        what the workload touches. Listing those here would put a model with no
        confidentiality one arrow-key away from one that has it, under a
        provider whose whole reason to exist is the difference.
        """
        models = gateway.confidential_models(base_url or self.base_url, timeout=timeout)
        return models or None

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        """Track the live cheap tier, so a retired model id is not a 404 tax."""
        if vision:
            return ""  # the enclave serves no vision model; the caller falls through
        return gateway.cheapest_model(self.base_url)

    def default_vision_model(self) -> str | None:
        return None

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        """``(ok, label, detail)`` rows: gateway, wallet, attestation, budget."""
        return gateway.doctor_rows(self.base_url)


prism = PrismInferenceProfile(
    name="prism",
    aliases=("prism-inference", "prism-confidential"),
    display_name="Prism (confidential inference)",
    description="Side models run in a GPU enclave, paid per call from a wallet",
    signup_url="https://prismnetwork.tech",
    # The gateway authorises a request by paying for it, so there is no
    # provider key. Hermes still wants a non-empty credential to build a
    # client, so the documented placeholder is the literal "unused"; set
    # PRISM_INFERENCE_API_KEY to a real value only when the gateway is behind
    # PRISM_HERMES_TOKEN.
    env_vars=("PRISM_INFERENCE_API_KEY",),
    base_url=DEFAULT_BASE_URL,
    fallback_models=tuple(model for model, _price in CONFIDENTIAL_MODELS),
    default_aux_model=CHEAPEST_MODEL,
    # The gateway caps output at 1024 tokens and charges for the cap, so
    # asking for more costs money and returns a 400.
    default_max_tokens=1024,
    supports_vision=False,
    supports_health_check=True,
)

register_provider(prism)
