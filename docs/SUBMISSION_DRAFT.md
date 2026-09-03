# TrustReconciler — Builder Portal Submission Draft (Projects track)

**Category:** Projects
**Status:** ready for manual portal submission by Fai
**Repo:** https://github.com/faisalnugroho/trustreconciler
**Live dApp:** https://faisalnugroho.github.io/trustreconciler/
**Contract (Studionet):** https://explorer-studio.genlayer.com/address/0x54cf383f888Ef2cB50B70FA06Fa5042938CcC621

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
payload, empty label dataset), the run stops **before any LLM judgment** and
commits `Undetermined` with an explanation of exactly which fetch failed.
Partial data never reaches arbitration.

## Evidence

- **Repository:** https://github.com/faisalnugroho/trustreconciler
- **Contract (Studionet explorer):**
  https://explorer-studio.genlayer.com/address/0x54cf383f888Ef2cB50B70FA06Fa5042938CcC621
- **Live dApp (GitHub Pages):** https://faisalnugroho.github.io/trustreconciler/
- **Tests:** 32/32 gltest direct-mode tests pass
  (`module=account` fetch pipeline, input validation, cooldown/re-eval,
  LLM hardening, data-failure fail-safe, dataset poisoning, Base chain).
  `genvm-lint check --json` → ok (22 W004 bare-assertion warnings — the
  established GenLayer revert pattern, fewer than prior accepted submissions).

### Live smoke-test transactions (Studionet, real consensus — not mocks)

All three scenarios use **real mainnet wallet addresses** whose history was
manually verified on the same Blockscout instance the contract fetches from
(exact contract fetch window: first-100 `sort=asc`, txlist + tokentx).

| # | Scenario | Wallet | Tx | Live result |
|---|----------|--------|----|-------------|
| S1 | Established clean wallet (≈3.5 yr history, 88 unique counterparties, 0 flagged contact) | `0x930B88…7508` | [`0x68f4b985…f1f2e6b`](https://explorer-studio.genlayer.com/tx/0x68f4b9850ed6edc491e198405fd55a7e5e28191bb99634fe05b0ad2b7f1f2e6b) | `Aligned-Trustworthy`, confidence High, A=15 / B=85, 53.8 s |
| S2 | **Fresh wallet (3 days old, 6 clean counterparties) — the core divergence case** | `0xcF2Ae4…Db0` | [`0xac9cf51b…394966`](https://explorer-studio.genlayer.com/tx/0xac9cf51b88925ba28ff473f799e25b411a587b3622c8c1ec79774b9905394966) | `Divergent-Resolved-Trust`, confidence Medium, A=55 / B=80, divergence=True, 62.3 s — arbiter reasoning explicitly resolves Signal A's "insufficient history" penalty vs Signal B's "no negative evidence" trust |
| S3 | **Live API failure** (base.blockscout txlist/tokentx returned genuine HTTP 500 during consensus) | `0x51FfD9…33bF` (chain=base) | [`0xf1587a9b…e98db5`](https://explorer-studio.genlayer.com/tx/0xf1587a9b9993a1c35a136199bc2e4a62841d470b21a8d69f1f6ab6f903e98db5) | `Undetermined`, confidence Low, fail-safe fired BEFORE LLM judgment: *"reconciliation stopped before any LLM judgment because required data could not be retrieved: txlist:AssertionError('http_500'); tokentx:AssertionError('http_500')"* — 80.6 s |

S3 is a genuine network failure caught during the run, not a staged one —
base.blockscout's account-txlist endpoint was intermittently returning
HTTP 500 on that address (documented in
`docs/deployment_log.json` and `docs/smoke_console.log`).

Additional live on-chain records created through the deployed dApp during
E2E verification are visible on the contract's explorer page.

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
  set (5,743 addresses), synced off-chain by `scripts/sync_dataset.py`
  and fetched by the contract from a pinned, immutable-per-commit public
  URL so all validators see the identical dataset.
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
  can underestimate true age for wallets with a coverage gap. The
  contract honestly scores the data it can verifiably fetch, and the
  reasoning fields state the evidence used. Smoke-test wallets were
  manually verified on the same instance.
- **First-100-transaction window:** the contract fetches the first 100
  native + 100 token transfers (`sort=asc`) to keep validator fetches
  comparable and payloads bounded; for very high-volume wallets this is a
  sample, not full history.
- **Phishing labels are a periodic snapshot:** addresses newly flagged
  after the last sync are unknown to the contract until the next sync.

## Architecture summary

- `contracts/TrustReconciler.py` — GenLayer Intelligent Contract
  (sdk v0.2.16): 2 view + 2 write methods. `request_reconciliation`
  (permissionless; validated, checksummed input; cooldown-guarded) runs
  the single fail-safe pipeline inside `gl.nondet`: 4 fetches →
  deterministic metric/signal computation → LLM arbitration with strict
  output validation and a hard gate (flagged contact mandates Risk).
  `request_reevaluation` re-runs after a 1-hour anti-spam cooldown,
  incrementing `reevaluation_count`. Consensus equivalence compares the
  deterministic fields (signal scores, divergence flag, final verdict);
  free-text reasoning is intentionally not compared.
- `tests/` — 32 direct-mode tests covering all 10 spec scenarios plus
  hardening extras (checksum/chain validation, LLM hard gate, malformed
  LLM output, dataset poisoning, cumulative re-eval count).
- `scripts/` — dataset sync (with optional keyed cross-check), deploy +
  smoke, smoke-wallet research tooling.
- `frontend/` — single-page dApp (GitHub Pages): burner/imported in-browser
  wallets, faucet, full write→consensus→read lifecycle, Signal A/B
  side-by-side cards, divergence badge, verdict panel with complete
  reasoning, re-check button that respects the cooldown.

## Why this fits the Projects track

Same structure as prior accepted Projects submissions (VeriBid,
SecondHandCarInspectionEscrow): one repo combining contract + tests +
frontend + deployment evidence, a working live deployment on Studionet
with real consensus transactions, and an honest README documenting data
sources and limitations.
