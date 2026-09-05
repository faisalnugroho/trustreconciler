#!/usr/bin/env python3
"""Run ONLY the SW (window-evidence) scenario against the existing v3
contract 0xc88eCa8285929F25e231e0D2c78d1fDfC339EEaF.

SW wallet 0xEF6FD3e9E3E86f2a576E3Fd8Ee872cf1AeEa2E06 was found by
/tmp/hunt7.py: 172 native txs (short page 2 -> full drain under the
3-page window) + 124 token transfers (<200 cap) — the contract MUST
classify it history_coverage='full_window' (steward fix-5 live proof;
the old first-100 contract would have cut it to 'partial_window').

Appends the result to docs/deployment_log.json round3.sw.
"""
import json
import sys
import time
from pathlib import Path

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

ADDR = "0xc88eCa8285929F25e231e0D2c78d1fDfC339EEaF"
SW = "0xEF6FD3e9E3E86f2a576E3Fd8Ee872cf1AeEa2E06"
KEYFILE = Path("scripts/smoke_deployer.json")


def load_account():
    data = json.loads(KEYFILE.read_text())
    return create_account(account_private_key=data["private_key"])


def wait_final(client, tx_hash, label):
    for _ in range(6):
        try:
            receipt = client.wait_for_transaction_receipt(
                transaction_hash=tx_hash,
                status=TransactionStatus.FINALIZED,
                retries=100, interval=3000)
            break
        except Exception as e:
            print(f"  [{label}] rpc hiccup: {str(e)[:80]}", flush=True)
            time.sleep(15)
    else:
        raise RuntimeError(f"{label} rpc failed")
    if isinstance(receipt, dict):
        leader = (receipt.get("consensus_data") or {}).get("leader_receipt", [{}])
        lead = leader[0] if leader else {}
        exec_result = lead.get("execution_result")
        if exec_result is None:
            exec_result = receipt.get("tx_execution_result_name")
        stderr = str((lead.get("genvm_result") or {}).get("stderr") or "")
        return {"execution_result": exec_result, "stderr": stderr,
                "receipt": receipt}
    return {"execution_result": "SUCCESS", "receipt": receipt}


def read_json(client, fn, args):
    raw = client.read_contract(address=ADDR, function_name=fn, args=args)
    return json.loads(raw) if isinstance(raw, str) else raw


def main():
    account = load_account()
    client = create_client(chain=studionet, account=account)
    print("deployer:", account.address, flush=True)

    # pre-check: any existing record / cooldown for SW?
    try:
        prev = read_json(client, "get_reconciliation", [SW])
        print("pre-check: existing record:", json.dumps(prev)[:200], flush=True)
    except Exception as e:
        print("pre-check: no record yet:", str(e)[:100], flush=True)

    t0 = time.time()
    tx = client.write_contract(
        address=ADDR, function_name="request_reconciliation",
        args=[SW, "eth"], account=client.local_account)
    res = wait_final(client, tx, "SW")
    rec = read_json(client, "get_reconciliation", [SW])
    secs = round(time.time() - t0, 1)
    print(f"  -> verdict={rec.get('final_verdict')} "
          f"A={rec.get('signal_a_score')} B={rec.get('signal_b_score')} "
          f"coverage={rec.get('history_coverage')} [{secs}s]", flush=True)
    print(f"     A: {str(rec.get('signal_a_reasoning'))[:140]}", flush=True)

    cov = rec.get("history_coverage")
    entry = {"tx_hash": tx, "wallet": SW, "chain": "eth", "secs": secs,
             "execution_result": res["execution_result"],
             "stderr": res.get("stderr", "")[-1500:],
             "record": rec,
             "full_window": cov == "full_window"}
    print(f"  coverage={cov} full_window={cov == 'full_window'}", flush=True)

    out = json.loads(Path("docs/deployment_log.json").read_text())
    out["round3"]["sw"] = entry
    Path("docs/deployment_log.json").write_text(json.dumps(out, indent=2))
    print("LOGGED to docs/deployment_log.json round3.sw", flush=True)
    print("DONE. tx:", tx, flush=True)
    if cov != "full_window":
        print("!!! EXPECTED full_window — INVESTIGATE", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
