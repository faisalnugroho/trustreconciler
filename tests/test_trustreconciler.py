"""TrustReconciler test suite — spec scenarios 1-10 + hardening extras.

Spec mapping (build spec section 5):
  1  invalid address format          -> revert before nondet block
  2  happy path, both align trust    -> Aligned-Trustworthy
  3  happy path, both align risk     -> Aligned-Risky
  4  CORE divergence scenario        -> Divergent-Resolved-* + root cause
  5  txlist API 500/timeout          -> Undetermined (no LLM run)
  6  tokentx malformed/missing field -> Undetermined
  7  rate-limited 429                -> Undetermined
  8  re-eval before cooldown         -> revert/denied
  9  re-eval after cooldown w/ API failure -> Undetermined (fail-safe
     holds on the re-eval path too — the WarrantyClaimOracle lesson)
  10 dataset empty/garbled          -> Undetermined, never silently skipped

Additional hardening:
  + checksum-invalid address         -> revert
  + unsupported chain                -> revert
  + LLM malformed output            -> Undetermined
  + LLM tries Trust despite flagged  -> Undetermined (hard gate)
  + re-eval of unknown wallet       -> revert
  + cumulative reevaluation_count
  + Base chain happy path
  + view helpers
"""
import json
import time

import pytest

from gltest.direct.loader import deploy_contract
from gltest.direct.sdk_loader import setup_sdk_paths

from helpers import (
    CONTRACT, TARGET_WALLET, FLAGGED_CP,
    REQUESTER, DATASET_URL_PREFIX,
    addr_str, iso_now, iso_in, set_time,
    tx, ok_list, empty_list, ok_balance, dataset_payload,
    old_clean_wallet_txs, fresh_clean_wallet_txs,
    flagged_funding_wallet_txs,
    mock_web_ok, mock_llm_direction, mock_llm_divergent_root_cause,
    get_record, reconcile,
)

setup_sdk_paths()

DATASET_MOCK_PATTERN = DATASET_URL_PREFIX.replace(".", "\\.")


def now_ts():
    return int(time.time())


@pytest.fixture()
def env(direct_vm):
    set_time(direct_vm, iso_now())
    contract = deploy_contract(CONTRACT, direct_vm)
    return direct_vm, contract


def register_full_ok(vm, txs, ttxs=None, balance=10**18, dataset_body=None):
    """Register the complete 4-fetch happy-path mock set.

    gltest web mocks are FIRST-MATCH-WINS: a dataset mock registered here
    is permanently shadowed by any later dataset mock. To poison ONLY the
    dataset (tests 5-7, 10), pass ``dataset_body`` — the bad payload is
    baked into THIS registration, so no shadowed mock situation can occur.
    """
    vm.mock_web("module=account&action=txlist",
                {"status": 200, "body": json.dumps(ok_list(txs))})
    vm.mock_web("module=account&action=tokentx",
                {"status": 200,
                 "body": json.dumps(ok_list(ttxs if ttxs is not None else []))})
    vm.mock_web("module=account&action=balance",
                {"status": 200, "body": json.dumps(ok_balance(balance))})
    vm.mock_web(DATASET_MOCK_PATTERN,
                {"status": 200,
                 "body": (dataset_body if dataset_body is not None
                          else json.dumps(dataset_payload()))})


def poison_llm(vm, marker):
    """An LLM mock that must NEVER fire; if its marker shows up in the
    record, partial data reached the LLM — test failure."""
    vm.mock_llm("arbiter", json.dumps({
        "final_direction": "Trust", "confidence": "High",
        "final_reasoning": "BUG-" + marker + ": partial data reached the LLM"}))


def assert_undetermined(rec, expected_frag):
    assert rec["final_verdict"] == "Undetermined", rec
    assert rec["confidence"] == "Low"
    assert rec["signal_a_score"] == -1
    assert rec["signal_b_score"] == -1
    assert expected_frag in rec["final_reasoning"], rec["final_reasoning"]
    assert "Fail-safe" in rec["final_reasoning"]


# ---------------------------------------------------------------------------
# 1. Input validation — cheap checks BEFORE the nondet block
# ---------------------------------------------------------------------------
class TestInputValidation:
    def test_invalid_format_reverts(self, env):
        vm, c = env
        with pytest.raises(AssertionError):
            c.request_reconciliation("0x1234", "eth")
        with pytest.raises(AssertionError):
            c.request_reconciliation("not-an-address", "eth")
        with pytest.raises(AssertionError):
            c.request_reconciliation("0xZZ" + "0" * 38, "eth")
        assert json.loads(c.get_reconciliation("0x1234")) == {}

    def test_bad_checksum_reverts(self, env):
        vm, c = env
        bad = "0x" + "aB" * 20  # right shape, wrong EIP-55 case
        with pytest.raises(AssertionError):
            c.request_reconciliation(bad, "eth")
        assert json.loads(c.get_reconciliation(bad)) == {}

    def test_unsupported_chain_reverts(self, env):
        vm, c = env
        with pytest.raises(AssertionError):
            c.request_reconciliation(TARGET_WALLET, "solana")
        with pytest.raises(AssertionError):
            c.request_reconciliation(TARGET_WALLET, "")
        assert json.loads(c.get_reconciliation(TARGET_WALLET)) == {}


# ---------------------------------------------------------------------------
# 2-4. Deterministic signals + LLM arbitration happy paths
# ---------------------------------------------------------------------------
class TestHappyPaths:
    def test_aligned_trustworthy(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Aligned-Trustworthy"
        assert rec["confidence"] == "High"
        assert rec["divergence_detected"] is False
        assert rec["signal_a_score"] <= 20
        assert rec["signal_b_score"] >= 75
        back = get_record(c)
        assert back["final_verdict"] == "Aligned-Trustworthy"
        assert back["reevaluation_count"] == 1

    def test_aligned_risky_flagged_funding(self, env):
        vm, c = env
        register_full_ok(vm, flagged_funding_wallet_txs(now_ts()))
        mock_llm_direction(
            vm, "Risk",
            reasoning=("The wallet was funded by an address carrying the "
                       "Fake_Phishing label; both models independently "
                       "identify this as decisive negative evidence."))
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Aligned-Risky"
        assert rec["divergence_detected"] is False
        assert rec["signal_a_score"] >= 85
        assert rec["signal_b_score"] <= 10

    def test_core_divergence_scenario(self, env):
        vm, c = env
        register_full_ok(vm, fresh_clean_wallet_txs(now_ts()))
        mock_llm_divergent_root_cause(vm, "Trust")
        rec = reconcile(vm, c)
        assert rec["divergence_detected"] is True
        assert rec["final_verdict"] == "Divergent-Resolved-Trust"
        assert rec["confidence"] == "Medium"
        assert 50 <= rec["signal_a_score"] <= 60
        assert rec["signal_b_score"] >= 55
        assert "insufficient history" in rec["final_reasoning"]
        assert "Model A" in rec["final_reasoning"]
        assert "Model B" in rec["final_reasoning"]
        gap = abs(rec["signal_a_score"] - (100 - rec["signal_b_score"]))
        assert gap > 30

    def test_divergence_flag_deterministic_from_scores(self, env):
        vm, c = env
        register_full_ok(vm, fresh_clean_wallet_txs(now_ts()))
        mock_llm_divergent_root_cause(vm, "Trust")
        rec = reconcile(vm, c)
        gap = abs(rec["signal_a_score"]
                  - (100 - rec["signal_b_score"]))
        assert rec["divergence_detected"] == (gap > 30)


# ---------------------------------------------------------------------------
# 5-7, 10. Data failures -> Undetermined BEFORE any LLM judgment
# ---------------------------------------------------------------------------
class TestDataFailuresFailSafe:
    def test_txlist_http_500(self, env):
        vm, c = env
        vm.mock_web("module=account&action=txlist",
                    {"status": 500, "body": "server error"})
        vm.mock_web("module=account&action=tokentx",
                    {"status": 200, "body": json.dumps(ok_list([]))})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(10**18))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "txlist500")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "txlist")

    def test_tokentx_malformed_result_not_list(self, env):
        vm, c = env
        vm.mock_web("module=account&action=txlist",
                    {"status": 200, "body": json.dumps(ok_list([]))})
        # missing 'status' field + result is not a list
        vm.mock_web("module=account&action=tokentx",
                    {"status": 200,
                     "body": json.dumps({"result": "not-a-list"})})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(0))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "tokentx-malformed")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "tokentx")

    def test_rate_limited_429(self, env):
        vm, c = env
        vm.mock_web("module=account&action=txlist",
                    {"status": 429, "body": "rate limited"})
        vm.mock_web("module=account&action=tokentx",
                    {"status": 429, "body": "rate limited"})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(0))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "429")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "http_429")

    def test_dataset_empty_undetermined(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()),
                         dataset_body=json.dumps({"addresses": []}))
        poison_llm(vm, "dataset-empty")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "dataset")

    def test_dataset_garbled_json(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()),
                         dataset_body="this is not json{{{")
        poison_llm(vm, "dataset-garbled")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "dataset")

    def test_dataset_bad_entry_shape(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()),
                         dataset_body=json.dumps(
                             {"addresses": ["0x123", 5, None]}))
        poison_llm(vm, "dataset-bad-entry")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "dataset")


# ---------------------------------------------------------------------------
# 8-9. Cooldown + re-evaluation fail-safe consistency
# ---------------------------------------------------------------------------
class TestCooldownAndReeval:
    def _seed_first_run(self, vm, c):
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        return reconcile(vm, c)

    def test_reeval_before_cooldown_reverts(self, env):
        vm, c = env
        self._seed_first_run(vm, c)
        with pytest.raises(AssertionError):
            vm.sender = REQUESTER
            c.request_reevaluation(TARGET_WALLET, "eth")

    def test_request_reconcile_before_cooldown_also_reverts(self, env):
        vm, c = env
        self._seed_first_run(vm, c)
        with pytest.raises(AssertionError):
            vm.sender = REQUESTER
            c.request_reconciliation(TARGET_WALLET, "eth")

    def test_reeval_after_cooldown_api_failure_fail_safe(self, env):
        vm, c = env
        first = self._seed_first_run(vm, c)
        assert first["final_verdict"] == "Aligned-Trustworthy"
        set_time(vm, iso_in(3700))          # past the 1h cooldown
        vm.clear_mocks()
        # API now failing on the re-eval attempt
        vm.mock_web("module=account&action=txlist",
                    {"status": 500, "body": "down"})
        vm.mock_web("module=account&action=tokentx",
                    {"status": 200, "body": json.dumps(ok_list([]))})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(0))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "reeval-bypass")
        vm.sender = REQUESTER
        rec = json.loads(c.request_reevaluation(TARGET_WALLET, "eth"))
        assert rec["final_verdict"] == "Undetermined"
        assert rec["confidence"] == "Low"
        assert "txlist" in rec["final_reasoning"]
        assert rec["reevaluation_count"] == 2
        back = get_record(c)
        assert back["final_verdict"] == "Undetermined"
        assert back["reevaluation_count"] == 2

    def test_reeval_after_cooldown_success_overwrites(self, env):
        vm, c = env
        self._seed_first_run(vm, c)
        set_time(vm, iso_in(3700))
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        rec = json.loads(c.request_reevaluation(TARGET_WALLET, "eth"))
        assert rec["final_verdict"] == "Aligned-Trustworthy"
        assert rec["reevaluation_count"] == 2
        back = get_record(c)
        assert back["reevaluation_count"] == 2

    def test_reeval_unknown_wallet_reverts(self, env):
        vm, c = env
        with pytest.raises(AssertionError):
            vm.sender = REQUESTER
            c.request_reevaluation(TARGET_WALLET, "eth")


# ---------------------------------------------------------------------------
# LLM hardening
# ---------------------------------------------------------------------------
class TestLLMHardening:
    def test_malformed_llm_output_undetermined(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        vm.mock_llm("arbiter", "I am a string, not JSON at all")
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Undetermined"
        assert "llm" in rec["final_reasoning"].lower()

    def test_llm_trust_despite_flagged_contact_blocked(self, env):
        vm, c = env
        txs = [tx(FLAGGED_CP, TARGET_WALLET.lower(),
                  now_ts() - 60 * 86400)]
        for i in range(8):
            cp = "0x" + format(0x6600 + i, "04x") + "0" * 36
            ts = now_ts() - 50 * 86400 + i * 5 * 86400
            txs.append(tx(TARGET_WALLET.lower(), cp, ts))
        register_full_ok(vm, txs)
        mock_llm_direction(
            vm, "Trust",
            reasoning=("I choose to trust this wallet despite the "
                       "flagged contact because plausible-sounding "
                       "reasons."))
        rec = reconcile(vm, c)
        # hard gate: flagged contact mandates Risk; a Trust arbitration
        # is invalid -> fail-safe Undetermined, never stored as Trust
        assert rec["final_verdict"] == "Undetermined"
        assert "flagged_contact_requires_risk" in rec["final_reasoning"]

    def test_divergent_reasoning_must_be_substantive(self, env):
        vm, c = env
        register_full_ok(vm, fresh_clean_wallet_txs(now_ts()))
        # short reasoning with no root cause on a divergent case
        vm.mock_llm("arbiter", json.dumps({
            "final_direction": "Trust", "confidence": "High",
            "final_reasoning": "looks fine to me"}))
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Undetermined"
        assert "divergence_root_cause_missing" in rec["final_reasoning"]


# ---------------------------------------------------------------------------
# Base chain support
# ---------------------------------------------------------------------------
class TestBaseChain:
    def test_base_chain_happy_path(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c, chain="base")
        assert rec["final_verdict"] == "Aligned-Trustworthy"
        assert rec["chain"] == "base"


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------
class TestViews:
    def test_get_reconciliation_unknown(self, env):
        vm, c = env
        assert json.loads(c.get_reconciliation(TARGET_WALLET)) == {}

    def test_cooldown_info(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        reconcile(vm, c)
        info = json.loads(c.get_cooldown_info(TARGET_WALLET))
        assert info["wallet_known"] is True
        assert info["cooldown_seconds"] == 3600
        assert info["last_updated"] > 0


def first_requested(vm, c):
    return json.loads(c.get_reconciliation(TARGET_WALLET)).get(
        "requested_at", 0)
