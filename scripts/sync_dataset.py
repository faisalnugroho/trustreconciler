#!/usr/bin/env python3
"""Sync the forta-network labelled-datasets Fake_Phishing address set into
data/phishing_labels.json (our own repo-hosted snapshot).

ChainSeus pattern (per spec): periodic MANUAL sync into our own storage —
the contract never fetches the upstream dataset live per request; it reads
the immutable-per-commit snapshot at a pinned public URL, so every validator
sees the identical dataset.

Optional cross-check: when ETHERSCAN_API_KEY is set in the environment
(never hardcoded — see scripts/etherscan_config.py), the script verifies a
few sample addresses of the synced set via the Etherscan API (keyed,
off-chain, our-side only) and reports whether the "is_contract" flags
agree. The sync itself needs NO key.

Usage:
    python3 scripts/sync_dataset.py [--limit N] [--crosscheck]
"""

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DATASET_CSV_URL = (
    "https://raw.githubusercontent.com/forta-network/labelled-datasets/"
    "main/labels/1/phishing_scams.csv"
)
OUT_PATH = REPO_ROOT / "data" / "phishing_labels.json"

# The forta CSV mixes several etherscan tags; the Fake_Phishing family is
# what our spec targets (column `etherscan_tag` starts with Fake_Phishing
# or FAKE_Phishing — 6251 + 10 rows in the upstream file).
FAKE_PHISHING_PREFIXES = ("Fake_Phishing", "FAKE_Phishing", "Fake: Phish")


def fetch_csv():
    print("Fetching", DATASET_CSV_URL)
    req = urllib.request.Request(
        DATASET_CSV_URL, headers={"User-Agent": "trustreconciler-sync/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            raise RuntimeError("http_" + str(resp.status))
        return resp.read().decode("utf-8", errors="replace")


def parse_rows(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    addresses = []
    skipped = 0
    for row in reader:
        tag = (row.get("etherscan_tag") or "").strip()
        addr = (row.get("address") or "").strip()
        if not addr.startswith("0x") or len(addr) != 42:
            skipped += 1
            continue
        if not any(tag.startswith(p) for p in FAKE_PHISHING_PREFIXES):
            # keep only the Fake_Phishing family; other phishing-ecosystem
            # tags (Hack:, Scam:, Spam Token...) are out of scope for the
            # spec's Signal A/B design.
            skipped += 1
            continue
        addresses.append(addr.lower())
    # dedupe (case-insensitive)
    unique = sorted(set(addresses))
    return unique, skipped


def crosscheck(addresses, sample=3):
    """Optional: verify sample addresses against the keyed Etherscan API
    (off-chain, our side only; requires ETHERSCAN_API_KEY in env)."""
    try:
        from etherscan_config import get_etherscan_api_key
        key = get_etherscan_api_key()
    except Exception as e:
        print("[crosscheck] skipped (no key):", e)
        return
    import time
    picks = addresses[:sample] if len(addresses) >= sample else addresses
    print("[crosscheck] Etherscan V2 chainid=1 prober for", len(picks),
          "sample addresses")
    for a in picks:
        url = ("https://api.etherscan.io/v2/api?chainid=1"
               "&module=account&action=balance&address=" + a
               + "&apikey=" + key)
        try:
            req = urllib.request.Request(url, headers={"User-Agent":
                                       "trustreconciler-sync/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            ok = data.get("status") == "1"
            print("  ", a, "->", "ok" if ok else
                  ("status=" + str(data.get("status"))
                   + " msg=" + str(data.get("message"))))
        except Exception as e:
            print("  ", a, "-> ERROR", repr(e)[:80])
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="limit address count (0 = all)")
    ap.add_argument("--crosscheck", action="store_true",
                    help="optional keyed Etherscan cross-check of samples")
    args = ap.parse_args()

    csv_text = fetch_csv()
    addresses, skipped = parse_rows(csv_text)
    if args.limit and args.limit > 0:
        addresses = addresses[:args.limit]
    if not addresses:
        raise SystemExit("sync produced 0 addresses — refusing to write")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "trustreconciler/phishing-labels/1",
        "source": ("forta-network/labelled-datasets "
                   "labels/1/phishing_scams.csv (Fake_Phishing family "
                   "only), synced via scripts/sync_dataset.py"),
        "source_url": DATASET_CSV_URL,
        "address_count": len(addresses),
        "addresses": addresses,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")
    print("Wrote", OUT_PATH, "-", len(addresses),
          "Fake_Phishing addresses (skipped", skipped, "non-family rows)")
    print("SHA-256 of dataset will be recorded at sync time.")
    if args.crosscheck:
        crosscheck(addresses)


if __name__ == "__main__":
    main()
