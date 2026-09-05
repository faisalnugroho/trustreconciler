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
    tx, tokentx, ok_list, empty_list, ok_balance, dataset_payload,
    old_clean_wallet_txs, fresh_clean_wallet_txs,
    flagged_funding_wallet_txs, busy_wallet_txs, window_wallet_txs,
    mock_web_ok, mock_web_paginated, mock_web_paginated_explicit,
    mock_llm_direction, mock_llm_divergent_root_cause,
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


def register_full_ok(vm, txs, ttxs=None, balance=10**18, dataset_body=None,
                     page_aware=False):
    """Register the complete happy-path mock set for the PAGINATED
    pipeline (steward fix 5).

    gltest web mocks are FIRST-MATCH-WINS: a dataset mock registered here
    is permanently shadowed by any later dataset mock. To poison ONLY the
    dataset (tests 5-7, 10), pass ``dataset_body`` — the bad payload is
    baked into THIS registration, so no shadowed mock situation can occur.

    page_aware=False (default) registers the OLD catch-all pattern
    ("action=txlist" matches every ?page=N URL): every page returns the
    same body. Use this when a test only cares that SOME tx data arrives
    (fail-safe tests, cooldown tests, happy-path verdicts where the
    wallet's history fits one page). page_aware=True uses
    mock_web_paginated to serve proper per-page slices (pagination
    tests). NOTE: dataset_body poisoning is only supported in the
    catch-all mode (first-match-wins: mock_web_paginated registers its
    own dataset mock that would shadow a later one)."""
    ttxs_list = ttxs if ttxs is not None else []
    if page_aware:
        assert dataset_body is None, \
            "dataset poisoning not supported in page_aware mode"
        mock_web_paginated(vm, txs, ttxs_list, balance=balance)
        return
    vm.mock_web("module=account&action=txlist",
                {"status": 200, "body": json.dumps(ok_list(txs))})
    vm.mock_web("module=account&action=tokentx",
                {"status": 200,
                 "body": json.dumps(ok_list(ttxs_list))})
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


# --------------------------------------------------------------------------- 
# Steward fix 5 (Sep 2026 round-3) — paginated history window
# ---------------------------------------------------------------------------
class TestStewardFixPaginatedWindow:
    """Fix 5: the history fetch is PAGINATED (3 txlist pages + 2 tokentx
    pages of 100, ascending) with a FIXED page plan fetched identically
    by the leader and every validator; wallets between the old 100-tx
    cap and the new 300-tx window must now classify full_window."""

    def test_short_history_stops_drain_at_short_page(self, env):
        """A wallet whose full history fits ONE page (old_clean_wallet,
        54 txs) must be fetched with exactly ONE txlist page: pages 2-3
        are registered to FAIL (HTTP 500) — the contract's short-page
        break must stop the drain, so the run succeeds on page 1 alone
        and never touches page 2."""
        vm, c = env
        txs = old_clean_wallet_txs(now_ts())
        # page 1 only, via the explicit helper
        mock_web_paginated_explicit(
            vm,
            tx_pages_bodies={1: json.dumps(ok_list(txs))},
            ttx_pages_bodies={1: json.dumps(ok_list([]))})
        # pages 2-3 txlist + page 2 tokentx: landmines — fetching any
        # of them fails the run (proves the drain stopped at page 1)
        for pat in ("module=account&action=txlist.*page=2&",
                    "module=account&action=txlist.*page=3&",
                    "module=account&action=tokentx.*page=2&"):
            vm.mock_web(pat, {"status": 500, "body": "down"})
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Aligned-Trustworthy"
        assert rec["history_coverage"] == "full_window"

    def test_wallet_over_100_under_300_now_full_window(self, env):
        """THE steward-requested coverage evidence: a wallet with
        100 < n_txs < 300 (previously truncated to a partial first-100
        sample) is now fully covered inside the 3-page window and must
        classify history_coverage='full_window'."""
        vm, c = env
        txs = window_wallet_txs(now_ts(), n=250)
        register_full_ok(vm, txs, page_aware=True)
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        assert rec["history_coverage"] == "full_window", rec
        assert "LIMITED DATA" not in rec["signal_a_reasoning"]
        # all 250 txs made it through pagination: 20 cycled counterparties
        # => metrics reflect the full window, not a 100-tx truncation
        assert "no conservative risk factors" in rec["signal_a_reasoning"]

    def test_wallet_exactly_300_is_partial_window(self, env):
        """300 txs = the window is FULL (3 x 100 pages, no short page):
        bounded sample, honestly classified partial_window."""
        vm, c = env
        register_full_ok(vm, busy_wallet_txs(now_ts(), 300), page_aware=True)
        mock_llm_direction(
            vm, "Trust",
            reasoning=("Based on this partial window the wallet shows "
                       "organic activity; the verdict rests on the "
                       "first-300-tx sample, not full history."))
        rec = reconcile(vm, c)
        assert rec["history_coverage"] == "partial_window"

    def test_wallet_over_300_still_partial_window(self, env):
        """A wallet with MORE than 300 txs (pages 1-3 all full, history
        continues beyond): still honestly partial_window."""
        vm, c = env
        register_full_ok(vm, busy_wallet_txs(now_ts(), 450), page_aware=True)
        mock_llm_direction(
            vm, "Trust",
            reasoning=("Based on this partial window the wallet shows "
                       "organic activity; the verdict rests on the "
                       "first-300-tx sample, not full history."))
        rec = reconcile(vm, c)
        assert rec["history_coverage"] == "partial_window"

    def test_fixed_page_plan_3_txlist_2_tokentx(self, env):
        """The fetch plan is FIXED (3 txlist pages), not adaptive: mock
        a wallet with EXACTLY 200 txs (pages 1-2 full, no short page)
        where page 3 is an HTTP-500 landmine. A drain-until-exhausted
        loop would stop after the full page 2 and succeed; the FIXED
        plan fetches page 3 too, hits the 500, and the run fails
        Undetermined naming txlist page=3 — proving leader and every
        validator execute the identical 3-page plan."""
        vm, c = env
        txs = busy_wallet_txs(now_ts(), 200)     # exactly 2 full pages
        mock_web_paginated_explicit(
            vm,
            tx_pages_bodies={
                1: json.dumps(ok_list(txs[:100])),
                2: json.dumps(ok_list(txs[100:])),
                3: json.dumps({"status": 500, "message": "Something went"
                               " wrong.", "result": None}),
            },
            ttx_pages_bodies={
                1: json.dumps(ok_list([])),
                2: json.dumps(empty_list()),
            })
        poison_llm(vm, "fixed-plan")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "txlist")
        # the failure must name the PAGE that failed — page 3 of the
        # fixed plan (and never pages 1-2, which served valid data)
        assert "page3" in rec["final_reasoning"], rec["final_reasoning"]

    def test_tokentx_fixed_plan_2_pages(self, env):
        """Same proof for the tokentx side: exactly 100 token transfers
        (page 1 full, no short page) with page 2 an HTTP-500 landmine —
        the FIXED 2-page plan fetches page 2, hits the 500, and the run
        fails Undetermined naming tokentx page=2."""
        vm, c = env
        ttxs = [tokentx(COUNTERPARTY_1, TARGET_WALLET.lower(),
                        now_ts() - 30 * 86400) for _ in range(100)]
        mock_web_paginated_explicit(
            vm,
            tx_pages_bodies={
                1: json.dumps(ok_list(old_clean_wallet_txs(now_ts()))),
                2: json.dumps(empty_list()),
                3: json.dumps(empty_list()),
            },
            ttx_pages_bodies={
                1: json.dumps(ok_list(ttxs)),
                2: json.dumps({"status": 500, "message": "Something went"
                               " wrong.", "result": None}),
            })
        poison_llm(vm, "ttx-fixed-plan")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "tokentx")
        assert "page2" in rec["final_reasoning"], rec["final_reasoning"]

    def test_empty_beyond_last_page_is_not_a_failure(self, env):
        """The real-world no-more-transactions response
        (status "0", result []) on a beyond-last page must NOT fail the
        run — it simply ends the history early (matches the live
        eth.blockscout behavior verified 2026-09-05)."""
        vm, c = env
        txs = window_wallet_txs(now_ts(), n=150)   # 1 full + 1 half page
        mock_web_paginated_explicit(
            vm,
            tx_pages_bodies={
                1: json.dumps(ok_list(txs[:100])),
                2: json.dumps(ok_list(txs[100:])),        # 50 txs, short
                3: json.dumps(empty_list()),              # status "0", []
            },
            ttx_pages_bodies={
                1: json.dumps(ok_list([])),
                2: json.dumps(empty_list()),
            })
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        assert rec["final_verdict"] == "Aligned-Trustworthy"
        assert rec["history_coverage"] == "full_window"

    def test_page2_http_failure_fails_safe(self, env):
        """A mid-window HTTP failure (page 2 of 3 serves a genuine
        HTTP 500, like the live base.blockscout incident) fails the
        WHOLE run Undetermined BEFORE any LLM judgment — pagination
        never weakens the fail-safe."""
        vm, c = env
        txs = busy_wallet_txs(now_ts(), 200)
        # pages 1 and 3 serve valid data via the explicit helper
        mock_web_paginated_explicit(
            vm,
            tx_pages_bodies={
                1: json.dumps(ok_list(txs[:100])),
                3: json.dumps(empty_list()),
            },
            ttx_pages_bodies={
                1: json.dumps(ok_list([])),
                2: json.dumps(empty_list()),
            })
        # page 2: genuine HTTP 500 (transport-level), the live S3 shape
        vm.mock_web("module=account&action=txlist.*page=2&",
                    {"status": 500, "body": "down"})
        poison_llm(vm, "page2-500")
        rec = reconcile(vm, c)
        assert_undetermined(rec, "txlist")
        assert "http_500" in rec["final_reasoning"]

    def test_data_sources_name_the_window(self, env):
        """The stored record's data_sources field explicitly documents
        the paginated window (3+2 pages of 100)."""
        vm, c = env
        register_full_ok(vm, old_clean_wallet_txs(now_ts()))
        mock_llm_direction(vm, "Trust")
        rec = reconcile(vm, c)
        assert "paginated history window: 3 txlist pages + 2 tokentx pages" \
            in rec["data_sources"]

    def test_window_constants_are_sane(self, env):
        """Static guard: the new window is exactly 300 native + 200
        token txs (3 x 100 + 2 x 100), strictly larger than the old
        first-100 window, and the page counts are the fixed plan."""
        src = Path("contracts/TrustReconciler.py").read_text()
        assert "TXLIST_PAGE_SIZE = 100" in src
        assert "TXLIST_PAGES = 3" in src
        assert "TOKENTX_PAGE_SIZE = 100" in src
        assert "TOKENTX_PAGES = 2" in src
        # the fetch call passes the page constants, not a raw "until
        # exhausted" loop bound from the response
        assert "_fetch_pages(\"txlist\", TXLIST_PAGE_SIZE," in src
        assert "_fetch_pages(\"tokentx\", TOKENTX_PAGE_SIZE," in src
        assert "MAX_TXS_FETCHED = TXLIST_PAGE_SIZE * TXLIST_PAGES" in src
        assert "MAX_TOKEN_TXS_FETCHED = TOKENTX_PAGE_SIZE * TOKENTX_PAGES" in src


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
        # 300 txs exactly = the full 3-page window: classified partial
        # (bounded sample), LIMITED DATA clauses present.
        register_full_ok(vm, busy_wallet_txs(now_ts(), 300), page_aware=True)
        mock_llm_direction(
            vm, "Trust",
            reasoning=("Based on this partial window the wallet shows "
                       "organic, non-flagged activity; the verdict rests "
                       "on the first-300-tx sample, not full history."))
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
        register_full_ok(vm, busy_wallet_txs(now_ts(), 300), page_aware=True)
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
