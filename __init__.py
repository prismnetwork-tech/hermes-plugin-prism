"""Prism Network terminal backend plugin for Hermes Agent.

Registers ``terminal.backend: prism`` — the agent's shell commands run on an
NVIDIA GPU rented by the second from the agent's own wallet, paid on-chain in
USDG on Robinhood Chain, bounded by a per-lease and a daily spend cap the model
cannot raise.

Install into ``~/.hermes/plugins/prism/`` and enable it:

    pip install prismnetwork
    hermes plugins enable prism
    hermes config set terminal.backend prism
    # PRISM_AGENT_KEY in ~/.hermes/.env (a funded wallet on Robinhood Chain)

Built by Prism Network. Not affiliated with Nous Research.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any, Dict, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

_SDK_SPEC = "prismnetwork>=0.4.0,<0.5.0"

#: Where the wallet key is read from. The key stays here: it is stripped from
#: every subprocess and never copied to the rented machine.
_ENV_FILE = ".env in your Hermes home"

#: Wallet key, chain endpoints and spend configuration. Stripped from every
#: subprocess the agent spawns, so a model-authored command can neither read
#: the key that pays for its GPU nor discover where the caps are kept. Any
#: other ``PRISM_``-prefixed name in the environment is stripped alongside
#: them, because a vault or vendor token added later is a credential too.
_PRISM_ENV_KEYS = frozenset({
    "PRISM_AGENT_KEY",
    "PRISM_ESCROW",
    "PRISM_API_BASE",
    "PRISM_RPC_URL",
    "PRISM_PUBLIC_API",
    "PRISM_LEDGER_PATH",
    "PRISM_MAX_USDG",
    "PRISM_DAILY_BUDGET_USDG",
})


def _sdk_installed() -> bool:
    """True when the prismnetwork SDK is importable.

    ``find_spec`` raises ValueError for an already-imported module whose
    ``__spec__`` is None (e.g. injected test doubles); treat presence in
    sys.modules as installed.
    """
    import sys

    if "prismnetwork" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("prismnetwork") is not None
    except (ImportError, ValueError):
        return False


def _get_key() -> Optional[str]:
    try:
        from agent.secret_scope import get_secret

        return get_secret("PRISM_AGENT_KEY")
    except Exception:
        return os.getenv("PRISM_AGENT_KEY")


def _wallet_address() -> Optional[str]:
    """The address the key spends from, without touching the network."""
    key = (_get_key() or "").strip()
    if not key:
        return None
    try:
        from eth_account import Account

        return Account.from_key(key).address
    except Exception:
        return None


def _env_module():
    """This plugin's environment module, however the package was loaded.

    Normal path: imported by the Hermes plugin manager as a package. Test and
    direct-import path: the repo root is on sys.path and there is no package to
    be relative to.
    """
    try:
        from . import prism_environment
    except ImportError:
        import prism_environment

    return prism_environment


def _settings():
    return _env_module().read_settings()


def _budget_row() -> tuple[bool, str]:
    try:
        from prismnetwork import SpendLedger, usdg

        settings = _settings()
        ledger = SpendLedger(settings.ledger_path, settings.daily_micros,
                             settings.max_per_call_micros)
        remaining = ledger.remaining()
    except Exception as e:
        return (False, f"({e})")
    left = "unlimited" if remaining is None else usdg(remaining)
    return (True, f"(up to {usdg(settings.max_per_call_micros)} per lease, "
                  f"{left} left today — ledger {settings.ledger_path})")


class PrismProvider(TerminalEnvironmentProvider):
    """Prism Network — a rented NVIDIA GPU, paid for by the agent's wallet."""

    name = "prism"
    display_name = "Prism Network"
    is_remote = True
    is_container = True
    # ``container_persistent: false`` is a statement that state must not be
    # shared across sessions, and a rented machine at trust class "open" is
    # shared state: one session's files, processes and history sit on a disk the
    # next one would attach to. Each session gets its own lease instead.
    session_isolated_when_nonpersistent = True

    @property
    def description(self) -> str:
        return (
            "Run commands on an NVIDIA GPU rented by the second from your own "
            "wallet, under a spend cap the model cannot raise."
        )

    @property
    def env_description(self) -> str:
        return "a rented NVIDIA GPU on Prism Network (Linux, CUDA)"

    @property
    def cache_path_base(self) -> Optional[str]:
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        return _PRISM_ENV_KEYS | {k for k in os.environ if k.startswith("PRISM_")}

    def is_available(self) -> bool:
        return _sdk_installed() and bool((_get_key() or "").strip())

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        if not _sdk_installed():
            logger.error(
                "the Prism terminal backend needs the prismnetwork SDK (%s), "
                "which Hermes does not install for you", _SDK_SPEC,
            )
            return False
        if not (_get_key() or "").strip():
            logger.error(
                "the Prism backend rents GPUs from a wallet: put PRISM_AGENT_KEY "
                "in %s, funded with USDG and gas on Robinhood Chain.", _ENV_FILE,
            )
            return False
        return True

    def probe(self):
        if not _sdk_installed():
            return ("needs_setup", f"prismnetwork SDK not installed — needs {_SDK_SPEC}.")
        address = _wallet_address()
        if not address:
            return ("needs_setup", "Set PRISM_AGENT_KEY to a funded Robinhood Chain wallet.")
        try:
            from prismnetwork import usdg

            return ("ready", f"{address}, up to {usdg(_settings().max_per_call_micros)} per lease")
        except Exception as e:
            return ("needs_setup", str(e))

    def setup_instructions(self):
        return [
            "Commands run on an NVIDIA GPU rented by the second and paid for",
            "on-chain in USDG on Robinhood Chain (id 4663).",
            "Create a wallet, fund it with USDG and a little ETH for gas, and",
            f"save its private key as PRISM_AGENT_KEY in {_ENV_FILE}.",
            "The key stays on this machine. It is stripped from every command",
            "the model runs and is never copied to the rented GPU.",
            "Prices and available GPUs: https://prismnetwork.tech",
            "Two caps bound the spend, and the model can raise neither:",
            "  hermes config set terminal.prism.max_usdg 1",
            "  hermes config set terminal.prism.daily_budget_usdg 5",
            "At trust class 'open' the host operator can read anything the",
            "workload touches; raise terminal.prism.trust_class for sensitive work.",
            "The rented disk is ephemeral: it is destroyed with the lease.",
            "A quiet session gives the GPU back after five minutes and rents",
            "again on the next command; set terminal.prism.idle_release_seconds",
            "to change that, or 0 to hold the lease for the whole session.",
            "Every lease leaves a proof capsule behind. `hermes doctor` says",
            "where they are written.",
        ]

    def doctor_checks(self):
        rows = []
        address = _wallet_address()
        rows.append((
            bool(address),
            "Prism wallet",
            f"({address} on Robinhood Chain)" if address
            else f"(required — set PRISM_AGENT_KEY in {_ENV_FILE} to a funded wallet)",
        ))
        sdk_ok = _sdk_installed()
        rows.append((sdk_ok, "prismnetwork SDK",
                     "(installed)" if sdk_ok else f"(required: {_SDK_SPEC})"))
        budget_ok, budget_detail = _budget_row()
        rows.append((budget_ok, "Prism spend caps", budget_detail))
        try:
            trust = _settings().trust_class
        except Exception:
            trust = "open"
        rows.append((
            True,
            "Prism trust class",
            f"({trust} — the host operator can read anything the workload touches)"
            if trust == "open" else f"({trust})",
        ))
        rows.append((True, "Prism lease", self._lease_detail()))
        rows.append((True, "Prism proof capsules", self._capsule_detail()))
        return rows

    def _capsule_detail(self) -> str:
        try:
            directory = _env_module().capsule_dir()
        except Exception:
            return "(unknown)"
        return f"({directory}: one file per lease, written when the GPU is released)"

    def _lease_detail(self) -> str:
        try:
            leases = _env_module().live_leases()
        except Exception:
            return "(unknown)"
        if not leases:
            return ("(none: a GPU is rented on the first command, and given back "
                    "when the session goes quiet)")
        return "; ".join(
            f"lease {row['lease_id']} on {row['gpu']}, {row['seconds_left'] // 60}m of paid time left"
            for row in leases
        )

    def create_environment(self, *, cwd, timeout, task_id="default",
                           image=None, container_config=None, **kwargs):
        # ``image`` is the core's container image knob; a Prism lease takes a
        # digest-pinned image the control plane will match against, configured
        # under terminal.prism.image. Nothing in container_config to read
        # either: a lease always lives for the session and its disk always dies
        # with it, so container_persistent only decides whether two sessions
        # share one rented machine, which core keys for us.
        return _env_module().PrismEnvironment(cwd=cwd, timeout=timeout, task_id=task_id)


def register(ctx):
    ctx.register_terminal_environment_provider(PrismProvider())
