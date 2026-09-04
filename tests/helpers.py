"""Shared test helpers for TrustReconciler direct-mode tests.

Patterns proven on SecondHandCarInspectionEscrow (60/60): set_time +
message_raw patch for cooldown control; mock_web/mock_llm drive the
pipeline; pytest.raises(AssertionError) for contract reverts (gltest
direct-mode convention).
"""
import json
import sys
import time

from eth_utils import to_checksum_address

sys.path.insert(0, "scripts")

CONTRACT = "contracts/TrustReconciler.py"

# --- Dataset pin for TESTS (steward fix 4) --------------------------------
# Tests deploy the contract with their OWN pin (a fake 40-hex commit and
# the keccak256 of the MOCKED dataset payload). This proves the pin
# machinery end-to-end (URL construction, hash verification, ref
# recording) without depending on repo data contents, and lets tests
# poison the dataset content to prove hash-mismatch detection.
#
# NOTE: keccak is computed via tests/vendor_keccak.py — a byte-identical
# vendored copy of the SDK's genlayer/py/keccak.py (Apache-2.0, ctz/
# keccak — the same file the contract itself uses). We deliberately do
# NOT import genlayer here: importing the SDK from the test helpers at
# module scope poisons pytest's module lifecycle (gltest evicts genlayer.*
# per-test via VMContext teardown; a pre-test import breaks the
# "only one contract is allowed" reset between tests and every env
# fixture after the first errors out).
TEST_DATASET_COMMIT = "0" * 39 + "1"          # fake but valid 40-hex sha
TEST_DATASET_URL = ("https://raw.githubusercontent.com/faisalnugroho/"
                    "trustreconciler/" + TEST_DATASET_COMMIT
                    + "/data/phishing_labels.json")

import vendor_keccak  # noqa: E402  (vendored, byte-identical to SDK)


def _test_dataset_keccak(payload_text):
    """keccak256 of the mocked dataset body — computed exactly the way
    the contract computes it (over the decoded UTF-8 body text)."""
    return vendor_keccak.Keccak256(
        payload_text.encode("utf-8")).hexdigest()

# Deterministic test identities (checksummed; EIP-55 valid).
def _ck(hex40):
    return to_checksum_address(hex40)

TARGET_WALLET = _ck("0x" + "11" * 20)          # the wallet being reconciled
COUNTERPARTY_1 = _ck("0x" + "22" * 20)         # clean counterparty
COUNTERPARTY_2 = _ck("0x" + "33" * 20)         # clean counterparty
FLAGGED_CP = _ck("0x" + "aa" * 20)              # in the label set
FLAGGED_FUNDER = _ck("0x" + "bb" * 20)          # in the label set
# vm.sender must be RAW BYTES (gltest direct mode) — the contract converts
# it to a checksummed Address via gl.message.sender_address.
REQUESTER = b"\xdd" * 20
REQUESTER_HEX = _ck("0x" + "dd" * 20)

DATASET_URL_PREFIX = TEST_DATASET_URL

ETH_API = "https://eth.blockscout.com/api"
BASE_API = "https://base.blockscout.com/api"

# Mocked dataset: only the fake test labels (NOT the real synced file —
# tests must not depend on repo data contents).
TEST_LABELS = [FLAGGED_CP.lower(), FLAGGED_FUNDER.lower()]


def addr_str(raw):
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "as_bytes"):
        raw = raw.as_bytes
    return to_checksum_address(bytes(raw))


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def iso_in(seconds):
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() + seconds)) + ".000Z"


def set_time(vm, iso):
    vm.warp(iso)
    gl_mod = sys.modules.get("genlayer.gl")
    if gl_mod is not None:
        try:
            if getattr(gl_mod, "message_raw", None):
                gl_mod.message_raw["datetime"] = iso
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API response builders (Etherscan/Blockscout compatible shape)
# ---------------------------------------------------------------------------
def tx(frm, to, ts, err="0", value="1000000000000000000"):
    return {
        "from": frm.lower(), "to": to.lower(),
        "timeStamp": str(ts), "isError": err, "value": value,
        "hash": "0x" + "ab" * 32,
    }


def tokentx(frm, to, ts, token="0x" + "ee" * 20):
    return {
        "from": frm.lower(), "to": to.lower(),
        "timeStamp": str(ts), "value": "5",
        "contractAddress": token.lower(), "hash": "0x" + "cd" * 32,
    }


def ok_list(items):
    return {"status": "1", "message": "OK", "result": items}


def empty_list():
    # Blockscout/Etherscan on an address with no history:
    return {"status": "0", "message": "No transactions found", "result": []}


def ok_balance(wei):
    return {"status": "1", "message": "OK", "result": str(wei)}


def dataset_payload(addresses=None):
    return {"addresses": list(addresses if addresses is not None
                             else TEST_LABELS),
            "schema": "test"}


# The canonical mocked-dataset body text and its pin: tests deploy the
# contract pinned to THIS keccak (computed over the exact body text the
# mocks serve), so hash verification passes on happy paths and fails
# deterministically on poisoned bodies.
DATASET_BODY = json.dumps(dataset_payload())
TEST_DATASET_KECCAK = _test_dataset_keccak(DATASET_BODY)
TEST_DATASET_REF = (TEST_DATASET_COMMIT[:12] + ":"
                    + TEST_DATASET_KECCAK[:16])


# ---------------------------------------------------------------------------
# Wallet fixtures for each scenario
# ---------------------------------------------------------------------------
def old_clean_wallet_txs(now, n=25):
    """Established (multi-year), organic history with 15+ unique clean
    counterparties — both signals should AGREE the wallet is fine."""
    txs = []
    cp_cycle = [COUNTERPARTY_1, COUNTERPARTY_2]
    # 15 unique clean counterparties, spread over ~800 days
    for i in range(n):
        cp = _ck("0x" + format(0x4400 + i, "04x") + "0" * 36)
        ts = now - 800 * 86400 + i * 30 * 86400  # 30 days apart
        txs.append(tx(cp, TARGET_WALLET.lower(), ts))
        txs.append(tx(TARGET_WALLET.lower(), cp.lower(), ts + 60))
    # some same-cp repeat interactions to mimic organic usage
    for i in range(4):
        ts = now - 30 * 86400 + i * 86400
        txs.append(tx(TARGET_WALLET.lower(), COUNTERPARTY_1.lower(), ts))
    return txs


def fresh_clean_wallet_txs(now):
    """Divergence core scenario: created < 7 days ago, a handful of clean
    interactions, zero flagged contact. Signal A: insufficient history ->
    MEDIUM risk. Signal B: no negative evidence -> HIGH trust."""
    txs = []
    for i in range(4):
        cp = _ck("0x" + format(0x4400 + i, "04x") + "0" * 36)
        ts = now - 5 * 86400 + i * 3600
        txs.append(tx(cp, TARGET_WALLET.lower(), ts))
    txs.append(tx(TARGET_WALLET.lower(), COUNTERPARTY_1.lower(),
                  now - 4 * 86400))
    return txs


def flagged_funding_wallet_txs(now):
    """Funding source IS in the Fake_Phishing label set — both signals
    go negative and AGREE on Risk."""
    txs = [tx(FLAGGED_FUNDER, TARGET_WALLET.lower(), now - 90 * 86400)]
    for i in range(5):
        cp = _ck("0x" + format(0x5500 + i, "04x") + "0" * 36)
        ts = now - 80 * 86400 + i * 7 * 86400
        txs.append(tx(TARGET_WALLET.lower(), cp.lower(), ts))
    return txs


def busy_wallet_txs(now, n=100):
    """100 native txs — fills the fetch page cap exactly, forcing
    history_coverage='partial_window' (steward fix 4)."""
    txs = []
    for i in range(n):
        cp = _ck("0x" + format(0x7700 + i % 20, "04x") + "0" * 36)
        ts = now - 400 * 86400 + i * 3 * 86400
        txs.append(tx(TARGET_WALLET.lower(), cp.lower(), ts))
    return txs


# ---------------------------------------------------------------------------
# Mock registration
# ---------------------------------------------------------------------------
def mock_web_ok(vm, target=TARGET_WALLET, txs=None, ttxs=None, balance=10**18,
                chain_api=ETH_API, dataset_addresses=None):
    """Register happy-path mocks for the full 4-fetch pipeline."""
    now = int(time.time())
    txs = txs if txs is not None else old_clean_wallet_txs(now)
    ttxs = ttxs if ttxs is not None else []
    vm.mock_web("module=account&action=txlist",
                {"status": 200, "body": json.dumps(ok_list(txs))})
    vm.mock_web("module=account&action=tokentx",
                {"status": 200, "body": json.dumps(ok_list(ttxs))})
    vm.mock_web("module=account&action=balance",
                {"status": 200, "body": json.dumps(ok_balance(balance))})
    vm.mock_web(DATASET_URL_PREFIX.replace(".", "\\."),
                {"status": 200,
                 "body": json.dumps(dataset_payload(dataset_addresses))})


def mock_llm_direction(vm, direction, confidence="High", reasoning=None):
    """Mock the arbiter LLM to return a given final direction."""
    if reasoning is None:
        reasoning = (
            "Both models align on the wallet's clean, established "
            "history with no flagged contact and organic diversity.")
    payload = json.dumps({
        "final_direction": direction,
        "confidence": confidence,
        "final_reasoning": reasoning,
    })
    vm.mock_llm("arbiter", payload)


def mock_llm_divergent_root_cause(vm, direction="Trust"):
    reasoning = (
        "The models diverge on insufficient history: Model A penalizes the "
        "wallet's young age as unproven risk while Model B credits the "
        "complete absence of flagged contact. For a 5-day-old wallet with "
        "only ordinary transfers and zero negative evidence, the optimistic "
        "reading fits the actual evidence better; the conservative penalty "
        "is precaution, not evidence of wrongdoing.")
    mock_llm_direction(vm, direction, "Medium", reasoning)


def get_record(c, wallet=TARGET_WALLET):
    return json.loads(c.get_reconciliation(wallet))


def reconcile(vm, c, wallet=TARGET_WALLET, chain="eth", sender=REQUESTER):
    vm.sender = sender
    return json.loads(c.request_reconciliation(wallet, chain))
