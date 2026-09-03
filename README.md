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

Build in progress — **STOP 1 complete** (contract + 32/32 direct-mode
tests passing, genvm-lint ok). Studionet deployment and frontend follow.

## Secret handling

The Etherscan API key is read exclusively from the `ETHERSCAN_API_KEY`
environment variable — never hardcoded, and never used by the on-chain
contract (the contract uses keyless public endpoints; see "Data sources").
Local development uses a git-ignored `.env` file at the repo root (see
`.env.example`); systemd services use a separate root-owned
`EnvironmentFile=` outside the repo.

## Repository layout

    contracts/   GenLayer Intelligent Contract (Signal A/B + LLM arbiter)
    tests/       gltest direct-mode test suite (fail-safe coverage)
    scripts/     dataset sync, deploy, smoke tests (env-key consumers)
    data/        synced public label datasets (JSON)
    frontend/    dApp (wallet connect, side-by-side signal cards, verdict)
    docs/        SUBMISSION_DRAFT.md and architecture notes
