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
synced off-chain by `scripts/sync_dataset.py` and pinned at a public
immutable-per-commit URL. The contract never fetches the upstream dataset
live per request.

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

- **Contract (Studionet):** [0x54cf383f888Ef2cB50B70FA06Fa5042938CcC621](https://explorer-studio.genlayer.com/address/0x54cf383f888Ef2cB50B70FA06Fa5042938CcC621) — 32/32 direct-mode tests, genvm-lint ok, 3/3 live consensus smoke scenarios (`docs/deployment_log.json`).
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

- **Blockscout address-history coverage (verified live 2026-09-03):** the
  public `eth.blockscout.com` instance used by the contract does not return
  full address history for every wallet. Several long-established addresses
  (e.g. `0xd8dA...c0Ab`, Binance hot wallets, 1inch router) return
  "No transactions found" and `null` balances, while recent/active addresses
  return complete history. Chain-level data (blocks, recent transactions) is
  live and consistent with Ethereum mainnet. Consequences: (a) Signal A's
  wallet-age metric is computed from the oldest transaction *visible on the
  instance* — for wallets with a coverage gap this underestimates true age;
  (b) a genuinely old wallet may be scored as if it were fresh. Mitigation:
  this is a data-source limitation, not a logic failure — the contract
  honestly scores the data it can verifiably fetch, and the reasoning fields
  always state which evidence the scores were computed from. Smoke tests
  use wallets whose history was manually verified to be present on the same
  instance.
- **First-100-transaction window:** the contract fetches the first 100
  native + 100 token transfers (`sort=asc`) to keep validator fetches
  comparable and payloads bounded. For very high-volume wallets this is a
  sample, not the full history; signals are computed on that sample and
  the record states the window size.
- **Phishing labels are a snapshot:** the Fake_Phishing set is synced
  periodically from the public forta-network labelled-datasets repository;
  addresses newly flagged after the last sync are unknown to the contract
  until the next sync.

## Repository layout

    contracts/   GenLayer Intelligent Contract (Signal A/B + LLM arbiter)
    tests/       gltest direct-mode test suite (fail-safe coverage)
    scripts/     dataset sync, deploy, smoke tests (env-key consumers)
    data/        synced public label datasets (JSON)
    frontend/    dApp (wallet connect, side-by-side signal cards, verdict)
    docs/        SUBMISSION_DRAFT.md and architecture notes
