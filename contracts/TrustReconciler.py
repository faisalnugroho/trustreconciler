# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
import json

"""
TrustReconciler — GenLayer Intelligent Contract.

Problem: on the Etherscan wallet page, third-party reputation providers
(Trust Score / AML Risk / zScore "Cards") frequently CONTRADICT each other
for the same wallet on the same page (real motivating case: Wallet Trust
Score 100/100 from provider A alongside AML Risk Score MEDIUM from provider
B). Users are left guessing which one to trust.

Solution: compute TWO independent heuristic signals with OPPOSING
philosophies from the same on-chain wallet data, then let GenLayer LLM
consensus ARBITRATE: compare the signals, explain the root cause when they
diverge (not just average them), and issue a final verdict + confidence +
auditable reasoning.

  Signal A — Risk-Conservative: absence of positive evidence = risk.
  Signal B — Trust-Optimistic:  absence of negative evidence = trust.

Honest data sourcing (also stated in README + SUBMISSION_DRAFT):
both signals are OUR OWN heuristics computed from FREE, keyless, public
chain-data APIs (Blockscout's Etherscan-compatible endpoints — same
txlist/tokentx/balance modules and response shape as the Etherscan API)
plus the public forta-network labelled-datasets Fake_Phishing address set,
synced periodically into this repo (data/phishing_labels.json). They are
NOT passthroughs of any commercial reputation provider's API, and no API
key is used — or usable — anywhere in the contract (an on-chain key would
be public, so the contract only ever calls keyless public endpoints).

Design hard lessons encoded (from prior submissions):

  1. FAIL-SAFE UNDETERMINED EVERYWHERE — request AND re-evaluation run the
     SAME single pipeline (_run_pipeline); any fetch failure (non-200,
     empty body, malformed JSON, missing fields) stops the run BEFORE any
     LLM judgment and stores final_verdict="Undetermined" with the failed
     fetch named in the reasoning. There is no second code path, so no
     path can skip the fail-safe. (Lesson: WarrantyClaimOracle rejection —
     its appeal path could still override the fail-safe.)
  2. SINGLE VALIDATION PATH — every external fetch goes through the same
     _get_json helper (PRBountyEscrow pattern); no special-case shortcut.
  3. DETERMINISTIC SIGNALS, LLM ONLY ARBITRATES — the two signal scores
     are pure functions of the fetched data, computed identically by
     leader and every validator. The LLM never computes scores; it only
     resolves the FINAL DIRECTION (Trust vs Risk) — a genuine judgment
     call exactly when the two philosophies conflict — plus confidence
     and human-readable reasoning. The canonical verdict string is
     derived deterministically from (direction, divergence), so it can
     never be malformed.
  4. PARTIAL-FIELD EQUIVALENCE — validators independently re-fetch and
     re-compute, then compare ONLY the structured decision fields
     (final_verdict, divergence_detected, signal scores). Free-text
     reasoning and confidence are deliberately NOT compared — they
     naturally differ between LLM runs.
  5. RE-EVALUATION COOLDOWN — a per-wallet cooldown guards against
     spam/griefing; enforced identically on both entrypoints.
  6. NO EVENT API in GenVM v0.2.16 — the stored record IS the on-chain
     receipt (readable via get_reconciliation; the GenLayer explorer
     shows every state change). Documented in README.

Storage: a single TreeMap[str, ReconciliationRecord] keyed by lowercase
wallet address (direct-mode homogeneous-TreeMap workaround, proven in
SecondHandCarInspectionEscrow — 60/60 tests).
"""


# ---------------------------------------------------------------------------
# Parameters (each documented with its rationale; see README "Parameters")
# ---------------------------------------------------------------------------
REEVAL_COOLDOWN_SECONDS = 3600
# 1 hour between runs for the same wallet. Rationale: the public APIs used
# are rate-limited; an hour fully stops spam/griefing loops while still
# allowing genuinely new activity to be re-checked the same day.

DIVERGENCE_THRESHOLD_POINTS = 30
# divergence_detected = |risk_A - (100 - trust_B)| > 30. Rationale: on a
# 0-100 scale a 30-point gap is where two scoring philosophies stop being
# a rounding difference and become materially different conclusions about
# the same wallet — mirroring the motivating Trust-100 vs AML-MEDIUM case.

MAX_TXS_FETCHED = 100       # fixed page size keeps leader/validator
MAX_TOKEN_TXS_FETCHED = 100  # fetches comparable and the payload bounded

WALLET_YOUNG_DAYS = 30      # Signal A: < 30 days = "insufficient history"
BURST_WINDOW_SECONDS = 3600
BURST_THRESHOLD_TXS = 10    # Signal A: >=10 txs within 1h = bot/farming
LOW_DIVERSITY_RATIO = 30    # Signal A: unique/total < 30% = pass-through
HIGH_FAIL_RATIO = 30        # Signal A: >30% failed txs = erratic
MIN_UNIQUE_COUNTERPARTIES = 10  # Signal B: diversity earns trust

SUPPORTED_CHAINS = ("eth", "base")

ALLOWED_DIRECTIONS = ("Trust", "Risk")
ALLOWED_CONFIDENCE = ("Low", "Medium", "High")

# Blockscout-hosted Etherscan-compatible API bases — keyless, public,
# verified live (2026-09-03): identical status/message/result shape to
# the Etherscan module endpoints (module=account action=txlist|tokentx|
# balance). Chosen over api.etherscan.io because the contract executes
# on-chain, where ANY apikey would be publicly readable (= burned key);
# keyless public endpoints keep every validator's evidence independent
# and verifiable. Fai's Etherscan key is used only OFF-chain (dataset
# cross-checks, smoke-test verification), never in the contract.
BLOCKSCOUT_BASES = {
    "eth": "https://eth.blockscout.com/api",
    "base": "https://base.blockscout.com/api",
}

# Our own repo-hosted snapshot of the public forta-network
# labelled-datasets Fake_Phishing address set (synced periodically by
# scripts/sync_dataset.py — ChainSeus pattern: manual periodic sync into
# our own storage, never a live upstream fetch per request). Every
# validator fetches the same immutable-per-commit public URL.
DATASET_URL = ("https://raw.githubusercontent.com/faisalnugroho/"
               "trustreconciler/main/data/phishing_labels.json")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
@allow_storage
@dataclass
class ReconciliationRecord:
    """One reconciliation run for one wallet address."""
    wallet_address: str          # checksummed input address
    chain: str                   # "eth" | "base"
    requester: Address
    requested_at: bigint         # epoch seconds
    signal_a_score: bigint       # 0-100 risk (higher = riskier); -1 if not computed
    signal_a_reasoning: str
    signal_b_score: bigint       # 0-100 trust (higher = more trusted); -1 if not computed
    signal_b_reasoning: str
    divergence_detected: bool
    final_verdict: str           # see ALLOWED_VERDICTS in README
    confidence: str              # "Low" | "Medium" | "High"
    final_reasoning: str
    data_sources: str            # human-readable provenance of this run
    last_updated: bigint         # epoch seconds
    reevaluation_count: bigint


# ---------------------------------------------------------------------------
# Pure helpers (deterministic — identical for leader and validators)
# ---------------------------------------------------------------------------
def _parse_iso_epoch(iso: str) -> int:
    """Parse 'YYYY-MM-DDTHH:MM:SS(.ffffff)?(Z|+00:00)?' to epoch seconds
    with pure integer math (Howard Hinnant _days_from_civil). No datetime
    module, no floats. (Proven pattern, SecondHandCarInspectionEscrow.)"""
    try:
        date_part = iso.split("T")[0]
        y = int(date_part[0:4])
        m = int(date_part[5:7])
        d = int(date_part[8:10])
        time_part = iso.split("T")[1]
        hh = int(time_part[0:2])
        mm = int(time_part[3:5])
        ss = int(time_part[6:8])
    except Exception:
        return 0
    yy = y
    if m <= 2:
        yy -= 1
    era = int(yy / 400) if yy >= 0 else -int((-yy + 399) / 400)
    yoe = yy - era * 400
    mp = (m + 9) % 12
    doy = int((153 * mp + 2) / 5) + d - 1
    doe = yoe * 365 + int(yoe / 4) - int(yoe / 100) + doy
    days = era * 146097 + doe - 719468
    return days * 86400 + hh * 3600 + mm * 60 + ss


_HEX_ALPHABET = "0123456789abcdefABCDEF"


def _is_hex_address(raw) -> bool:
    if not isinstance(raw, str):
        return False
    if len(raw) != 42 or not raw.startswith("0x"):
        return False
    for ch in raw[2:]:
        if ch not in _HEX_ALPHABET:
            return False
    return True


def _is_checksummed_address(raw: str) -> bool:
    """EIP-55 checksum validation using Keccak256 (available inside the
    GenVM via genlayer.py.keccak — verified live in direct mode 2026-09)."""
    if not _is_hex_address(raw):
        return False
    body = raw[2:]
    if not any(c in "abcdefABCDEF" for c in body):
        # purely numeric body: no letters to case-check; matches the
        # EIP-55 degenerate case (eth_utils behavior for 0x000...001).
        return True
    from genlayer.py.keccak import Keccak256
    lowered = body.lower()
    digest = Keccak256(lowered.encode("ascii")).hexdigest()
    check = "0x"
    for i in range(40):
        c = lowered[i]
        if c in "0123456789" or int(digest[i], 16) < 8:
            check += c
        else:
            check += c.upper()
    return check == raw


def _record_to_dict(r: ReconciliationRecord) -> dict:
    return {
        "wallet_address": r.wallet_address,
        "chain": r.chain,
        "requester": str(r.requester),
        "requested_at": r.requested_at,
        "signal_a_score": r.signal_a_score,
        "signal_a_reasoning": r.signal_a_reasoning,
        "signal_b_score": r.signal_b_score,
        "signal_b_reasoning": r.signal_b_reasoning,
        "divergence_detected": r.divergence_detected,
        "final_verdict": r.final_verdict,
        "confidence": r.confidence,
        "final_reasoning": r.final_reasoning,
        "data_sources": r.data_sources,
        "last_updated": r.last_updated,
        "reevaluation_count": r.reevaluation_count,
    }


def _undetermined_record(wallet_address: str, chain: str,
                          requester: Address, now: int,
                          failures) -> ReconciliationRecord:
    """Fail-safe record used for ANY data-acquisition failure. The only
    Undetermined construction path — both entrypoints share it, so the
    fail-safe cannot be bypassed on any path (first run or re-eval)."""
    reason_txt = "; ".join(str(f) for f in failures) if failures \
        else "unknown_data_failure"
    return ReconciliationRecord(
        wallet_address=wallet_address,
        chain=chain,
        requester=requester,
        requested_at=now,
        signal_a_score=-1,
        signal_a_reasoning="not_computed_data_unavailable",
        signal_b_score=-1,
        signal_b_reasoning="not_computed_data_unavailable",
        divergence_detected=False,
        final_verdict="Undetermined",
        confidence="Low",
        final_reasoning=(
            "Fail-safe: reconciliation stopped before any LLM judgment "
            "because required data could not be retrieved: " + reason_txt +
            ". This verdict asserts nothing about the wallet's trust or "
            "risk; re-run once the data source recovers."),
        data_sources="data_unavailable",
        last_updated=now,
        reevaluation_count=0,
    )


def _extract_counterparties(wallet_lower: str, txs, ttxs):
    """Single pass over fetched txs -> (counterparties, funding_sources,
    timestamps, failed_count). Pure function of fetched data."""
    counterparties = set()
    funding_sources = set()
    timestamps = []
    failed = 0
    for t in txs:
        try:
            frm = str(t.get("from") or "").lower()
            to = str(t.get("to") or "").lower()
            ts = int(t.get("timeStamp", 0))
            err = str(t.get("isError", "0"))
        except Exception:
            continue
        if ts > 0:
            timestamps.append(ts)
        if frm and frm != wallet_lower:
            counterparties.add(frm)
            if to == wallet_lower:
                funding_sources.add(frm)
        if to and to != wallet_lower:
            counterparties.add(to)
        if err == "1":
            failed += 1
    for t in ttxs:
        try:
            frm = str(t.get("from") or "").lower()
            to = str(t.get("to") or "").lower()
        except Exception:
            continue
        if frm and frm != wallet_lower:
            counterparties.add(frm)
        if to and to != wallet_lower:
            counterparties.add(to)
    return counterparties, funding_sources, timestamps, failed


def _detect_burst(timestamps) -> int:
    """Two-pointer scan: max txs inside any BURST_WINDOW_SECONDS window.
    Returns the max window count (0 if fewer than the threshold)."""
    if len(timestamps) < BURST_THRESHOLD_TXS:
        return 0
    timestamps.sort()
    n = len(timestamps)
    best = 0
    i = 0
    j = 0
    while i < n:
        if j < i:
            j = i
        while j < n and timestamps[j] - timestamps[i] < BURST_WINDOW_SECONDS:
            j += 1
        window = j - i
        if window > best:
            best = window
        i += 1
        if best >= n:
            break
    return best


def _compute_metrics(wallet_address: str, chain: str, txs, ttxs,
                     balance_wei: int, now: int, flagged_set):
    """Deterministic metrics + both signal scores. Pure function of the
    fetched data — leader and every validator run this identically."""
    wl = wallet_address.lower()
    counterparties, funding_sources, timestamps, failed = \
        _extract_counterparties(wl, txs, ttxs)
    n_txs = len(txs)
    n_ttxs = len(ttxs)
    unique_cp = len(counterparties)
    total_interactions = n_txs + n_ttxs

    diversity_pct = 0
    if total_interactions > 0:
        diversity_pct = int(unique_cp * 100 / total_interactions)

    first_ts = min(timestamps) if timestamps else 0
    wallet_age_days = 0
    if first_ts > 0 and now >= first_ts:
        wallet_age_days = int((now - first_ts) / 86400)

    burst_window_txs = _detect_burst(list(timestamps))

    fail_pct = 0
    if n_txs > 0:
        fail_pct = int(failed * 100 / n_txs)

    flagged_cp = len(counterparties & flagged_set)
    flagged_funding = len(funding_sources & flagged_set)

    metrics = {
        "chain": chain,
        "native_txs": n_txs,
        "token_txs": n_ttxs,
        "unique_counterparties": unique_cp,
        "counterparty_diversity_pct": diversity_pct,
        "wallet_age_days": wallet_age_days,
        "failed_tx_pct": fail_pct,
        "max_txs_in_burst_window": burst_window_txs,
        "native_balance_wei": balance_wei,
        "flagged_counterparty_hits": flagged_cp,
        "flagged_funding_hits": flagged_funding,
    }

    # --- Signal A — Risk-Conservative: no positive evidence = risk ------
    a_reasons = []
    risk = 20  # conservative prior: unproven != clean
    if flagged_funding > 0:
        risk = 95
        a_reasons.append("funding source is phishing-flagged (Fake_Phishing "
                         "label set): " + str(flagged_funding) + " hit(s)")
    if flagged_cp > 0:
        risk = max(risk, 85)
        a_reasons.append("interacted with phishing-flagged address(es): "
                         + str(flagged_cp) + " hit(s)")
    if n_txs + n_ttxs == 0:
        risk = max(risk, 45)
        a_reasons.append("no transaction history to evaluate")
    if 0 < wallet_age_days < WALLET_YOUNG_DAYS:
        risk = max(risk, 55)
        a_reasons.append("wallet age " + str(wallet_age_days) + " days < "
                         + str(WALLET_YOUNG_DAYS)
                         + " — insufficient history")
    elif wallet_age_days == 0 and n_txs + n_ttxs > 0:
        risk = max(risk, 55)
        a_reasons.append("wallet younger than 1 day — insufficient history")
    if burst_window_txs >= BURST_THRESHOLD_TXS:
        risk = max(risk, 60)
        a_reasons.append("burst pattern: " + str(burst_window_txs)
                         + " txs within " + str(BURST_WINDOW_SECONDS) + "s")
    if total_interactions > 0 and diversity_pct < LOW_DIVERSITY_RATIO:
        risk = max(risk, 50)
        a_reasons.append("low counterparty diversity: " + str(unique_cp)
                         + " unique of " + str(total_interactions)
                         + " interactions — pass-through pattern")
    if fail_pct > HIGH_FAIL_RATIO:
        risk = max(risk, 45)
        a_reasons.append("erratic history: " + str(fail_pct)
                         + "% failed txs")
    if not a_reasons:
        risk = 15
        a_reasons.append("no conservative risk factors found: established "
                         "age, organic diversity, no flagged contact")
    if risk > 100:
        risk = 100
    signal_a = {"score": risk, "reasoning": "; ".join(a_reasons)}

    # --- Signal B — Trust-Optimistic: no negative evidence = trust ------
    b_reasons = []
    if flagged_cp > 0:
        trust = 5
        b_reasons.append("interaction with phishing-flagged address(es): "
                         "negative evidence overrides optimism")
    elif n_txs + n_ttxs == 0:
        trust = 50
        b_reasons.append("no history: no negative evidence, but nothing "
                         "positive to trust either")
    else:
        trust = 80  # clean slate: absence of negative evidence = HIGH trust
        b_reasons.append("zero contact with phishing-flagged addresses "
                         "across all " + str(unique_cp) + " counterparties")
        if unique_cp >= MIN_UNIQUE_COUNTERPARTIES:
            trust = max(trust, 80)
            b_reasons.append("high counterparty diversity: " + str(unique_cp)
                            + " unique addresses")
        if n_txs > 0 and fail_pct <= 10:
            trust = max(trust, 75)
            b_reasons.append("consistent history: only " + str(fail_pct)
                             + "% failed txs")
        if trust >= 80 and len(b_reasons) >= 3:
            trust = min(100, trust + 5)
    if trust > 100:
        trust = 100
    signal_b = {"score": trust, "reasoning": "; ".join(b_reasons)}

    gap = abs(risk - (100 - trust))
    divergence = gap > DIVERGENCE_THRESHOLD_POINTS
    metrics["model_gap_points"] = gap
    return metrics, signal_a, signal_b, divergence


def _build_arbitration_prompt(wallet_address: str, chain: str, metrics,
                              signal_a, signal_b, divergence) -> str:
    gap = metrics["model_gap_points"]
    if divergence:
        relation = ("The two models DIVERGE by " + str(gap) + " points "
                    "(beyond the " + str(DIVERGENCE_THRESHOLD_POINTS)
                    + "-point divergence threshold).")
    else:
        relation = ("The two models AGREE within " + str(gap)
                    + " points (inside the "
                    + str(DIVERGENCE_THRESHOLD_POINTS)
                    + "-point divergence threshold).")
    return (
        "You are the neutral arbiter of two wallet-trust analysis models "
        "with opposing philosophies, running as a GenLayer consensus "
        "judgment.\n\n"
        "WALLET: " + wallet_address + "  (chain: " + chain + ")\n\n"
        "MODEL A — RISK-CONSERVATIVE (treats absence of positive evidence "
        "as risk):\n"
        "  risk_score: " + str(signal_a["score"]) + "/100 (higher = riskier)\n"
        "  findings: " + signal_a["reasoning"] + "\n\n"
        "MODEL B — TRUST-OPTIMISTIC (treats absence of negative evidence "
        "as trust):\n"
        "  trust_score: " + str(signal_b["score"]) + "/100 (higher = more trusted)\n"
        "  findings: " + signal_b["reasoning"] + "\n\n"
        "MEASURED WALLET FACTS (computed deterministically from on-chain "
        "data — the same facts both models used):\n"
        + json.dumps(metrics, sort_keys=True) + "\n\n"
        + relation + "\n\n"
        "YOUR TASK — decide the FINAL trust posture for this wallet:\n"
        "1. final_direction: \"Trust\" or \"Risk\".\n"
        "2. If the models diverge, your reasoning MUST identify the root "
        "cause of the divergence — WHICH measured fact drives the two "
        "philosophies apart — and justify which philosophy fits this "
        "wallet's actual evidence better. Do NOT simply average the two "
        "scores; resolve the conflict.\n\n"
        "DECISION CRITERIA (apply strictly):\n"
        "- ANY phishing-flagged contact (flagged_counterparty_hits or "
        "flagged_funding_hits > 0) mandates \"Risk\".\n"
        "- Resolve \"Trust\" only when the measured evidence genuinely "
        "supports it (no flagged contact and organic activity patterns).\n"
        "- A young-but-clean wallet is the intentionally hard case: Model "
        "A reads insufficient history as risk, Model B reads absence of "
        "negative evidence as trust. Judge THIS wallet's actual activity.\n\n"
        "Return ONLY valid JSON with exactly these keys:\n"
        "{\"final_direction\": \"Trust\" or \"Risk\",\n"
        " \"confidence\": \"Low\" or \"Medium\" or \"High\",\n"
        " \"final_reasoning\": \"one concise paragraph; if the models "
        "diverge it must name the root cause of the divergence\"}"
    )


def _validate_arbiter_output(parsed, divergence: bool, metrics):
    """Structural + hard-gate validation of the LLM arbitration. Returns
    (ok, payload_dict_or_error_string). The final verdict STRING is derived
    deterministically from (direction, divergence) — the LLM can never
    emit a malformed or out-of-family verdict."""
    if not isinstance(parsed, dict):
        return False, "not_a_dict"
    direction = parsed.get("final_direction")
    if direction not in ALLOWED_DIRECTIONS:
        return False, "invalid_direction"
    confidence = parsed.get("confidence")
    if confidence not in ALLOWED_CONFIDENCE:
        return False, "invalid_confidence"
    reasoning = str(parsed.get("final_reasoning") or "").strip()
    # On divergence the 60-char root-cause requirement SUBSUMES the
    # general 20-char minimum, so the specific error wins (a divergent
    # verdict without root-cause reasoning is the more precise diagnosis).
    if divergence and len(reasoning) < 60:
        return False, "divergence_root_cause_missing"
    if len(reasoning) < 20:
        return False, "reasoning_too_short"
    # HARD GATE: flagged contact mandates Risk — the LLM may not override
    # negative evidence with optimism (validated identically by every
    # validator, so this is consensus-stable).
    if (direction == "Trust"
            and (metrics.get("flagged_counterparty_hits", 0) > 0
                 or metrics.get("flagged_funding_hits", 0) > 0)):
        return False, "flagged_contact_requires_risk"
    verdict = ((("Divergent-Resolved-" + direction)
                if divergence
                else ("Aligned-Trustworthy" if direction == "Trust"
                      else "Aligned-Risky")))
    return True, {
        "final_verdict": verdict,
        "confidence": str(confidence),
        "final_reasoning": reasoning[:900],
    }


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class TrustReconciler(gl.Contract):
    """Two independent trust/risk signals + GenLayer LLM consensus arbiter."""

    reconciliations: TreeMap[str, ReconciliationRecord]

    def __init__(self):
        self.reconciliations = TreeMap()

    # ------------------------------------------------------------------ view
    @gl.public.view
    def get_reconciliation(self, wallet_address: str) -> str:
        """Read one reconciliation record (JSON). '{}' if none exists."""
        key = wallet_address.strip().lower()
        if key not in self.reconciliations:
            return json.dumps({})
        return json.dumps(_record_to_dict(self.reconciliations[key]))

    @gl.public.view
    def get_cooldown_info(self, wallet_address: str) -> str:
        """Pure-storage cooldown info for frontends: the cooldown constant
        and the wallet's last_updated. The REMAINING time is computed
        client-side from wall clock (a view call has no trustworthy
        transaction datetime; the write path enforces the real guard)."""
        key = wallet_address.strip().lower()
        if key not in self.reconciliations:
            return json.dumps({"wallet_known": False,
                               "cooldown_seconds": REEVAL_COOLDOWN_SECONDS,
                               "last_updated": 0})
        r = self.reconciliations[key]
        return json.dumps({"wallet_known": True,
                          "cooldown_seconds": REEVAL_COOLDOWN_SECONDS,
                          "last_updated": r.last_updated})

    # ------------------------------------------------------------ entrypoints
    @gl.public.write
    def request_reconciliation(self, wallet_address: str, chain: str) -> str:
        """Permissionless first (or renewed) reconciliation request.
        Cheap input validation happens BEFORE the non-deterministic block
        so clearly-invalid input wastes no consensus gas. Returns the
        stored record as JSON."""
        chain = (chain or "").strip().lower()
        if chain not in SUPPORTED_CHAINS:
            raise AssertionError("unsupported_chain:" + chain)
        if not _is_hex_address(wallet_address):
            raise AssertionError("invalid_address_format")
        if not _is_checksummed_address(wallet_address):
            raise AssertionError("invalid_address_checksum")

        key = wallet_address.strip().lower()
        now = _parse_iso_epoch(gl.message_raw["datetime"])
        requester = gl.message.sender_address

        # Cooldown guard applies to BOTH entrypoints identically — a fresh
        # request for a recently-reconciled wallet is the same griefing
        # vector as a re-evaluation, so it gets the same guard.
        if key in self.reconciliations:
            elapsed = now - self.reconciliations[key].last_updated
            if elapsed < REEVAL_COOLDOWN_SECONDS:
                raise AssertionError("cooldown_active:"
                                   + str(REEVAL_COOLDOWN_SECONDS - elapsed))

        record = self._run_pipeline(wallet_address, chain, requester, now)
        # Cumulative pipeline-run counter across BOTH entrypoints (a
        # re-request after cooldown is functionally a re-evaluation too).
        prev_count = 0
        if key in self.reconciliations:
            prev_count = self.reconciliations[key].reevaluation_count
        record.reevaluation_count = prev_count + 1
        self.reconciliations[key] = record
        return json.dumps(_record_to_dict(record))

    @gl.public.write
    def request_reevaluation(self, wallet_address: str, chain: str) -> str:
        """Re-run the pipeline for an already-reconciled wallet. Runs the
        exact SAME pipeline as request_reconciliation — including the
        Undetermined fail-safe (WarrantyClaimOracle lesson: no appeal or
        retry path may ever bypass it)."""
        chain = (chain or "").strip().lower()
        if chain not in SUPPORTED_CHAINS:
            raise AssertionError("unsupported_chain:" + chain)
        if not _is_hex_address(wallet_address):
            raise AssertionError("invalid_address_format")
        if not _is_checksummed_address(wallet_address):
            raise AssertionError("invalid_address_checksum")

        key = wallet_address.strip().lower()
        now = _parse_iso_epoch(gl.message_raw["datetime"])
        requester = gl.message.sender_address

        if key not in self.reconciliations:
            raise AssertionError("no_prior_reconciliation")
        prev = self.reconciliations[key]
        elapsed = now - prev.last_updated
        if elapsed < REEVAL_COOLDOWN_SECONDS:
            raise AssertionError("cooldown_active:"
                               + str(REEVAL_COOLDOWN_SECONDS - elapsed))

        record = self._run_pipeline(wallet_address, chain, requester, now)
        record.reevaluation_count = prev.reevaluation_count + 1
        self.reconciliations[key] = record
        return json.dumps(_record_to_dict(record))

    # ------------------------------------------------------------- pipeline
    def _run_pipeline(self, wallet_address: str, chain: str,
                      requester: Address, now: int) -> ReconciliationRecord:
        """THE single reconciliation pipeline. Both entrypoints call
        exactly this; every data failure inside funnels to the same
        Undetermined record, so no path can skip the fail-safe."""

        def leader_fn():
            base = BLOCKSCOUT_BASES[chain]
            failures = []

            def _get_json(url):
                # SINGLE validation path for ALL external fetches
                # (PRBountyEscrow pattern): non-200, empty body, malformed
                # JSON, or a non-dict payload all raise so the caller
                # records the failure and the run fails safe.
                resp = gl.nondet.web.get(url)
                if resp.status != 200:
                    raise AssertionError("http_" + str(resp.status))
                if resp.body is None:
                    raise AssertionError("empty_body")
                data = json.loads(resp.body.decode("utf-8", errors="replace"))
                if not isinstance(data, dict) or "status" not in data:
                    raise AssertionError("malformed_payload")
                return data

            def _fetch_list(action: str, offset: int, what: str):
                d = _get_json(base + "?module=account&action=" + action
                             + "&address=" + wallet_address
                             + "&page=1&offset=" + str(offset)
                             + "&sort=asc")
                if str(d.get("status")) != "1" and d.get("result") != []:
                    raise AssertionError(what + "_status_"
                                       + str(d.get("status")))
                result = d.get("result")
                if not isinstance(result, list):
                    raise AssertionError(what + "_result_not_list")
                for item in result:
                    if not isinstance(item, dict):
                        raise AssertionError(what + "_malformed_entry")
                return result

            # Step 1-3: txlist / tokentx / balance (Blockscout
            # Etherscan-compatible, keyless — see module docstring).
            txs = None
            try:
                txs = _fetch_list("txlist", MAX_TXS_FETCHED, "txlist")
            except Exception as e:
                failures.append("txlist:" + repr(e)[:60])
            ttxs = None
            try:
                ttxs = _fetch_list("tokentx", MAX_TOKEN_TXS_FETCHED,
                                   "tokentx")
            except Exception as e:
                failures.append("tokentx:" + repr(e)[:60])
            balance_wei = None
            try:
                d = _get_json(base + "?module=account&action=balance"
                              + "&address=" + wallet_address)
                if str(d.get("status")) != "1":
                    raise AssertionError("balance_status_"
                                       + str(d.get("status")))
                if not isinstance(d.get("result"), str):
                    raise AssertionError("balance_result_not_str")
                balance_wei = int(d.get("result"))
            except Exception as e:
                failures.append("balance:" + repr(e)[:60])

            def _get_raw_json(url):
                # HTTP-level validation only (status 200 + JSON dict).
                # Payload-shape validation is the CALLER's job — the
                # Etherscan-compatible endpoints use _get_json above,
                # while our own JSON dataset has a different schema.
                resp = gl.nondet.web.get(url)
                if resp.status != 200:
                    raise AssertionError("http_" + str(resp.status))
                if resp.body is None:
                    raise AssertionError("empty_body")
                data = json.loads(resp.body.decode("utf-8", errors="replace"))
                if not isinstance(data, dict):
                    raise AssertionError("malformed_payload")
                return data

            # Step 4: phishing label dataset (our repo-hosted snapshot of
            # the public forta Fake_Phishing set). Empty, unparseable, or
            # badly-shaped dataset = explicit failure -> Undetermined
            # (test scenario: never silently skip the flag check).
            flagged_set = None
            try:
                d = _get_raw_json(DATASET_URL)
                addresses = d.get("addresses")
                if not isinstance(addresses, list) or len(addresses) == 0:
                    raise AssertionError("dataset_empty")
                lowered = set()
                for a in addresses:
                    if not isinstance(a, str) or not a.startswith("0x") \
                            or len(a) != 42:
                        raise AssertionError("dataset_bad_entry")
                    lowered.add(a.lower())
                flagged_set = lowered
            except Exception as e:
                failures.append("dataset:" + repr(e)[:60])

            # Step 5: ANY data failure => stop BEFORE the LLM. Partial
            # data must never reach arbitration.
            if txs is None or ttxs is None or balance_wei is None \
                    or flagged_set is None:
                return {"undetermined": True, "failures": failures}

            # Step 6: deterministic signals (identical for every validator)
            metrics, signal_a, signal_b, divergence = _compute_metrics(
                wallet_address, chain, txs, ttxs, balance_wei, now,
                flagged_set)

            # Step 7: LLM arbitration over complete data.
            prompt = _build_arbitration_prompt(
                wallet_address, chain, metrics, signal_a, signal_b,
                divergence)
            try:
                raw = gl.nondet.exec_prompt(prompt, response_format="json")
            except Exception as e:
                return {"undetermined": True,
                        "failures": ["llm:" + repr(e)[:60]]}
            parsed = raw
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except Exception:
                    return {"undetermined": True,
                            "failures": ["llm:malformed_json"]}
            ok, payload = _validate_arbiter_output(parsed, divergence,
                                                   metrics)
            if not ok:
                return {"undetermined": True,
                        "failures": ["llm:" + str(payload)]}

            return {
                "undetermined": False,
                "failures": [],
                "metrics": metrics,
                "signal_a": signal_a,
                "signal_b": signal_b,
                "divergence_detected": divergence,
                "final_verdict": payload["final_verdict"],
                "confidence": payload["confidence"],
                "final_reasoning": payload["final_reasoning"],
            }

        def validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            try:
                mine = leader_fn()
            except Exception:
                return False
            ld = leader_res.calldata
            # Equivalence — PARTIAL FIELD MATCHING (pattern 1): compare
            # only the structured decision fields. The signal scores and
            # the divergence flag are deterministic functions of the
            # fetched public data; the final verdict embeds the one
            # genuine LLM judgment (direction). Free-text reasoning and
            # confidence are deliberately NOT compared — independent LLM
            # runs always word them differently. Undetermined must agree
            # as a flag (the specific failure text is the leader's report
            # and is not consensus-critical).
            if ld.get("undetermined"):
                return bool(mine.get("undetermined"))
            if mine.get("undetermined"):
                return False
            return (str(ld.get("final_verdict"))
                    == str(mine.get("final_verdict"))
                    and bool(ld.get("divergence_detected"))
                    == bool(mine.get("divergence_detected"))
                    and int(ld.get("signal_a", {}).get("score", -1))
                    == int(mine.get("signal_a", {}).get("score", -1))
                    and int(ld.get("signal_b", {}).get("score", -1))
                    == int(mine.get("signal_b", {}).get("score", -1)))

        result = gl.vm.run_nondet(leader_fn, validator_fn)

        if result.get("undetermined"):
            return _undetermined_record(
                wallet_address, chain, requester, now,
                result.get("failures", []))

        data_sources = ("blockscout-" + chain
                        + " (module=account: txlist, tokentx, balance; "
                        "Etherscan-compatible keyless public API); "
                        "forta-network labelled-datasets Fake_Phishing "
                        "snapshot: " + DATASET_URL)
        return ReconciliationRecord(
            wallet_address=wallet_address,
            chain=chain,
            requester=requester,
            requested_at=now,
            signal_a_score=int(result["signal_a"]["score"]),
            signal_a_reasoning=str(result["signal_a"]["reasoning"])[:600],
            signal_b_score=int(result["signal_b"]["score"]),
            signal_b_reasoning=str(result["signal_b"]["reasoning"])[:600],
            divergence_detected=bool(result["divergence_detected"]),
            final_verdict=str(result["final_verdict"]),
            confidence=str(result["confidence"]),
            final_reasoning=str(result["final_reasoning"]),
            data_sources=data_sources,
            last_updated=now,
            reevaluation_count=0,
        )
