"""Reads the local prism-hermes gateway: health, model catalog, doctor rows.

Nothing here imports hermes-agent, so the same code runs on its own:

    python3 ~/.hermes/plugins/prism-inference/gateway.py

That matters because ``hermes doctor`` calls ``doctor_checks()`` for terminal
backends but not yet for model providers, and these rows are the difference
between "the provider is registered" and "a side-model call would actually be
paid for and served".
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"
DEFAULT_PORT = 8787

# The confidential tier as Prism published it on 2026-09-03, cheapest first.
# Price is what one generation costs at the gateway's 1024-token output cap;
# you pay for the cap, not for the tokens the model emits. Live figures come
# from GET /v1/models, which is free. This list is what the picker shows when
# the gateway is not running.
CONFIDENTIAL_MODELS: tuple[tuple[str, float], ...] = (
    ("openai/gpt-oss-20b", 0.011024),
    ("deepseek/deepseek-v4-flash", 0.012048),
    ("openai/gpt-oss-120b", 0.013072),
    ("z-ai/glm-5.3-flash", 0.013072),
    ("phala/gemma-4-26b-a4b-uncensored", 0.014096),
    ("phala/qwen3.6-35b-a3b-uncensored", 0.018192),
    ("meta-llama/llama-3.3-70b-instruct", 0.020240),
    ("qwen/qwen3.8-27b", 0.025360),
    ("z-ai/glm-5.2", 0.025360),
)

CHEAPEST_MODEL = CONFIDENTIAL_MODELS[0][0]

_AUX_MODEL_CACHE: dict[str, tuple[float, str]] = {}
_AUX_MODEL_TTL_SECONDS = 300.0


def reset_cache() -> None:
    """Drop the cheap-model cache. Tests call this; nothing else needs to."""
    _AUX_MODEL_CACHE.clear()


def origin(base_url: str) -> str:
    """Return the scheme and authority of *base_url*, with no path.

    ``/healthz`` and ``/v1/models`` hang off different roots, so both callers
    need the origin rather than the configured ``/v1`` inference base.
    """
    parts = urlsplit(base_url or DEFAULT_BASE_URL)
    if not parts.scheme or not parts.netloc:
        return "http://127.0.0.1:8787"
    return f"{parts.scheme}://{parts.netloc}"


def is_loopback(base_url: str) -> bool:
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _auth_token() -> str:
    """The bearer the gateway wants, if it wants one.

    ``PRISM_INFERENCE_API_KEY`` is what Hermes sends on every generation, so
    an operator who set ``PRISM_HERMES_TOKEN`` has already copied it there.
    """
    for name in ("PRISM_INFERENCE_API_KEY", "PRISM_HERMES_TOKEN"):
        value = os.getenv(name, "").strip()
        if value and value != "unused":
            return value
    return ""


def get_json(url: str, timeout: float) -> dict | None:
    """GET *url* and parse it, or return None. Never raises."""
    request = Request(url)
    request.add_header("Accept", "application/json")
    token = _auth_token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (loopback by default)
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def health(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 2.0) -> dict | None:
    """The gateway's own view of itself: wallet, allowance, what it enforces."""
    return get_json(f"{origin(base_url)}/healthz", timeout)


def catalog(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 6.0) -> list[dict]:
    """Every model the gateway serves, confidential and open tier alike."""
    payload = get_json(f"{origin(base_url)}/v1/models", timeout)
    if not payload:
        return []
    data = payload.get("data")
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def _is_confidential(entry: dict) -> bool:
    prism = entry.get("prism")
    return isinstance(prism, dict) and prism.get("tier") == "confidential"


def _price(entry: dict) -> float:
    prism = entry.get("prism")
    if not isinstance(prism, dict):
        return float("inf")
    try:
        return float(prism["price_usdg_at_max"])
    except (KeyError, TypeError, ValueError):
        return float("inf")


def confidential_models(base_url: str = DEFAULT_BASE_URL, *, timeout: float = 6.0) -> list[str]:
    """Confidential model ids from the live catalog, cheapest first.

    The gateway also serves an open tier, where the host operator can read what
    the workload touches. Those models are deliberately not returned: a side
    model picked from this profile is picked for confidentiality.
    """
    entries = [entry for entry in catalog(base_url, timeout=timeout) if _is_confidential(entry)]
    entries.sort(key=_price)
    return [str(entry["id"]) for entry in entries if entry.get("id")]


def cheapest_model(base_url: str = DEFAULT_BASE_URL) -> str:
    """The cheapest confidential model the gateway is serving right now, or "".

    Auxiliary tasks resolve their model on every client build, so this caches
    for five minutes and only probes a loopback gateway. A remote one is left
    alone: a hot path should not wait on a network round trip to learn that a
    hardcoded default is still current.
    """
    if not is_loopback(base_url):
        return ""
    cached = _AUX_MODEL_CACHE.get(base_url)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    models = confidential_models(base_url, timeout=1.5)
    resolved = models[0] if models else ""
    _AUX_MODEL_CACHE[base_url] = (now + _AUX_MODEL_TTL_SECONDS, resolved)
    return resolved


def _mask(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 12 else wallet


def _port_note(base_url: str) -> str:
    """Warn when the gateway's own port setting disagrees with the profile."""
    configured = os.getenv("PRISM_HERMES_PORT", "").strip()
    if not configured:
        return ""
    port = urlsplit(base_url or DEFAULT_BASE_URL).port or DEFAULT_PORT
    if configured == str(port):
        return ""
    return (
        f"; PRISM_HERMES_PORT is {configured}, so set auxiliary.<task>.base_url "
        f"to http://127.0.0.1:{configured}/v1"
    )


def doctor_rows(base_url: str = DEFAULT_BASE_URL) -> list[tuple[bool, str, str]]:
    """``(ok, label, detail)`` triples, the shape ``hermes doctor`` prints.

    Four rows in a fixed order whether or not the gateway answers, so the
    output reads the same on a broken machine as on a working one.
    """
    report = health(base_url)
    note = _port_note(base_url)

    if report is None:
        return [
            (
                False,
                "Prism inference gateway",
                f"(nothing answering at {origin(base_url)}. Start it with: npx -y prism-hermes{note})",
            ),
            (
                bool(os.getenv("PRISM_AGENT_KEY", "").strip()),
                "Prism wallet",
                "(PRISM_AGENT_KEY is set; start the gateway to see the address and balance)"
                if os.getenv("PRISM_AGENT_KEY", "").strip()
                else "(PRISM_AGENT_KEY is not set, so generations will be refused)",
            ),
            (False, "Prism attestation", "(unknown until the gateway is running)"),
            (False, "Prism daily budget", "(unknown until the gateway is running)"),
        ]

    wallet = str(report.get("wallet") or "").strip()
    attested = bool(report.get("require_attestation"))
    sealed = bool(report.get("e2ee"))
    remaining = report.get("remaining_usdg")

    if attested and sealed:
        attestation_detail = "(quote verified and the prompt sealed to the enclave before it is sent)"
    elif attested:
        attestation_detail = "(quote verified, prompt sent in plaintext: PRISM_HERMES_E2EE=0)"
    else:
        attestation_detail = "(off: PRISM_HERMES_REQUIRE_ATTESTATION=0 returns unverified output)"

    try:
        left = float(remaining)
    except (TypeError, ValueError):
        left = None

    if left is None:
        budget_detail = "(the gateway did not report an allowance)"
    elif left <= 0:
        budget_detail = "(spent. Raise PRISM_HERMES_DAILY_USDG or wait for the 24-hour window to roll)"
    else:
        budget_detail = f"({left:.4f} USDG left of today's ceiling)"

    return [
        (True, "Prism inference gateway", f"(serving {origin(base_url)}{note})"),
        (
            bool(wallet),
            "Prism wallet",
            f"(paying from {_mask(wallet)})" if wallet else "(no wallet. Set PRISM_AGENT_KEY and restart the gateway)",
        ),
        (attested, "Prism attestation", attestation_detail),
        (left is not None and left > 0, "Prism daily budget", budget_detail),
    ]


def main() -> int:
    rows = doctor_rows(DEFAULT_BASE_URL)
    for ok, label, detail in rows:
        print(f"{'✓' if ok else '✗'} {label:26} {detail}")
    return 0 if all(ok for ok, _, _ in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
