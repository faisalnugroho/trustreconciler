# Draft balasan ke steward (TrustReconciler — round 2, frontend fixes)

Round 1 (contract-side: chain pinning, cooldown, dataset) — accepted.
Round 2 items (both frontend) — fixed, tested, proven live below.

---

Thank you — both remaining frontend bugs are fixed, regression-tested
(12 new source-level tests), and proven with live consensus runs on the
production dApp.

## 1. Re-evaluation chain is now derived from the record, not the UI

`doRecheck()` never reads the chain selector. Right before sending, it
re-reads the on-chain record via `get_reconciliation` and sends that
record's pinned `chain` to `request_reevaluation`. The selector value is
ignored entirely on this path; if the record carries no chain the UI
refuses to send rather than guess.

Live proof — the exact scenario you asked for: opened the Base record
(S3 `0x51FfD9b1…33bF`), set the selector to eth, clicked Re-check.
The transaction still sent chain=base, provable from the tx calldata
itself:

- tx 0x720a025521e9bb504ef86cd2324432c56bb365ac2ce9e80f5098b076d5fac4be
  (FINALIZED, exec SUCCESS) — calldata:
  {"method":"request_reevaluation","args":["0x51FfD9b1…33bF","base"]}
- dApp console: `[recheck] chain sent to request_reevaluation = base
  (derived from stored record; UI selector was eth)`
- screenshots: selector visibly on "eth (mainnet)" while the verdict
  panel shows "chain base" (artifacts/live_s1_selector_sabotaged_eth.png),
  and the post-run state with the record mutated re-evals 2→3
  (artifacts/live_s1_final.png) — base API was rate-limited (http_429)
  during this run, so the record honestly went Undetermined again;
  the chain and mutation behavior are what matter here.

## 2. Success state now requires verified before/after, not optimism

The tx lifecycle waits for FINALIZED (was ACCEPTED). On a receipt-wait
timeout it polls the actual receipt until FINALIZED or a terminal
non-final status — the old optimistic state-probe (`return true`) is
deleted. After FINALIZED, the UI re-reads the record and only shows
success when `last_updated` or `reevaluation_count` actually moved
versus the snapshot taken before the tx was sent; a finalized tx with
an unchanged record renders as an explicit anomaly error with the
before/after values — never success.

Live proof (normal path, S1 `0x930B88…7508` eth):
tx 0xc2c360338c01b2e7c41c0e67b40fc1a7d7ba15f15011a35689b438b014c3c0ec
(FINALIZED, exec SUCCESS). The console shows the comparison the UI ran
before showing success:
`[recheck] SUCCESS VERIFIED on chain — before {last_updated:1788489860,
reevals:2} → after {last_updated:1788550179, reevals:3}` — independently
confirmed via read_contract (artifacts/live_round2_s2_proof.json).

Both proofs are documented in SUBMISSION_DRAFT.md ("Steward round-2
frontend fixes") with all tx links, screenshots, and JSON evidence in
the repo. Full suite: 72/72 (60 contract + 12 frontend regression).

---

# (round 1 below — kept for reference)


Konteks: 4 poin "Action needed" dari review. Semua sudah diperbaiki,
di-test, dan dideploy ulang. Kontrak baru (v2):
https://explorer-studio.genlayer.com/address/0x3c639D84c6B1463eaFE91EA0A2Db8d767e742c2B
Repo: https://github.com/faisalnugroho/trustreconciler (commit 5e3cd55)
dApp: https://faisalnugroho.github.io/trustreconciler/

---

Thank you for the detailed review — all four items are fixed, tested, and
redeployed. Point-by-point with evidence:

## 1. Re-evaluation now preserves the original record's chain

A wallet's record is now permanently pinned to the chain of its FIRST
reconciliation. Both entrypoints (`request_reconciliation` renewals and
`request_reevaluation`) reject a different chain with an explicit
`chain_mismatch:pinned_to:<chain>` revert BEFORE the non-deterministic
block — an eth record can never be overwritten with base data or vice
versa. `get_cooldown_info` also exposes the pinned chain, and the dApp
locks the chain selector client-side.

Evidence:
- Live consensus tx proving the revert (attempted re-eval of the
  eth-pinned wallet with chain=base):
  https://explorer-studio.genlayer.com/tx/0x1d5502681b86a38151b53c6df7ae6c6d119c288cc8bb02033ba7c55bf0a38af9
  — the leader stderr shows `AssertionError: chain_mismatch:pinned_to:eth`
  and the stored record is untouched (chain still eth, re-evals still 1).
- 6 regression tests: `TestStewardFixChainPinning` (wrong chain on
  re-eval, wrong chain on renewal, wrong chain inside cooldown, correct
  chain allowed after cooldown, first record pins all later runs,
  cooldown-info view exposes pinned chain).

## 2. Stored record provably changes after cooldown + re-evaluation

Re-evaluations are now asserted non-no-op at three layers in
`TestStewardFixRecordMutation`:
- `last_updated` and `requested_at` strictly bump,
- `reevaluation_count` strictly increments,
- when the new data differs, at least one substantive field (signal
  score / final verdict / reasoning) must change —
- and the assertion runs against the record COMMITTED TO STORAGE (read
  back through `get_reconciliation`), not just the transaction return
  value.

Both directions are covered: a success mutation (wallet's flagged funder
disappears from its window: Aligned-Risky → Aligned-Trustworthy with a
concrete score drop) and a fail-safe mutation (API down on re-eval → the
record concretely changes to Undetermined).

Evidence: 2 regression tests + a live post-cooldown re-evaluation of the
S1 smoke wallet on the new contract, captured before/after in
`docs/live_mutation_proof.json` (all checks true: count incremented,
timestamps bumped, substantive fields changed, storage matches return).

## 3. Cooldown now provably uniform for Undetermined (bypass removed)

On-chain, the cooldown was already verdict-agnostic — both entrypoints
guard on the same `last_updated` regardless of verdict family, and there
was never a code path that let Undetermined skip it. The bypass that DID
exist was in the dApp UI: the "Re-check" button enabled immediately on
Undetermined records, inviting guaranteed-to-revert consensus rounds.
We removed that UI bypass (the UI now shows the same countdown for
Undetermined as for every other verdict), and added the explicit
regression tests so the uniformity can't regress silently:
`TestStewardFixUndeterminedCooldown` — re-eval and renewed request
inside the cooldown on an Undetermined record both revert with
`cooldown_active` (and nothing changes), retry succeeds only after the
full window, at which point the record concretely changes to the
recovered verdict. The Undetermined record's stored reasoning now also
states the cooldown applies to it.

## 4. Dataset pinned to a specific commit + partial-history honesty

Pinning, three layers:
- The dataset is fetched from an immutable-by-commit URL:
  `raw.githubusercontent.com/faisalnugroho/trustreconciler/53246b6bb348b41b4336657dd9ae1eaf8dfc43d5/data/phishing_labels.json`.
  The commit SHA and the expected keccak256 of the dataset bytes are
  CONSTRUCTOR ARGUMENTS — moving refs (`main`/`HEAD`/`master`) and
  short/empty pins are rejected at construction
  (`invalid_dataset_commit_sha`), so there is no "fetch latest" path in
  the contract at all.
- The leader AND every validator recompute the keccak256 over the
  fetched bytes on every run and compare to the pinned hash — a moved,
  replaced, or tampered dataset fails the run explicitly
  (`dataset_hash_mismatch` → Undetermined) instead of silently changing
  verdicts.
- Every stored record embeds `dataset_ref = "<commit12>:<keccak16>"`
  (current: `53246b6bb348b:ee0076523ad355d5`), and the active pin is
  readable on-chain via the new `get_dataset_pin()` view — verified live
  on the new deployment.

Partial-history honesty:
- Every record now carries a machine-readable `history_coverage`:
  `full_window`, `partial_window` (fetch page cap hit — bounded sample),
  or `no_visible_history` (mirror coverage gap).
- On partial/empty windows BOTH signal reasonings append explicit
  "LIMITED DATA" clauses (e.g. "verdict based on the first 100 txs
  only"; "zero flagged contact verified within the first 100-tx window
  only"; "absence of data is NOT proof of a new or inactive wallet").
- The arbitration prompt carries the same DATA WINDOW CAVEAT, and the
  arbiter output is validated: on a partial window the final reasoning
  MUST acknowledge partial data, otherwise the verdict is rejected
  (`partial_data_ack_missing` → Undetermined).
- The dApp displays the coverage classification and the dataset ref on
  every verdict.

Evidence: live smoke on the new contract — S1 (`0x2be624…c516`)
returned `history_coverage=partial_window` with the LIMITED DATA clauses
visible in the committed consensus output; 11 dataset-pinning tests
(`TestStewardFixDatasetPinning`: ref in records, pin view, real-keccak
crosscheck, hash-mismatch fail-safe, constructor rejections of
main/HEAD/master/short/empty pins, no moving-ref URL in source,
Undetermined record carries ref) + 4 partial-history tests
(`TestStewardFixPartialHistoryHonesty`).

## Regression + deployment summary

- Test suite: 60/60 direct-mode tests (was 32; +28 covering the four
  fixes). `genvm-lint`: validate.ok, 3 view + 2 write methods,
  2 constructor params.
- Redeployed (contract v2):
  https://explorer-studio.genlayer.com/address/0x3c639D84c6B1463eaFE91EA0A2Db8d767e742c2B
  with dataset pin `53246b6bb348b:ee0076523ad355d5` read back on-chain
  via `get_dataset_pin()`.
- Live smoke on v2, real consensus (not mocks):
  - S1 Aligned-Trustworthy: https://explorer-studio.genlayer.com/tx/0x2be62413cc0c4f9ce401d6f8d838506bfb064f2e52330cc30d0b44338f52c516
  - S2 Divergent-Resolved-Trust: https://explorer-studio.genlayer.com/tx/0xb5c1833b170ccc73e3568f6adcaad39ebd67e88e188fc26c36d0c5388f7317b6
  - S3 Undetermined (genuine live API failure): https://explorer-studio.genlayer.com/tx/0xcc7ce0a7fab4f40241e070f5cedcad196a6b3307843f5702620ea7acfcd472ef
- README "Steward review fixes" section documents all of the above.

Happy to adjust anything further — thanks again for pushing the
submission toward this level of rigor.
