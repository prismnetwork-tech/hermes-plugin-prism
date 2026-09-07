"""Prism execution environment (standalone Hermes plugin).

Runs the agent's shell commands on an NVIDIA GPU rented by the second from the
agent's own wallet, over the ``prismnetwork`` SDK: quote, fund an on-chain USDG
escrow on Robinhood Chain, then SSH into the machine for the rest of the
session. The lease is taken on the first command, kept warm for every command
after it, released when the session goes quiet, and released again on cleanup.

Every lease is written to the shared spend ledger before the money moves, so a
model that decides to rent forty machines in a row is stopped by the operator's
daily ceiling rather than by the wallet balance.

The workspace is the rented machine's disk. It is destroyed with the lease;
nothing is resumed and nothing is pulled back. What survives is the proof
capsule: one JSON file per lease naming the image, the machine that answered,
and a hash of everything the session sent and received.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shlex
import tarfile
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from prismnetwork import (
    DEFAULT_ESCROW,
    DEFAULT_IMAGE,
    TRUST_CLASSES,
    BudgetError,
    PrismAgent,
    PrismError,
    SpendLedger,
    host_key_policy,
    read_budget,
    strip_unexpanded,
    usdg,
)

from tools.environments.base import BaseEnvironment

# Hermes moved the concrete process handle out of `base` into `base_output`,
# leaving a Protocol of the same public name behind. Take the implementation
# from wherever this build keeps it, so one plugin runs on either release.
try:
    from tools.environments.base_output import _ThreadedProcessHandle
except ImportError:  # hermes-agent before the split
    from tools.environments.base import _ThreadedProcessHandle
from tools.environments.file_sync import FileSyncManager, quoted_rm_command

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 3600
DEFAULT_MIN_VRAM_MIB = 16000
DEFAULT_TRUST_CLASS = "open"
#: Seconds without a command before the GPU is given back. The next command
#: rents a fresh one, under the same caps. 0 keeps the lease for the session.
DEFAULT_IDLE_RELEASE_S = 300

#: Ledger label carried into cap messages the model reads.
LEDGER_TOOL = "hermes terminal"

#: A file larger than this is skipped rather than inlined. The SDK's only
#: transfer is a command over the SSH channel, and a multi-megabyte base64
#: argument stalls the session for every file queued behind it.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
#: One tar stream carries the whole sync. The skills tree alone is close to a
#: thousand files, and a round trip per file on a machine that closes idle
#: connections spent an entire paid window uploading nothing the session asked
#: for. Past this the sync is refused with a line naming what to trim, rather
#: than pushed a file at a time.
MAX_BULK_UPLOAD_BYTES = 32 * 1024 * 1024

#: Stop using a lease a minute before its paid window closes; a command that
#: starts inside that minute is cut off mid-run when the escrow settles.
EXPIRY_MARGIN_S = 60
#: The shortest lease worth funding. A machine that booted seconds ago is given
#: minutes to answer its first SSH, and the last EXPIRY_MARGIN_S of the paid
#: window belongs to the settlement, so anything shorter buys a GPU the session
#: can never run a command on.
MIN_LEASE_SECONDS = 300

#: How long a lease attempt that already put a transaction on the wire blocks
#: the next one. The terminal tool answers any error it cannot read as a command
#: timeout by calling execute() again, three times over, and every call re-enters
#: _ensure_lease: without this, one model command funds four escrows.
FUNDING_HOLD_S = 120

CONNECT_DELAY_S = 10
#: The SDK's ssh calls carry ConnectTimeout=15, so a failed attempt costs that
#: on top of the delay before the next one. A retry budget that counts only the
#: delay is wrong by a factor of two and a half.
SSH_CONNECT_TIMEOUT_S = 15
#: What one attempt that never reaches the machine costs: the ssh connect
#: deadline, then the sleep the SDK takes before trying again. This is the
#: price of a retry, and only of a retry.
ATTEMPT_S = SSH_CONNECT_TIMEOUT_S + CONNECT_DELAY_S
#: What the SDK adds to the timeout it is handed before it gives up on the ssh
#: subprocess. The attempt that runs the command always costs this on top of
#: the command's own timeout, so no deadline shorter than the two together can
#: bound a command that asks for it.
EXEC_OVERHEAD_S = 20
#: No command is worth running with less time than this.
MIN_COMMAND_S = 5
#: What the first command of a session can take before it runs anything: the
#: escrow gives a machine ten minutes to open access, the SDK waits that long
#: for it, and the home probe waits out sshd on top. Hermes bounds every
#: sequential tool call at ``timeouts.tools.sequential_call`` (420 s unless
#: set) and abandons the call past it, which leaves a funded lease with nobody
#: waiting for it. The backend refuses to rent under a ceiling that cannot
#: hold a provision, and names the setting to raise.
PROVISION_BUDGET_S = 900
#: SSH retries allowed while the freshly provisioned box is still coming up.
#: Bounded by the command's own deadline so a warm-up loop can never outlive
#: the wait that is watching it.
WARMUP_RETRIES = 12
#: Retries once the machine has answered at least once. A live box that stops
#: answering is a real failure, not a warm-up.
STEADY_RETRIES = 2
#: Exit codes the SDK returns for "nothing answered": 255 when every connect
#: attempt failed, -1 when the exec ran past its deadline.
UNREACHABLE_CODES = frozenset({255, -1})

#: Setup steps come in pairs: how long the remote command itself may take, and
#: how long the whole call may take including the connect attempts it takes to
#: get there. The gap between the two is what pays for a warm-up.
#:
#: First contact with a machine that booted seconds ago, and what it prints
#: decides where every synced file lands. ``echo $HOME`` answers instantly, so
#: the wide budget buys connect attempts rather than shell time: what this
#: waits on is sshd coming up. A cloud host reports its forwarded port before
#: anything listens on it, and a refused connection costs a second rather than
#: the connect timeout the budget prices, so this is sized for every warm-up
#: attempt the backend allows: WARMUP_RETRIES of them, then the answer.
PROBE_COMMAND_S = 15
PROBE_TIMEOUT_S = WARMUP_RETRIES * (SSH_CONNECT_TIMEOUT_S + CONNECT_DELAY_S) + PROBE_COMMAND_S + 20
#: A synced file arrives as base64 on stdin, up to MAX_UPLOAD_BYTES of it, and
#: the remote side decodes it before answering.
UPLOAD_TIMEOUT_S = 120
UPLOAD_COMMAND_S = 60
#: The bulk stream is one exec, so it gets one command's worth of time to
#: land and unpack, plus the warm-up any first contact is allowed.
BULK_UPLOAD_TIMEOUT_S = 240
BULK_UPLOAD_COMMAND_S = 120
DELETE_TIMEOUT_S = 60
DELETE_COMMAND_S = 20
#: Slack on the backstop watching a setup exec, so a healthy call returns
#: through its own path rather than being reported as a wedge.
BOUND_GRACE_S = 2.0
#: How often the idle monitor looks. The lease is paid for by the hour, so a
#: few seconds either side of the release point costs nothing.
IDLE_POLL_S = 5.0

#: Hermes builds a throwaway environment under this task id while it assembles
#: the system prompt and asks it what OS it is
#: (``agent.prompt_builder._probe_remote_backend``), then discards it. Nothing
#: about it comes from the session: it happens before the model has said a word,
#: and on a rented backend it buys a GPU to answer a sentence in a prompt. Core
#: already writes that sentence without an answer, so this one stays quiet.
PROMPT_PROBE_TASK_ID = "prompt-backend-probe"

#: The escrow refunds a deposit whose machine never handed over access on its
#: own: the provision window is ten minutes from funding and expiring it is
#: permissionless, so the refund lands some minutes after the failure and
#: usually not at the first look. Poll well past the window rather than guess
#: which minute somebody else's transaction lands in.
REFUND_POLL_S = 60
REFUND_POLLS = 15
#: Lease states the control plane reports for a deposit that came back. Anything
#: else, including a lease it no longer lists, leaves the reservation standing.
REFUNDED_STATES = frozenset({"refunded"})

#: Where a settled lease's receipt is published, a few minutes after the escrow
#: closes. The capsule is written before that, so it names the page instead.
PROOF_URL = "https://prismnetwork.tech/proof"
#: Capsule directory for a plugin Hermes has not handed a data directory to.
CAPSULE_FALLBACK_DIR = "~/.hermes/prism/capsules"

#: Spend ceilings an operator can export instead of writing into config.yaml.
ENV_CAPS = ("PRISM_MAX_USDG", "PRISM_DAILY_BUDGET_USDG")

#: Control-plane answers that mean "nothing on the network fits this request".
CAPACITY_CODES = frozenset({
    "no_capacity", "no_offer", "no_offers", "no_match", "no_matching_offer",
    "offer_unavailable", "capacity_unavailable", "quote_unavailable",
})
CAPACITY_STATUSES = frozenset({404, 409, 503})


class PrismLeaseError(RuntimeError):
    """A GPU could not be rented, or the one that was rented went away."""


class _SetupTimedOut(RuntimeError):
    """A setup exec ran past the deadline it was handed and was abandoned."""


_live: "weakref.WeakSet[PrismEnvironment]" = weakref.WeakSet()
_live_lock = threading.Lock()


def live_leases() -> list[dict]:
    """Leases held by environments in this process, for doctor and status."""
    with _live_lock:
        environments = list(_live)
    return [row for row in (env.lease_info() for env in environments) if row]


@dataclass
class Settings:
    image: str
    lease_seconds: int
    min_vram_mib: int
    trust_class: str
    sync_credentials: bool
    idle_release_seconds: int
    ledger_path: str
    max_per_call_micros: int
    daily_micros: int


def terminal_prism_config() -> dict:
    """The ``terminal.prism`` section of config.yaml, or a refusal to spend.

    ``terminal.*`` keys reach the terminal tool as ``TERMINAL_*`` env vars, but
    only the ones core knows about; a plugin's own subsection is not bridged,
    so it is read from the config file directly.

    A config that cannot be read is refused rather than defaulted. Core answers
    a parse failure by serving ``DEFAULT_CONFIG``, and the defaults it would
    hand back here are the SDK's 1 USDG per lease and 5 a day: real money, at
    ceilings the operator never chose and has no way to see being ignored.
    """
    try:
        from hermes_cli.config import get_config_path, load_config_readonly

        path = get_config_path()
    except Exception as e:
        raise PrismLeaseError(
            f"Prism cannot read the Hermes config that holds its spend caps ({e}), "
            "so it will not rent a GPU."
        ) from e

    on_disk = _config_file(path)
    try:
        config = load_config_readonly()
    except Exception as e:
        raise _unreadable(path, f"cannot be read ({e})") from e
    section = _prism_section(path, config)
    if on_disk is not None:
        # A copy: the loader hands back its cache, and a caller that edits the
        # section it was given edits every later reader's ceilings with it.
        return dict(section)

    # No file at all. That is a first run, where there are no operator ceilings
    # to lose, unless something claims otherwise: a section in force with no
    # file behind it, or exported caps with no file to check them against. Both
    # mean the numbers bounding the wallet are not the ones the operator can see.
    if section:
        raise _unreadable(path, "does not exist while a terminal.prism section is in force")
    if exported := [name for name in ENV_CAPS if os.environ.get(name, "").strip()]:
        raise PrismLeaseError(
            f"{' and '.join(exported)} bound the Prism wallet but {path} does not exist, "
            "so the caps in force cannot be read back from the file that is meant to hold "
            "them. Write them down first: `hermes config set terminal.prism.max_usdg 1`."
        )
    return {}


def _unreadable(path, reason: str) -> PrismLeaseError:
    return PrismLeaseError(
        f"Prism will not rent a GPU while {path} {reason}: that file carries the "
        "caps that bound the wallet. Repair it and retry."
    )


def _config_file(path):
    """The config file as it is on disk, or None when there is no file.

    Read separately from the loaded mapping because core's fallback is
    invisible from the caller's side: a broken config.yaml and one that sets no
    Prism keys at all come back looking the same.
    """
    from hermes_cli.config import fast_safe_load

    try:
        with open(path, encoding="utf-8") as fh:
            parsed = fast_safe_load(fh)
    except FileNotFoundError:
        return None
    except Exception as e:
        raise _unreadable(path, f"cannot be read ({e})") from e
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise _unreadable(path, f"parses as {type(parsed).__name__} rather than a mapping")
    return parsed


def _prism_section(path, config) -> dict:
    """``terminal.prism`` out of a loaded config, refusing anything malformed.

    A ``terminal:`` that is not a mapping cannot hold the caps and must not be
    read as an absent one: falling through to SDK defaults there spends the
    wallet at ceilings written in a file that is being ignored.
    """
    if not isinstance(config, dict):
        raise _unreadable(path, f"loads as {type(config).__name__} rather than a mapping")
    terminal = config.get("terminal")
    if terminal is None:
        return {}
    if not isinstance(terminal, dict):
        raise _unreadable(path, "has a terminal: section that is not a mapping")
    prism = terminal.get("prism")
    if prism is None:
        return {}
    if not isinstance(prism, dict):
        raise _unreadable(path, "has a terminal.prism section that is not a mapping")
    return prism


def _int(config: dict, key: str, fallback: int, floor: int = 1) -> int:
    try:
        value = int(config[key])
    except (KeyError, TypeError, ValueError):
        return fallback
    return value if value >= floor else fallback


def read_settings() -> Settings:
    """Resolve plugin config and the wallet's spend caps.

    ``terminal.prism.max_usdg`` and ``terminal.prism.daily_budget_usdg`` are the
    operator's ceilings expressed where the rest of the backend is configured;
    they are handed to :func:`prismnetwork.read_budget` as the environment it
    would otherwise read, so the config file and ``PRISM_MAX_USDG`` /
    ``PRISM_DAILY_BUDGET_USDG`` mean exactly the same thing. The config keys win
    because a model can reach neither, and a stale exported value is the one
    thing an operator cannot see.

    ``ledger_path`` is pinned the same way, and for the same reason one level
    down: it names the file the day's spend is counted in, so an environment
    that repoints it hands back a budget that has already been spent.
    """
    config = terminal_prism_config()
    env = strip_unexpanded(dict(os.environ))
    if config.get("max_usdg") is not None:
        env["PRISM_MAX_USDG"] = str(config["max_usdg"])
    if config.get("daily_budget_usdg") is not None:
        env["PRISM_DAILY_BUDGET_USDG"] = str(config["daily_budget_usdg"])
    if ledger_path := str(config.get("ledger_path") or "").strip():
        env["PRISM_LEDGER_PATH"] = os.path.expanduser(ledger_path)
    budget = read_budget(env)

    trust_class = str(config.get("trust_class") or DEFAULT_TRUST_CLASS).strip().lower()
    if trust_class not in TRUST_CLASSES:
        raise PrismLeaseError(
            f"terminal.prism.trust_class must be one of {', '.join(TRUST_CLASSES)}, "
            f"not {trust_class!r}"
        )
    _check_tool_ceiling()
    lease_seconds = _int(config, "lease_seconds", DEFAULT_LEASE_SECONDS)
    if lease_seconds < MIN_LEASE_SECONDS:
        raise PrismLeaseError(
            f"terminal.prism.lease_seconds is {lease_seconds}, below the "
            f"{MIN_LEASE_SECONDS}s minimum. Prism stops using a lease "
            f"{EXPIRY_MARGIN_S}s before its escrow settles, so a shorter window "
            "pays for a GPU no command can be run on."
        )
    return Settings(
        image=str(config.get("image") or DEFAULT_IMAGE),
        lease_seconds=lease_seconds,
        min_vram_mib=_int(config, "min_vram_mib", DEFAULT_MIN_VRAM_MIB),
        trust_class=trust_class,
        sync_credentials=bool(config.get("sync_credentials", False)),
        idle_release_seconds=_int(config, "idle_release_seconds",
                                  DEFAULT_IDLE_RELEASE_S, floor=0),
        ledger_path=budget.ledger_path,
        max_per_call_micros=budget.max_per_call_micros,
        daily_micros=budget.daily_micros,
    )


def sequential_tool_ceiling() -> float | None:
    """Hermes's deadline for one sequential tool call, or None when unbounded.

    Read through core's own resolver so the answer is the one the executor
    will enforce, config first and the legacy env var second. A core without
    the resolver predates the deadline and bounds nothing.
    """
    try:
        from agent.tool_executor import _resolve_sequential_tool_timeout
    except ImportError:
        return None
    try:
        ceiling = _resolve_sequential_tool_timeout()
    except Exception:
        return None
    if ceiling is None or ceiling <= 0:
        return None
    return float(ceiling)


def _check_tool_ceiling() -> None:
    ceiling = sequential_tool_ceiling()
    if ceiling is None or ceiling >= PROVISION_BUDGET_S:
        return
    raise PrismLeaseError(
        f"Hermes abandons a tool call after {int(ceiling)}s "
        "(timeouts.tools.sequential_call) and the first Prism command may spend "
        f"{PROVISION_BUDGET_S}s renting and reaching its GPU, so a lease could be "
        "funded with nothing left waiting for it. Prism will not rent under that "
        f"ceiling. Run: hermes config set timeouts.tools.sequential_call {PROVISION_BUDGET_S}"
    )


def secret(name: str) -> str:
    """Read a Prism credential through the active profile's secret scope."""
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name)
    except Exception:
        value = os.getenv(name)
    return (value or "").strip()


def build_agent() -> PrismAgent:
    """The wallet this backend spends from."""
    key = secret("PRISM_AGENT_KEY")
    if not key:
        raise PrismLeaseError(
            "The Prism backend needs a funded wallet. Put PRISM_AGENT_KEY in "
            ".env in your Hermes home (a 32-byte hex key holding USDG and gas "
            "on Robinhood Chain), or run `hermes setup terminal`."
        )
    options = {}
    if api_base := secret("PRISM_API_BASE"):
        options["api_base"] = api_base
    if rpc_url := secret("PRISM_RPC_URL"):
        options["rpc_url"] = rpc_url
    try:
        return PrismAgent(key, secret("PRISM_ESCROW") or DEFAULT_ESCROW, **options)
    except ValueError as e:
        raise PrismLeaseError(f"PRISM_AGENT_KEY is not usable: {e}") from e


def gpu_model(quote: dict) -> str:
    gpu = quote.get("gpu") if isinstance(quote, dict) else None
    if isinstance(gpu, dict) and gpu.get("model"):
        return str(gpu["model"])
    return str((quote or {}).get("gpu_model") or "GPU")


def image_digest(image: str) -> str:
    """The digest a pinned image reference names. The control plane refuses
    anything else, so the tag half carries no information the capsule needs."""
    reference = str(image or "")
    _, _, digest = reference.partition("@")
    return digest or reference


def is_capacity_error(e: PrismError) -> bool:
    return e.code in CAPACITY_CODES or e.status in CAPACITY_STATUSES


def broadcast_reference(e: BaseException) -> str | None:
    """The transaction a failed lease already put on the wire, if any.

    ``confirmation_timeout`` and ``tx_reverted`` are raised after
    ``send_raw_transaction`` returned, so the funding is on-chain (or in a
    mempool on its way there) whatever the SDK managed to read back. Their hash
    arrives under ``hash`` rather than ``funding_hash`` because the failure
    happens inside the send, before ``lease()`` has a funding hash to attach.
    """
    body = getattr(e, "body", None)
    if not isinstance(body, dict):
        return None
    for key in ("funding_hash", "payment_tx", "hash"):
        if body.get(key):
            return str(body[key])
    return None


def refundable_lease(e: BaseException) -> int | None:
    """The lease a funded failure left on chain, if it named one.

    The SDK attaches the id once the escrow is funded and the control plane has
    recorded the lease, which is the only case where a deposit can come back
    without anyone asking: a machine that never hands over access has its
    provision expired and the money returned.
    """
    body = getattr(e, "body", None)
    lease_id = body.get("lease_id") if isinstance(body, dict) else None
    return lease_id if isinstance(lease_id, int) and not isinstance(lease_id, bool) else None


def lease_state(agent, lease_id) -> str | None:
    """What the control plane says about one lease, or None if it will not say.

    None covers every uncertainty — the endpoint failed, the lease is not in the
    list, the row has no state — because the caller spends money on the
    difference between "refunded" and "cannot tell".
    """
    try:
        rows = agent.leases()
    except Exception:
        logger.debug("Prism: could not read lease state for %s", lease_id, exc_info=True)
        return None
    for row in rows or ():
        if isinstance(row, dict) and row.get("lease_id") == lease_id:
            state = row.get("state")
            return state if isinstance(state, str) else None
    return None


#: Watchers in flight, so a test can wait for one and a long-lived process does
#: not accumulate finished threads.
_watchers: list[threading.Thread] = []
_watcher_lock = threading.Lock()


def watch_for_refund(agent, ledger, *, lease_id, entry_id: str,
                     reference: str | None) -> threading.Thread:
    """Give the day its budget back once the chain says the deposit came back.

    A provision that times out is charged in full and stays charged: until the
    escrow expires nobody can tell a machine that is late from one that is never
    coming, and the ledger's job is to be wrong in the expensive direction. The
    refund is observable afterwards, and a day that keeps paying for GPUs the
    session never reached is a day of budget spent on nothing.

    Runs on its own thread with its own lifetime. The environment that funded
    the lease is usually gone by the time the refund lands — a failed provision
    is what ends it — and the obligation is the ledger's rather than the
    machine's.
    """
    idle = threading.Event()

    def watch() -> None:
        for _ in range(REFUND_POLLS):
            idle.wait(REFUND_POLL_S)
            if lease_state(agent, lease_id) not in REFUNDED_STATES:
                continue
            try:
                ledger.settle(entry_id, micros=0, reference=reference)
            except Exception:
                logger.warning("Prism: lease %s was refunded but the ledger entry could "
                               "not be credited", lease_id, exc_info=True)
                return
            logger.info("Prism: the network refunded lease %s; today's budget has it back",
                        lease_id)
            return
        logger.info("Prism: lease %s was not refunded within %ds; the reservation stands",
                    lease_id, REFUND_POLLS * REFUND_POLL_S)

    thread = threading.Thread(target=watch, name=f"prism-refund:{lease_id}", daemon=True)
    with _watcher_lock:
        _watchers[:] = [t for t in _watchers if t.is_alive()]
        _watchers.append(thread)
    thread.start()
    return thread


_TIMEOUT_WORD = re.compile(r"TIMEOUT|[Tt]imeout")


def _plain(text: str) -> str:
    """Put the word the terminal tool reads as "the command timed out" in the past.

    Any error text containing it is reported to the model as a command timeout,
    which buries the real reason a lease failed along with the transaction to
    chase. Only borrowed text reaches this: the messages below are written
    without the word. Past tense is what keeps that borrowed text readable,
    because the substring is usually inside the one detail worth keeping.
    ``requests.exceptions.ReadTimeout`` comes back as ``ReadTimedOut`` and is
    still the class the operator has to look up.
    """

    def past_tense(m: re.Match) -> str:
        word = m.group(0)
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if "_" in (before, after):
            return "TIMED_OUT" if word == "TIMEOUT" else "timed_out"
        if before.isalnum() or after.isalnum():
            if word == "TIMEOUT":
                return "TIMEDOUT"
            return "TimedOut" if word[0] == "T" else "timedOut"
        return "TIMED OUT" if word == "TIMEOUT" else "timed out"

    return _TIMEOUT_WORD.sub(past_tense, text)


def funding_may_be_live(e: BaseException) -> bool:
    """Whether a failed lease attempt could have left a transaction on the wire.

    The SDK answers this itself on every ``lease()`` failure: ``broadcast`` is
    the only field that says whether the money left the wallet, and ``body`` is
    whatever the far side sent back. An RPC that gives out while the balances
    are being read fails below the signing, and booking that would let one bad
    hour on a node take the day's ceiling to zero without a single escrow.

    Only an exception that carries no answer falls back to the conservative
    reading, where anything that is not a refused quote or an empty network is
    treated as money that may already be moving.
    """
    broadcast = getattr(e, "broadcast", None) if isinstance(e, PrismError) else None
    if broadcast is not None:
        return bool(broadcast)
    if not isinstance(e, PrismError):
        return True
    return bool(broadcast_reference(e)) or e.code == "chain_error"


def describe_lease_failure(e: BaseException) -> str:
    """What the model is told when a lease does not happen."""
    if isinstance(e, PrismLeaseError):
        return str(e)  # this plugin's own refusals are already written for the model
    if not isinstance(e, PrismError):
        # Below the SDK's own error handling: web3's transport gave out while
        # funding. Whether the signed transaction reached a mempool is exactly
        # what is unknown here, so the message does not guess.
        return _plain(
            f"Prism could not reach Robinhood Chain to fund the lease "
            f"({type(e).__name__}: {e}). Any transaction it had already signed "
            "stays counted against today's budget; check `hermes doctor` before "
            "retrying."
        )
    body = e.body or {}
    # A failure that broadcast something is answered first: money moved, and a
    # funded failure must never be reported as one that cost nothing. The hash
    # is only worth naming when the SDK agrees this wallet sent it, because
    # ``body`` carries whatever the far side chose to put there.
    if funding_may_be_live(e) and (reference := broadcast_reference(e)):
        # The next two say "sent while funding" rather than "the funding
        # transaction" because the SDK raises them from inside the send, and the
        # wallet sends a USDG approval before the deposit: the hash is not
        # always the payment. Further down, a lease that reached provisioning
        # has one, and it is.
        if e.code == "confirmation_timeout":
            return _plain(
                f"Transaction {reference}, sent while funding the lease, got no "
                "receipt back in time, so no GPU was rented. It counts against "
                "today's budget because a broadcast transaction usually lands; "
                "check it on Robinhood Chain (id 4663) before retrying."
            )
        if e.code == "tx_reverted":
            return _plain(
                f"Transaction {reference}, sent while funding the lease, reverted "
                "on-chain, so no GPU was rented and the gas is spent."
            )
        if e.code == "access_timeout":
            return _plain(
                "The rented GPU never came up inside its provisioning window. "
                f"The escrow is funded (tx {reference}) and settles on-chain."
            )
        return _plain(
            f"The escrow is funded (tx {reference}) but the lease did not come "
            f"up: {body.get('cause') or e.code}"
        )
    if e.code == "pre_broadcast_failure":
        return _plain(
            "Prism could not reach Robinhood Chain to price the lease "
            f"({body.get('cause') or 'the node did not answer'}). Nothing was "
            "signed or sent, so nothing was charged; retry in a moment."
        )
    if is_capacity_error(e):
        return (
            "No Prism GPU is available for this request right now, so nothing "
            "was charged. Retry in a few minutes, or lower "
            "terminal.prism.min_vram_mib / terminal.prism.trust_class."
        )
    if e.code == "wallet_unfunded":
        return (
            f"The Prism wallet {body.get('address')} holds "
            f"{usdg(body.get('usdg', 0))} and "
            f"{int(body.get('eth_wei', 0)) / 1e18:.6f} ETH for gas. Fund it on "
            "Robinhood Chain (id 4663) before renting a GPU."
        )
    if e.code == "cost_exceeds_max":
        return (
            f"The GPU on offer costs {usdg(body.get('required', 0))} for this "
            f"lease, above the {usdg(body.get('max', 0))} per-lease cap. Raise "
            "terminal.prism.max_usdg or shorten terminal.prism.lease_seconds."
        )
    detail = body.get("cause") or body.get("hint")
    return _plain(f"Prism could not rent a GPU: {e.code}{f' ({detail})' if detail else ''}")


def capsule_dir() -> Path:
    """Where this plugin's own files live for the active profile.

    Hermes hands portable plugins a ``PLUGIN_DATA`` directory; a native plugin
    reaches the same profile-scoped path through its ``PluginState``. Without
    one, capsules go under the Hermes home rather than being dropped: a lease
    that leaves no record behind is a lease nobody can check.
    """
    try:
        from hermes_cli.plugins import PluginState

        return Path(PluginState("prism").data_dir) / "capsules"
    except Exception:
        logger.debug("Prism: no plugin data directory; using the fallback", exc_info=True)
        return Path(CAPSULE_FALLBACK_DIR).expanduser()


def _utc_stamp() -> str:
    return (datetime.fromtimestamp(time.time(), timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


class ProofCapsule:
    """What a lease can be checked against once the machine is gone.

    A rented disk is destroyed with its lease, so the only durable record of
    what ran on it is this one: the image that was pinned, the machine that
    answered and how far its identity could be checked, and a rolling hash over
    everything the session sent to it and read back. The settled figures are not
    in it. A receipt is published a few minutes after the escrow closes, which
    is after the last moment this file can be written, so the amount is left
    null and the file names where it lands.
    """

    def __init__(self, lease, *, image: str, trust_class: str):
        policy = host_key_policy(lease.access)
        self.lease_id = lease.lease_id
        self.image_digest = image_digest(image)
        self.gpu_model = gpu_model(lease.quote)
        self.trust_class = trust_class
        self.funding_hash = lease.funding_hash
        self.host_key_fingerprint = policy["fingerprint"]
        self.host_key_verdict = policy["mode"]
        self.receipt_id = getattr(lease, "receipt_id", None)
        self.settlement_tx = getattr(lease, "settlement_tx", None)
        self.started_at = _utc_stamp()
        self.ended_at: str | None = None
        self.charged_seconds: int | None = None
        self.charged_base_units: int | None = None
        self.release: str | None = None
        self._started = time.monotonic()
        self._stdout = hashlib.sha256()
        self._stderr = hashlib.sha256()
        self._artifacts: dict[str, str] = {}
        self._lock = threading.Lock()

    def record_output(self, stdout: str, stderr: str) -> None:
        with self._lock:
            self._stdout.update(stdout.encode("utf-8", "surrogatepass"))
            self._stderr.update(stderr.encode("utf-8", "surrogatepass"))

    def record_artifact(self, remote_path: str, data: bytes) -> None:
        with self._lock:
            self._artifacts[remote_path] = hashlib.sha256(data).hexdigest()

    def close(self) -> None:
        with self._lock:
            if self.ended_at is not None:
                return
            self.ended_at = _utc_stamp()
            self.charged_seconds = max(0, int(time.monotonic() - self._started))

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "lease_id": self.lease_id,
                "image_digest": self.image_digest,
                "gpu_model": self.gpu_model,
                "trust_class": self.trust_class,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "charged_seconds": self.charged_seconds,
                "charged_base_units": self.charged_base_units,
                "release": self.release,
                "stdout_sha256": self._stdout.hexdigest(),
                "stderr_sha256": self._stderr.hexdigest(),
                "artifact_hashes": dict(self._artifacts),
                "funding_hash": self.funding_hash,
                "receipt_id": self.receipt_id,
                "settlement_tx": self.settlement_tx,
                "proof_url": PROOF_URL,
                "host_key_fingerprint": self.host_key_fingerprint,
                "host_key_verdict": self.host_key_verdict,
            }


def write_capsule(capsule: ProofCapsule) -> Path:
    """Write the capsule for a lease that has ended and return its path.

    Written through a temporary file so a reader never sees half a capsule, and
    at 0600 because it names the machine, the wallet's funding transaction and
    the shape of everything that ran.
    """
    directory = capsule_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{capsule.lease_id}.json"
    scratch = path.with_name(f".{path.name}.tmp")
    body = json.dumps(capsule.to_dict(), indent=2, sort_keys=True) + "\n"
    with open(os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
              encoding="utf-8") as handle:
        handle.write(body)
    os.replace(scratch, path)
    return path


class ExecBudget(NamedTuple):
    """One SSH exec, priced the way the SDK really runs it.

    ``command_timeout`` is the caller's own timeout, passed through untouched:
    the attempt that reaches the machine gets the time the caller asked for, and
    the deadline buys retries with whatever is left over.
    """

    retries: int
    command_timeout: int
    deadline: int

    @property
    def attempts(self) -> int:
        return self.retries + 1

    @property
    def waited(self) -> int:
        """The worst wait this can cost, which is the one worth reporting.

        Every retry burns its ssh connect deadline and the delay after it, then
        the attempt that answers runs to the SDK's own subprocess bound of the
        command timeout plus EXEC_OVERHEAD_S.
        """
        return self.retries * ATTEMPT_S + self.command_timeout + EXEC_OVERHEAD_S

    @property
    def overruns(self) -> bool:
        """Whether a single attempt already outlasts the deadline."""
        return self.command_timeout + EXEC_OVERHEAD_S > self.deadline

    @property
    def overrun_note(self) -> str:
        return (
            f"Prism: a {self.command_timeout}s command takes up to "
            f"{self.command_timeout + EXEC_OVERHEAD_S}s to reach the GPU and come "
            f"back, which is past the {self.deadline}s this call was given. The "
            f"command keeps the full {self.command_timeout}s it asked for and the "
            "wait runs long."
        )


def _check_deadline(deadline: int) -> None:
    """Refuse a deadline that buys no useful command time.

    Nothing here shortens a command's timeout to make it fit, so the floor is
    about the command rather than the SSH around it: under MIN_COMMAND_S there
    is no run worth renting a GPU for.
    """
    if deadline < MIN_COMMAND_S:
        raise PrismLeaseError(
            f"A Prism command needs at least {MIN_COMMAND_S}s, not {deadline}s."
        )


def _bounded(fn, seconds: float, label: str):
    """Run ``fn`` under a wall-clock bound, abandoning it if it overruns.

    The setup execs run inside ``_before_execute``, ahead of the backstop in
    ``execute()``, so a wedged SSH there has nothing watching it and takes the
    whole session down with it.
    """
    try:
        from agent.deadline import run_bounded_sync
    except ImportError:
        pass
    else:
        outcome = run_bounded_sync(fn, seconds, label=label)
        if outcome.timed_out:
            raise _SetupTimedOut(label)
        return outcome.value

    box: dict = {}

    def worker():
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised in the caller; must not vanish
            box["error"] = e

    thread = threading.Thread(target=worker, name=label, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise _SetupTimedOut(label)
    if "error" in box:
        raise box["error"]
    return box["value"]


class PrismEnvironment(BaseEnvironment):
    """Prism backend: one rented GPU for as long as the session is using it.

    Spawn-per-call via Hermes' process handle around the SDK's blocking
    ``run()``, which is a single SSH exec against the leased machine. Nothing
    from the controller's environment is forwarded: the agent's own wallet key
    is what pays for the box, and the box must never see it.
    """

    _stdin_mode = "pipe"
    # The first command on a fresh lease bootstraps its snapshot over an SSH
    # channel to a machine that booted seconds ago.
    _snapshot_timeout = 120
    # Read by terminal_tool.is_persistent_env(): without it the turn finalizer
    # calls cleanup_vm at the end of every turn and throws away a lease that is
    # paid for by the hour. The lease's lifetime is the session; the idle
    # monitor and session teardown are what end it.
    _persistent = True

    def __init__(self, cwd: str = "/root", timeout: int = 60, task_id: str = "default"):
        super().__init__(cwd=cwd, timeout=timeout)
        self._requested_cwd = cwd
        self._task_id = task_id
        self._settings = read_settings()
        self._ledger = SpendLedger(
            self._settings.ledger_path,
            self._settings.daily_micros,
            self._settings.max_per_call_micros,
        )
        self._agent = build_agent()
        # Re-entrant: init_session() runs through _run_bash, which asks for the
        # very lease whose provisioning is holding this lock.
        self._lock = threading.RLock()
        self._lease = None
        self._capsule: ProofCapsule | None = None
        self._expires_at = 0.0
        self._hold_until = 0.0
        self._hold_error: BaseException | None = None
        self._reachable = False
        self._remote_home = "/root"
        self._sync_manager: FileSyncManager | None = None
        # Notices are written from the exec worker thread while the lease lock
        # is held by the thread that started it: init_session() runs its first
        # command from inside _ensure_lease. Sharing the lease lock with them
        # deadlocks the first command of every session.
        # Whether the exec being priced on this thread is the plugin's own
        # setup rather than a command the session asked for.
        self._setup = threading.local()
        self._notice_lock = threading.Lock()
        self._notices: list[str] = []
        self._noted_overrun = False
        self._last_command_at = time.monotonic()
        self._in_flight = 0
        self._monitor: threading.Thread | None = None
        self._stopped = threading.Event()
        with _live_lock:
            _live.add(self)

    # ------------------------------------------------------------------
    # Leasing
    # ------------------------------------------------------------------

    def _ensure_lease(self):
        if self._task_id == PROMPT_PROBE_TASK_ID:
            raise PrismLeaseError(
                "Prism rents a GPU for the session's commands, not to describe itself "
                "while the system prompt is being built. Nothing was charged."
            )
        with self._lock:
            # Setup runs its commands from inside the provisioning that holds
            # this lock, on the lease being provisioned. Re-deciding here would
            # let a slow first contact rent a second GPU from inside the first.
            if getattr(self._setup, "active", False) and self._lease is not None:
                return self._lease
            if self._lease is not None and time.monotonic() < self._expires_at:
                return self._lease
            if self._lease is not None:
                logger.info(
                    "Prism: lease %s reached the end of its paid window; renting a fresh GPU",
                    self._lease.lease_id,
                )
                self._release()
            if held := self._funding_hold():
                raise PrismLeaseError(held)
            lease = self._provision()
            self._lease = lease
            self._capsule = ProofCapsule(lease, image=self._settings.image,
                                         trust_class=self._settings.trust_class)
            try:
                self._expires_at = time.monotonic() + self._lease_window(lease)
            except PrismLeaseError as e:
                # Funded, and too short to use. Giving it straight back stops the
                # access key from outliving the refusal, and the hold stops the
                # caller's retry from buying the same unusable window again.
                self._release()
                self._hold_funding(e)
                raise
            self._last_command_at = time.monotonic()
            self._reachable = False
            self._setup.active = True
            try:
                try:
                    self._remote_home = self._probe_home(lease)
                except PrismLeaseError:
                    # Paid for and unreachable. Releasing stops the meter at
                    # the seconds it was open; the next command rents afresh
                    # and the network remembers the host that never answered.
                    self._release()
                    raise
                if self._requested_cwd in {"~", "/root"}:
                    self.cwd = self._remote_home
                # A fresh machine holds none of the files the last one did, so
                # the sync state starts empty with it.
                self._sync_manager = FileSyncManager(
                    get_files_fn=self._files_to_sync,
                    upload_fn=self._upload,
                    bulk_upload_fn=self._bulk_upload,
                    delete_fn=self._delete,
                )
                self._sync_manager.sync(force=True)
                self.init_session()
            finally:
                self._setup.active = False
            self._start_idle_monitor()
            logger.info(
                "Prism: lease %s live on %s for task %s (%ds paid, tx %s)",
                lease.lease_id, gpu_model(lease.quote), self._task_id,
                lease.quote.get("duration_seconds"), lease.funding_hash,
            )
            return lease

    def _funding_hold(self) -> str | None:
        """Why this call must not fund an escrow yet, if it must not.

        A failed command comes back through here up to three more times, because
        the terminal tool retries anything it cannot read as a command timeout.
        The first attempt is the one that paid; the retries are told what it paid
        for, in place of a second escrow, a third and a fourth.
        """
        if self._hold_error is None:
            return None
        seconds_left = int(self._hold_until - time.monotonic())
        if seconds_left <= 0:
            self._hold_error = None
            return None
        return (
            f"{describe_lease_failure(self._hold_error)} Prism is not funding "
            f"another lease for {seconds_left}s, so a retry cannot pay twice for "
            "one command."
        )

    def _hold_funding(self, e: BaseException) -> None:
        self._hold_until = time.monotonic() + FUNDING_HOLD_S
        self._hold_error = e

    def _lease_window(self, lease) -> int:
        """How long the lease may be used for, which is not how long it was paid for.

        The last EXPIRY_MARGIN_S of the paid window belongs to the settlement,
        so it is taken off rather than floored at: a window that reaches the
        moment the escrow closes is one whose last command is cut off mid-run.
        """
        duration = int(lease.quote.get("duration_seconds") or self._settings.lease_seconds)
        window = duration - EXPIRY_MARGIN_S
        if window < MIN_COMMAND_S:
            raise PrismLeaseError(
                f"The GPU on offer is paid for {duration}s. Prism stops using a lease "
                f"{EXPIRY_MARGIN_S}s before its escrow settles, so this one has no time "
                "to run a command in and has been given back."
            )
        return window

    def _provision(self):
        """Rent a GPU, writing the spend before the money moves.

        Settings are re-read here rather than trusted from construction: a
        session outlives the config edit that lowers its ceiling, and an
        operator who drops the daily budget mid-run means it for the next lease,
        not the next process.
        """
        settings = self._settings = read_settings()
        self._ledger = SpendLedger(settings.ledger_path, settings.daily_micros,
                                   settings.max_per_call_micros)
        try:
            return self._rent(settings)
        except BudgetError as e:
            raise PrismLeaseError(str(e)) from e
        except Exception as e:
            raise PrismLeaseError(describe_lease_failure(e)) from e

    def _rent(self, settings: Settings):
        """One lease, reserved against the ledger before the funding call.

        The reservation is committed first, so the per-lease and daily caps
        refuse here rather than at the wallet. It is given back for a failure
        the SDK reports as having broadcast nothing, and kept for everything
        past the broadcast, including a transport error that leaves a signed
        transaction in a mempool: a wallet that funds escrow after escrow while
        the day's ceiling reads zero is the failure this ledger exists to
        prevent. A broadcast also holds off the next attempt, because the
        caller's retry is automatic and the money it would spend again is not.
        """
        micros = settings.max_per_call_micros
        entry_id = self._ledger.commit(LEDGER_TOOL, micros)

        def reconcile(action, **kwargs):
            # Bookkeeping must never be the reason a caller loses a machine it
            # paid for, or the reason a failure is reported as the wrong one.
            try:
                getattr(self._ledger, action)(entry_id, **kwargs)
            except Exception:
                logger.warning("Prism: could not %s the ledger entry for the lease",
                               action, exc_info=True)

        try:
            lease = self._agent.lease(
                image=settings.image,
                duration_seconds=settings.lease_seconds,
                min_vram_mib=settings.min_vram_mib,
                max_deposit=micros,
                min_trust_class=settings.trust_class,
            )
        except Exception as e:
            if not funding_may_be_live(e):
                reconcile("revert")
                raise
            reference = broadcast_reference(e)
            if reference:
                # The ledger keys its deduplication on this. Two attempts can
                # only report one hash when one signed transaction went out
                # twice, which is what a re-broadcast USDG approval at an
                # unchanged nonce is, so collapsing them is the right answer.
                reconcile("settle", reference=reference)
            elif isinstance(e, PrismError):
                reconcile("settle")  # sent, then unreadable: no hash to pin it to
            else:
                logger.warning(
                    "Prism: funding failed below the SDK; the reservation stands because "
                    "a signed transaction may still be in a mempool", exc_info=True,
                )
            if (lease_id := refundable_lease(e)) is not None:
                watch_for_refund(self._agent, self._ledger, lease_id=lease_id,
                                 entry_id=entry_id, reference=reference)
            self._hold_funding(e)
            raise

        reconcile("settle", micros=self._deposited(lease, micros),
                  reference=lease.funding_hash)
        return lease

    @staticmethod
    def _deposited(lease, cap: int) -> int:
        """What the escrow took, clamped to the reservation it was funded under.

        A quote that asks for more than the cap is the supplier's figure and the
        day is charged the operator's. A quote that asks for nothing is charged
        nothing, which is why zero is read as an amount rather than as an
        absence: only a lease that says nothing at all about its deposit leaves
        the reservation standing.
        """
        for amount in (getattr(lease, "deposit", None),
                       (lease.quote or {}).get("maximum_escrow")):
            if amount is None:
                continue
            try:
                return max(0, min(int(amount), cap))
            except (TypeError, ValueError):
                logger.warning("Prism: ignoring an unreadable deposit figure %r", amount)
        return cap

    def _probe_home(self, lease) -> str:
        """First contact. A machine that never answers here is not a machine.

        The escrow has granted access, so the wallet is paying from this
        moment; a host whose sshd never comes up is released and reported as a
        failed provision, in one line, rather than handed to the model as a
        shell that refuses every command. The wait is bounded by the window
        too: a machine that answers with no time left to use is the same
        machine, and waiting for it would have the model's first command rent
        the next one.
        """
        window = max(MIN_COMMAND_S, int(self._expires_at - time.monotonic()) - MIN_COMMAND_S)
        deadline = min(PROBE_TIMEOUT_S, window)
        try:
            res = self._run_bounded(lease, "echo $HOME", deadline=deadline,
                                    timeout=PROBE_COMMAND_S)
        except Exception as e:
            logger.debug("Prism: home probe failed", exc_info=True)
            raise PrismLeaseError(
                f"Prism: lease {lease.lease_id} opened access but its machine could not "
                f"be reached in {deadline}s ({e}). It has been released; the "
                "seconds it was open settle on-chain and the next command rents a "
                "fresh GPU."
            ) from e
        if time.monotonic() >= self._expires_at:
            raise PrismLeaseError(
                f"Prism: lease {lease.lease_id} answered only as its paid window ran out. "
                "It has been released; the seconds it was open settle on-chain and the "
                "next command rents a fresh GPU."
            )
        if res.get("code") in UNREACHABLE_CODES:
            raise PrismLeaseError(
                f"Prism: lease {lease.lease_id} opened access but nothing answered on "
                f"{lease.access.get('ssh_host')}:{lease.access.get('ssh_port')} within "
                f"{deadline}s: {(res.get('stderr') or '').strip()[:160]}. It has "
                "been released; the seconds it was open settle on-chain and the next "
                "command rents a fresh GPU."
            )
        home = (res.get("stdout") or "").strip().splitlines()
        return home[-1] if home and home[-1].startswith("/") else "/root"

    def lease_info(self) -> dict | None:
        lease = self._lease
        if lease is None:
            return None
        return {
            "lease_id": lease.lease_id,
            "gpu": gpu_model(lease.quote),
            "task_id": self._task_id,
            "seconds_left": max(0, int(self._expires_at - time.monotonic())),
        }

    def capsule(self) -> dict | None:
        """The proof capsule for the lease being held, as it stands right now."""
        capsule = self._capsule
        return capsule.to_dict() if capsule is not None else None

    # ------------------------------------------------------------------
    # File sync
    # ------------------------------------------------------------------

    def _files_to_sync(self) -> list[tuple[str, str]]:
        from tools.credential_files import (
            get_credential_file_mounts,
            iter_cache_files,
            iter_skills_files,
        )

        base = f"{self._remote_home}/.hermes"
        files = [
            (entry["host_path"], entry["container_path"])
            for entry in (*iter_skills_files(container_base=base),
                          *iter_cache_files(container_base=base))
        ]
        # Credentials are opt-in, unlike every bind-mounted backend. At trust
        # class "open" the supplier can read anything the workload touches, so
        # pushing the operator's API keys onto a stranger's machine has to be a
        # decision rather than a default.
        if self._settings.sync_credentials:
            files += [
                (entry["host_path"],
                 entry["container_path"].replace("/root/.hermes", base, 1))
                for entry in get_credential_file_mounts()
            ]
        return files

    def _upload(self, host_path: str, remote_path: str) -> None:
        data = Path(host_path).read_bytes()
        if len(data) > MAX_UPLOAD_BYTES:
            # Skipped rather than raised: a raise rolls the whole cycle back and
            # retries it forever, and this file will never get smaller.
            logger.warning(
                "Prism: %s is %d bytes, past the %d byte inline transfer limit — not synced",
                host_path, len(data), MAX_UPLOAD_BYTES,
            )
            return
        parent = posixpath.dirname(remote_path)
        command = (
            f"mkdir -p {shlex.quote(parent)} && umask 077 && "
            f"base64 -d > {shlex.quote(remote_path)}"
        )
        payload = base64.b64encode(data).decode("ascii") + "\n"
        try:
            res = self._run_bounded(self._lease, command, deadline=UPLOAD_TIMEOUT_S,
                                    timeout=UPLOAD_COMMAND_S, stdin=payload)
        except _SetupTimedOut as e:
            raise PrismLeaseError(
                f"Prism: writing {remote_path} to the leased GPU did not finish inside "
                f"{UPLOAD_TIMEOUT_S}s"
            ) from e
        if int(res.get("code") or 0) != 0:
            raise PrismLeaseError(
                f"Prism: could not write {remote_path}: "
                f"{res.get('stderr') or res.get('stdout') or 'exit ' + str(res.get('code'))}"
            )
        if (capsule := self._capsule) is not None:
            capsule.record_artifact(remote_path, data)

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Every file of a sync cycle in one exec, as a tar stream on stdin.

        Files past the inline limit are left out with a warning, as the single
        upload leaves them out, so a cycle is never rolled back for a file that
        will never fit.
        """
        buffer = io.BytesIO()
        recorded: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for host_path, remote_path in files:
                data = Path(host_path).read_bytes()
                if len(data) > MAX_UPLOAD_BYTES:
                    logger.warning(
                        "Prism: %s is %d bytes, past the %d byte inline transfer limit — not synced",
                        host_path, len(data), MAX_UPLOAD_BYTES,
                    )
                    continue
                info = tarfile.TarInfo(name=remote_path.lstrip("/"))
                info.size = len(data)
                info.mode = 0o600
                info.mtime = int(time.time())
                archive.addfile(info, io.BytesIO(data))
                recorded.append((remote_path, data))
        payload = buffer.getvalue()
        if len(payload) > MAX_BULK_UPLOAD_BYTES:
            raise PrismLeaseError(
                f"Prism: the files Hermes syncs to a rented machine come to {len(payload)} "
                f"bytes compressed, past the {MAX_BULK_UPLOAD_BYTES} byte limit. Trim the "
                "skills and cache directories this profile syncs, or set "
                "terminal.prism.sync_credentials and the skills external_dirs to what the "
                "session needs."
            )
        # Staged rather than piped: the archive is written, decoded and only
        # then unpacked, each step its own command. A decode piped straight
        # into an extractor is the shape a malware scanner is built to catch,
        # and a backend that cannot pass `hermes plugins install` cannot be
        # installed by anyone.
        stage = f"/tmp/prism-sync-{self._task_id}"
        command = (
            f"umask 077 && cat > {shlex.quote(stage + '.b64')} && "
            f"base64 -d {shlex.quote(stage + '.b64')} > {shlex.quote(stage + '.tar.gz')} && "
            f"rm -f {shlex.quote(stage + '.b64')} && "
            f"tar -xzf {shlex.quote(stage + '.tar.gz')} -C / && "
            f"rm -f {shlex.quote(stage + '.tar.gz')}"
        )
        encoded = base64.b64encode(payload).decode("ascii") + "\n"
        try:
            res = self._run_bounded(self._lease, command, deadline=BULK_UPLOAD_TIMEOUT_S,
                                    timeout=BULK_UPLOAD_COMMAND_S, stdin=encoded)
        except _SetupTimedOut as e:
            raise PrismLeaseError(
                f"Prism: syncing {len(recorded)} files to the leased GPU did not finish "
                f"inside {BULK_UPLOAD_TIMEOUT_S}s"
            ) from e
        if int(res.get("code") or 0) != 0:
            raise PrismLeaseError(
                f"Prism: could not sync {len(recorded)} files: "
                f"{res.get('stderr') or res.get('stdout') or 'exit ' + str(res.get('code'))}"
            )
        if (capsule := self._capsule) is not None:
            for remote_path, data in recorded:
                capsule.record_artifact(remote_path, data)

    def _delete(self, remote_paths: list[str]) -> None:
        try:
            res = self._run_bounded(self._lease, quoted_rm_command(remote_paths),
                                    deadline=DELETE_TIMEOUT_S, timeout=DELETE_COMMAND_S)
        except _SetupTimedOut as e:
            raise PrismLeaseError(
                f"Prism: removing {remote_paths} from the leased GPU did not finish "
                f"inside {DELETE_TIMEOUT_S}s"
            ) from e
        if int(res.get("code") or 0) != 0:
            # Propagates so FileSyncManager rolls back and retries. Committing a
            # deletion that did not happen drops the path from the synced set,
            # and the file stays on a machine somebody else operates.
            raise PrismLeaseError(f"Prism: could not remove {remote_paths}: {res.get('stderr')}")

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def init_session(self) -> None:
        # The bootstrap runs on the internal snapshot timeout, which is not a
        # number the session chose and cannot act on. Marking it keeps the
        # overrun note about the caller's own timeout.
        self._setup.active = True
        try:
            super().init_session()
        finally:
            self._setup.active = False

    def _before_execute(self) -> None:
        self._ensure_lease()
        if self._sync_manager is not None:
            self._sync_manager.sync()

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        # Ahead of super(), because its _before_execute hook is what rents the
        # GPU. The same check in _run_bash runs on the far side of that and
        # refuses a command the wallet has already paid for.
        timeout = kwargs.get("timeout")
        _check_deadline(int(timeout) if timeout and timeout > 0 else self.timeout)
        with self._lock:
            self._in_flight += 1
        try:
            result = super().execute(command, cwd, **kwargs)
        finally:
            with self._lock:
                self._in_flight -= 1
                self._last_command_at = time.monotonic()
        if notices := self._drain_notices():
            result["output"] = "\n".join([*notices, result.get("output") or ""]).rstrip("\n")
        return result

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120, stdin_data: str | None = None):
        deadline = int(timeout) if timeout and timeout > 0 else self.timeout
        _check_deadline(deadline)  # refuse before renting, not after
        lease = self._ensure_lease()
        shell = "bash -l -c" if login else "bash -c"
        remote = f"{shell} {shlex.quote(cmd_string)}"
        # Read here rather than in exec_fn: this runs on the thread that set it,
        # exec_fn does not.
        mine = not getattr(self._setup, "active", False)

        def exec_fn() -> tuple[str, int]:
            # A model command's deadline is its own timeout, so the SDK's
            # overhead has nowhere to come from but the wait.
            budget = self._exec_budget(deadline, deadline)
            if budget.overruns and mine:
                self._note_overrun(budget)
            try:
                res = self._ssh(lease, remote, budget, stdin=stdin_data)
            except PrismError as e:
                return (f"prism: the leased GPU could not be reached ({e.code})\n", 255)
            code = int(res.get("code") or 0)
            # 255 is every connect attempt having failed and -1 the exec running
            # past its deadline. Neither is the machine answering, and treating
            # them as one drops the next command to two retries against a box
            # that has never said anything.
            if code not in UNREACHABLE_CODES:
                self._reachable = True
            stdout, stderr = res.get("stdout") or "", res.get("stderr") or ""
            if (capsule := self._capsule) is not None:
                capsule.record_output(stdout, stderr)
            output = "\n".join(p for p in (stdout, stderr) if p)
            if code == -1:
                return (f"{output}\n[the GPU did not answer within {budget.waited}s]", 124)
            return (output, code)

        # The SDK gives no handle on the running SSH process, so /stop unblocks
        # the agent without stopping the remote command. What ends it is the
        # bound the SDK puts on its own ssh subprocess, which is the command's
        # timeout plus EXEC_OVERHEAD_S and never less.
        return _ThreadedProcessHandle(exec_fn, cancel_fn=None)

    def _ssh(self, lease, command: str, budget: ExecBudget, stdin: str | None = None) -> dict:
        return self._agent.run(lease, command, timeout=budget.command_timeout,
                               connect_retries=budget.retries,
                               connect_delay=CONNECT_DELAY_S, stdin=stdin)

    def _run_bounded(self, lease, command: str, *, deadline: int, timeout: int,
                     stdin: str | None = None) -> dict:
        """One SSH exec, with a backstop on how long it may sit there.

        Setup steps run inside ``_before_execute``, ahead of the backstop in
        ``execute()``, so both halves are needed: a retry budget the deadline
        pays for, and a bound that abandons the call if the SDK sits on it
        anyway. The bound follows the budget rather than the deadline, because a
        command whose own timeout outlasts the deadline is still owed its run.
        """
        budget = self._exec_budget(deadline, timeout)
        wall = max(deadline, budget.waited) + BOUND_GRACE_S
        return _bounded(lambda: self._ssh(lease, command, budget, stdin=stdin),
                        wall, f"prism.setup:{self._task_id}")

    def _exec_budget(self, deadline: int, timeout: int) -> ExecBudget:
        """Price one exec the way the SDK runs it.

        The SDK makes ``connect_retries + 1`` attempts, sleeps CONNECT_DELAY_S
        between them, and bounds every ssh subprocess at the command timeout
        plus EXEC_OVERHEAD_S. So the attempt that answers costs
        ``timeout + EXEC_OVERHEAD_S`` and each retry before it costs ATTEMPT_S.

        The command's timeout is never touched: shortening it to make room for
        warm-up charges the run for attempts it did not need, and a caller that
        asks for more GPU time would get less. Retries take what the deadline
        has left, and none when it has nothing left.
        """
        _check_deadline(deadline)
        ceiling = STEADY_RETRIES if self._reachable else WARMUP_RETRIES
        room = deadline - (timeout + EXEC_OVERHEAD_S)
        retries = max(0, min(ceiling, room // ATTEMPT_S))
        return ExecBudget(retries, timeout, deadline)

    def _note_overrun(self, budget: ExecBudget) -> None:
        """Say once per session that a deadline cannot bound its own command.

        Once, because it is a property of how the caller sets its timeouts
        rather than of any one command, and a banner on every result is a
        banner the model stops reading.
        """
        with self._notice_lock:
            if self._noted_overrun:
                return
            self._noted_overrun = True
        self._note(budget.overrun_note)

    # ------------------------------------------------------------------
    # Idle release
    # ------------------------------------------------------------------

    def _start_idle_monitor(self) -> None:
        if self._monitor is not None or self._settings.idle_release_seconds <= 0:
            return
        self._monitor = threading.Thread(target=self._watch_idle,
                                         name=f"prism-idle:{self._task_id}", daemon=True)
        self._monitor.start()

    def _watch_idle(self) -> None:
        while not self._stopped.wait(IDLE_POLL_S):
            try:
                self._release_if_idle()
            except Exception:
                logger.debug("Prism: idle check failed", exc_info=True)

    def _release_if_idle(self) -> bool:
        """Give the GPU back when the session has stopped using it.

        A lease is paid for whether or not anything is running on it, and an
        agent that goes quiet mid-session is the common case. The next command
        rents again, under the same caps, so this costs a provisioning wait
        rather than the work.
        """
        idle_after = self._settings.idle_release_seconds
        if idle_after <= 0:
            return False
        with self._lock:
            if self._lease is None or self._in_flight:
                return False
            idle_for = time.monotonic() - self._last_command_at
            if idle_for < idle_after:
                return False
            # The model has to be told the disk went with the machine, or it
            # spends its next command wondering where its files are.
            self._note(
                f"prism: lease {self._lease.lease_id} was idle for "
                f"{int(idle_for // 60)}m and has been given back. Its disk is gone; "
                "the next command rents a fresh GPU."
            )
            self._release()
            return True

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _note(self, line: str) -> None:
        """Put one line in front of the session's next command output."""
        logger.info("%s", line)
        with self._notice_lock:
            self._notices.append(line)

    def _drain_notices(self) -> list[str]:
        with self._notice_lock:
            notices, self._notices = self._notices, []
        return notices

    def _release(self) -> None:
        lease, self._lease = self._lease, None
        if lease is None:
            return
        # Releasing on the network is what stops the meter: settlement charges
        # the seconds between access opening and the release. A release the
        # network refuses leaves the machine billing until its window ends, and
        # that is worth a line in the transcript, not only the log.
        try:
            released = self._agent.end_lease(lease) or {}
            release = str(released.get("release") or "queued")
        except Exception as e:
            release = f"failed: {e}"
            logger.warning("Prism: releasing lease %s failed: %s", lease.lease_id, e)
            self._note(
                f"prism: lease {lease.lease_id} could not be released ({e}). Its access "
                "key is gone but it may bill until its window ends; the receipt on "
                f"{PROOF_URL} shows the settled charge."
            )
        if self._capsule is not None:
            self._capsule.release = release
        self._write_capsule(lease)
        logger.info("Prism: released lease %s (%s)", lease.lease_id, release)

    def _write_capsule(self, lease) -> None:
        capsule, self._capsule = self._capsule, None
        if capsule is None:
            return
        capsule.close()
        try:
            path = write_capsule(capsule)
        except OSError as e:
            logger.warning("Prism: could not write the capsule for lease %s: %s",
                           lease.lease_id, e)
            return
        self._note(f"prism: lease {lease.lease_id} released, capsule {path}")

    def cleanup(self):
        lock = getattr(self, "_lock", None)
        if lock is None:
            return  # constructor failed before the lock existed
        self._stopped.set()
        with lock:
            self._release()
