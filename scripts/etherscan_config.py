"""TrustReconciler — Etherscan API key configuration (BACKEND SCRIPTS ONLY).

Rules enforced by this module:
  * The API key is read EXCLUSIVELY from the environment
    (``os.environ["ETHERSCAN_API_KEY"]``), optionally seeded from a local
    git-ignored ``.env`` file at the repo root.
  * The key is NEVER hardcoded in any source file, and this module is
    NEVER imported by the on-chain contract (the GenLayer contract cannot
    read host environment variables; how the contract obtains its data is
    decided in the architecture doc, see docs/ARCHITECTURE.md once written).
  * Missing key -> loud RuntimeError with actionable instructions
    (fail-fast), never a silent default.

Usage (deploy scripts, dataset sync, smoke tests):
    from etherscan_config import load_env_file, get_etherscan_api_key
    load_env_file()                     # optional: seed env from .env
    key = get_etherscan_api_key()        # raises if unset
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

# Etherscan V2 API (single key works for all chains via chainid param).
ETHEREUM_CHAINID = 1
BASE_CHAINID = 8453
ETHEREUM_SCAN_URL = "https://api.etherscan.io/v2/api"
BASESCAN_URL = "https://api.basescan.org/v2/api"


def load_env_file(path: Path = ENV_FILE, override: bool = False) -> list:
    """Parse ``KEY=VALUE`` lines from a .env-style file into os.environ.

    - Comments (#) and blank lines are skipped.
    - Existing environment variables WIN unless ``override=True``
      (so systemd EnvironmentFile / CI secrets always take precedence).
    - Returns the list of keys actually loaded.
    """
    path = Path(path)
    if not path.exists():
        return []
    loaded = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not key:
                continue
            if override or key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded


def get_etherscan_api_key() -> str:
    """Fail-fast getter for the Etherscan API key.

    Raises RuntimeError with actionable instructions if the key is unset
    or empty. NEVER returns a hardcoded fallback.
    """
    key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ETHERSCAN_API_KEY is not set. Fix one of these ways: "
            "(1) create {env_file} at the repo root (copy from .env.example, "
            "fill in your key — .env is git-ignored); "
            "(2) export it in the shell before running the script; "
            "(3) for systemd services, use EnvironmentFile= pointing to a "
            "root-owned file with mode 600 OUTSIDE the repo. "
            "Never hardcode the key in source files.".format(env_file=ENV_FILE)
        )
    return key
