#!/usr/bin/env python3
"""Find the window-evidence smoke wallet (steward fix 5).

Target: a REAL mainnet wallet whose FULL visible history on
eth.blockscout.com is BETWEEN the old window (100 txs) and the new one
(300 native txs) — under the new paginated window it must classify
history_coverage='full_window' where the old contract would have
truncated it to 'partial_window'.

Protocol (per the wallet-selection skill rule):
1. mine recent-block senders (blocks ~3-30 days back),
2. profile each candidate in the contract's EXACT fetch plan:
   txlist pages 1-3 x 100 asc + tokentx pages 1-2 x 100 asc,
3. require: 100 < total txs <= 300 (all pages), tokentx total <= 200,
   0 flagged hits across BOTH lists' counterparties, no coverage gap,
   age > 60 days (established), some diversity (>=10 unique cps).
Sleep 10s between probes (base/eth blockscout rate limits).
"""
import json
import time
import urllib.request

API = "https://eth.blockscout.com/api"
V2 = "https://eth.blockscout.com/api/v2"
LABELS = json.load(open("data/phishing_labels.json"))
FLAGGED = set(a.lower() for a in LABELS["addresses"])
print(f"labels loaded: {len(FLAGGED)}")


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "tr-hunt/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def contract_window(addr):
    """Replicate the contract's EXACT fetch plan; return profile dict."""
    txs = []
    for page in (1, 2, 3):
        d = get(f"{API}?module=account&action=txlist&address={addr}"
                f"&page={page}&offset=100&sort=asc")
        res = d.get("result")
        if not isinstance(res, list):
            return None
        txs.extend(res)
        if len(res) < 100:
            break
    ttxs = []
    for page in (1, 2):
        d = get(f"{API}?module=account&action=tokentx&address={addr}"
                f"&page={page}&offset=100&sort=asc")
        res = d.get("result")
        if not isinstance(res, list):
            return None
        ttxs.extend(res)
        if len(res) < 100:
            break
    wl = addr.lower()
    cps = set()
    funders = set()
    ts_list = []
    fails = 0
    for t in txs:
        try:
            f = str(t.get("from") or "").lower()
            to = str(t.get("to") or "").lower()
            ts = int(t.get("timeStamp", 0))
            err = str(t.get("isError", "0"))
        except Exception:
            continue
        if ts > 0:
            ts_list.append(ts)
        if f and f != wl:
            cps.add(f)
            if to == wl:
                funders.add(f)
        if to and to != wl:
            cps.add(to)
        if err == "1":
            fails += 1
    for t in ttxs:
        try:
            f = str(t.get("from") or "").lower()
            to = str(t.get("to") or "").lower()
        except Exception:
            continue
        if f and f != wl:
            cps.add(f)
        if to and to != wl:
            cps.add(to)
    flagged_cp = len(cps & FLAGGED)
    flagged_fund = len(funders & FLAGGED)
    first_ts = min(ts_list) if ts_list else 0
    return {
        "n_txs": len(txs), "n_ttxs": len(ttxs), "unique_cps": len(cps),
        "flagged_cp": flagged_cp, "flagged_funder": flagged_fund,
        "age_days": (time.time() - first_ts) / 86400 if first_ts else 0,
        "fail_pct": int(fails * 100 / len(txs)) if txs else 0,
        "full_page3": len(txs) >= 300,
        "ttx_capped": len(ttxs) >= 200,
    }


def mine_candidates(n_blocks=6, days_back=25):
    # latest height (v2 endpoint returns a bare list)
    mp = get(f"{V2}/main-page/blocks")
    items = mp if isinstance(mp, list) else (mp.get("items") or [])
    if not items or not isinstance(items[0], dict):
        return []
    latest = items[0].get("height")
    # ~12s/block -> 25 days back
    target_h = latest - int(days_back * 86400 / 12)
    cands = []
    for h in range(target_h, target_h + n_blocks):
        try:
            b = get(f"{V2}/blocks/{h}/transactions")
        except Exception:
            continue
        for t in (b.get("items") or []):
            f = t.get("from", {})
            fh = f.get("hash") if isinstance(f, dict) else None
            if fh:
                cands.append(fh)
        time.sleep(2)
    return list(dict.fromkeys(cands))


def main():
    cands = mine_candidates()
    print(f"mined {len(cands)} candidate senders; profiling in the "
          f"contract's exact window…")
    winners = []
    for i, addr in enumerate(cands[:40]):
        try:
            p = contract_window(addr)
        except Exception as e:
            print(f"  [{i}] {addr[:12]}… probe error {str(e)[:60]}")
            time.sleep(10)
            continue
        if p is None:
            print(f"  [{i}] {addr[:12]}… coverage-gap/None")
            time.sleep(10)
            continue
        print(f"  [{i}] {addr[:12]}… txs={p['n_txs']} ttxs={p['n_ttxs']} "
              f"cps={p['unique_cps']} flag={p['flagged_cp']}/"
              f"{p['flagged_funder']} age={p['age_days']:.0f}d "
              f"fail={p['fail_pct']}%")
        if (100 < p["n_txs"] <= 300 and not p["full_page3"]
                and p["n_ttxs"] < 200 and p["flagged_cp"] == 0
                and p["flagged_funder"] == 0 and p["age_days"] > 60
                and p["unique_cps"] >= 10):
            winners.append((addr, p))
            print(f"  *** WINNER: {addr}")
            if len(winners) >= 2:
                break
        time.sleep(10)
    print(json.dumps({"winners": [(a, p) for a, p in winners]}, indent=1))


if __name__ == "__main__":
    main()
