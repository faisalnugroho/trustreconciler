"""STOP 0 evidence tests: secret-handling hygiene before any real key exists.

These run BEFORE the real API key is ever placed on this machine, proving:
  1. .gitignore really ignores .env (and would ignore it at commit time)
  2. .env.example exists, is committed, and contains NO real value
  3. the config getter fails fast when the key is missing
  4. the .env file parser works and never overrides a real env var
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import etherscan_config as ec  # noqa: E402


def test_env_file_is_git_ignored():
    """`.env` must be ignored BEFORE any real key is ever written there."""
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", ".env"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, ".env is NOT git-ignored — refusing to proceed"
    assert ".gitignore" in r.stdout, "ignored by something other than .gitignore"


def test_env_example_exists_with_empty_value():
    example = REPO_ROOT / ".env.example"
    assert example.exists(), ".env.example is missing"
    found = False
    for line in example.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ETHERSCAN_API_KEY="):
            found = True
            assert stripped == "ETHERSCAN_API_KEY=", (
                ".env.example must NOT contain a value: %r" % stripped
            )
    assert found, ".env.example must contain the ETHERSCAN_API_KEY= entry"


def test_env_example_is_trackable():
    """.env.example must NOT be git-ignored (it is the committed template)."""
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", ".env.example"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1, ".env.example is being git-ignored — it should be committed"


def test_getter_fails_fast_when_key_missing(monkeypatch):
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    raised = False
    try:
        ec.get_etherscan_api_key()
    except RuntimeError as e:
        raised = True
        assert "ETHERSCAN_API_KEY" in str(e)
        assert ".env" in str(e)
    assert raised, "getter must raise RuntimeError when key is missing"


def test_getter_returns_env_value(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "unit-test-dummy-key")
    assert ec.get_etherscan_api_key() == "unit-test-dummy-key"


def test_env_file_parser_loads_key(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment line\nETHERSCAN_API_KEY=file-key-123\nOTHER_VAR=5\n")
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    loaded = ec.load_env_file(f)
    assert "ETHERSCAN_API_KEY" in loaded
    assert ec.get_etherscan_api_key() == "file-key-123"


def test_env_file_parser_never_overrides_real_env(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("ETHERSCAN_API_KEY=from-file\n")
    monkeypatch.setenv("ETHERSCAN_API_KEY", "from-env")
    ec.load_env_file(f)
    assert ec.get_etherscan_api_key() == "from-env", (
        "existing environment must take precedence over .env file"
    )


def test_missing_env_file_is_silent_noop():
    assert ec.load_env_file(Path("/nonexistent/path/.env")) == []
