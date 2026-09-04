"""Frontend source regression tests — steward round-2 fixes.

These pin the two frontend behaviors the steward required, at the
source level, so they cannot silently regress:

1. The chain sent to request_reevaluation is DERIVED FROM THE STORED
   RECORD (re-read right before the tx), never from the UI selector
   state (currentChain / chain-select .value).
2. The success path requires a VERIFIED before/after record mutation
   (last_updated or reevaluation_count moved) after a FINALIZED tx —
   no optimistic success, and an explicit anomaly branch exists.

They run with plain pytest (no gltest/venv needed): parse the single
inline <script> of frontend/index.html.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "frontend" / "index.html"


def _app_js():
    html = HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline app script found in frontend/index.html"
    return blocks[-1]


JS = _app_js()


class TestStewardFixFrontendChainDerivation:
    """Fix 1: re-eval chain comes from the stored record, not the UI."""

    def test_reeval_args_use_derived_chain_not_ui_state(self):
        # the ONLY request_reevaluation call site must pass the chain
        # variable derived from the record read (recChain), not
        # currentChain (the selector state)
        calls = re.findall(
            r"sendWrite\(\s*['\"]request_reevaluation['\"]\s*,\s*\[([^\]]+)\]",
            JS,
        )
        assert calls, "request_reevaluation call site missing"
        assert len(calls) == 1, "more than one request_reevaluation call site"
        assert "recChain" in calls[0], (
            "re-eval args must derive the chain from the stored record "
            f"(recChain), got: {calls[0]}"
        )
        assert "currentChain" not in calls[0], (
            "re-eval must never pass the UI selector state as chain"
        )

    def test_recchain_is_read_from_chain_before_tx(self):
        # recChain must be computed from a fresh get_reconciliation read
        # performed inside doRecheck BEFORE the tx is sent
        m = re.search(
            r"async function doRecheck\(\)\{.*?(?=\$\('btn-reconcile'\))",
            JS, re.S,
        )
        assert m, "doRecheck function not found"
        body = m.group(0)
        before_read = re.search(
            r"const before = parseJsonSafe\(\s*await readContract\('get_reconciliation'",
            body,
        )
        assert before_read, (
            "doRecheck must read the stored record (before) via "
            "get_reconciliation before sending the re-eval tx"
        )
        assert body.index(before_read.group(0)) < body.index(
            "request_reevaluation"
        ), "the before-read must happen BEFORE the re-eval tx is sent"
        assert re.search(
            r"const recChain = String\(before\.chain", body
        ), "recChain must be derived from before.chain (the stored record)"

    def test_reeval_refuses_to_send_when_chain_missing(self):
        # no pinned chain on the record -> refuse instead of guessing
        m = re.search(r"async function doRecheck\(\)\{.*?(?=\$\('btn-reconcile'\))", JS, re.S)
        assert m and "refusing to send a re-evaluation rather than guess" in m.group(0)

    def test_no_reeval_bypass_around_derivation(self):
        # there must be NO other write path to request_reevaluation
        # (e.g. an onclick calling it directly with currentChain)
        assert JS.count("request_reevaluation'") + JS.count(
            'request_reevaluation"'
        ) <= 2, "unexpected extra request_reevaluation references"

    def test_view_button_syncs_selector_to_pinned_chain(self):
        # the read-only View stored record path syncs the selector to the
        # record's pinned chain so the UI never contradicts the record
        assert "btn-view" in HTML_READ
        assert re.search(
            r"\$\('chain-select'\)\.value = recRaw\.chain", JS
        ), "View stored record must sync the chain selector to the pinned chain"


HTML_READ = HTML.read_text(encoding="utf-8")


class TestStewardFixFrontendVerifiedSuccess:
    """Fix 2: success requires FINALIZED + verified record mutation."""

    def test_sendwrite_waits_finalized_not_accepted(self):
        assert "status: SDK.TransactionStatus.FINALIZED" in JS, (
            "sendWrite must wait for FINALIZED, not ACCEPTED"
        )
        assert "status: SDK.TransactionStatus.ACCEPTED" not in JS, (
            "no code path may settle for ACCEPTED as the success criterion"
        )

    def test_success_requires_verified_mutation(self):
        # after the tx, the record is re-read and both
        # reevaluation_count and last_updated are compared to the
        # before-snapshot; success only when one moved
        m = re.search(r"async function doRecheck\(\)\{.*?(?=\$\('btn-reconcile'\))", JS, re.S)
        assert m, "doRecheck function not found"
        body = m.group(0)
        assert "reevaluation_count) !== Number(before.reevaluation_count" in body
        assert "last_updated) !== Number(before.last_updated" in body
        assert "verifying the stored record actually changed" in body

    def test_anomaly_branch_exists_and_shows_not_success(self):
        m = re.search(r"async function doRecheck\(\)\{.*?(?=\$\('btn-reconcile'\))", JS, re.S)
        assert m, "doRecheck function not found"
        body = m.group(0)
        assert "did NOT change" in body, "explicit anomaly branch missing"
        assert "not treating this as success" in body
        # the anomaly branch must NOT call the success toast
        anomaly = body[body.index("if (!mutated){"):body.index("return;\n    }\n    renderRecord(after")]
        assert "Re-evaluation committed" not in anomaly

    def test_no_optimistic_state_probe(self):
        # the old optimistic probe (return true) is gone entirely
        assert "stateProbe" not in JS
        assert "return true; } catch { return false; } }" not in JS

    def test_recovery_path_polls_receipt_until_finalized(self):
        # timeout recovery polls the actual receipt to FINALIZED — it never
        # fabricates success from a state probe
        assert "TX_FINAL_WAIT_TIMEOUT" in JS
        assert "polling the tx until FINALIZED" in JS
        assert "TERMINAL_NOT_FINAL" in JS
        # recovery must not produce success: caller still verifies record
        m = re.search(r"TX_FINAL_WAIT_TIMEOUT.*?return \{ txHash, receipt \}", JS, re.S)
        assert m, "recovery loop must end by returning the receipt for caller-side verification"


class TestStewardFixFrontendMisc:
    def test_html_parses_and_buttons_exist(self):
        for btn in ("btn-reconcile", "btn-recheck", "btn-view", "chain-select"):
            assert f'id="{btn}"' in HTML_READ, f"missing element #{btn}"

    def test_file_still_carries_contract_address(self):
        assert "0x3c639D84c6B1463eaFE91EA0A2Db8d767e742c2B" in HTML_READ
