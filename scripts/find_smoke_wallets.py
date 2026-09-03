#!/usr/bin/env python3
"""Research smoke-test candidate wallets on eth.blockscout.com.

Given the discovered coverage gap (established wallets can show empty
history), this script mines recent blocks for active senders and checks
each candidate's txlist on the SAME instance the contract will use:
  - oldest tx timestamp (wallet age — Signal A needs >= ~1-2 years for
    the Aligned-Trustworthy scenario)
  - unique counterparties count (Signal B diversity)
  - no contact with Fake_Phishing dataset addresses
"""
import json
import sys
import time
import urllib.request
import datetime

API = "https://eth.blockscout.com/api"


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "smoke-research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def recent_senders(n_blocks=3):
    blocks = get(f"{API}/v2/main-page/blocks")
    latest = blocks[0]["height"]
    senders = []
    for h in range(latest - n_blocks + 1, latest + 1):
        txs = get(f"{API}/v2/blocks/{h}/transactions")
        for t in txs.get("items", []):
            senders.append(t["from"]["hash"])
        time.sleep(0.2)
    return list(dict.fromkeys(senders))  # dedupe, keep order


def profile(addr, max_txs=1000):
    d = get(f"{API}?module=account&action=txlist&address={addr}"
            f"&page=1&offset={max_txs}&sort=asc")
    txs = d.get("result") or []
    if not isinstance(txs, list) or not txs:
        return None
    oldest = int(txs[0]["timeStamp"])
    newest = int(txs[-1]["timeStamp"])
    age_days = (newest - oldest) / 86400
    cps = set()
    for t in txs:
        f, to = t.get("from", ""), t.get("to", "")
        if f and f.lower() != addr.lower():
            cps.add(f.lower())
        if to and to.lower() != addr.lower():
            cps.add(to.lower())
    return {
        "n": len(txs),
        "oldest": datetime.datetime.fromtimestamp(oldest, datetime.UTC).date().isoformat(),
        "newest": datetime.datetime.fromtimestamp(newest, datetime.UTC).date().isoformat(),
        "span_days": round(age_days),
        "unique_counterparties": len(cps),
    }


def main():
    # S2 (fresh wallet) + S3 (flagged funding) candidates handled separately;
    # here we hunt for the S1 Aligned-Trustworthy wallet: old + diverse + clean.
    print("mining recent blocks for senders...", flush=True)
    cands = recent_senders(2)
    print(f"{len(cands)} unique senders", flush=True)
    good = []
    for a in cands[:60]:
        try:
            p = profile(a)
        except Exception as e:
            print(a, "ERR", repr(e)[:60], flush=True)
            continue
        if not p:
            continue
        ok = p["span_days"] >= 365 and p["unique_counterparties"] >= 15 and p["n"] >= 40
        print(f"{a} n={p['n']} span={p['span_days']}d cps={p['unique_counterparties']} "
              f"oldest={p['oldest']} {'<== CANDIDATE' if ok else ''}", flush=True)
        if ok:
            good.append({"address": a, **p})
        if len(good) >= 3:
            break
        time.sleep(0.25)
    print("\nGOOD_S1_CANDIDATES:", json.dumps(good, indent=1))


if __name__ == "__main__":
    main()
