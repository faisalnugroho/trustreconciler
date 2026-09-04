# Draft balasan ke steward (TrustReconciler — "Action needed")

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
