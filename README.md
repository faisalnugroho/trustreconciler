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

**Honesty note (data sources):** Signal A and Signal B are our own heuristic
models computed from free on-chain data. They are NOT passthroughs of any
paid reputation provider's API (Veritas Protocol, Satoshieye, zScore, etc.),
and this project does not aggregate those commercial APIs.

## Status

Build in progress — **STOP 0 complete** (project scaffold + secret-handling
hygiene). Contract, tests, frontend, and deployment are being added in
subsequent phases.

## Secret handling

The Etherscan API key is read exclusively from the `ETHERSCAN_API_KEY`
environment variable — never hardcoded. Local development uses a git-ignored
`.env` file at the repo root (see `.env.example`); systemd services use a
separate root-owned `EnvironmentFile=` outside the repo.

## Repository layout (planned)

    contracts/   GenLayer Intelligent Contract (Signal A/B + LLM arbiter)
    tests/       gltest direct-mode test suite (fail-safe coverage)
    scripts/     dataset sync, deploy, smoke tests (env-key consumers)
    data/        synced public label datasets (JSON)
    frontend/    dApp (wallet connect, side-by-side signal cards, verdict)
    docs/        SUBMISSION_DRAFT.md and architecture notes
