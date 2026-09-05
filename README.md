# TrustReconciler

**Reconciling contradictory wallet-reputation signals with GenLayer consensus.**

## The problem

On the Etherscan wallet page ("Cards" feature), multiple third-party wallet
reputation providers appear side by side — and they frequently contradict
each other for the *same* wallet, on the *same* page. The real case that
motivated this project: one wallet showed **Wallet Trust Score 100/100** from
provider A while showing **AML Risk Score: MEDIUM** from provider B. Users
are left guessing which one to trust.

## The approach

TrustReconciler computes **two independent heuristic signals** from free
on-chain wallet data (Etherscan/Basescan API + the public
`forta-network/labelled-datasets` Fake_Phishing label set), deliberately built
with **opposing philosophies** so that they can naturally disagree — mirroring
the real-world phenomenon of reputation providers reaching different
conclusions from the same data:

- **Signal A — Risk-Conservative Model**: absence of positive evidence is
  treated as risk (young wallets, burst patterns, low counterparty diversity).
- **Signal B — Trust-Optimistic Model**: absence of negative evidence is
  treated as trustworthiness (zero flagged-counterparty contact, high
  counterparty diversity, consistent transaction history).

A GenLayer LLM consensus then acts as the **arbiter**: it compares both
signals, explains *why* they diverge when they do (root cause, not a simple
average), and issues a final verdict with confidence level and auditable
reasoning.

## Data sources (honest description)

**On-chain data (inside the contract):** the contract fetches wallet
transactions, token transfers, and balance via the **Blockscout
Etherscan-compatible public API** (`eth.blockscout.com` / `base.blockscout.com`,
`module=account` `action=txlist|tokentx|balance`). These endpoints are
keyless: contract code is public and executed by every consensus validator,
so any key embedded in a contract would be effectively public (burned) and
rate-limited across the whole validator pool. Keyless public endpoints keep
every validator's evidence independent, identical, and independently
verifiable — which is what GenLayer consensus equivalence requires.

**Phishing labels (inside the contract):** our own repo-hosted snapshot of
the public `forta-network/labelled-datasets` Fake_Phishing address set,
synced off-chain by `scripts/sync_dataset.py`. The snapshot is **pinned per
deployment** (steward review fix, Sep 2026): the contract fetches it from
an immutable-by-commit `raw.githubusercontent.com` URL that embeds a
40-hex git commit SHA (moving refs like `main`/`HEAD` are rejected by the
constructor — there is no "fetch latest" path), AND re-computes the
keccak256 of the fetched bytes on every run, comparing it to the expected
hash supplied at deployment — so a moved, replaced, or tampered dataset
fails the run explicitly (`dataset_hash_mismatch` → `Undetermined`)
instead of silently changing verdicts. Every stored record embeds
`dataset_ref = "<commit12>:<keccak16>"` so each verdict is auditable
against the exact dataset bytes it was judged with; the active pin is
readable on-chain via `get_dataset_pin()`. The contract never fetches
the upstream dataset live per request.

**Etherscan API key (off-chain only):** `ETHERSCAN_API_KEY` is used
exclusively by off-chain tooling — dataset-sync cross-checks and smoke-test
verification — never by the contract. It is read from the environment
(`scripts/etherscan_config.py`), never hardcoded.

**Honesty note (models):** Signal A and Signal B are our own heuristic
models computed from free on-chain data. They are NOT passthroughs of any
paid reputation provider's API (Veritas Protocol, Satoshieye, zScore, etc.),
and this project does not aggregate those commercial APIs.

## Status

Deployed and live:

- **Contract (Studionet):** see `docs/deployment_log.json` for the current
  address and tx hashes — 60/60 direct-mode tests (incl. the four steward
  review fixes), genvm-lint ok (3 view + 2 write methods, 2 constructor
  params), live consensus smoke scenarios.
- **dApp (GitHub Pages):** [faisalnugroho.github.io/trustreconciler](https://faisalnugroho.github.io/trustreconciler/)
- **Submission draft:** `docs/SUBMISSION_DRAFT.md`

## Secret handling

The Etherscan API key is read exclusively from the `ETHERSCAN_API_KEY`
environment variable — never hardcoded, and never used by the on-chain
contract (the contract uses keyless public endpoints; see "Data sources").
Local development uses a git-ignored `.env` file at the repo root (see
`.env.example`); systemd services use a separate root-owned
`EnvironmentFile=` outside the repo.

## Known limitations

- **In-flight UI state across refresh (fixed Sep 2026, QA round-3):** an
  accidental refresh used to wipe the address being processed, forcing a
  retype. The address + chain of a pending request are now mirrored to
  `localStorage` (`trustreconciler_inflight`, 30-min expiry) and restored
  on load with a status note pointing at **View stored record** — the
  consensus itself always kept running on chain either way. No
  transaction is ever auto-sent on load, and the before/after mutation
  verification flow is untouched. What is still NOT persisted: the
  live countdown/status strip of the in-flight tx (the tx hash is only
  recoverable from the explorer or after the record commits).

- **Blockscout address-history coverage (verified live 2026-09-03):** the
  public `eth.blockscout.com` instance used by the contract does not return
  full address history for every wallet. Several long-established addresses
  (e.g. `0xd8dA...c0Ab`, Binance hot wallets, 1inch router) return
  "No transactions found" and `null` balances, while recent/active addresses
  return complete history. Chain-level data (blocks, recent transactions) is
  live and consistent with Ethereum mainnet. Consequences: (a) Signal A's
  wallet-age metric is computed from the oldest transaction *visible on the
  instance* — for wallets with a coverage gap this underestimates true age;
  (b) a genuinely old wallet may be scored as if it were fresh. Mitigation
  (steward review fix, Sep 2026): every record now carries an explicit
  `history_coverage` field — `full_window`, `partial_window` (the fetch
  page cap was hit), or `no_visible_history` (coverage gap) — and BOTH
  signal reasonings append a "LIMITED DATA" clause stating the verdict
  rests on a bounded window / no visible history, so a verdict can never
  present itself as full-history analysis. The arbitration prompt receives
  the same caveat and the LLM's reasoning is validated to acknowledge
  partial data on partial windows (a full-history-sounding verdict on a
  bounded window is rejected → `Undetermined`). Smoke tests use wallets
  whose history was manually verified to be present on the same instance.
- **Bounded paginated history window (extended Sep 2026, steward fix 5):**
  the contract fetches the first **300 native transactions (3 txlist
  pages) + 200 token transfers (2 tokentx pages)** of 100 each
  (`sort=asc`, ascending) — a FIXED page plan fetched identically by
  the leader and every validator (never "until exhausted", which would
  make the window node-dependent and break consensus determinism).
  Wallets whose visible history fits inside this window are covered
  completely; very high-volume wallets beyond it are a bounded sample,
  now explicitly recorded per record (`history_coverage`). Bounds
  rationale (measured live): a 100-tx txlist page is ~100 KB, while a
  100-entry tokentx page can reach 7-10 MB (entries embed full calldata)
  — the 3+2 page plan balances coverage against the non-deterministic
  block's time budget on every validator.
- **Phishing labels are a snapshot:** the Fake_Phishing set is synced
  periodically from the public forta-network labelled-datasets repository;
  addresses newly flagged after the last sync are unknown to the contract
  until the next sync. Each sync commits a new immutable dataset version
  and a NEW deployment pins it (commit + keccak256) — the previous
  deployment's records keep referencing the exact dataset they were
  judged with via their stored `dataset_ref`.

## Steward review fixes (Sep 2026 — "Action needed" response)

Round-3 items (two "Declined" feedback points, both fixed and proven
live — see `docs/SUBMISSION_DRAFT.md` "Steward round-3 fixes"):

7. **Evidence completeness beyond the first 100 txs (paginated
   window).** The history fetch is now PAGINATED over the Blockscout
   Etherscan-compatible API: a FIXED plan of **3 txlist pages + 2
   tokentx pages** (100 per page, `sort=asc`) — 300 native + 200 token
   txs. The page count is a constant, so the leader and every validator
   execute the identical fetch plan (never "until exhausted", which
   would make the window node-dependent and break equivalence); an
   empty beyond-last page (the API's documented no-more-transactions
   response) simply ends the history early without failing the run,
   while any HTTP error on ANY page still fails the whole run
   Undetermined before the LLM (fail-safe unchanged). `history_coverage`
   semantics, all "LIMITED DATA" clauses, the arbitration-prompt caveat,
   `data_sources`, and this README now state the 300+200 window
   explicitly instead of "first 100". Bounds rationale is documented in
   the contract source (measured page sizes: ~100 KB per txlist page vs
   up to ~10 MB per tokentx page — the 3+2 plan balances coverage
   against the non-deterministic block's time budget). Tests:
   `TestStewardFixPaginatedWindow` (10 tests, incl. the steward-requested
   coverage case: a wallet with 100 < txs < 300 now classifies
   `full_window`, and HTTP-500 landmines on later pages prove the fixed
   page plan is actually fetched).
8. **Chain selector locked after a record is loaded (pure UX).** Once a
   record is displayed — fresh reconciliation, opening an existing one,
   or the Reconcile pre-check detecting a pinned wallet — the chain
   dropdown is VISUALLY REPLACED by a "🔒 chain locked to: <chain>"
   badge. The selector only re-activates when the user moves to a
   different wallet address (typing fires `syncChainLockUI`). This is a
   display-only change: the transaction logic is untouched — re-eval
   still derives the chain from the stored record (round-2 fix 1, with
   a regression test asserting `doRecheck`'s send path never consults
   the lock UI), and genuinely NEW wallets still pick their chain
   normally. Verified live with screenshots (badge replaces dropdown on
   record load; selector returns for a different address) —
   `artifacts/live_chainlock_badge.png` +
   `artifacts/live_chainlock_unlocked.png` (SW record loaded with the
   badge replacing the dropdown; typing a different wallet re-activates
   the selector).

Adversarial QA round-3 (7 production scenarios executed against the live
dApp — double-click races, mid-consensus refresh, signer switches, chain
flips, lowercase/checksum duplicates, last-second cooldown boundary, RPC
cuts mid-verification; all 7 PASS) surfaced two UI gaps, both fixed
(commit `38f7275`) and re-verified live:

5. **Reconcile now pre-checks the on-chain cooldown before sending.**
   Previously, clicking Reconcile on a wallet with an existing record
   still inside its 1-hour anti-spam window sent a transaction that was
   guaranteed to revert (`cooldown_active`) — a wasted consensus round.
   The dApp now reads `get_cooldown_info` first (the same call it already
   made for chain-pin locking) and refuses at the UI level: no tx is
   sent, the existing record is rendered with its countdown, and the
   message states exactly when re-evaluation unlocks. Verified live: 0
   `eth_sendRawTransaction` for an in-cooldown wallet (previously 1
   guaranteed revert per click). The contract guard is unchanged and
   remains the source of truth.
6. **In-flight address persists across refresh.** See Known limitations
   above for scope. Verified live: after F5 mid-consensus, the input is
   re-filled, a restoration note is shown, zero txs are sent by the
   reload, and the record commits + renders normally afterwards.

Four issues raised by the Builder Portal steward review, all fixed with
regression tests (direct-mode suite: 60/60):

1. **Re-evaluation chain pinning.** A wallet's record is permanently
   bound to the chain of its FIRST reconciliation. Both entrypoints
   (`request_reconciliation` renewals and `request_reevaluation`)
   reject a different chain with an explicit `chain_mismatch:pinned_to:<chain>`
   error BEFORE the non-deterministic block — no silent overwrite of an
   eth record with base data (or vice versa). `get_cooldown_info` exposes
   the pinned chain so frontends enforce it too (the dApp locks the
   chain selector). Tests: `TestStewardFixChainPinning` (6 tests).
2. **Provable record mutation after re-evaluation.** Re-evaluations are
   proven non-no-op: `last_updated` and `requested_at` bump,
   `reevaluation_count` increments, AND when the new data differs, at
   least one substantive field (signal score / verdict / reasoning)
   must change — asserted against the record COMMITTED TO STORAGE (read
   back via `get_reconciliation`), not just the return value. Both a
   success case (flagged funder gone → Aligned-Risky →
   Aligned-Trustworthy) and a failure case (API down → Undetermined
   overwrite) are covered. Tests: `TestStewardFixRecordMutation` (2
   tests).
3. **Cooldown uniform for all verdicts including Undetermined.** The
   on-chain cooldown was already uniform (both entrypoints guard on the
   same `last_updated` regardless of verdict family); the bypass that
   DID exist was in the dApp UI, which enabled the re-check button
   immediately on Undetermined records (leading to guaranteed-to-revert
   consensus rounds). Removed: the UI now shows the same countdown for
   Undetermined as every other verdict, and the contract-side uniformity
   is now explicitly regression-tested. Tests:
   `TestStewardFixUndeterminedCooldown` (3 tests).
4. **Dataset pinning + partial-history honesty.** Dataset fetched from
   an immutable-by-commit URL (constructor rejects moving refs;
   per-run keccak256 content verification; every record stores
   `dataset_ref`) — see "Data sources". Partial/empty history windows
   are explicit in the data itself: `history_coverage` field,
   LIMITED-DATA clauses in both signal reasonings, prompt caveat, and a
   validation gate that rejects full-history-sounding LLM verdicts on
   partial windows. Tests: `TestStewardFixDatasetPinning` (11) +
   `TestStewardFixPartialHistoryHonesty` (4).

## Repository layout

    contracts/   GenLayer Intelligent Contract (Signal A/B + LLM arbiter)
    tests/       gltest direct-mode test suite (fail-safe coverage)
    scripts/     dataset sync, deploy, smoke tests (env-key consumers)
    data/        synced public label datasets (JSON)
    frontend/    dApp (wallet connect, side-by-side signal cards, verdict)
    docs/        SUBMISSION_DRAFT.md and architecture notes
