"""What the plugin ships beyond the terminal backend: skills, mcp.json, profile,
and the prose that tells an operator what the thing does.

These run against a throwaway HERMES_HOME so the developer's real profile is
never read or written. They exercise Hermes' own discovery, not a reimplementation
of it: the plugin manager loads the directory, the skills tool finds the skills
through ``skills.external_dirs``, and the Agent Plugins v1 validator parses
``mcp.json``.

The documentation tests read the numbers out of hermes-agent and this plugin
rather than restating them, so a constant that moves upstream fails here instead
of quietly making the docs wrong.
"""

import importlib
import re
import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
SIDE_MODELS_DOC = REPO_ROOT / "docs" / "confidential-side-models.md"
SKILLS = ("prism-compute", "prism-cuda-repro", "prism-receipts")
REPRO_TOOLS = (
    "prism_gpu_capacity",
    "prism_prepare_gpu_repro",
    "prism_gpu_repro_status",
    "prism_gpu_repro_evidence",
    "prism_verify_gpu_repro",
    "prism_gpu_receipts",
)


@pytest.fixture(scope="module")
def hermes_home(tmp_path_factory):
    """A Hermes home holding a copy of this plugin, with the skills wired in."""
    home = tmp_path_factory.mktemp("hermes-home")
    shutil.copytree(
        REPO_ROOT,
        home / "plugins" / "prism",
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.egg-info"),
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - prism\n"
        "skills:\n  external_dirs:\n    - plugins/prism/skills\n"
    )
    return home


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture(scope="module")
def loaded_plugin(hermes_home, monkeypatch_module):
    monkeypatch_module.setenv("HERMES_HOME", str(hermes_home))
    # These read HERMES_HOME at import time and cache it, so an earlier test
    # module that resolved the developer's real home has to be re-resolved
    # before discovery runs against the throwaway one.
    for name in ("hermes_constants", "hermes_cli.config", "agent.skill_utils",
                 "tools.skills_tool", "hermes_cli.plugins"):
        module = importlib.import_module(name)
        importlib.reload(module)
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    return manager


def test_plugin_loads_with_the_new_files_present(loaded_plugin):
    plugin = loaded_plugin._plugins.get("prism")
    assert plugin is not None, "prism plugin was not discovered"
    assert plugin.enabled, plugin.error


def test_skills_are_discovered_through_external_dirs(loaded_plugin, hermes_home):
    from agent.skill_utils import get_all_skills_dirs
    from tools.skills_tool import _find_all_skills

    # Guards the test itself: a stale HERMES_HOME would scan the developer's
    # real skills tree and the assertion below would mean nothing.
    assert hermes_home.resolve() / "plugins" / "prism" / "skills" in [
        directory.resolve() for directory in get_all_skills_dirs()
    ]
    found = {skill["name"] for skill in _find_all_skills()}
    assert set(SKILLS) <= found, f"missing {set(SKILLS) - found}"


def test_each_skill_renders(loaded_plugin):
    from tools.skills_tool import skill_view

    for name in SKILLS:
        rendered = skill_view(name)
        assert isinstance(rendered, str) and len(rendered) > 500, name


def test_skills_pass_the_hermes_linter():
    from tools.skill_linter import format_findings, has_errors, lint_skill

    for name in SKILLS:
        findings = lint_skill(REPO_ROOT / "skills" / name / "SKILL.md")
        assert not has_errors(findings), f"{name}:\n{format_findings(findings)}"
        assert not findings, f"{name}:\n{format_findings(findings)}"


def test_mcp_json_parses_and_exposes_only_the_read_only_endpoint(hermes_home):
    from hermes_cli.agent_plugins import _discover_mcp

    diagnostics = []
    servers = _discover_mcp(
        hermes_home / "plugins" / "prism",
        hermes_home / "plugin-data" / "prism",
        diagnostics,
        create_data=False,
    )
    assert not diagnostics, [(d.scope, d.message) for d in diagnostics]
    assert set(servers) == {"prism-repro"}
    assert servers["prism-repro"]["url"] == "https://prismnetwork.tech/api/mcp"
    # A stdio entry here would put the wallet-side npm server, and its spending
    # tools, one config copy away from a model with no allowlist to stop it.
    assert "command" not in servers["prism-repro"]


def test_compute_profile_fragment_is_valid_and_bounded():
    fragment = yaml.safe_load((REPO_ROOT / "profiles" / "compute" / "config.yaml").read_text())
    prism = fragment["terminal"]["prism"]

    assert fragment["terminal"]["backend"] == "prism"
    assert prism["max_usdg"] <= 1, "profile must not loosen the plugin default"
    assert prism["daily_budget_usdg"] <= 5, "profile must not loosen the plugin default"
    assert prism["sync_credentials"] is False
    assert fragment["skills"]["external_dirs"] == ["plugins/prism/skills"]
    assert fragment["mcp_servers"]["prism-repro"]["tools"]["include"] == list(REPRO_TOOLS)


def test_compute_profile_ships_a_soul_and_instructions():
    for name in ("SOUL.md", "README.md"):
        path = REPO_ROOT / "profiles" / "compute" / name
        assert path.is_file() and path.stat().st_size > 500, name


def test_readme_documents_the_idle_release_this_plugin_owns():
    from prism_environment import DEFAULT_IDLE_RELEASE_S

    readme = README.read_text()
    row = re.search(
        r"^\|\s*`idle_release_seconds`\s*\|\s*`(\d+)`\s*\|(.+?)\|\s*$", readme, re.M
    )
    assert row, "idle_release_seconds is missing from the configuration table"
    assert int(row.group(1)) == DEFAULT_IDLE_RELEASE_S
    assert "`0`" in row.group(2), "the table has to say what 0 does"
    assert f"{DEFAULT_IDLE_RELEASE_S // 60} minutes" in readme

    # The idle release is this plugin's own daemon thread. Hermes reaps exited
    # Docker containers and nothing else, so crediting it here sends an
    # operator whose lease outlived a session to the wrong config file.
    assert not re.search(r"Hermes\s+reaps", readme)


def test_side_model_doc_states_the_window_the_gateway_advertises():
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

    doc = SIDE_MODELS_DOC.read_text()
    assert "4,096-token context window" in doc
    assert f"{MINIMUM_CONTEXT_LENGTH:,}" in doc

    # Hermes reads a custom endpoint's window from that endpoint's own
    # /v1/models, so it sees 4096 and rejects the model up front. The earlier
    # text had it missing the lookup and defaulting to 256K, which described a
    # mid-session surprise that cannot happen.
    for wrong in ("256,000", "public model catalog", "nothing objects at startup"):
        assert wrong not in doc, wrong


def test_side_model_doc_does_not_offer_the_enclave_as_the_main_model():
    doc = SIDE_MODELS_DOC.read_text()
    para = next(
        p for p in doc.split("\n\n") if p.startswith("`hermes model` lists Prism")
    )
    assert "refuses to start" in para
    assert "you can also drive" not in para


def test_side_model_doc_puts_the_compression_failure_where_hermes_raises_it():
    """The two rejections happen at different moments and the doc must say so.

    A main model under the floor is rejected while the agent is constructed
    (``agent_init`` raises before a prompt is sent). The compression model is
    probed lazily, on the first compaction, so the same misconfiguration
    surfaces mid-session. Telling an operator the session "refuses to start"
    sends them looking for a startup error that never appears.
    """
    import inspect

    from agent import agent_init, conversation_compression
    from agent.auxiliary_client import _task_minimum_context_length
    from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

    assert _task_minimum_context_length("compression") == MINIMUM_CONTEXT_LENGTH
    for task in ("title_generation", "memory_query_rewrite"):
        assert _task_minimum_context_length(task) is None, task

    # The probe hangs off the compaction path, not off agent construction.
    assert "check_compression_model_feasibility(agent)" in inspect.getsource(
        conversation_compression.compress_context
    )
    assert not re.search(
        r"_check_compression_model_feasibility\(\)", inspect.getsource(agent_init)
    )

    section = SIDE_MODELS_DOC.read_text().split(
        "### Context compression, and why it is not offered", 1
    )[1]
    assert "refuses to start" not in section
    assert "the compaction" in section


def test_side_model_doc_prompt_sizes_match_hermes():
    import inspect

    from agent import title_generator
    from agent.context_compressor import _SUMMARY_INPUT_MAX_CHARS
    from agent.title_generator import MAX_TITLE_INPUT_CHARS
    from plugins.memory import query_rewrite
    from plugins.memory.query_rewrite import _MAX_INPUT_CHARS

    doc = SIDE_MODELS_DOC.read_text()
    for chars in (MAX_TITLE_INPUT_CHARS, _MAX_INPUT_CHARS, _SUMMARY_INPUT_MAX_CHARS):
        assert f"{chars:,} chars" in doc, chars

    # How far over the line the compression prompt is. The confidential tier
    # takes a 32 KiB body and sealing roughly doubles the envelope, so the
    # plaintext budget the doc quotes everywhere is 16 KiB.
    multiple = _SUMMARY_INPUT_MAX_CHARS / (16 * 1024)
    assert 9.5 <= multiple <= 10.5, multiple
    assert "roughly ten times" in doc
    assert "two and a half times" not in doc

    # The output caps are literals at the call sites upstream, and they are
    # half of what makes these two tasks fit a 1024-token ceiling.
    for module, cap in ((title_generator, "64"), (query_rewrite, "96")):
        assert f"max_tokens={cap}" in inspect.getsource(module), module.__name__
        assert f"| {cap} tokens |" in doc, cap
