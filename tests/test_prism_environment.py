"""Unit tests for the Prism terminal backend.

Everything is mocked at the :class:`prismnetwork.PrismAgent` boundary: no
network, no wallet, no chain. What is under test is the part this plugin owns —
when a GPU is rented, when it is not, and what the spend ledger says about it
either way.
"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from prismnetwork import Lease, PrismError, usdg

import prism_environment
from prism_environment import (
    CAPSULE_FALLBACK_DIR,
    DELETE_COMMAND_S,
    DELETE_TIMEOUT_S,
    EXEC_OVERHEAD_S,
    EXPIRY_MARGIN_S,
    MIN_COMMAND_S,
    MIN_LEASE_SECONDS,
    PROVISION_BUDGET_S,
    PROBE_COMMAND_S,
    PROBE_TIMEOUT_S,
    STEADY_RETRIES,
    UPLOAD_COMMAND_S,
    UPLOAD_TIMEOUT_S,
    WARMUP_RETRIES,
    PrismEnvironment,
    PrismLeaseError,
)

MICROS = 1_000_000
#: Marks a value the SDK does not report at all, which is not the same answer
#: as reporting zero.
UNSET = object()


def make_lease(lease_id=1, deposit=750_000, duration=3600, gpu="H100"):
    quote = {
        "quote_id": f"q-{lease_id}",
        "maximum_escrow": deposit,
        "duration_seconds": duration,
        "gpu": {"model": gpu, "vram_mib": 81920},
    }
    if deposit is UNSET:
        del quote["maximum_escrow"]
    return Lease(
        lease_id=lease_id,
        access={"ssh_host": "gpu.invalid", "ssh_port": 2222, "ssh_user": "root"},
        key_path=f"/tmp/prism-test-{lease_id}/id_ed25519",
        key_dir=f"/tmp/prism-test-{lease_id}",
        public_key="ssh-ed25519 AAAA test",
        funding_hash=f"0x{lease_id:064x}",
        quote=quote,
    )


def sdk_wall(timeout, retries, delay=None):
    """The worst wall time ``PrismAgent.run`` can take for these arguments.

    It makes ``connect_retries + 1`` attempts, sleeps ``connect_delay`` between
    them, and bounds every ssh subprocess at ``timeout + 20`` with
    ``ConnectTimeout=15``. The worst case is every retry burning its connect
    deadline and the attempt that answers running to that bound. Written out
    from the SDK rather than from the plugin, so it can disagree with it.
    """
    delay = prism_environment.CONNECT_DELAY_S if delay is None else delay
    return retries * (prism_environment.SSH_CONNECT_TIMEOUT_S + delay) + timeout + 20


@dataclass
class SignedLease(Lease):
    """A lease that reports what the escrow was actually funded with, which the
    SDK signs for and the quote only asked for."""

    deposit: int = 0


class FakeAgent:
    """A PrismAgent that never touches a chain or a machine."""

    def __init__(self):
        self.lease_calls = []
        self.run_calls = []
        self.ended = []
        self.lease_error = None
        self.on_lease = None
        self.deposit = 750_000
        # What the quote says the escrow pays for, which is the supplier's
        # answer to the duration that was asked for rather than an echo of it.
        self.duration = 3600
        # What the quote asks the escrow for, when that is not the figure the
        # caller's ceiling was checked against.
        self.quoted_escrow = None
        # What the SDK says the escrow took, when it reports one at all.
        self.signed_deposit = None
        self.result = {"code": 0, "stdout": "ok", "stderr": ""}
        # Per-command answers, keyed on the start of the remote command. The
        # login shell only ever runs the session bootstrap.
        self.results = {}
        self._next_id = 1
        # What leases() says about state_lease_id, one answer per call. Past the
        # end the last answer repeats, the way a settled state on chain does.
        self.states = ["provisioning"]
        self.state_lease_id = 3
        self.state_polls = 0
        self.leases_error = None

    def lease(self, **kwargs):
        self.lease_calls.append(kwargs)
        if self.on_lease is not None:
            self.on_lease()
        if self.lease_error is not None:
            raise self.lease_error
        # The SDK checks the deposit against the caller's ceiling before it
        # funds anything, which is the whole point of passing one.
        if self.deposit > kwargs["max_deposit"]:
            raise PrismError(402, "cost_exceeds_max",
                             {"required": self.deposit, "max": kwargs["max_deposit"]})
        escrow = self.deposit if self.quoted_escrow is None else self.quoted_escrow
        lease = make_lease(self._next_id, deposit=escrow, duration=self.duration)
        self._next_id += 1
        if self.signed_deposit is None:
            return lease
        return SignedLease(**vars(lease), deposit=self.signed_deposit)

    # connect_retries defaults to the SDK's 24, so a caller that passes none
    # records what it would really have waited.
    def run(self, lease, command, timeout=120, connect_retries=24,
            connect_delay=prism_environment.CONNECT_DELAY_S, stdin=None):
        elapsed = sdk_wall(timeout, connect_retries, connect_delay)
        self.run_calls.append({"lease": lease, "command": command, "stdin": stdin,
                               "timeout": timeout, "retries": connect_retries,
                               "elapsed": elapsed})
        for prefix, result in self.results.items():
            if command.startswith(prefix):
                return dict(result)
        if command == "echo $HOME":
            return {"code": 0, "stdout": "/root", "stderr": ""}
        return dict(self.result)

    def leases(self):
        self.state_polls += 1
        if self.leases_error is not None:
            raise self.leases_error
        state = self.states[min(self.state_polls - 1, len(self.states) - 1)]
        return [{"lease_id": self.state_lease_id, "state": state}]

    def end_lease(self, lease):
        self.ended.append(lease.lease_id)
        return {"lease_id": lease.lease_id, "state": "active", "release": "queued"}

    @property
    def commands(self):
        return [call["command"] for call in self.run_calls]


class FakeClock:
    """The environment's clock, moved by hand.

    The funding hold and the idle window are both waits, and a test that sits
    through either for real is a test nobody runs.
    """

    def __init__(self):
        self.now = 1_000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_760_000_000.0 + (self.now - 1_000.0)

    def advance(self, seconds):
        self.now += seconds


class PrismTestCase(unittest.TestCase):
    """Shared harness: a fake wallet, a scratch ledger, and no host file sync."""

    config = {"max_usdg": 1, "daily_budget_usdg": 5}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prism-plugin-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = os.path.join(self.tmp, "spend.json")
        self.capsules = Path(self.tmp) / "capsules"
        self.agent = FakeAgent()

        env = {k: v for k, v in os.environ.items() if not k.startswith("PRISM_")}
        env["PRISM_LEDGER_PATH"] = self.ledger_path
        self._enter(mock.patch.dict(os.environ, env, clear=True))
        self._enter(mock.patch.object(prism_environment, "capsule_dir",
                                      lambda: self.capsules))
        self._enter(mock.patch.object(prism_environment, "terminal_prism_config",
                                      lambda: dict(self.config)))
        self._enter(mock.patch.object(prism_environment, "build_agent", lambda: self.agent))
        # Hermes' real default ceiling (420 s) cannot hold a provision and the
        # backend refuses under it; the guard has its own test.
        self._unpatched_ceiling = prism_environment.sequential_tool_ceiling
        self._enter(mock.patch.object(prism_environment, "sequential_tool_ceiling",
                                      lambda: None))
        # A refund watch outlives the environment that started it by design, so
        # nothing here may leave one polling on the real minute-long interval.
        self._enter(mock.patch.object(prism_environment, "REFUND_POLL_S", 0))
        prism_environment._watchers.clear()
        self.addCleanup(prism_environment._watchers.clear)
        self._enter(mock.patch("tools.environments.base.is_interrupted", lambda: False))
        for name in ("iter_skills_files", "iter_cache_files", "get_credential_file_mounts"):
            self._enter(mock.patch(f"tools.credential_files.{name}", lambda **kw: []))

    def _enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_env(self, **kwargs):
        env = PrismEnvironment(cwd=kwargs.pop("cwd", "/root"),
                               timeout=kwargs.pop("timeout", 30),
                               task_id=kwargs.pop("task_id", "task-1"))
        self.addCleanup(env.cleanup)
        return env

    def unconfirmed_fundings(self):
        """A wallet whose funding transaction never confirms, on a fresh hash."""
        attempts = iter(range(1, 20))

        def broadcast_and_lose_it():
            raise PrismError(504, "confirmation_timeout",
                             {"hash": f"0x{next(attempts):064x}"})

        self.agent.on_lease = broadcast_and_lose_it

    def fake_clock(self) -> FakeClock:
        clock = FakeClock()
        self._enter(mock.patch.object(prism_environment, "time", clock))
        return clock

    def entries(self, path=None):
        try:
            with open(path or self.ledger_path) as fh:
                return json.load(fh)["entries"]
        except FileNotFoundError:
            return []

    def seed_ledger(self, micros, path=None):
        with open(path or self.ledger_path, "w") as fh:
            json.dump({"version": 1, "entries": [
                {"id": "seed", "at": int(time.time() * 1000), "tool": "test", "micros": micros}
            ]}, fh)

    def written_capsule(self, lease_id=1) -> dict:
        return json.loads((self.capsules / f"{lease_id}.json").read_text())


class TestProvisioning(PrismTestCase):
    def test_construction_rents_nothing(self):
        env = self.make_env()
        self.assertEqual(self.agent.lease_calls, [])
        self.assertIsNone(env.lease_info())
        self.assertEqual(self.entries(), [])

    def test_first_command_rents_a_gpu(self):
        env = self.make_env()
        result = env.execute("nvidia-smi")

        self.assertEqual(len(self.agent.lease_calls), 1)
        self.assertEqual(result["returncode"], 0)
        self.assertIn("ok", result["output"])
        self.assertEqual(env.lease_info()["gpu"], "H100")

    def test_a_machine_that_never_answers_is_released_and_reported_as_one_line(self):
        self.agent.results["echo $HOME"] = {
            "code": 255, "stdout": "", "stderr": "ssh: connect to host 1.2.3.4 port 40022: Connection refused",
        }
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("nvidia-smi")

        self.assertEqual(len(self.agent.lease_calls), 1)
        self.assertEqual(len(self.agent.ended), 1)
        self.assertIn("nothing answered", str(raised.exception))
        self.assertIn("released", str(raised.exception))
        self.assertIsNone(env.lease_info())
        # The next command rents again rather than retrying the dead machine.
        self.agent.results.pop("echo $HOME")
        env.execute("nvidia-smi")
        self.assertEqual(len(self.agent.lease_calls), 2)

    def test_a_slow_first_contact_never_rents_a_second_gpu_from_inside_the_first(self):
        # Seen live: the home probe took longer than the paid window, the
        # bootstrap's own command re-entered _ensure_lease, and a second escrow
        # was funded while the first was still being set up.
        clock = self.fake_clock()
        self.agent.duration = 600

        def slow_probe(lease, command, **kw):
            if command == "echo $HOME":
                clock.advance(700)
                return {"code": 0, "stdout": "/root", "stderr": ""}
            return {"code": 0, "stdout": "ok", "stderr": ""}

        self.agent.run = slow_probe
        env = self.make_env()
        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("nvidia-smi")

        self.assertIn("ran out", str(raised.exception))
        self.assertEqual(len(self.agent.lease_calls), 1)
        self.assertEqual(len(self.agent.ended), 1)
        self.assertIsNone(env.lease_info())

    def test_first_contact_is_given_every_warm_up_attempt(self):
        env = self.make_env()
        env.execute("true")
        probe = next(c for c in self.agent.run_calls if c["command"] == "echo $HOME")
        self.assertEqual(probe["retries"], prism_environment.WARMUP_RETRIES)

    def test_second_command_reuses_the_lease(self):
        env = self.make_env()
        env.execute("echo one")
        env.execute("echo two")

        self.assertEqual(len(self.agent.lease_calls), 1)
        self.assertEqual(self.agent.ended, [])

    def test_lease_request_carries_the_configured_shape(self):
        self.config = {"max_usdg": 2, "daily_budget_usdg": 5, "min_vram_mib": 40000,
                       "trust_class": "attested", "lease_seconds": 900}
        env = self.make_env()
        env.execute("true")

        request = self.agent.lease_calls[0]
        self.assertEqual(request["min_vram_mib"], 40000)
        self.assertEqual(request["min_trust_class"], "attested")
        self.assertEqual(request["duration_seconds"], 900)
        self.assertEqual(request["max_deposit"], 2 * MICROS)

    def test_commands_run_through_a_login_free_shell_on_the_lease(self):
        env = self.make_env()
        env.execute("echo hi")

        remote = self.agent.commands[-1]
        self.assertTrue(remote.startswith("bash -c "))
        self.assertIn("echo hi", remote)

    def test_a_command_with_no_time_to_run_in_is_refused_before_it_is_rented(self):
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true", timeout=MIN_COMMAND_S - 1)
        self.assertEqual(self.agent.lease_calls, [])
        self.assertEqual(self.entries(), [])

    def test_expired_lease_is_replaced(self):
        env = self.make_env()
        env.execute("echo one")
        env._expires_at = time.monotonic() - 1
        env.execute("echo two")

        self.assertEqual(len(self.agent.lease_calls), 2)
        self.assertEqual(self.agent.ended, [1])
        self.assertEqual(env.lease_info()["lease_id"], 2)


class TestLeaseWindow(PrismTestCase):
    """A lease is usable for what it was paid for, less the settlement margin.

    The escrow closes at the end of the paid window and cuts off whatever is
    running when it does, so the last EXPIRY_MARGIN_S are not time the session
    can spend.
    """

    def test_the_margin_comes_off_the_window_it_exists_to_protect(self):
        self.fake_clock()
        for duration, usable in ((3600, 3540), (600, 540), (120, 60), (90, 30)):
            with self.subTest(duration=duration):
                self.agent.duration = duration
                env = self.make_env()
                env.execute("true")
                self.assertEqual(env.lease_info()["seconds_left"], usable)

    def test_a_configured_lease_too_short_to_use_is_refused_before_it_is_rented(self):
        self.config = {"max_usdg": 1, "daily_budget_usdg": 5,
                       "lease_seconds": MIN_LEASE_SECONDS - 1}

        with self.assertRaises(PrismLeaseError) as raised:
            self.make_env()
        self.assertIn("lease_seconds", str(raised.exception))
        self.assertEqual(self.agent.lease_calls, [])

    def test_a_tool_ceiling_that_cannot_hold_a_provision_is_refused_before_it_is_rented(self):
        self.config = {"max_usdg": 1, "daily_budget_usdg": 5}
        for ceiling, rents in ((420.0, False), (PROVISION_BUDGET_S - 1, False),
                               (PROVISION_BUDGET_S, True), (None, True)):
            with self.subTest(ceiling=ceiling):
                with mock.patch.object(prism_environment, "sequential_tool_ceiling",
                                       lambda c=ceiling: c):
                    if rents:
                        self.make_env()
                        continue
                    with self.assertRaises(PrismLeaseError) as raised:
                        self.make_env()
                    self.assertIn("timeouts.tools.sequential_call", str(raised.exception))
                    self.assertIn(str(PROVISION_BUDGET_S), str(raised.exception))
        self.assertEqual(self.agent.lease_calls, [])

    def test_the_tool_ceiling_is_read_through_hermes_own_resolver(self):
        from agent import tool_executor

        read = self._unpatched_ceiling
        for resolved, expected in ((420.0, 420.0), (0, None), (-1, None), (None, None),
                                   (900, 900.0)):
            with self.subTest(resolved=resolved):
                with mock.patch.object(tool_executor, "_resolve_sequential_tool_timeout",
                                       lambda r=resolved: r):
                    self.assertEqual(read(), expected)

    def test_a_quote_with_no_usable_window_is_handed_back_rather_than_run_against(self):
        self.fake_clock()
        self.agent.duration = EXPIRY_MARGIN_S
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertIn("given back", str(raised.exception))
        self.assertEqual(self.agent.ended, [1])
        self.assertIsNone(env.lease_info())

        # The terminal tool retries what it cannot read as a command timeout, and
        # a second escrow for a window that was unusable the first time is money
        # spent on nothing.
        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        self.assertEqual(len(self.agent.lease_calls), 1)


class TestSpendLedger(PrismTestCase):
    def test_spend_is_written_before_the_money_moves(self):
        seen = []
        self.agent.on_lease = lambda: seen.append(self.entries())
        env = self.make_env()
        env.execute("true")

        self.assertEqual(len(seen[0]), 1, "the lease was funded before the ledger was written")
        self.assertEqual(seen[0][0]["micros"], MICROS)
        self.assertEqual(seen[0][0]["tool"], "hermes terminal")

    def test_a_failure_before_funding_gives_the_reservation_back(self):
        self.agent.lease_error = PrismError(402, "wallet_unfunded", {
            "address": "0xdead", "usdg": 0, "eth_wei": 0,
        })
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertIn("Fund it on Robinhood Chain", str(raised.exception))
        self.assertEqual(self.entries(), [])

    def test_a_failure_after_funding_keeps_the_spend(self):
        self.agent.lease_error = PrismError(502, "lease_failed_after_funding", {
            "funding_hash": "0xfeed", "lease_id": 7, "key_path": "/tmp/k",
        })
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["reference"], "0xfeed")

    def test_the_settled_entry_records_what_the_escrow_actually_took(self):
        env = self.make_env()
        env.execute("true")

        entry = self.entries()[0]
        self.assertEqual(entry["micros"], 750_000)
        self.assertEqual(entry["reference"], make_lease(1).funding_hash)

    def test_the_per_lease_cap_bounds_the_deposit_and_refuses_above_it(self):
        self.agent.deposit = 4 * MICROS
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertEqual(self.agent.lease_calls[0]["max_deposit"], MICROS)
        self.assertIn("per-lease cap", str(raised.exception))
        self.assertEqual(self.entries(), [], "a quote that was never funded must not be charged")

    def test_the_daily_cap_refuses_before_any_funding_call(self):
        self.config = {"max_usdg": 1, "daily_budget_usdg": 1}
        self.seed_ledger(900_000)
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertEqual(self.agent.lease_calls, [], "the wallet was asked to pay past the daily cap")
        self.assertIn("daily cap", str(raised.exception))
        self.assertEqual([e["id"] for e in self.entries()], ["seed"])

    def test_a_per_lease_cap_above_the_daily_one_is_refused_as_configuration(self):
        self.config = {"max_usdg": 10, "daily_budget_usdg": 5}
        with self.assertRaises(Exception) as raised:
            self.make_env()
        self.assertIn("PRISM_DAILY_BUDGET_USDG", str(raised.exception))
        self.assertEqual(self.agent.lease_calls, [])

    def test_an_escrow_above_the_cap_is_booked_at_the_cap(self):
        # A quote that comes back above what was asked for is the supplier's
        # figure; what the day is charged is the operator's. The ledger clamps
        # this too — the backend does not hand it the number to argue with.
        self.agent.quoted_escrow = 4 * MICROS
        env = self.make_env()
        with mock.patch.object(prism_environment.SpendLedger, "settle", autospec=True) as settle:
            env.execute("true")

        self.assertEqual(settle.call_args.kwargs["micros"], MICROS)
        self.assertEqual(self.entries()[0]["micros"], MICROS)

    def test_lowering_the_daily_cap_mid_session_takes_effect(self):
        env = self.make_env()
        env.execute("true")
        self.config = {"max_usdg": 0.2, "daily_budget_usdg": 0.5}
        env._expires_at = time.monotonic() - 1

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertIn("daily cap", str(raised.exception))
        self.assertEqual(len(self.agent.lease_calls), 1)

    def test_an_unreadable_config_stops_the_lease_mid_session(self):
        env = self.make_env()
        broken = PrismLeaseError(
            "Prism will not rent a GPU while ~/.hermes/config.yaml cannot be read"
        )
        with mock.patch.object(prism_environment, "terminal_prism_config",
                               mock.Mock(side_effect=broken)):
            with self.assertRaises(PrismLeaseError) as raised:
                env.execute("true")

        self.assertIn("config.yaml", str(raised.exception))
        self.assertEqual(self.agent.lease_calls, [], "a GPU was rented on caps nobody chose")
        self.assertEqual(self.entries(), [])

    def test_config_caps_win_over_stale_exported_ones(self):
        self.config = {"max_usdg": 0.25, "daily_budget_usdg": 2}
        with mock.patch.dict(os.environ, {"PRISM_MAX_USDG": "9", "PRISM_DAILY_BUDGET_USDG": "9"}):
            settings = prism_environment.read_settings()
        self.assertEqual(settings.max_per_call_micros, 250_000)
        self.assertEqual(settings.daily_micros, 2 * MICROS)

    def test_the_configured_ledger_wins_over_an_exported_one(self):
        pinned = os.path.join(self.tmp, "pinned.json")
        self.config = {"max_usdg": 1, "daily_budget_usdg": 2, "ledger_path": pinned}
        with mock.patch.dict(os.environ, {"PRISM_LEDGER_PATH": "/tmp/elsewhere.json"}):
            settings = prism_environment.read_settings()
        self.assertEqual(settings.ledger_path, pinned)

    def test_a_repointed_ledger_cannot_hand_back_a_day_already_spent(self):
        # The caps are counted in a file, so the file is a cap. Pinning it in
        # config has to bind as hard as pinning the numbers does.
        pinned = os.path.join(self.tmp, "pinned.json")
        self.config = {"max_usdg": 1, "daily_budget_usdg": 2, "ledger_path": pinned}
        self.seed_ledger(2 * MICROS, path=pinned)
        env = self.make_env()

        with mock.patch.dict(os.environ, {"PRISM_LEDGER_PATH": os.path.join(self.tmp, "fresh.json")}):
            with self.assertRaises(PrismLeaseError) as raised:
                env.execute("true")

        self.assertIn("daily cap", str(raised.exception))
        self.assertEqual(self.agent.lease_calls, [])
        self.assertEqual([e["id"] for e in self.entries(pinned)], ["seed"])


class TestErrorText(unittest.TestCase):
    """The terminal tool reads any error text holding "timeout" as a command
    timeout and throws the rest away, so borrowed text is put in the past tense
    on the way out. The exception class is the diagnostic and has to survive it.
    """

    def test_the_exception_class_survives(self):
        for raw, expected in (
            ("requests.exceptions.ReadTimeout: read timed out",
             "requests.exceptions.ReadTimedOut: read timed out"),
            ("TimeoutError: no route to host", "TimedOutError: no route to host"),
            ("urllib3.exceptions.ConnectTimeoutError",
             "urllib3.exceptions.ConnectTimedOutError"),
            ("read_timeout hit", "read_timed_out hit"),
            ("connection timeout after 30s", "connection timed out after 30s"),
        ):
            with self.subTest(raw=raw):
                plain = prism_environment._plain(raw)
                self.assertEqual(plain, expected)
                self.assertNotIn("timeout", plain.lower())


class TestCapacity(PrismTestCase):
    def test_no_capacity_says_so_and_says_to_retry(self):
        self.agent.lease_error = PrismError(404, "no_capacity", {})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        message = str(raised.exception)
        self.assertIn("No Prism GPU is available", message)
        self.assertIn("Retry", message)
        # The terminal tool reports any error text containing "timeout" as a
        # command timeout, which would bury the real reason.
        self.assertNotIn("timeout", message.lower())
        self.assertEqual(self.entries(), [])

    def test_a_control_plane_failure_is_not_dressed_up_as_a_command_timeout(self):
        self.agent.lease_error = PrismError(504, "confirmation_timeout", {"hash": "0x1"})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertNotIn("timeout", str(raised.exception).lower())

    def test_a_funded_failure_is_never_reported_as_a_free_one(self):
        # Same status a missing offer answers with, but this one funded first.
        self.agent.lease_error = PrismError(404, "access_timeout",
                                            {"funding_hash": "0xfeed", "lease_id": 3})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        message = str(raised.exception)
        self.assertIn("0xfeed", message)
        self.assertNotIn("nothing was charged", message)
        self.assertEqual(self.entries()[0]["reference"], "0xfeed")

    def test_an_empty_network_can_be_tried_again_at_once(self):
        # Nothing was funded, so nothing is held: the hold is about money, and a
        # capacity answer costs none.
        self.fake_clock()
        self.agent.lease_error = PrismError(503, "no_capacity", {})
        env = self.make_env()

        for _ in range(3):
            with self.assertRaises(PrismLeaseError):
                env.execute("true")
        self.assertEqual(len(self.agent.lease_calls), 3)
        self.assertEqual(self.entries(), [])

    def test_an_unmatched_request_does_not_leave_a_lease_behind(self):
        self.agent.lease_error = PrismError(409, "no_matching_offer", {})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        self.assertIsNone(env.lease_info())
        self.assertEqual(self.agent.ended, [])


class TestBroadcastFunding(PrismTestCase):
    """Failures that happen after the funding transaction is on the wire.

    The SDK raises these from inside the send, so the hash arrives under "hash"
    and there is no ``funding_hash`` to recognise. Handing the reservation back
    for them would let one wallet fund escrow after escrow through an RPC having
    a bad hour while the day's ceiling reports nothing spent.
    """

    config = {"max_usdg": 0.8, "daily_budget_usdg": 5}

    def test_an_unconfirmed_funding_keeps_its_reservation(self):
        self.agent.lease_error = PrismError(504, "confirmation_timeout", {"hash": "0xabc"})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["micros"], 800_000)
        self.assertEqual(entries[0]["reference"], "0xabc")

    def test_a_reverted_funding_keeps_its_reservation(self):
        self.agent.lease_error = PrismError(402, "tx_reverted", {"hash": "0xdead"})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        self.assertEqual(self.entries()[0]["reference"], "0xdead")

    def test_repeated_unconfirmed_fundings_exhaust_the_day(self):
        clock = self.fake_clock()
        self.unconfirmed_fundings()
        env = self.make_env()

        for _ in range(6):
            with self.assertRaises(PrismLeaseError):
                env.execute("true")
            clock.advance(prism_environment.FUNDING_HOLD_S)
        self.assertEqual([e["micros"] for e in self.entries()], [800_000] * 6)

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertIn("daily cap", str(raised.exception))
        self.assertEqual(len(self.agent.lease_calls), 6,
                         "a seventh escrow was funded past the day's ceiling")

    def test_a_retried_command_is_not_funded_a_second_time(self):
        # What the terminal tool does with a lease failure it cannot read as a
        # command timeout: run the same command again, three times over.
        self.fake_clock()
        self.unconfirmed_fundings()
        env = self.make_env()

        messages = []
        for _ in range(4):
            with self.assertRaises(PrismLeaseError) as raised:
                env.execute("nvidia-smi")
            messages.append(str(raised.exception))

        self.assertEqual(len(self.agent.lease_calls), 1,
                         "one command funded more than one escrow")
        self.assertEqual([e["micros"] for e in self.entries()], [800_000])
        for message in messages:
            self.assertIn(f"0x{1:064x}", message,
                          "a retry hid the transaction the first attempt paid for")
        self.assertIn("is not funding another lease", messages[-1])

    def test_one_transaction_reported_twice_is_charged_once(self):
        # A USDG approval re-signed at an unchanged nonce is the same
        # transaction under the same hash, and the ledger books it once however
        # many attempts report it. The hash is the receipt: distinct fundings
        # cannot share one, so nothing distinct is being collapsed here.
        clock = self.fake_clock()

        def approval_never_confirms():
            raise PrismError(504, "confirmation_timeout", {"hash": "0xsame"})

        self.agent.on_lease = approval_never_confirms
        env = self.make_env()

        for _ in range(3):
            with self.assertRaises(PrismLeaseError):
                env.execute("true")
            clock.advance(prism_environment.FUNDING_HOLD_S)

        self.assertEqual(len(self.agent.lease_calls), 3)
        self.assertEqual([e["micros"] for e in self.entries()], [800_000])

    def test_the_hold_lifts_once_its_wait_is_over(self):
        clock = self.fake_clock()
        self.unconfirmed_fundings()
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        clock.advance(prism_environment.FUNDING_HOLD_S + 1)
        with self.assertRaises(PrismLeaseError):
            env.execute("true")

        self.assertEqual(len(self.agent.lease_calls), 2)
        self.assertEqual([e["micros"] for e in self.entries()], [800_000] * 2)

    def test_a_funded_lease_that_never_came_up_holds_the_retry_too(self):
        self.fake_clock()
        self.agent.lease_error = PrismError(404, "access_timeout",
                                            {"funding_hash": "0xfeed", "lease_id": 3})
        env = self.make_env()

        for _ in range(4):
            with self.assertRaises(PrismLeaseError) as raised:
                env.execute("true")
        self.assertIn("0xfeed", str(raised.exception))
        self.assertEqual(len(self.agent.lease_calls), 1)

    def test_the_transaction_to_chase_is_named(self):
        self.agent.lease_error = PrismError(504, "confirmation_timeout",
                                            {"hash": "0xabc", "cause": "not in the chain"})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        message = str(raised.exception)
        self.assertIn("0xabc", message)
        self.assertNotIn("timeout", message.lower())

    def test_a_transport_failure_while_funding_keeps_the_reservation(self):
        self.agent.lease_error = ConnectionResetError("connection reset by peer")
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        self.assertIn("could not reach Robinhood Chain", str(raised.exception))
        self.assertEqual([e["micros"] for e in self.entries()], [800_000])

    def test_a_transport_failure_is_not_dressed_up_as_a_command_timeout(self):
        class ReadTimeout(OSError):
            pass

        self.agent.lease_error = ReadTimeout("read timed out")
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        message = str(raised.exception)
        self.assertNotIn("timeout", message.lower())
        # The class name is the diagnostic, so it survives in a readable form
        # rather than being scrubbed into a word nobody can look up.
        self.assertIn("ReadTimedOut", message)

    def test_a_transport_failure_holds_the_next_attempt(self):
        # The signed transaction may be in a mempool, which is exactly why the
        # retry must not sign another one.
        self.fake_clock()
        self.agent.lease_error = ConnectionResetError("connection reset by peer")
        env = self.make_env()

        for _ in range(4):
            with self.assertRaises(PrismLeaseError):
                env.execute("true")
        self.assertEqual(len(self.agent.lease_calls), 1)
        self.assertEqual([e["micros"] for e in self.entries()], [800_000])

    def test_a_quote_that_was_never_funded_is_still_given_back(self):
        self.agent.lease_error = PrismError(402, "cost_exceeds_max", {"required": 9, "max": 1})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        self.assertEqual(self.entries(), [])


class TestHostRetries(PrismTestCase):
    """One model command, through the retry loop the terminal tool really runs.

    A lease failure is an exception like any other to that loop: it runs the
    command again, three times over, unless the text reads as a command timeout.
    Four attempts at a lease that already paid is four escrows, and the model is
    shown the last one's error.
    """

    config = {"max_usdg": 1, "daily_budget_usdg": 5}

    def test_one_command_funds_one_lease(self):
        import __init__ as plugin_pkg
        import tools.terminal_tool as tt
        from agent import terminal_env_registry as reg

        self.unconfirmed_fundings()
        reg._reset_for_tests()
        self.addCleanup(reg._reset_for_tests)
        self.addCleanup(tt.cleanup_vm, "tid-retry")
        reg.register_provider(plugin_pkg.PrismProvider())

        with mock.patch.object(tt, "_ensure_terminal_env_bridged", lambda: None), \
             mock.patch("time.sleep", lambda _: None), \
             mock.patch.dict(os.environ, {"TERMINAL_ENV": "prism"}):
            result = json.loads(tt.terminal_tool(command="nvidia-smi", task_id="tid-retry",
                                                 timeout=30, force=True))

        self.assertEqual(len(self.agent.lease_calls), 1,
                         "one command funded more than one lease")
        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])
        # The transaction the wallet actually paid for is the one the model is
        # told to chase, not the fourth one it never saw.
        self.assertIn(f"0x{1:064x}", result["error"])


class TestConnectBudget(PrismTestCase):
    """Every SSH the backend starts is priced the way the SDK really runs it.

    The SDK makes ``connect_retries + 1`` attempts, sleeps CONNECT_DELAY_S
    between them, and bounds every ssh subprocess at the command timeout plus
    twenty seconds. A command's own timeout is never shortened to pay for that,
    so the wall a call is allowed is ``max(deadline, timeout + 20)``.
    FakeAgent.run reports what the loop costs in the worst case and these
    assert against it.
    """

    #: Every call site: the command, the wall budget the backend hands it, and
    #: the timeout the command itself asks for.
    CALL_SITES = (
        ("echo $HOME", PROBE_TIMEOUT_S, PROBE_COMMAND_S),
        ("base64 -d", UPLOAD_TIMEOUT_S, UPLOAD_COMMAND_S),
        ("rm -", DELETE_TIMEOUT_S, DELETE_COMMAND_S),
    )
    #: The same call sites as a deadline-to-timeout rule, for sweeping deadlines
    #: no call site uses today. A model command's deadline is its own timeout.
    SHAPES = (
        ("echo $HOME", lambda deadline: PROBE_COMMAND_S),
        ("base64 -d", lambda deadline: UPLOAD_COMMAND_S),
        ("rm -", lambda deadline: DELETE_COMMAND_S),
        ("model command", lambda deadline: deadline),
    )

    def run_call(self, command_fragment):
        return next(c for c in reversed(self.agent.run_calls)
                    if command_fragment in c["command"])

    def exercise_setup_steps(self, env):
        source = os.path.join(self.tmp, "skill.md")
        with open(source, "w") as fh:
            fh.write("hello")
        env._upload(source, "/root/.hermes/skills/skill.md")
        env._delete(["/root/.hermes/skills/skill.md"])

    def test_no_setup_step_can_outrun_the_deadline_it_was_handed(self):
        # These run in _before_execute, ahead of the run_bounded_sync backstop
        # in execute(): nothing else is watching how long they sleep.
        env = self.make_env()
        env.execute("true")
        self.exercise_setup_steps(env)

        for fragment, deadline, _ in self.CALL_SITES:
            with self.subTest(command=fragment):
                self.assertLessEqual(self.run_call(fragment)["elapsed"], deadline)

    def test_a_cold_machine_does_not_buy_its_warm_up_with_the_deadline(self):
        # The warm path may ask for less than the cold one; neither may ask for
        # more than the deadline pays for.
        env = self.make_env()
        env.execute("true")
        env._reachable = False
        self.exercise_setup_steps(env)

        for fragment, deadline, _ in self.CALL_SITES[1:]:
            with self.subTest(command=fragment):
                self.assertLessEqual(self.run_call(fragment)["elapsed"], deadline)

    def test_a_setup_step_gets_the_command_timeout_it_asked_for(self):
        env = self.make_env()
        env.execute("true")
        self.exercise_setup_steps(env)

        for fragment, _, timeout in self.CALL_SITES:
            with self.subTest(command=fragment):
                self.assertEqual(self.run_call(fragment)["timeout"], timeout)

    def test_a_model_command_keeps_the_timeout_it_asked_for(self):
        # The warm-up reservation used to come out of the running attempt, so
        # asking for more GPU time could hand the command less of it.
        for timeout in (30, 60, 99, 100, 120, 900):
            with self.subTest(timeout=timeout):
                env = self.make_env(timeout=timeout)
                env.execute("nvidia-smi")
                call = self.run_call("nvidia-smi")
                self.assertEqual(call["timeout"], timeout)
                self.assertLessEqual(call["elapsed"], timeout + EXEC_OVERHEAD_S)

    def test_a_longer_deadline_never_buys_a_command_less_gpu_time(self):
        env = self.make_env()
        for reachable in (False, True):
            env._reachable = reachable
            previous = 0
            for deadline in range(MIN_COMMAND_S, 901):
                with self.subTest(deadline=deadline, reachable=reachable):
                    granted = env._exec_budget(deadline, deadline).command_timeout
                    self.assertEqual(granted, deadline)
                    self.assertGreaterEqual(granted, previous)
                    previous = granted

    def test_every_call_site_fits_the_wall_its_deadline_allows(self):
        # The exhaustive check: a call may run past its deadline only when the
        # command's own timeout leaves it no choice, and then by the SDK's
        # overhead and nothing more. The wall comes from sdk_wall rather than
        # from the budget, so a budget that misprices itself cannot pass this.
        env = self.make_env()
        for label, timeout_for in self.SHAPES:
            for reachable in (False, True):
                env._reachable = reachable
                for deadline in range(MIN_COMMAND_S, 901):
                    timeout = timeout_for(deadline)
                    budget = env._exec_budget(deadline, timeout)
                    if budget.command_timeout != timeout:
                        self.fail(f"{label} at deadline={deadline}: asked for {timeout}s, "
                                  f"got {budget.command_timeout}s")
                    wall = sdk_wall(budget.command_timeout, budget.retries)
                    allowed = max(deadline, sdk_wall(timeout, 0))
                    if wall > allowed:
                        self.fail(f"{label} at deadline={deadline} reachable={reachable}: "
                                  f"waits {wall}s, allowed {allowed}s")

    def test_a_retry_budget_is_the_largest_the_deadline_pays_for(self):
        env = self.make_env()
        for label, timeout_for in self.SHAPES:
            for reachable in (False, True):
                env._reachable = reachable
                ceiling = STEADY_RETRIES if reachable else WARMUP_RETRIES
                for deadline in range(MIN_COMMAND_S, 901):
                    timeout = timeout_for(deadline)
                    budget = env._exec_budget(deadline, timeout)
                    with self.subTest(site=label, deadline=deadline, reachable=reachable):
                        self.assertGreaterEqual(budget.retries, 0)
                        self.assertLessEqual(budget.retries, ceiling)
                        if budget.retries < ceiling:
                            self.assertGreater(sdk_wall(timeout, budget.retries + 1), deadline)

    def test_the_wait_a_budget_reports_is_the_one_the_sdk_takes(self):
        # The overhead is per attempt that runs the command, and a retry that
        # never connects costs its connect deadline plus the delay instead.
        env = self.make_env()
        env.execute("true")
        self.exercise_setup_steps(env)

        for fragment, deadline, timeout in self.CALL_SITES:
            with self.subTest(command=fragment):
                call = self.run_call(fragment)
                budget = prism_environment.ExecBudget(call["retries"], call["timeout"], deadline)
                self.assertEqual(budget.waited, call["elapsed"])
                self.assertEqual(timeout, call["timeout"])

    def test_a_deadline_that_buys_no_command_time_is_refused(self):
        env = self.make_env()
        for deadline in range(1, MIN_COMMAND_S):
            with self.subTest(deadline=deadline):
                with self.assertRaises(PrismLeaseError):
                    env._exec_budget(deadline, deadline)

    def test_a_deadline_too_short_to_bound_its_command_still_runs_it(self):
        # A 15s command cannot be stopped inside 15s: the SSH around it costs
        # twenty more. Refusing it leaves the caller with nothing, so it runs
        # with the timeout it asked for and the session is told what that means.
        env = self.make_env(timeout=15)
        result = env.execute("true")

        call = self.run_call("true")
        self.assertEqual(call["timeout"], 15)
        self.assertEqual(call["retries"], 0)
        self.assertIn("past the 15s this call was given", result["output"])

    def test_the_overrun_note_does_not_wait_on_the_lease_lock(self):
        # init_session() runs the session's first command from inside
        # _ensure_lease, which holds the lease lock, and that command runs on a
        # worker thread. A note taken under the same lock never returns.
        env = self.make_env(timeout=15)
        budget = env._exec_budget(15, 15)
        noted = threading.Event()

        with env._lock:
            threading.Thread(target=lambda: (env._note_overrun(budget), noted.set()),
                             daemon=True).start()
            self.assertTrue(noted.wait(5), "_note_overrun blocked on the lease lock")

    def test_a_session_hears_about_a_long_wait_once(self):
        env = self.make_env(timeout=15)
        first = env.execute("true")
        second = env.execute("true")

        self.assertIn("past the 15s this call was given", first["output"])
        self.assertNotIn("past the", second["output"])

    def test_the_login_probe_runs_with_the_timeout_it_was_given(self):
        # When the login shell fails the snapshot bootstrap, init_session probes
        # with a 15s timeout. The probe is the fallback that decides whether the
        # session uses bash -l at all, so it has to reach the machine.
        self.agent.results = {"bash -l": {"code": 7, "stdout": "", "stderr": "login is broken"}}
        env = self.make_env(timeout=120)
        env.execute("true")

        probes = [c for c in self.agent.run_calls if c["command"].startswith("bash -c")]
        self.assertTrue(probes)
        self.assertEqual(min(c["timeout"] for c in probes), 15)

    def test_a_warm_lease_never_asks_for_more_than_a_cold_one(self):
        env = self.make_env()
        env._reachable = False
        cold = env._exec_budget(3600, 120)
        env._reachable = True
        warm = env._exec_budget(3600, 120)

        self.assertLessEqual(warm.attempts, cold.attempts)
        self.assertEqual(cold.retries, WARMUP_RETRIES)
        self.assertEqual(warm.retries, STEADY_RETRIES)

    def test_a_gpu_that_never_answered_still_gets_its_warm_up(self):
        for code in (255, -1):
            with self.subTest(code=code):
                self.agent.result = {"code": code, "stdout": "", "stderr": "ssh: no route"}
                env = self.make_env()
                env.execute("true")

                self.assertFalse(env._reachable)
                self.assertGreater(env._exec_budget(3600, 120).retries, STEADY_RETRIES)

    def test_a_machine_that_answered_drops_to_the_steady_budget(self):
        env = self.make_env()
        env.execute("true")

        self.assertTrue(env._reachable)
        self.assertEqual(env._exec_budget(3600, 120).retries, STEADY_RETRIES)

    def test_a_setup_exec_that_wedges_is_abandoned_rather_than_waited_out(self):
        env = self.make_env()
        env.execute("true")
        # The subject is the wall-clock bound, so the deadline floor is lifted
        # for it: a real one would make the test sit through half a minute.
        with mock.patch.object(env, "_ssh", lambda *a, **kw: time.sleep(2)):
            with self.assertRaises(PrismLeaseError) as raised:
                with mock.patch.object(prism_environment, "DELETE_TIMEOUT_S", 0.05), \
                     mock.patch.object(prism_environment, "DELETE_COMMAND_S", 0), \
                     mock.patch.object(prism_environment, "EXEC_OVERHEAD_S", 0), \
                     mock.patch.object(prism_environment, "BOUND_GRACE_S", 0.05), \
                     mock.patch.object(prism_environment, "MIN_COMMAND_S", 0):
                    env._delete(["/root/.hermes/skills/skill.md"])
        self.assertIn("did not finish", str(raised.exception))

    def test_a_command_that_never_answered_reports_the_wait_it_really_took(self):
        # The SDK's overhead is on top of the timeout it is handed, so the
        # nominal deadline is not what the caller sat through.
        self.agent.result = {"code": -1, "stdout": "", "stderr": "timed out"}
        env = self.make_env(timeout=120)
        result = env.execute("sleep 1000")

        budget = env._exec_budget(120, 120)
        self.assertNotEqual(budget.waited, 120)
        self.assertIn(f"within {budget.waited}s", result["output"])


class TestBroadcastContract(PrismTestCase):
    """The SDK says whether the money left the wallet; nothing else guesses.

    ``PrismError.broadcast`` is populated for every phase of a failed lease.
    ``body`` is whatever the far side sent back and can say anything, including
    a hash that belongs to somebody else's transaction.
    """

    config = {"max_usdg": 0.8, "daily_budget_usdg": 5}

    def test_an_rpc_outage_before_the_broadcast_gives_the_reservation_back(self):
        self.agent.lease_error = PrismError(504, "pre_broadcast_failure",
                                            {"cause": "balance read failed"}, broadcast=False)
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        message = str(raised.exception)
        self.assertIn("nothing was charged", message)
        self.assertNotIn("timeout", message.lower())
        self.assertEqual(self.entries(), [], "an outage below the signing was booked as a spend")

    def test_an_outage_before_the_broadcast_can_be_retried_at_once(self):
        # Nothing was signed, so there is nothing for a hold to protect.
        self.fake_clock()
        self.agent.lease_error = PrismError(504, "pre_broadcast_failure", {}, broadcast=False)
        env = self.make_env()

        for _ in range(3):
            with self.assertRaises(PrismLeaseError):
                env.execute("true")
        self.assertEqual(len(self.agent.lease_calls), 3)
        self.assertEqual(self.entries(), [])

    def test_a_broadcast_the_sdk_owns_up_to_keeps_its_reservation(self):
        self.agent.lease_error = PrismError(504, "confirmation_timeout",
                                            {"hash": "0xabc"}, broadcast=True)
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        self.assertEqual([e["reference"] for e in self.entries()], ["0xabc"])

    def test_a_broadcast_the_body_does_not_name_still_keeps_its_reservation(self):
        # The far side answered without a hash, but the SDK knows it signed and
        # sent one. A reservation handed back here pays for the next escrow too.
        self.fake_clock()
        self.agent.lease_error = PrismError(502, "lease_failed_after_funding",
                                            {"cause": "the node never reported"},
                                            broadcast=True)
        env = self.make_env()

        for _ in range(3):
            with self.assertRaises(PrismLeaseError):
                env.execute("true")
        self.assertEqual([e["micros"] for e in self.entries()], [800_000])
        self.assertEqual(len(self.agent.lease_calls), 1)

    def test_the_sdk_answer_wins_over_a_hash_in_the_body(self):
        self.agent.lease_error = PrismError(502, "pre_broadcast_failure",
                                            {"hash": "0xsomebodyelses"}, broadcast=False)
        env = self.make_env()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("true")
        message = str(raised.exception)
        self.assertNotIn("0xsomebodyelses", message,
                         "a hash this wallet never sent was reported as its own funding")
        self.assertIn("nothing was charged", message)
        self.assertEqual(self.entries(), [])

    def test_an_exception_that_says_nothing_is_treated_as_money_that_moved(self):
        self.assertTrue(prism_environment.funding_may_be_live(ConnectionResetError("reset")))
        self.assertTrue(prism_environment.funding_may_be_live(
            PrismError(502, "chain_error", {})))
        self.assertFalse(prism_environment.funding_may_be_live(
            PrismError(404, "no_capacity", {})))


class TestDepositBooking(PrismTestCase):
    """What the day is charged for a lease that came up."""

    def test_a_quote_that_asked_for_nothing_is_charged_nothing(self):
        self.agent.quoted_escrow = 0
        env = self.make_env()
        env.execute("true")

        self.assertEqual([e["micros"] for e in self.entries()], [0])

    def test_a_lease_that_reports_no_deposit_at_all_keeps_the_reservation(self):
        # Nothing said what was signed, so the ceiling the escrow was funded
        # under is what stands.
        self.agent.quoted_escrow = UNSET
        env = self.make_env()
        env.execute("true")

        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])

    def test_what_the_sdk_signed_for_wins_over_what_the_quote_asked(self):
        self.agent.quoted_escrow = 0
        self.agent.signed_deposit = 400_000
        env = self.make_env()
        env.execute("true")

        self.assertEqual([e["micros"] for e in self.entries()], [400_000])

    def test_a_signed_deposit_above_the_cap_is_booked_at_the_cap(self):
        self.agent.signed_deposit = 4 * MICROS
        env = self.make_env()
        env.execute("true")

        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])


class TestProofCapsule(PrismTestCase):
    config = {"max_usdg": 1, "daily_budget_usdg": 5, "idle_release_seconds": 0}

    def quiet_session_bootstrap(self):
        """Silence the login-shell snapshot so the rolling hashes are only what
        the session's own commands printed."""
        self.agent.results["bash -l -c"] = {"code": 0, "stdout": "", "stderr": ""}

    def test_a_released_lease_leaves_a_capsule_behind(self):
        env = self.make_env()
        env.execute("nvidia-smi")
        env.cleanup()

        capsule = self.written_capsule()
        self.assertEqual(capsule["lease_id"], 1)
        self.assertEqual(capsule["gpu_model"], "H100")
        self.assertEqual(capsule["trust_class"], "open")
        self.assertTrue(capsule["image_digest"].startswith("sha256:"))
        self.assertEqual(capsule["funding_hash"], make_lease(1).funding_hash)
        self.assertEqual(capsule["proof_url"], "https://prismnetwork.tech/proof")
        self.assertIsNone(capsule["charged_base_units"], "a receipt was claimed before it exists")
        self.assertIsNotNone(capsule["started_at"])
        self.assertIsNotNone(capsule["ended_at"])
        self.assertGreaterEqual(capsule["charged_seconds"], 0)

    def test_the_capsule_names_how_far_the_machine_could_be_checked(self):
        env = self.make_env()
        env.execute("true")
        env.cleanup()

        capsule = self.written_capsule()
        self.assertEqual(capsule["host_key_verdict"], "unverified")
        self.assertIsNone(capsule["host_key_fingerprint"])

    def test_a_pinned_host_key_is_recorded_with_its_verdict(self):
        env = self.make_env()
        env.execute("true")
        with mock.patch.object(prism_environment, "host_key_policy",
                               lambda access: {"mode": "attested",
                                               "fingerprint": "SHA256:abc",
                                               "source": "snp_report"}):
            env._expires_at = time.monotonic() - 1
            env.execute("true")
        env.cleanup()

        capsule = self.written_capsule(2)
        self.assertEqual(capsule["host_key_verdict"], "attested")
        self.assertEqual(capsule["host_key_fingerprint"], "SHA256:abc")

    def test_the_hashes_roll_over_everything_the_session_ran(self):
        self.quiet_session_bootstrap()
        env = self.make_env()
        self.agent.result = {"code": 0, "stdout": "alpha", "stderr": "beta"}
        env.execute("one")

        self.assertEqual(env.capsule()["stdout_sha256"], hashlib.sha256(b"alpha").hexdigest())
        self.assertEqual(env.capsule()["stderr_sha256"], hashlib.sha256(b"beta").hexdigest())

        env.execute("two")
        self.assertEqual(env.capsule()["stdout_sha256"],
                         hashlib.sha256(b"alphaalpha").hexdigest())
        self.assertEqual(env.capsule()["stderr_sha256"],
                         hashlib.sha256(b"betabeta").hexdigest())

        env.cleanup()
        self.assertEqual(self.written_capsule()["stdout_sha256"],
                         hashlib.sha256(b"alphaalpha").hexdigest())

    def test_every_file_pushed_to_the_gpu_is_named_by_its_hash(self):
        source = os.path.join(self.tmp, "skill.md")
        with open(source, "w") as fh:
            fh.write("hello")
        env = self.make_env()
        env.execute("true")
        env._upload(source, "/root/.hermes/skills/skill.md")
        env.cleanup()

        self.assertEqual(self.written_capsule()["artifact_hashes"],
                         {"/root/.hermes/skills/skill.md": hashlib.sha256(b"hello").hexdigest()})

    def test_a_file_too_large_to_inline_is_not_claimed_as_an_artifact(self):
        source = os.path.join(self.tmp, "model.bin")
        with open(source, "wb") as fh:
            fh.write(b"x" * (prism_environment.MAX_UPLOAD_BYTES + 1))
        env = self.make_env()
        env.execute("true")
        env._upload(source, "/root/.hermes/cache/model.bin")
        env.cleanup()

        self.assertEqual(self.written_capsule()["artifact_hashes"], {})

    def test_the_release_line_names_the_capsule(self):
        env = self.make_env()
        env.execute("true")
        env._expires_at = time.monotonic() - 1
        result = env.execute("echo two")

        self.assertIn(f"prism: lease 1 released, capsule {self.capsules / '1.json'}",
                      result["output"])
        self.assertIn("ok", result["output"], "the notice replaced the command's own output")

    def test_a_fresh_lease_starts_a_fresh_capsule(self):
        env = self.make_env()
        env.execute("true")
        env._expires_at = time.monotonic() - 1
        env.execute("true")

        self.assertEqual(env.capsule()["lease_id"], 2)
        self.assertEqual(self.written_capsule(1)["lease_id"], 1)

    def test_no_lease_means_no_capsule(self):
        env = self.make_env()
        self.assertIsNone(env.capsule())

    def test_a_capsule_that_cannot_be_written_does_not_lose_the_lease(self):
        env = self.make_env()
        env.execute("true")
        with mock.patch.object(prism_environment, "write_capsule",
                               mock.Mock(side_effect=OSError("read-only"))):
            env.cleanup()

        self.assertEqual(self.agent.ended, [1])


class TestCapsuleLocation(unittest.TestCase):
    def test_capsules_land_under_the_directory_hermes_gives_the_plugin(self):
        state = mock.Mock()
        state.return_value.data_dir = Path("/tmp/hermes-plugin-data/prism")
        with mock.patch("hermes_cli.plugins.PluginState", state):
            self.assertEqual(prism_environment.capsule_dir(),
                             Path("/tmp/hermes-plugin-data/prism/capsules"))
        self.assertEqual(state.call_args.args, ("prism",))

    def test_without_one_capsules_still_land_somewhere(self):
        with mock.patch("hermes_cli.plugins.PluginState",
                        mock.Mock(side_effect=RuntimeError("no profile"))):
            self.assertEqual(prism_environment.capsule_dir(),
                             Path(CAPSULE_FALLBACK_DIR).expanduser())


class TestIdleRelease(PrismTestCase):
    config = {"max_usdg": 1, "daily_budget_usdg": 5, "idle_release_seconds": 300}

    def test_a_quiet_session_gives_the_gpu_back(self):
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")

        clock.advance(299)
        self.assertFalse(env._release_if_idle())
        clock.advance(2)
        self.assertTrue(env._release_if_idle())

        self.assertEqual(self.agent.ended, [1])
        self.assertIsNone(env.lease_info())

    def test_the_next_command_rents_and_books_again(self):
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")
        clock.advance(400)
        env._release_if_idle()

        result = env.execute("echo two")
        self.assertEqual(len(self.agent.lease_calls), 2)
        self.assertEqual([e["micros"] for e in self.entries()], [750_000, 750_000])
        self.assertEqual(env.lease_info()["lease_id"], 2)
        self.assertIn("prism: lease 1 released, capsule", result["output"])
        # The rented disk went with the machine, and the model is told so
        # before it goes looking for the files it left there.
        self.assertIn("Its disk is gone", result["output"])

    def test_renting_again_still_answers_to_the_daily_cap(self):
        self.config = {"max_usdg": 1, "daily_budget_usdg": 1,
                       "idle_release_seconds": 300}
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")
        clock.advance(400)
        env._release_if_idle()

        with self.assertRaises(PrismLeaseError) as raised:
            env.execute("echo two")
        self.assertIn("daily cap", str(raised.exception))
        self.assertEqual(len(self.agent.lease_calls), 1)

    def test_a_running_command_is_not_idle(self):
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")
        clock.advance(400)
        env._in_flight = 1

        self.assertFalse(env._release_if_idle())
        self.assertEqual(self.agent.ended, [])

    def test_a_long_command_is_not_reaped_out_from_under_itself(self):
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")

        def slow_command():
            clock.advance(3600)
            self.assertFalse(env._release_if_idle())
            return ("done", 0)

        with mock.patch.object(env, "_run_bash",
                               lambda *a, **kw: prism_environment._ThreadedProcessHandle(
                                   slow_command, cancel_fn=None)):
            env.execute("train.py")
        self.assertEqual(self.agent.ended, [])

    def test_zero_keeps_the_lease_for_the_session(self):
        self.config = {"max_usdg": 1, "daily_budget_usdg": 5, "idle_release_seconds": 0}
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")
        clock.advance(86_400)

        self.assertFalse(env._release_if_idle())
        self.assertIsNone(env._monitor, "a monitor was started for a disabled release")
        self.assertEqual(self.agent.ended, [])

    def test_the_monitor_stops_with_the_session(self):
        with mock.patch.object(prism_environment, "IDLE_POLL_S", 0.01):
            env = self.make_env()
            env.execute("true")
            monitor = env._monitor
            self.assertIsNotNone(monitor)
            env.cleanup()
            monitor.join(timeout=5)

        self.assertFalse(monitor.is_alive())

    def test_an_idle_release_writes_the_capsule_too(self):
        clock = self.fake_clock()
        env = self.make_env()
        env.execute("true")
        clock.advance(400)
        env._release_if_idle()

        self.assertEqual(self.written_capsule()["lease_id"], 1)


class TestSessionLifetime(PrismTestCase):
    def test_the_lease_survives_the_end_of_a_turn(self):
        import tools.terminal_tool as tt

        env = self.make_env(task_id="tid-1")
        env.execute("true")
        with tt._env_lock:
            tt._active_environments["tid-1"] = env
        self.addCleanup(lambda: tt._active_environments.pop("tid-1", None))

        # What the turn finalizer asks before it calls cleanup_vm on the
        # environment holding a GPU that is paid for by the hour.
        self.assertTrue(tt.is_persistent_env("tid-1"))

        env.execute("echo two")
        self.assertEqual(len(self.agent.lease_calls), 1)
        self.assertEqual(self.agent.ended, [])


class TestCleanup(PrismTestCase):
    def test_cleanup_ends_the_lease(self):
        env = self.make_env()
        env.execute("true")
        env.cleanup()

        self.assertEqual(self.agent.ended, [1])
        self.assertIsNone(env.lease_info())

    def test_cleanup_is_idempotent(self):
        env = self.make_env()
        env.execute("true")
        env.cleanup()
        env.cleanup()

        self.assertEqual(self.agent.ended, [1])

    def test_cleanup_survives_a_constructor_that_never_finished(self):
        half_built = PrismEnvironment.__new__(PrismEnvironment)
        half_built.cleanup()

    def test_a_failed_release_does_not_raise_and_warns_that_the_meter_may_run(self):
        env = self.make_env()
        env.execute("true")
        self.agent.end_lease = mock.Mock(side_effect=RuntimeError("gone"))
        env.cleanup()
        notices = " ".join(env._drain_notices())
        self.assertIn("could not be released", notices)
        self.assertIn("bill until its window ends", notices)

    def test_the_capsule_records_whether_the_meter_was_stopped(self):
        env = self.make_env()
        env.execute("true")
        capsule = env._capsule
        env.cleanup()
        self.assertEqual(capsule.to_dict()["release"], "queued")


class TestFileSync(PrismTestCase):
    def test_files_travel_as_base64_over_the_lease(self):
        source = os.path.join(self.tmp, "skill.md")
        with open(source, "w") as fh:
            fh.write("hello")
        env = self.make_env()
        env.execute("true")
        env._upload(source, "/root/.hermes/skills/skill.md")

        call = self.agent.run_calls[-1]
        self.assertIn("base64 -d > /root/.hermes/skills/skill.md", call["command"])
        self.assertIn("mkdir -p /root/.hermes/skills", call["command"])
        self.assertEqual(call["stdin"].strip(), "aGVsbG8=")

    def test_a_sync_cycle_is_one_tar_stream_not_one_exec_per_file(self):
        import base64
        import io
        import tarfile

        paths = []
        for i in range(40):
            source = os.path.join(self.tmp, f"skill-{i}.md")
            with open(source, "w") as fh:
                fh.write(f"skill {i}")
            paths.append((source, f"/root/.hermes/skills/s{i}/SKILL.md"))
        env = self.make_env()
        env.execute("true")
        before = len(self.agent.run_calls)

        env._bulk_upload(paths)

        self.assertEqual(len(self.agent.run_calls), before + 1)
        call = self.agent.run_calls[-1]
        self.assertIn("tar -xzf", call["command"])
        self.assertIn("-C /", call["command"])
        self.assertNotIn("| tar", call["command"], "a decode piped into an extractor trips plugin scanners")
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(call["stdin"])), mode="r:gz") as archive:
            names = sorted(archive.getnames())
            self.assertEqual(len(names), 40)
            self.assertEqual(names[0], "root/.hermes/skills/s0/SKILL.md")
            self.assertEqual(archive.extractfile(names[0]).read(), b"skill 0")
        self.assertEqual(len(env._capsule.to_dict()["artifact_hashes"]), 40)

    def test_the_sync_manager_is_wired_to_the_bulk_upload(self):
        env = self.make_env()
        env.execute("true")
        self.assertIs(env._sync_manager._bulk_upload_fn.__func__, PrismEnvironment._bulk_upload)

    def test_a_bulk_sync_past_the_limit_names_what_to_trim(self):
        source = os.path.join(self.tmp, "blob.bin")
        with open(source, "wb") as fh:
            fh.write(os.urandom(prism_environment.MAX_UPLOAD_BYTES))
        env = self.make_env()
        env.execute("true")
        with mock.patch.object(prism_environment, "MAX_BULK_UPLOAD_BYTES", 1024):
            with self.assertRaises(PrismLeaseError) as raised:
                env._bulk_upload([(source, "/root/.hermes/cache/blob.bin")])
        self.assertIn("Trim", str(raised.exception))

    def test_a_file_too_large_to_inline_is_skipped_not_retried_forever(self):
        source = os.path.join(self.tmp, "model.bin")
        with open(source, "wb") as fh:
            fh.write(b"x" * (prism_environment.MAX_UPLOAD_BYTES + 1))
        env = self.make_env()
        env.execute("true")
        before = len(self.agent.run_calls)
        env._upload(source, "/root/.hermes/cache/model.bin")

        self.assertEqual(len(self.agent.run_calls), before)

    def test_a_failed_delete_propagates_so_the_sync_retries(self):
        env = self.make_env()
        env.execute("true")
        self.agent.result = {"code": 1, "stdout": "", "stderr": "read-only"}

        with self.assertRaises(PrismLeaseError):
            env._delete(["/root/.hermes/creds/x.json"])

    def test_credentials_stay_off_the_rented_machine_by_default(self):
        mounts = [{"host_path": "/host/creds.json", "container_path": "/root/.hermes/creds.json"}]
        with mock.patch("tools.credential_files.get_credential_file_mounts", lambda: mounts):
            env = self.make_env()
            env.execute("true")
            self.assertEqual(env._files_to_sync(), [])

            env._settings.sync_credentials = True
            self.assertEqual(env._files_to_sync(),
                             [("/host/creds.json", "/root/.hermes/creds.json")])


class TestConfigFailsClosed(unittest.TestCase):
    """The caps live in config.yaml, so an unreadable one is not a default one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prism-plugin-config-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = Path(self.tmp) / "config.yaml"
        for patcher in (
            mock.patch("hermes_cli.config.get_config_path", lambda: self.path),
            mock.patch.dict(os.environ, {k: "" for k in prism_environment.ENV_CAPS}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_config_that_cannot_be_loaded_refuses_to_spend(self):
        self.path.write_text("terminal:\n  prism:\n    max_usdg: 0.1\n")
        with mock.patch("hermes_cli.config.load_config_readonly",
                        mock.Mock(side_effect=OSError("permission denied"))):
            with self.assertRaises(PrismLeaseError) as raised:
                prism_environment.terminal_prism_config()
        self.assertIn(str(self.path), str(raised.exception))

    def test_defaults_are_not_substituted_for_an_unparseable_config(self):
        # What core hands back after a parse failure: its own defaults, which
        # are 1 USDG per lease and 5 a day of somebody else's money.
        self.path.write_text("terminal:\n  prism:\n    max_usdg: [0.1\n")
        defaults = {"terminal": {"backend": "local"}}
        with mock.patch("hermes_cli.config.load_config_readonly", lambda: defaults):
            with self.assertRaises(PrismLeaseError) as raised:
                prism_environment.terminal_prism_config()
        self.assertIn(str(self.path), str(raised.exception))

    def test_a_config_that_is_not_a_mapping_refuses_to_spend(self):
        self.path.write_text("- terminal\n")
        with mock.patch("hermes_cli.config.load_config_readonly", lambda: {}):
            with self.assertRaises(PrismLeaseError) as raised:
                prism_environment.terminal_prism_config()
        self.assertIn(str(self.path), str(raised.exception))

    def test_a_readable_config_gives_back_its_prism_section(self):
        self.path.write_text("terminal:\n  prism:\n    max_usdg: 0.1\n")
        with mock.patch("hermes_cli.config.load_config_readonly",
                        lambda: {"terminal": {"prism": {"max_usdg": 0.1}}}):
            self.assertEqual(prism_environment.terminal_prism_config(), {"max_usdg": 0.1})

    def test_the_section_comes_back_as_a_copy(self):
        # The loader hands back its cache, and a caller that edits what it was
        # given edits every later reader's ceilings with it.
        cached = {"terminal": {"prism": {"max_usdg": 0.1}}}
        self.path.write_text("terminal:\n  prism:\n    max_usdg: 0.1\n")
        with mock.patch("hermes_cli.config.load_config_readonly", lambda: cached):
            section = prism_environment.terminal_prism_config()
            section["max_usdg"] = 99

        self.assertEqual(cached["terminal"]["prism"], {"max_usdg": 0.1})

    def test_a_terminal_section_that_is_not_a_mapping_refuses_to_spend(self):
        self.path.write_text("terminal: local\n")
        with mock.patch("hermes_cli.config.load_config_readonly", lambda: {"terminal": "local"}):
            with self.assertRaises(PrismLeaseError) as raised:
                prism_environment.terminal_prism_config()
        self.assertIn(str(self.path), str(raised.exception))

    def test_a_prism_section_that_is_not_a_mapping_refuses_to_spend(self):
        self.path.write_text("terminal:\n  prism: yes\n")
        with mock.patch("hermes_cli.config.load_config_readonly",
                        lambda: {"terminal": {"prism": True}}):
            with self.assertRaises(PrismLeaseError) as raised:
                prism_environment.terminal_prism_config()
        self.assertIn(str(self.path), str(raised.exception))

    def test_a_missing_config_is_a_first_run_not_a_failure(self):
        with mock.patch("hermes_cli.config.load_config_readonly", lambda: {}):
            self.assertEqual(prism_environment.terminal_prism_config(), {})

    def test_a_missing_config_under_a_live_section_refuses_to_spend(self):
        # Nothing on disk, yet a section is in force: the ceilings being used
        # are not the ones the operator can open a file and read.
        with mock.patch("hermes_cli.config.load_config_readonly",
                        lambda: {"terminal": {"prism": {"max_usdg": 3}}}):
            with self.assertRaises(PrismLeaseError) as raised:
                prism_environment.terminal_prism_config()
        self.assertIn(str(self.path), str(raised.exception))

    def test_exported_caps_without_a_config_file_refuse_to_spend(self):
        for name in prism_environment.ENV_CAPS:
            with self.subTest(cap=name):
                with mock.patch.dict(os.environ, {name: "9"}), \
                     mock.patch("hermes_cli.config.load_config_readonly", lambda: {}):
                    with self.assertRaises(PrismLeaseError) as raised:
                        prism_environment.terminal_prism_config()
                self.assertIn(name, str(raised.exception))

    def test_exported_caps_are_fine_once_the_file_they_belong_in_exists(self):
        self.path.write_text("terminal:\n  backend: prism\n")
        with mock.patch.dict(os.environ, {"PRISM_MAX_USDG": "9"}), \
             mock.patch("hermes_cli.config.load_config_readonly",
                        lambda: {"terminal": {"backend": "prism"}}):
            self.assertEqual(prism_environment.terminal_prism_config(), {})


class TestProviderContract(unittest.TestCase):
    def setUp(self):
        import __init__ as plugin_pkg

        self.plugin = plugin_pkg
        self.provider = plugin_pkg.PrismProvider()

    def test_identity_and_classification(self):
        self.assertEqual(self.provider.name, "prism")
        self.assertTrue(self.provider.is_remote)
        self.assertTrue(self.provider.is_container)
        self.assertEqual(self.provider.cache_path_base, "~/.hermes")

    def test_a_non_persistent_session_gets_its_own_lease(self):
        import tools.terminal_tool as tt
        from agent import terminal_env_registry as reg

        reg._reset_for_tests()
        self.addCleanup(reg._reset_for_tests)
        reg.register_provider(self.provider)

        with mock.patch.object(tt, "_ensure_terminal_env_bridged", lambda: None), \
             mock.patch.dict(os.environ, {"TERMINAL_ENV": "prism",
                                          "TERMINAL_CONTAINER_PERSISTENT": "false"}):
            self.assertTrue(tt._session_isolation_enabled())

    def test_every_prism_credential_is_stripped_from_subprocesses(self):
        with mock.patch.dict(os.environ, {"PRISM_VAULT_TOKEN": "secret"}):
            keys = self.provider.strip_env_keys
        self.assertIn("PRISM_AGENT_KEY", keys)
        self.assertIn("PRISM_ESCROW", keys)
        self.assertIn("PRISM_VAULT_TOKEN", keys)

    def test_the_manifest_asks_for_the_wallet_key(self):
        import yaml

        manifest = yaml.safe_load(
            open(os.path.join(os.path.dirname(__file__), os.pardir, "plugin.yaml"))
        )
        self.assertEqual(manifest["name"], "prism")
        self.assertEqual(manifest["kind"], "backend")
        self.assertEqual([e["name"] for e in manifest["requires_env"]], ["PRISM_AGENT_KEY"])

    def test_without_a_wallet_the_backend_reports_setup_not_readiness(self):
        with mock.patch.object(self.plugin, "_get_key", lambda: None):
            self.assertFalse(self.provider.is_available())
            status, detail = self.provider.probe()
            self.assertEqual(status, "needs_setup")
            self.assertIn("PRISM_AGENT_KEY", detail)
            self.assertFalse(self.provider.check_requirements({}))

    def test_probe_reports_the_wallet_and_the_cap(self):
        with mock.patch.object(self.plugin, "_wallet_address", lambda: "0xabc"), \
             mock.patch.object(self.plugin, "_settings",
                               lambda: mock.Mock(max_per_call_micros=MICROS)):
            self.assertEqual(self.provider.probe(), ("ready", "0xabc, up to 1.000000 USDG per lease"))

    def test_doctor_reports_wallet_ledger_trust_class_and_lease(self):
        settings = mock.Mock(max_per_call_micros=MICROS, daily_micros=5 * MICROS,
                             ledger_path="/tmp/spend.json", trust_class="open")
        live = [{"lease_id": 42, "gpu": "H100", "task_id": "t", "seconds_left": 1800}]
        with mock.patch.object(self.plugin, "_wallet_address", lambda: "0xabc"), \
             mock.patch.object(self.plugin, "_settings", lambda: settings), \
             mock.patch.object(prism_environment, "live_leases", lambda: live):
            rows = dict((label, detail) for _, label, detail in self.provider.doctor_checks())
            failures = [label for ok, label, _ in self.provider.doctor_checks() if not ok]

        self.assertEqual(failures, [])
        self.assertIn("0xabc", rows["Prism wallet"])
        self.assertIn("/tmp/spend.json", rows["Prism spend caps"])
        self.assertIn(usdg(MICROS), rows["Prism spend caps"])
        self.assertIn("open", rows["Prism trust class"])
        self.assertIn("lease 42 on H100", rows["Prism lease"])
        self.assertIn("30m", rows["Prism lease"])

    def test_doctor_says_where_the_proof_capsules_are_written(self):
        with mock.patch.object(prism_environment, "capsule_dir",
                               lambda: Path("/tmp/capsules")):
            row = next(r for r in self.provider.doctor_checks()
                       if r[1] == "Prism proof capsules")
        self.assertTrue(row[0])
        self.assertIn("/tmp/capsules", row[2])

    def test_doctor_says_a_missing_wallet_is_required(self):
        with mock.patch.object(self.plugin, "_get_key", lambda: None):
            row = next(r for r in self.provider.doctor_checks() if r[1] == "Prism wallet")
        self.assertFalse(row[0])
        self.assertIn("PRISM_AGENT_KEY", row[2])


class TestDispatchWiring(unittest.TestCase):
    """config → terminal_tool dispatch → PrismEnvironment kwargs."""

    def test_create_environment_falls_through_to_the_provider(self):
        import __init__ as plugin_pkg
        import tools.terminal_tool as tt
        from agent import terminal_env_registry as reg

        captured = {}

        class FakePrismEnv:
            def __init__(self, cwd, timeout, task_id):
                captured.update(cwd=cwd, timeout=timeout, task_id=task_id)

        reg._reset_for_tests()
        self.addCleanup(reg._reset_for_tests)
        with mock.patch.object(prism_environment, "PrismEnvironment", FakePrismEnv):
            reg.register_provider(plugin_pkg.PrismProvider())
            env = tt._create_environment(
                env_type="prism",
                image="ignored",
                cwd="/root",
                timeout=60,
                container_config={"container_persistent": False},
                task_id="tid-1",
            )

        self.assertEqual(captured, {"cwd": "/root", "timeout": 60, "task_id": "tid-1"})
        self.assertEqual(getattr(env, "_hermes_backend_name", None), "prism")

    def test_the_registry_accepts_the_provider_under_its_config_name(self):
        import __init__ as plugin_pkg
        from agent import terminal_env_registry as reg

        reg._reset_for_tests()
        self.addCleanup(reg._reset_for_tests)
        plugin_pkg.register(_Context())

        self.assertIsNotNone(reg.get_provider("prism"))
        self.assertIn("PRISM_AGENT_KEY", reg.plugin_strip_env_keys())


class RegisteredProviderTestCase(PrismTestCase):
    """Built the way Hermes builds it: through the terminal tool's own factory.

    A backend that rents on construction, or on a call the session never made,
    only shows up on this path. Constructing PrismEnvironment directly skips
    every caller that reaches it through the registry.
    """

    def setUp(self):
        super().setUp()
        import __init__ as plugin_pkg
        from agent import terminal_env_registry as reg

        reg._reset_for_tests()
        self.addCleanup(reg._reset_for_tests)
        reg.register_provider(plugin_pkg.PrismProvider())

    def created_env(self, task_id="tid-lazy", timeout=30):
        import tools.terminal_tool as tt

        env = tt._create_environment(env_type="prism", image="", cwd="/root",
                                     timeout=timeout, task_id=task_id)
        self.addCleanup(env.cleanup)
        return env


class TestLazyProvisioning(RegisteredProviderTestCase):
    """Nothing is quoted or funded until the session runs a command."""

    def test_construction_rents_nothing(self):
        env = self.created_env()

        self.assertEqual(self.agent.lease_calls, [])
        self.assertIsNone(env.lease_info())
        self.assertEqual(self.entries(), [])

    def test_the_first_command_rents_one_gpu_and_the_second_reuses_it(self):
        env = self.created_env()

        env.execute("nvidia-smi")
        self.assertEqual(len(self.agent.lease_calls), 1)
        env.execute("nvidia-smi")
        self.assertEqual(len(self.agent.lease_calls), 1)


class TestPromptProbe(RegisteredProviderTestCase):
    """Hermes asks a remote backend what OS it is while it assembles the system
    prompt, then throws the environment away.

    On a rented backend that question costs a funded escrow and ten minutes of
    provisioning, before the model has produced a turn — and core already writes
    the prompt without an answer.
    """

    def probe(self):
        from agent import prompt_builder

        prompt_builder._clear_backend_probe_cache()
        self.addCleanup(prompt_builder._clear_backend_probe_cache)
        with mock.patch.dict(os.environ, {"TERMINAL_ENV": "prism"}):
            return prompt_builder._probe_remote_backend("prism")

    def test_the_prompt_build_probe_rents_nothing(self):
        self.assertIsNone(self.probe())
        self.assertEqual(self.agent.lease_calls, [])
        self.assertEqual(self.entries(), [])

    def test_the_probe_task_is_refused_whatever_it_asks_for(self):
        env = self.created_env(task_id=prism_environment.PROMPT_PROBE_TASK_ID)

        with self.assertRaises(PrismLeaseError):
            env.execute("uname -a", timeout=600)
        self.assertEqual(self.agent.lease_calls, [])
        self.assertEqual(self.entries(), [])


class TestRefundCredit(PrismTestCase):
    """A funded lease whose machine never hands over access is refunded by the
    escrow itself: the provision window expires and anyone may claim it back.

    Until that is seen on chain the reservation stands, because a late machine
    and one that is never coming read the same. Once it is seen, the day should
    not still be paying for a GPU the session never reached.
    """

    def provisioning_times_out(self, lease_id=3):
        self.agent.lease_error = PrismError(408, "access_timeout",
                                            {"lease_id": lease_id, "funding_hash": "0xfeed"})
        self.agent.state_lease_id = lease_id

    def failed_lease(self):
        """One funded lease that never came up, with its refund watch run out."""
        env = self.make_env()
        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        for watcher in prism_environment._watchers:
            watcher.join(10)
            self.assertFalse(watcher.is_alive(), "the refund watch did not finish")

    def test_a_refund_seen_on_chain_credits_the_day_back(self):
        self.provisioning_times_out()
        self.agent.states = ["provisioning", "provisioning", "refunded"]
        self.failed_lease()

        entry, = self.entries()
        self.assertEqual(entry["micros"], 0)
        # The transaction stays on the entry: what the day is owed back is a
        # different question from what the wallet did.
        self.assertEqual(entry["reference"], "0xfeed")
        self.assertEqual(self.agent.state_polls, 3)

    def test_a_lease_that_settles_normally_keeps_its_reservation(self):
        self.provisioning_times_out()
        self.agent.states = ["settlement_pending"]
        self.failed_lease()

        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])

    def test_a_control_plane_that_will_not_answer_keeps_the_reservation(self):
        self.provisioning_times_out()
        self.agent.leases_error = PrismError(503, "unavailable", {})
        self.failed_lease()

        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])

    def test_a_lease_the_control_plane_no_longer_lists_keeps_its_reservation(self):
        self.provisioning_times_out(lease_id=3)
        self.agent.state_lease_id = 99  # anything but the lease that was funded
        self.agent.states = ["refunded"]
        self.failed_lease()

        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])

    def test_a_failure_that_names_no_lease_is_not_watched(self):
        self.agent.lease_error = PrismError(504, "confirmation_timeout", {"hash": "0xabc"})
        env = self.make_env()

        with self.assertRaises(PrismLeaseError):
            env.execute("true")
        self.assertEqual(prism_environment._watchers, [])
        self.assertEqual([e["micros"] for e in self.entries()], [MICROS])


class _Context:
    """The one call a plugin makes into Hermes."""

    def register_terminal_environment_provider(self, provider):
        from agent import terminal_env_registry as reg

        reg.register_provider(provider)


if __name__ == "__main__":
    unittest.main()
