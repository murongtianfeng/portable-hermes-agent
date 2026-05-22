"""Shared fixtures for the hermes-agent test suite."""

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Windows CI exclusions (win32 only)
# ─────────────────────────────────────────────────────────────────────────────
# The upstream suite is POSIX-first. Some tests fail on Windows for legitimate
# platform reasons (persistent bash shell, Unix signals, POSIX file perms,
# audio/clipboard, macOS Homebrew, getpass/TUI, case-insensitive FS) or are
# upstream test bugs (hardcoded "/" paths, clear=True home-wipe, source files
# read without an explicit encoding). They are skipped on win32 ONLY so the
# Windows CI can be green; they still run on Linux/macOS. List + rationale live
# in tests/windows_skip.txt — remove entries there as they get fixed.

# These two simulate KeyboardInterrupt and abort the whole pytest session on
# Windows, so they must not even be collected.
collect_ignore = (
    ["test_interrupt_propagation.py", "test_real_interrupt_subagent.py"]
    if sys.platform == "win32"
    else []
)


def _load_windows_skip_nodeids() -> frozenset:
    if sys.platform != "win32":
        return frozenset()
    path = Path(__file__).parent / "windows_skip.txt"
    if not path.exists():
        return frozenset()
    return frozenset(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


_WINDOWS_SKIP_NODEIDS = _load_windows_skip_nodeids()


def pytest_collection_modifyitems(config, items):
    """Skip known Windows-incompatible tests on win32 (see tests/windows_skip.txt)."""
    if not _WINDOWS_SKIP_NODEIDS:
        return
    skip = pytest.mark.skip(
        reason="Windows-incompatible (POSIX-ism / upstream test bug); see tests/windows_skip.txt"
    )
    for item in items:
        if item.nodeid in _WINDOWS_SKIP_NODEIDS:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to a temp dir so tests never write to ~/.hermes/."""
    fake_home = tmp_path / "hermes_test"
    fake_home.mkdir()
    (fake_home / "sessions").mkdir()
    (fake_home / "cron").mkdir()
    (fake_home / "memories").mkdir()
    (fake_home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    # Reset plugin singleton so tests don't leak plugins from ~/.hermes/plugins/
    try:
        import hermes_cli.plugins as _plugins_mod
        monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
    except Exception:
        pass
    # Tests should not inherit the agent's current gateway/messaging surface.
    # Individual tests that need gateway behavior set these explicitly.
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal hermes config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Global test timeout ─────────────────────────────────────────────────────
# Kill any individual test that takes longer than 30 seconds.
# Prevents hanging tests (subprocess spawns, blocking I/O) from stalling the
# entire test suite.

def _timeout_handler(signum, frame):
    raise TimeoutError("Test exceeded 30 second timeout")

@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Kill any individual test that takes longer than 30 seconds.
    SIGALRM is Unix-only; skip on Windows."""
    if sys.platform == "win32":
        yield
        return
    old = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(30)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)
