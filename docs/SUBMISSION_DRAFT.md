# TrustReconciler — Builder Portal Submission Draft (Projects track)

**Category:** Projects
**Status:** steward "Action needed" items fixed (Sep 2026) — awaiting Fai's resubmit
**Repo:** https://github.com/faisalnugroho/trustreconciler
**Live dApp:** https://faisalnugroho.github.io/trustreconciler/
**Contract (Studionet, v2 — post-fix redeploy):** https://explorer-studio.genlayer.com/address/0x3c639D84c6B1463eaFE91EA0A2Db8d767e742c2B

---

## Problem statement

On the Etherscan wallet page (the "Cards" feature), multiple third-party
wallet-reputation providers appear side by side — and they frequently
**contradict each other for the same wallet, on the same page**. The real
case that motivated this project: one wallet simultaneously showed
**Wallet Trust Score 100/100** from provider A while showing **AML Risk
Score: MEDIUM** from provider B. A user staring at that page has no way to
know which score to trust, or *why* they disagree.

This is not an edge case — it is structural. Different providers use
different methodologies: some treat absence of evidence as risk
(conservative), others treat absence of negative evidence as trust
(optimistic). Two honest systems can reach opposite conclusions from the
same chain.

## Solution

TrustReconciler makes that disagreement **first-class** instead of hiding it:

1. **Signal A — Risk-Conservative Model.** Absence of positive evidence is
   treated as risk: young wallets, burst patterns, low counterparty
   diversity, erratic transaction history all raise the risk score.
2. **Signal B — Trust-Optimistic Model.** Absence of negative evidence is
   treated as trust: zero contact with phishing-flagged addresses, high
   counterparty diversity, and consistent history raise the trust score.
3. **GenLayer LLM consensus as arbiter.** When the two signals diverge
   beyond a 30-point gap, an on-chain LLM consensus run compares both
   signals and the underlying metrics, explains **the root cause of the
   divergence** (not a simple average), and commits a final verdict with
   confidence level and full auditable reasoning.

The two signals are deliberately built with opposing philosophies — so they
naturally diverge on exactly the wallets where reputation providers
contradict each other in the wild, which is the point: the product
demonstrates, on-chain, that GenLayer can adjudicate between two reasonable
but conflicting judgments and produce an explainable answer.

Verdict family: `Aligned-Trustworthy`, `Aligned-Risky`,
`Divergent-Resolved-Trust`, `Divergent-Resolved-Risk`, `Undetermined`.

Fail-safe: if ANY data fetch fails (HTTP error, rate-limit, malformed
payload, empty label dataset), the run stops **before any LLM judgment**
and commits `Undetermined` with an explanation of exactly which fetch failed.
Partial data never reaches arbitration.

## Steward review fixes (Sep 2026 — all four "Action needed" items)

### 1. Re-evaluation preserves the chain of the original record

A wallet's record is now permanently **pinned to the chain of its first
reconciliation**. Both entrypoints reject a different chain with an
explicit error BEFORE the non-deterministic block:

- `request_reevaluation(eth-wallet, "base")` → reverts
  `chain_mismatch:pinned_to:eth`
- a renewed `request_reconciliation` on a known wallet with a different
  chain → same explicit revert; the existing record is never overwritten.

**Live proof (real consensus tx, new contract):**
[`0x1d5502681b86a38151b53c6df7ae6c6d119c288cc8bb02033ba7c55bf0a38af9`](https://explorer-studio.genlayer.com/tx/0x1d5502681b86a38151b53c6df7ae6c6d119c288cc8bb02033ba7c55bf0a38af9)
— attempted re-evaluation of the eth-pinned S1 wallet with `chain=base`;
the leader stderr shows the contract revert
`AssertionError: chain_mismatch:pinned_to:eth` and the stored record is
untouched (still chain=eth, re-evals=1). `get_cooldown_info` now also
exposes the pinned chain, and the dApp locks the chain selector
client-side to avoid a guaranteed-to-revert consensus round.
Regression tests: `TestStewardFixChainPinning` (6 tests).

### 2. Stored record provably changes after cooldown + re-eval

Re-evaluations are asserted NON-NO-OP in three layers
(`TestStewardFixRecordMutation`):

- `last_updated` and `requested_at` strictly bump;
- `reevaluation_count` strictly increments;
- when the new data differs from the old, at least one substantive field
  (signal score / final verdict / signal reasoning / final reasoning)
  must change — and the change is asserted against the record **committed
  to storage** (read back via `get_reconciliation`), not just the tx
  return value.

Covered cases: success mutation (flagged-funder wallet → clean wallet:
Aligned-Risky → Aligned-Trustworthy with score change) AND fail-safe
mutation (API down on re-eval → record concretely changes to
Undetermined). Live on-chain proof: `docs/live_mutation_proof.json`
(before/after records of a real post-cooldown re-evaluation of the S1
smoke wallet on the new contract, with all checks true) — see also the S3
smoke tx where a failed fetch concretely overwrote the prior record state
(reevaluation_count incremented on-chain).

### 3. Cooldown uniform for ALL verdicts, including Undetermined

On-chain, the cooldown was already verdict-agnostic (both entrypoints
guard on the same `last_updated`); the real bypass was in the **dApp UI**,
which enabled the "Re-check" button immediately on Undetermined records,
inviting guaranteed-to-revert consensus rounds. The bypass is **removed**:
the UI now applies the same countdown to Undetermined as every other
verdict, and the Undetermined record's own reasoning text now states the
cooldown applies to it. Contract-side uniformity is explicitly
regression-tested (`TestStewardFixUndeterminedCooldown`, 3 tests:
re-eval and re-request inside cooldown both revert; retry only succeeds
after the full window, at which point the record concretely changes).

### 4. Dataset pinned to an immutable commit + partial-history honesty

**Pinning (three layers):**

- **URL immutability** — the contract fetches the label dataset from
  `raw.githubusercontent.com/faisalnugroho/trustreconciler/<40-hex-COMMIT-SHA>/data/phishing_labels.json`.
  The commit SHA is a **constructor argument**; moving refs
  (`main`/`HEAD`/`master`/short-sha) are **rejected at construction**
  (`invalid_dataset_commit_sha:must_be_40_hex_commit_not_moving_ref`) —
  there is no "fetch latest" path in the contract at all.
- **Content verification** — the deployer also passes the expected
  **keccak256 of the dataset bytes**; the leader AND every validator
  recompute the hash over the fetched bytes each run. A moved/replaced/
  tampered dataset fails the run explicitly
  (`dataset_hash_mismatch` → `Undetermined`), never silently changing
  verdicts.
- **Auditable record** — every stored record embeds
  `dataset_ref = "<commit12>:<keccak16>"`; the active pin is readable
  on-chain via the new `get_dataset_pin()` view.

Active production pin (verified live on-chain):
commit `53246b6bb348b41b4336657dd9ae1eaf8dfc43d5`, keccak256
`ee0076523ad355d5289b55757f61e1a7e555a4b0f952f305e9ddd8f36100fec7`,
ref `53246b6bb348b:ee0076523ad355d5` — every live record on the new
contract carries it.

**Partial-history honesty:**

- Every record carries a machine-readable `history_coverage` field:
  `full_window` | `partial_window` (fetch page cap hit — bounded sample,
  not full history) | `no_visible_history` (mirror coverage gap — absence
  of data proves nothing about true wallet age/activity).
- BOTH signal reasonings append an explicit "LIMITED DATA" clause on
  partial/empty windows (e.g. "verdict based on the first 100 txs only";
  "zero flagged contact verified within the first 100-tx window only";
  "absence of data is NOT proof of a new or inactive wallet").
- The arbitration prompt carries the same DATA WINDOW CAVEAT, and the
  arbiter output is **validated**: on a partial window the final
  reasoning MUST acknowledge partial data — a full-history-sounding
  verdict is rejected (`partial_data_ack_missing` → `Undetermined`).
- The dApp displays the coverage classification and dataset ref on
  every verdict.

Regression tests: `TestStewardFixDatasetPinning` (11 tests, incl.
constructor rejections of `main`/`HEAD`/`master`/short/empty pins and
the hash-mismatch fail-safe) + `TestStewardFixPartialHistoryHonesty`
(4 tests, incl. the LLM-acknowledgment gate).

## Evidence

- **Repository:** https://github.com/faisalnugroho/trustreconciler
- **Contract v2 (Studionet explorer, post-fix redeploy):**
  https://explorer-studio.genlayer.com/address/0x3c639D84c6B1463eaFE91EA0A2Db8d767e742c2B
- **Live dApp (GitHub Pages):** https://faisalnugroho.github.io/trustreconciler/
- **Tests:** 60/60 gltest direct-mode tests pass (was 32; +28 covering the
  four steward fixes) — chain pinning, record-mutation proofs, Undetermined
  cooldown uniformity, dataset pinning (constructor rejections +
  content-hash fail-safe + audit ref) and partial-history honesty.
  `genvm-lint check --json` → `validate.ok: true` (3 view + 2 write
  methods, 2 constructor params).

### Live smoke-test transactions (Studionet, real consensus — not mocks)

All scenarios use **real mainnet wallet addresses** whose history was
manually verified on the same Blockscout instance the contract fetches from
(exact contract fetch window: first-100 `sort=asc`, txlist + tokentx).

| # | Scenario | Wallet | Tx | Live result |
|---|----------|--------|----|-------------|
| S1 | Established clean wallet (age ~3.6 yr, 88 unique counterparties, 0 flagged contact) | `0x930B88…7508` | [`0x2be624…c516`](https://explorer-studio.genlayer.com/tx/0x2be62413cc0c4f9ce401d6f8d838506bfb064f2e52330cc30d0b44338f52c516) | `Aligned-Trustworthy`, A=15 / B=85, 75.5 s — record honestly flagged `history_coverage=partial_window` (≥100 txs) with LIMITED-DATA clauses in both signal reasonings and the arbiter reasoning acknowledging the limited window |
| S2 | **Fresh wallet (4 days old, 6 clean counterparties) — the core divergence case** | `0xcF2Ae4…Db0` | [`0xb5c183…7b6`](https://explorer-studio.genlayer.com/tx/0xb5c1833b170ccc73e3568f6adcaad39ebd67e88e188fc26c36d0c5388f7317b6) | `Divergent-Resolved-Trust`, confidence Medium, A=55 / B=80, divergence=True, 106.1 s — arbiter reasoning names the root cause: "the wallet is only 4 days old: Model A treats this lack of history itself as risk, while Model B relies on the observed clean activity" |
| S3 | **Live API failure** (base.blockscout returned genuine HTTP 500 during consensus) | `0x51FfD9…33bF` (chain=base) | [`0xcc7ce0…72ef`](https://explorer-studio.genlayer.com/tx/0xcc7ce0a7fab4f40241e070f5cedcad196a6b3307843f5702620ea7acfcd472ef) | `Undetermined`, fail-safe fired BEFORE LLM judgment: *"reconciliation stopped before any LLM judgment because required data could not be retrieved: txlist:AssertionError('http_500'); tokentx:AssertionError('http_500')"*, 60.8 s — record carries the dataset_ref even on failure |

S3 is a genuine network failure caught during the run, not a staged one —
base.blockscout's account-txlist endpoint was returning HTTP 500 on that
address during the consensus round (documented in
`docs/deployment_log.json`).

Additional live on-chain evidence created through the deployed dApp and
the mutation-proof script is visible on the contract's explorer page.

## Data sources (honest description)

- **On-chain wallet data (inside the contract):** the contract fetches
  txlist / tokentx / balance via the **Blockscout Etherscan-compatible
  public API** (`eth.blockscout.com` / `base.blockscout.com`,
  `module=account`). These endpoints are keyless. Contract code is public
  and executed by every consensus validator, so any key embedded in a
  contract would be effectively public (burned) and rate-limited across
  the whole validator pool; keyless public endpoints keep every
  validator's evidence independent, identical, and independently
  verifiable — which is what GenLayer consensus equivalence requires.
- **Phishing labels (inside the contract):** our own repo-hosted snapshot
  of the public `forta-network/labelled-datasets` Fake_Phishing address
  set (5,743 addresses), synced off-chain by `scripts/sync_dataset.py`,
  **pinned per deployment** to an immutable-by-commit public URL with
  per-run keccak256 content verification (see fix 4 above).
- **Etherscan API key (off-chain only):** `ETHERSCAN_API_KEY` is used
  exclusively by off-chain tooling — dataset-sync cross-checks and
  smoke-test verification — never by the contract. It is read from the
  environment (`scripts/etherscan_config.py`), never hardcoded, never in
  git history.
- **Signal models:** Signal A and Signal B are our own heuristic models
  computed from free on-chain data. They are NOT passthroughs of any paid
  reputation provider's API, and this project does not aggregate
  commercial reputation APIs.

## Known limitations

- **Blockscout address-history coverage (verified live 2026-09-03):** the
  public `eth.blockscout.com` instance does not return full address
  history for every wallet — several long-established addresses (e.g.
  `0xd8dA…c0Ab`, Binance hot wallets, the 1inch router) return "No
  transactions found" and `null` balances, while recent/active addresses
  return complete history. Consequence: Signal A's wallet-age metric is
  computed from the oldest transaction *visible on the instance*, which
  can underestimate true age. **Mitigation (fix 4):** every record
  explicitly classifies its coverage (`history_coverage`), both signal
  reasonings carry LIMITED-DATA clauses, and the LLM verdict is validated
  to acknowledge partial data — a verdict can never present itself as
  full-history analysis. Smoke-test wallets were manually verified on the
  same instance.
- **First-100-transaction window:** the contract fetches the first 100
  native + 100 token transfers (`sort=asc`) to keep validator fetches
  comparable and payloads bounded; for very high-volume wallets this is a
  sample, not full history — now explicitly recorded per record.
- **Phishing labels are a periodic snapshot:** addresses newly flagged
  after the last sync are unknown to the contract until the next sync;
  each sync commits a new immutable dataset version and a NEW deployment
  pins it, while previous records keep their own auditable `dataset_ref`.

## Architecture summary

- `contracts/TrustReconciler.py` — GenLayer Intelligent Contract
  (sdk v0.2.16): 3 view + 2 write methods, 2 constructor params (dataset
  commit pin + expected keccak). `request_reconciliation`
  (permissionless; validated, checksummed input; cooldown-guarded;
  chain-pinned) runs the single fail-safe pipeline inside `gl.nondet`:
  4 fetches (3 keyless Blockscout + 1 commit-pinned, hash-verified
  dataset fetch) → history-coverage classification → deterministic
  metric/signal computation → LLM arbitration with strict output
  validation and hard gates (flagged contact mandates Risk; partial
  window requires partial-data acknowledgment).
  `request_reevaluation` re-runs the SAME pipeline after the 1-hour
  anti-spam cooldown (uniform for all verdicts, Undetermined included),
  chain-pinned to the first record, incrementing `reevaluation_count`.
  Consensus equivalence compares the deterministic fields (signal scores,
  divergence flag, final verdict); free-text reasoning is intentionally
  not compared.
- `tests/` — 60 direct-mode tests covering all 10 spec scenarios plus
  hardening extras AND the four steward-fix regression classes
  (28 new tests).
- `scripts/` — dataset sync (with optional keyed cross-check), deploy +
  smoke (pins dataset commit+keccak at deploy, reads back
  `get_dataset_pin` on-chain), live mutation-proof script.
- `frontend/` — single-page dApp (GitHub Pages): burner/imported
  in-browser wallets, faucet, full write→consensus→read lifecycle, Signal
  A/B side-by-side cards, divergence badge, verdict panel with complete
  reasoning + history coverage + dataset ref, chain selector locked to
  the pinned chain, re-check button that respects the cooldown for ALL
  verdicts (Undetermined bypass removed).

## Why this fits the Projects track

Same structure as prior accepted Projects submissions (VeriBid,
SecondHandCarInspectionEscrow): one repo combining contract + tests +
frontend + deployment evidence, a working live deployment on Studionet
with real consensus transactions, and an honest README documenting data
sources and limitations — now hardened per the steward's review items
with live on-chain proofs.

---

## Steward round-2 frontend fixes (Sep 2026 — both remaining bugs)

The contract-side items (chain pinning, cooldown, dataset) were
accepted; the two remaining bugs were purely frontend and are fixed,
tested, and proven live below.

### Fix 1 — Re-evaluation chain is derived from the stored record

`doRecheck()` no longer reads the chain selector at all. Immediately
before sending the transaction it re-reads the on-chain record
(`get_reconciliation`) and sends THAT record's pinned `chain` field to
`request_reevaluation`. The selector's value is ignored entirely on this
path; if the record somehow carries no chain, the UI refuses to send
anything rather than guess. The read-only "View stored record" button
additionally syncs the selector to the pinned chain so the UI never
sits in a state that contradicts the stored record.

**Live proof (production dApp, real consensus tx):** the S3 wallet
(pinned to `base`) was displayed via "View stored record", the chain
selector was deliberately set to `eth`, and Re-check was clicked. The
transaction sent `chain=base` — derived from the record, not the
selector:
- dApp console evidence line: `[recheck] chain sent to
  request_reevaluation = base (derived from stored record; UI selector
  was eth)`
- tx: https://explorer-studio.genlayer.com/tx/0x720a025521e9bb504ef86cd2324432c56bb365ac2ce9e80f5098b076d5fac4be
- screenshots: `artifacts/live_s1_selector_sabotaged_eth.png` (selector
  on eth, record on base, re-eval in flight) and
  `artifacts/live_s1_final.png` (post-tx state).

### Fix 2 — Success only after FINALIZED + verified record mutation

The transaction lifecycle was rebuilt end to end:

1. `sendWrite()` now waits for **FINALIZED** (was ACCEPTED). If the
   receipt wait times out, a recovery loop polls the actual receipt
   until it is FINALIZED or ends in a terminal non-final status
   (UNDETERMINED / CANCELED / *_TIMEOUT) — never an optimistic
   success from a state probe (the old optimistic probe is deleted).
2. After FINALIZED, `doRecheck()` re-reads the record via
   `get_reconciliation` and requires **proof of mutation** before any
   success state: `last_updated` or `reevaluation_count` must differ
   from the values snapshotted before the transaction was sent.
3. If the tx finalized but the record did NOT change, the UI shows an
   explicit **anomaly** error (with before/after values and the tx
   link) — not success. The success toast only fires after the
   verified mutation, and a console evidence line prints the exact
   before/after comparison.

**Live proof:** two independent runs, both on the production dApp:

- Scenario 1 (sabotaged selector): the S3 wallet record (pinned to
  `base`) was displayed via "View stored record", the chain selector
  was deliberately set to `eth`, and Re-check was clicked. The
  transaction sent `chain=base` — the tx calldata itself proves it:
  `{"method":"request_reevaluation","args":["0x51FfD9b1…33bF","base"]}`
  — tx
  [`0x720a0255…fac4be`](https://explorer-studio.genlayer.com/tx/0x720a025521e9bb504ef86cd2324432c56bb365ac2ce9e80f5098b076d5fac4be)
  (FINALIZED, exec SUCCESS), record mutated re-evals 2→3.
  Console line: `[recheck] chain sent to request_reevaluation = base
  (derived from stored record; UI selector was eth)`.
- Scenario 2 (normal path): the S1 wallet (pinned `eth`) re-checked
  with a matching selector — tx
  [`0xc2c36033…c3c0ec`](https://explorer-studio.genlayer.com/tx/0xc2c360338c01b2e7c41c0e67b40fc1a7d7ba15f15011a35689b438b014c3c0ec)
  (FINALIZED, exec SUCCESS). The UI printed the exact before/after
  comparison it performed BEFORE showing success:
  `[recheck] SUCCESS VERIFIED on chain — before {last_updated:1788489860,
  reevals:2} → after {last_updated:1788550179, reevals:3}` — and the
  same values were confirmed independently off-band via
  `genlayer_py read_contract` (`artifacts/live_round2_proof.json`,
  `artifacts/live_round2_s2_proof.json`; screenshots in
  `artifacts/live_s1_*.png`, `artifacts/live_s2_*.png`).

### Regression lock

12 new source-level frontend tests
(`tests/test_frontend_source.py`) pin both fixes: the single
`request_reevaluation` call site must pass the record-derived chain;
the before-read must happen before the tx; FINALIZED (not ACCEPTED)
wait; verified-mutation comparison strings; anomaly branch must not
show success; no optimistic state probe. Full suite: **72/72 pass**
(60 contract + 12 frontend).
