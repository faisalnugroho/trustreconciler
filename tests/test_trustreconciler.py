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
import sys
import time
from pathlib import Path

import pytest

from gltest.direct.loader import deploy_contract
from gltest.direct.sdk_loader import setup_sdk_paths

from helpers import (
    CONTRACT, TARGET_WALLET, FLAGGED_CP,
    COUNTERPARTY_1,
    REQUESTER, DATASET_URL_PREFIX,
    TEST_DATASET_COMMIT, TEST_DATASET_KECCAK, TEST_DATASET_REF,
    TEST_DATASET_URL,
    addr_str, iso_now, iso_in, set_time,
    tx, ok_list, empty_list, ok_balance, dataset_payload,
    old_clean_wallet_txs, fresh_clean_wallet_txs,
    flagged_funding_wallet_txs, busy_wallet_txs,
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
    # Deploy pinned to the TEST dataset (fake 40-hex commit + keccak of
    # the canonical mocked payload) — the pin machinery is exercised on
    # every test, exactly as the production deploy pins the real commit.
    contract = deploy_contract(CONTRACT, direct_vm,
                               TEST_DATASET_COMMIT, TEST_DATASET_KECCAK)
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


# ---------------------------------------------------------------------------
# Steward "Action needed" fixes — regression proofs (Sep 2026)
# ---------------------------------------------------------------------------
class TestStewardFixChainPinning:
    """Fix 1: re-evaluation must preserve the chain of the original
    record; a different chain is explicitly rejected."""

    def _seed(self, vm, c, chain="eth"):
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        return json.loads(c.request_reconciliation(TARGET_WALLET, chain))

    def test_reeval_with_different_chain_reverts(self, env):
        vm, c = env
        first = self._seed(vm, c, "eth")
        assert first["chain"] == "eth"
        set_time(vm, iso_in(3700))          # cooldown fully elapsed
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Risk")
        vm.sender = REQUESTER
        with pytest.raises(AssertionError) as ei:
            c.request_reevaluation(TARGET_WALLET, "base")
        assert "chain_mismatch" in str(ei.value)
        assert "pinned_to:eth" in str(ei.value)

    def test_reeval_with_different_chain_reverts_even_inside_cooldown(self, env):
        vm, c = env
        self._seed(vm, c, "eth")
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Risk")
        vm.sender = REQUESTER
        # no time warp: still inside cooldown AND wrong chain — the chain
        # pin must reject regardless of cooldown state.
        with pytest.raises(AssertionError) as ei:
            c.request_reevaluation(TARGET_WALLET, "base")
        assert "chain_mismatch" in str(ei.value)

    def test_renewed_request_with_different_chain_reverts(self, env):
        vm, c = env
        self._seed(vm, c, "eth")
        set_time(vm, iso_in(3700))
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Risk")
        vm.sender = REQUESTER
        with pytest.raises(AssertionError) as ei:
            c.request_reconciliation(TARGET_WALLET, "base")
        assert "chain_mismatch" in str(ei.value)
        # and the base record must NOT have been created/overwritten
        back = get_record(c)
        assert back["chain"] == "eth"
        assert back["reevaluation_count"] == 1

    def test_renewed_request_same_chain_after_cooldown_allowed(self, env):
        vm, c = env
        self._seed(vm, c, "base")
        set_time(vm, iso_in(3700))
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        rec = json.loads(c.request_reconciliation(TARGET_WALLET, "base"))
        assert rec["chain"] == "base"
        assert rec["reevaluation_count"] == 2

    def test_first_record_pins_chain_for_all_later_runs(self, env):
        vm, c = env
        self._seed(vm, c, "base")          # FIRST record on base
        set_time(vm, iso_in(3700))
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        with pytest.raises(AssertionError) as ei:
            c.request_reevaluation(TARGET_WALLET, "eth")
        assert "pinned_to:base" in str(ei.value)

    def test_cooldown_info_exposes_pinned_chain(self, env):
        vm, c = env
        self._seed(vm, c, "eth")
        info = json.loads(c.get_cooldown_info(TARGET_WALLET))
        assert info["chain"] == "eth"
        info_unknown = json.loads(c.get_cooldown_info(COUNTERPARTY_1))
        assert info_unknown["wallet_known"] is False
        assert info_unknown["chain"] == ""


class TestStewardFixRecordMutation:
    """Fix 2: prove the stored record REALLY changes after cooldown —
    last_updated bump + reevaluation_count increment + at least one
    substantive field (score/verdict/reasoning) differs when the new
    data differs. A no-op re-eval must be impossible to observe."""

    def _seed(self, vm, c):
        register_full_ok(vm, flagged_funding_wallet_txs(now_ts()))
        mock_llm_direction(
            vm, "Risk",
            reasoning=("The wallet was funded by an address carrying the "
                       "Fake_Phishing label; both models independently "
                       "identify this as decisive negative evidence."))
        vm.sender = REQUESTER
        return json.loads(c.request_reconciliation(TARGET_WALLET, "eth"))

    def test_record_really_changes_after_cooldown(self, env):
        vm, c = env
        before = self._seed(vm, c)
        assert before["final_verdict"] == "Aligned-Risky"
        t_before = before["last_updated"]

        set_time(vm, iso_in(3700))          # past the 1h cooldown
        vm.clear_mocks()
        # NEW on-chain reality: the flagged funder is GONE from the
        # wallet's history window and it now looks established + clean.
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        after = json.loads(c.request_reevaluation(TARGET_WALLET, "eth"))

        # (a) provenance fields prove the run happened
        assert after["reevaluation_count"] == before["reevaluation_count"] + 1
        assert after["last_updated"] > t_before
        assert after["requested_at"] > before["requested_at"]
        # (b) substantive change is NOT a no-op — the new data differs,
        # so at least one of score/verdict/reasoning must differ
        substantive = [
            after["signal_a_score"] != before["signal_a_score"],
            after["signal_b_score"] != before["signal_b_score"],
            after["final_verdict"] != before["final_verdict"],
            after["signal_a_reasoning"] != before["signal_a_reasoning"],
            after["signal_b_reasoning"] != before["signal_b_reasoning"],
            after["final_reasoning"] != before["final_reasoning"],
        ]
        assert any(substantive), "re-eval was a silent no-op"
        assert after["final_verdict"] == "Aligned-Trustworthy"
        assert after["signal_a_score"] < before["signal_a_score"]

        # (c) the change is COMMITTED to storage, not just returned
        stored = get_record(c)
        assert stored["reevaluation_count"] == 2
        assert stored["final_verdict"] == "Aligned-Trustworthy"
        assert stored["signal_a_score"] == after["signal_a_score"]
        assert stored["final_reasoning"] == after["final_reasoning"]
        assert stored["last_updated"] == after["last_updated"]

    def test_record_really_changes_when_new_data_fails(self, env):
        vm, c = env
        before = self._seed(vm, c)
        t_before = before["last_updated"]
        set_time(vm, iso_in(3700))
        vm.clear_mocks()
        # API now failing -> the record must concretely CHANGE to the
        # fail-safe state (again: not a no-op).
        vm.mock_web("module=account&action=txlist",
                    {"status": 500, "body": "down"})
        vm.mock_web("module=account&action=tokentx",
                    {"status": 200, "body": json.dumps(ok_list([]))})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(0))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "mutation-failure")
        vm.sender = REQUESTER
        after = json.loads(c.request_reevaluation(TARGET_WALLET, "eth"))
        assert after["reevaluation_count"] == 2
        assert after["last_updated"] > t_before
        assert after["final_verdict"] == "Undetermined"
        assert after["final_verdict"] != before["final_verdict"]
        assert "txlist" in after["final_reasoning"]
        stored = get_record(c)
        assert stored["final_verdict"] == "Undetermined"
        assert stored["reevaluation_count"] == 2


class TestStewardFixUndeterminedCooldown:
    """Fix 3: cooldown applies identically to Undetermined — no bypass."""

    def _seed_undetermined(self, vm, c):
        # data-failure run -> stored Undetermined record
        vm.mock_web("module=account&action=txlist",
                    {"status": 500, "body": "down"})
        vm.mock_web("module=account&action=tokentx",
                    {"status": 200, "body": json.dumps(ok_list([]))})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(0))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "undetermined-seed")
        vm.sender = REQUESTER
        rec = json.loads(c.request_reconciliation(TARGET_WALLET, "eth"))
        assert rec["final_verdict"] == "Undetermined"
        return rec

    def test_undetermined_reeval_before_cooldown_reverts(self, env):
        vm, c = env
        self._seed_undetermined(vm, c)
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        with pytest.raises(AssertionError) as ei:
            c.request_reevaluation(TARGET_WALLET, "eth")
        assert "cooldown_active" in str(ei.value)
        # nothing ran, nothing changed
        assert get_record(c)["final_verdict"] == "Undetermined"
        assert get_record(c)["reevaluation_count"] == 1

    def test_undetermined_new_request_before_cooldown_reverts(self, env):
        vm, c = env
        self._seed_undetermined(vm, c)
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        with pytest.raises(AssertionError) as ei:
            c.request_reconciliation(TARGET_WALLET, "eth")
        assert "cooldown_active" in str(ei.value)
        assert get_record(c)["reevaluation_count"] == 1

    def test_undetermined_retry_only_after_full_cooldown(self, env):
        vm, c = env
        self._seed_undetermined(vm, c)
        t0 = get_record(c)["last_updated"]
        # +30 min: still inside cooldown — must revert
        set_time(vm, iso_in(1800))
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        with pytest.raises(AssertionError):
            c.request_reevaluation(TARGET_WALLET, "eth")
        # +61 min: past cooldown — retry allowed, record concretely
        # changes to the recovered verdict
        set_time(vm, iso_in(3660))
        vm.clear_mocks()
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        vm.sender = REQUESTER
        after = json.loads(c.request_reevaluation(TARGET_WALLET, "eth"))
        assert after["final_verdict"] == "Aligned-Trustworthy"
        assert after["reevaluation_count"] == 2
        assert after["last_updated"] > t0
        assert get_record(c)["final_verdict"] == "Aligned-Trustworthy"


class TestStewardFixDatasetPinning:
    """Fix 4a: dataset pinned to an immutable commit + content-hash
    verified; every record stores the dataset_ref for auditability."""

    def test_records_embed_dataset_ref(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        # "<commit12>:<keccak16>" — auditable pin in EVERY record, and
        # it matches the deployment's pin exactly.
        assert rec["dataset_ref"] == TEST_DATASET_REF
        stored = get_record(c)
        assert stored["dataset_ref"] == rec["dataset_ref"]

    def test_get_dataset_pin_view_exposes_full_pin(self, env):
        vm, c = env
        pin = json.loads(c.get_dataset_pin())
        assert pin["commit_sha"] == TEST_DATASET_COMMIT
        assert pin["keccak256"] == TEST_DATASET_KECCAK
        # the fetch URL embeds the COMMIT SHA — never a moving ref
        assert ("/" + TEST_DATASET_COMMIT + "/") in pin["url"]
        assert pin["url"] == TEST_DATASET_URL
        assert not pin["url"].rstrip("/").endswith(
            ("/main", "/HEAD", "/master"))
        assert pin["ref"] == TEST_DATASET_REF

    def test_dataset_ref_is_real_keccak_of_mock_body(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        # the ref's keccak half must equal the ACTUAL keccak of the body
        # the mocks served (helpers computed the same way the contract
        # does — over the decoded UTF-8 body text).
        assert rec["dataset_ref"].split(":")[1] \
            == TEST_DATASET_KECCAK[:16]

    def test_dataset_content_hash_mismatch_undetermined(self, env):
        vm, c = env
        # a DIFFERENT dataset (someone replaced the file at the pinned
        # URL / a tampered mirror) — content hash check must fail the
        # run explicitly, never silently use the new labels
        register_full_ok(vm, old_clean_wallet_txs(now_ts()),
                         dataset_body=json.dumps(
                             {"addresses": [FLAGGED_CP.lower()]}))
        poison_llm(vm, "hash-mismatch")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "dataset_hash_mismatch")

    def test_deploy_rejects_moving_ref_pin(self, direct_vm):
        # constructing with 'main' (the OLD moving-ref behavior) must be
        # impossible — the constructor rejects non-40-hex commit SHAs.
        with pytest.raises(AssertionError) as ei:
            deploy_contract(CONTRACT, direct_vm, "main",
                            TEST_DATASET_KECCAK)
        assert "invalid_dataset_commit_sha" in str(ei.value)

    def test_deploy_rejects_malformed_keccak(self, direct_vm):
        with pytest.raises(AssertionError) as ei:
            deploy_contract(CONTRACT, direct_vm, TEST_DATASET_COMMIT,
                            "not-a-hash")
        assert "invalid_dataset_keccak256" in str(ei.value)

    def test_deploy_rejects_HEAD(self, direct_vm):
        with pytest.raises(AssertionError):
            deploy_contract(CONTRACT, direct_vm, "HEAD",
                            TEST_DATASET_KECCAK)

    def test_deploy_rejects_master(self, direct_vm):
        with pytest.raises(AssertionError):
            deploy_contract(CONTRACT, direct_vm, "master",
                            TEST_DATASET_KECCAK)

    def test_deploy_rejects_short_sha(self, direct_vm):
        with pytest.raises(AssertionError):
            deploy_contract(CONTRACT, direct_vm, "53246b6bb348b",
                            TEST_DATASET_KECCAK)

    def test_deploy_rejects_39_hex_sha(self, direct_vm):
        with pytest.raises(AssertionError):
            deploy_contract(CONTRACT, direct_vm, TEST_DATASET_COMMIT[:39],
                            TEST_DATASET_KECCAK)

    def test_deploy_rejects_empty_sha(self, direct_vm):
        with pytest.raises(AssertionError):
            deploy_contract(CONTRACT, direct_vm, "",
                            TEST_DATASET_KECCAK)

    def test_contract_source_has_no_moving_dataset_url(self, env):
        vm, c = env
        # static source-level guard: no raw.githubusercontent URL with a
        # moving ref may appear anywhere in the contract source.
        import re
        src = Path("contracts/TrustReconciler.py").read_text()
        assert not re.search(
            r"raw\.githubusercontent\.com/faisalnugroho/trustreconciler/"
            r"(main|HEAD|master)/", src), "dataset URL uses a moving ref"
        # the contract URL is built ONLY from a validated 40-hex pin
        assert "DATASET_REPO" in src
        assert "_valid_dataset_commit_sha" in src

    def test_undetermined_record_also_carries_dataset_ref(self, env):
        vm, c = env
        vm.mock_web("module=account&action=txlist",
                    {"status": 500, "body": "down"})
        vm.mock_web("module=account&action=tokentx",
                    {"status": 200, "body": json.dumps(ok_list([]))})
        vm.mock_web("module=account&action=balance",
                    {"status": 200, "body": json.dumps(ok_balance(0))})
        vm.mock_web(DATASET_MOCK_PATTERN,
                    {"status": 200, "body": json.dumps(dataset_payload())})
        poison_llm(vm, "undet-ref")
        vm.sender = REQUESTER
        rec = json.loads(c.request_reconciliation(TARGET_WALLET, "eth"))
        assert rec["final_verdict"] == "Undetermined"
        assert rec["dataset_ref"] == TEST_DATASET_REF


class TestStewardFixPartialHistoryHonesty:
    """Fix 4b: fetched-window limitations are explicit in the stored
    record, signal reasoning, prompt, and arbiter output."""

    def test_full_window_recorded_as_full(self, env):
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        assert rec["history_coverage"] == "full_window"

    def test_full_page_cap_forces_partial_window(self, env):
        vm, c = env
        register_full_ok(vm, busy_wallet_txs(now_ts(), 100))
        mock_llm_direction(
            vm, "Trust",
            reasoning=("Based on this partial window the wallet shows "
                       "organic, non-flagged activity; the verdict rests "
                       "on the first-100-tx sample, not full history."))
        rec = reconcile(vm, c)
        assert rec["history_coverage"] == "partial_window"
        assert "LIMITED DATA" in rec["signal_a_reasoning"]
        assert "LIMITED DATA" in rec["signal_b_reasoning"]
        assert "window only, not the full history" \
            in rec["signal_b_reasoning"]

    def test_empty_history_marked_no_visible_history(self, env):
        vm, c = env
        register_full_ok(vm, [])     # no txs at all for the address
        mock_llm_direction(
            vm, "Trust",
            reasoning=("No history is visible in the fetched window, so "
                       "this reading means nothing negative is VISIBLE, "
                       "not that the history was checked and is clean."))
        rec = reconcile(vm, c)
        assert rec["history_coverage"] == "no_visible_history"
        assert "coverage gap" in rec["signal_a_reasoning"]
        assert "VISIBLE" in rec["signal_b_reasoning"]

    def test_partial_window_llm_must_acknowledge_limit(self, env):
        vm, c = env
        register_full_ok(vm, busy_wallet_txs(now_ts(), 100))
        # LLM tries to issue a full-history-sounding verdict without
        # acknowledging the partial window -> rejected, fail-safe.
        mock_llm_direction(
            vm, "Trust",
            reasoning=("The wallet's complete and total history is "
                       "absolutely spotless with no risk whatsoever."))
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Undetermined"
        assert "partial_data_ack_missing" in rec["final_reasoning"]


def first_requested(vm, c):
    return json.loads(c.get_reconciliation(TARGET_WALLET)).get(
        "requested_at", 0)
