"""Integration tests for the Prism terminal backend.

These rent a real GPU and spend real USDG from the wallet in PRISM_AGENT_KEY.
Nothing here runs unless you ask for it twice:

    PRISM_LIVE_SPEND=1 TERMINAL_ENV=prism pytest tests/test_prism_terminal_live.py -m integration

The suite leases once, runs every check on that one machine, and releases it at
the end. Cost is the lease deposit for ``terminal.prism.lease_seconds``, bounded
by the same per-lease cap as any other command. The escrow settles at the end of
the window that was paid for, so releasing early does not refund it.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# The project-wide hermetic conftest wipes secrets before each test, so the
# wallet key is captured at import time and re-injected below.
_AGENT_KEY = os.getenv("PRISM_AGENT_KEY")
if not _AGENT_KEY:
    pytest.skip("PRISM_AGENT_KEY not set", allow_module_level=True)
if os.getenv("PRISM_LIVE_SPEND") != "1":
    pytest.skip("live leases cost real USDG; set PRISM_LIVE_SPEND=1 to run them",
                allow_module_level=True)

# Import terminal_tool via importlib to avoid tools/__init__.py side effects.
# IMPORTANT: this creates a module object DISTINCT from ``tools.terminal_tool``;
# every helper and global this file touches must come from THIS module object,
# or assertions would read a registry the executed code never wrote to.
import importlib.util

_hermes_agent = Path(
    os.environ.get("HERMES_AGENT_REPO", Path.home() / ".hermes" / "hermes-agent")
).expanduser()
sys.path.insert(0, str(_hermes_agent))

spec = importlib.util.spec_from_file_location(
    "terminal_tool", _hermes_agent / "tools" / "terminal_tool.py"
)
terminal_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terminal_module)

terminal_tool = terminal_module.terminal_tool
cleanup_vm = terminal_module.cleanup_vm
_resolve_container_task_id = terminal_module._resolve_container_task_id
_active_environments = terminal_module._active_environments

_RUN_ID = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module", autouse=True)
def _force_prism():
    import __init__ as plugin_pkg
    from agent import terminal_env_registry as reg

    os.environ["PRISM_AGENT_KEY"] = _AGENT_KEY
    os.environ["TERMINAL_ENV"] = "prism"
    reg.register_provider(plugin_pkg.PrismProvider())
    yield
    reg._reset_for_tests()


@pytest.fixture(scope="module")
def task_id():
    """One lease for the whole module; released at the end."""
    tid = f"prism_live_{_RUN_ID}"
    yield tid
    cleanup_vm(_resolve_container_task_id(tid))


def _run(command, task_id, **kwargs):
    return json.loads(terminal_tool(command, task_id=task_id, **kwargs))


class TestPrismBasic:
    def test_echo(self, task_id):
        r = _run("echo 'Hello from a rented GPU'", task_id)
        assert r["exit_code"] == 0
        assert "Hello from a rented GPU" in r["output"]

    def test_nonzero_exit(self, task_id):
        assert _run("exit 42", task_id)["exit_code"] == 42

    def test_os_info(self, task_id):
        r = _run("uname -a", task_id)
        assert r["exit_code"] == 0
        assert "Linux" in r["output"]


class TestPrismGpu:
    def test_a_gpu_is_actually_attached(self, task_id):
        r = _run("nvidia-smi --query-gpu=name --format=csv,noheader", task_id, timeout=120)
        assert r["exit_code"] == 0
        assert r["output"].strip(), "the leased machine reports no GPU"

    def test_the_command_did_not_run_on_this_host(self, task_id):
        r = _run("cat /etc/machine-id 2>/dev/null || cat /proc/sys/kernel/random/boot_id", task_id)
        assert r["exit_code"] == 0
        host = Path("/proc/sys/kernel/random/boot_id")
        if host.exists():
            assert host.read_text().strip() not in r["output"]


class TestPrismSession:
    def test_the_lease_is_reused_across_commands(self, task_id):
        key = _resolve_container_task_id(task_id)
        _run("true", task_id)
        env = _active_environments[key]
        first = env.lease_info()["lease_id"]

        _run("true", task_id)
        assert _active_environments[key] is env
        assert env.lease_info()["lease_id"] == first

    def test_env_vars_and_files_survive_between_commands(self, task_id):
        _run("export PRISM_LIVE_MARKER=heyo && echo marker > /tmp/prism_live.txt", task_id)
        r = _run("echo $PRISM_LIVE_MARKER && cat /tmp/prism_live.txt", task_id)
        assert r["exit_code"] == 0
        assert "heyo" in r["output"]
        assert "marker" in r["output"]

    def test_the_wallet_key_never_reaches_the_rented_machine(self, task_id):
        r = _run("env | grep -c '^PRISM_' || true", task_id)
        assert r["output"].strip().splitlines()[-1] == "0"
