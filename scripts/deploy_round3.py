#!/usr/bin/env python3
"""TrustReconciler v3 deploy + round-3 smoke (steward fixes 5-6).

Deploys the paginated-window contract and runs, with full consensus:
  - S1 established clean (eth, >=300 txs -> partial_window under new
    window — same honest classification as v2)
  - S2 fresh clean divergent (eth, <window -> full_window)
  - S4 chain-flip proof: re-eval of a base-pinned wallet with chain=eth
    sent -> contract revert chain_mismatch (regression proof of the
    chain-pinning logic under the new fetch code)
  - SW window-evidence wallet (100 < txs < 300, tokentx < 200): must
    return history_coverage='full_window' — THE steward fix-5 live
    proof (the old 100-tx contract would have cut it to
    'partial_window').
Appends to docs/deployment_log.json under key "round3".
"""
import json
import sys
import time
from pathlib import Path

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

CODE = Path("contracts/TrustReconciler.py").read_text()
S1 = "0x930B88a592a045C428f3d99f7f3E5f95e3967508"
S2 = "0xcF2Ae489e77945F34265FF57393831966E358Db0"
S3 = "0x51FfD9b1F9c07286074e892d24AdFa7Ac44A33bF"  # base-pinned wallet

KEYFILE = Path("scripts/smoke_deployer.json")
SW = None  # window-evidence wallet, filled from CLI arg


def load_account():
    if KEYFILE.exists():
        data = json.loads(KEYFILE.read_text())
        return create_account(account_private_key=data["private_key"])
    raise SystemExit("no smoke_deployer.json — run scripts/deploy_studionet.py first")


def wait_final(client, tx_hash, label, allow_error=False):
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
        ok = exec_result in (None, "SUCCESS", "FINISHED_WITH_RETURN")
        if allow_error:
            return {"execution_result": exec_result, "stderr": stderr,
                    "receipt": receipt}
        if not ok:
            print("EXECUTION FAILED:")
            print(json.dumps(receipt.get("consensus_data"), default=str)[:2500])
            raise RuntimeError(label + " execution failed")
        return {"execution_result": exec_result or "SUCCESS", "receipt": receipt}
    return {"execution_result": "SUCCESS", "receipt": receipt}


def read_json(client, addr, fn, args):
    raw = client.read_contract(address=addr, function_name=fn, args=args)
    return json.loads(raw) if isinstance(raw, str) else raw


def reconcile_call(client, addr, wallet, chain, label, allow_error=False):
    t0 = time.time()
    tx = client.write_contract(
        address=addr, function_name="request_reconciliation",
        args=[wallet, chain], account=client.local_account)
    res = wait_final(client, tx, label, allow_error=allow_error)
    rec = read_json(client, addr, "get_reconciliation", [wallet])
    secs = round(time.time() - t0, 1)
    print(f"  -> verdict={rec.get('final_verdict')} "
          f"A={rec.get('signal_a_score')} B={rec.get('signal_b_score')} "
          f"coverage={rec.get('history_coverage')} [{secs}s]", flush=True)
    print(f"     A: {str(rec.get('signal_a_reasoning'))[:140]}", flush=True)
    # stderr stored in FULL: revert reasons (AssertionError: …) live at the
    # END of a ~900-char runner traceback — a [:400] prefix cut hides them
    # (s4_flip round-3 lesson: revert was present, log said "False").
    return {"tx_hash": tx, "wallet": wallet, "chain": chain, "secs": secs,
            "execution_result": res["execution_result"],
            "stderr": res.get("stderr", "")[-1500:], "record": rec}


def reeval_call(client, addr, wallet, chain, label, allow_error=False):
    t0 = time.time()
    tx = client.write_contract(
        address=addr, function_name="request_reevaluation",
        args=[wallet, chain], account=client.local_account)
    res = wait_final(client, tx, label, allow_error=allow_error)
    secs = round(time.time() - t0, 1)
    # stderr TAIL, not prefix: revert reasons live at the end of the
    # runner traceback (round-3 s4_flip lesson).
    return {"tx_hash": tx, "wallet": wallet, "chain": chain, "secs": secs,
            "execution_result": res["execution_result"],
            "stderr": res.get("stderr", "")[-1500:]}


def main():
    global SW
    if len(sys.argv) > 1:
        SW = sys.argv[1]
    account = load_account()
    client = create_client(chain=studionet, account=account)
    print("deployer:", account.address, flush=True)

    # dataset pin: same committed dataset
    import subprocess as sp
    pin_commit = sp.run(
        ["git", "log", "-1", "--format=%H", "--", "data/phishing_labels.json"],
        capture_output=True, text=True, check=True).stdout.strip()
    sys.path.insert(0, "tests")
    import vendor_keccak
    pin_keccak = vendor_keccak.Keccak256(
        Path("data/phishing_labels.json").read_bytes()).hexdigest()
    print(f"pin: {pin_commit[:12]}:{pin_keccak[:16]}", flush=True)

    tx = client.deploy_contract(code=CODE, account=client.local_account,
                                args=[pin_commit, pin_keccak],
                                leader_only=True)
    res = wait_final(client, tx, "deploy")
    addr = res["receipt"].get("data", {}).get("contract_address") \
        or res["receipt"].get("to_address")
    pin = read_json(client, addr, "get_dataset_pin", [])
    print("CONTRACT v3:", addr, flush=True)
    print("pin on-chain:", json.dumps(pin), flush=True)
    log = {"deploy": {"tx_hash": tx, "address": addr, "dataset_pin": pin}}

    print("\n=== S1: established clean (eth, >=300 txs -> partial) ===",
          flush=True)
    log["s1"] = reconcile_call(client, addr, S1, "eth", "S1")

    print("\n=== S2: fresh clean divergent (eth, full_window) ===",
          flush=True)
    log["s2"] = reconcile_call(client, addr, S2, "eth", "S2")

    print("\n=== S4: chain-flip regression (base-pinned wallet, "
          "send chain=eth) ===", flush=True)
    # seed a base record if none: the S3 wallet already has a base record
    # only on the OLD contract; the v3 storage is fresh, so seed one.
    try:
        seed = reconcile_call(client, addr, S3, "base", "S4-seed",
                              allow_error=True)
        log["s4_seed"] = seed
        print(f"  seed verdict={seed['record'].get('final_verdict')} "
              f"(any verdict family is fine — the record pins chain=base)",
              flush=True)
    except Exception as e:
        print("  seed failed:", str(e)[:200], flush=True)
        log["s4_seed"] = {"error": str(e)[:300]}
    # now flip: re-eval with chain=eth MUST revert chain_mismatch
    flip = reeval_call(client, addr, S3, "eth", "S4-flip", allow_error=True)
    log["s4_flip"] = flip
    ok = "chain_mismatch" in flip["stderr"] or \
        "chain_mismatch" in json.dumps(flip)
    print(f"  chain-flip revert seen: {ok} "
          f"(exec={flip['execution_result']})", flush=True)
    log["s4_flip"]["chain_mismatch_reverted"] = ok

    if SW:
        print(f"\n=== SW: window-evidence wallet {SW} "
              f"(100<txs<300 -> MUST be full_window) ===", flush=True)
        log["sw"] = reconcile_call(client, addr, SW, "eth", "SW")
        cov = (log["sw"].get("record") or {}).get("history_coverage")
        log["sw"]["full_window"] = cov == "full_window"
        print(f"  coverage={cov} full_window={cov == 'full_window'}",
              flush=True)
    else:
        print("\n(no window-evidence wallet passed — SW skipped)", flush=True)

    out = json.loads(Path("docs/deployment_log.json").read_text())
    out["round3"] = log
    Path("docs/deployment_log.json").write_text(json.dumps(out, indent=2))
    print("\nSUMMARY:")
    print(" S1:", log["s1"]["record"].get("final_verdict"),
          log["s1"]["record"].get("history_coverage"))
    print(" S2:", log["s2"]["record"].get("final_verdict"),
          log["s2"]["record"].get("history_coverage"))
    print(" S4 chain_mismatch:", log["s4_flip"].get(
        "chain_mismatch_reverted"))
    if SW:
        print(" SW full_window:", log["sw"].get("full_window"))
    print("DONE. contract:", addr, flush=True)


if __name__ == "__main__":
    main()
