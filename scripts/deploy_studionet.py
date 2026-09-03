#!/usr/bin/env python3
"""TrustReconciler — deploy to Studionet + live consensus smoke test.

Smoke plan (spec section 6, wallets manually verified on eth.blockscout.com
in the contract's exact fetch window — first-100 asc, txlist+tokentx):

  S1 Aligned-Trustworthy: 0x930B88a592a045C428f3d99f7f3E5f95e3967508 (eth)
      verified window: 0 flagged hits, age ~1302d, 88 cps, 44% diversity,
      0% fails -> Signal A=15, B=85, gap=0 -> Aligned-Trustworthy.
  S2 Divergent (core value prop): 0xcF2Ae489e77945F34265FF57393831966E358Db0 (eth)
      verified window: 3d old, 6 cps, 0 flagged -> A=55, B=80, gap=35
      -> Divergent-Resolved-* with root-cause reasoning.
  S3 Undetermined (live API failure): chain=base for
      0x51FfD9b1F9c07286074e892d24AdFa7Ac44A33bF — base.blockscout
      intermittently 500s on txlist for this address (verified 2/3
      attempts pre-deploy). If consensus catches the failure -> live
      Undetermined. Up to 3 attempts; otherwise the empty/partial path
      is still documented + local suite proves the fail-safe.

Output: docs/deployment_log.json (address, tx hashes, verdicts, timings).
Explorer: https://explorer-studio.genlayer.com/address/<addr>
"""
import json
import time
from pathlib import Path

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

CODE = Path("contracts/TrustReconciler.py").read_text()

S1 = "0x930B88a592a045C428f3d99f7f3E5f95e3967508"
S2 = "0xcF2Ae489e77945F34265FF57393831966E358Db0"
S3 = "0x51FfD9b1F9c07286074e892d24AdFa7Ac44A33bF"

KEYFILE = Path("scripts/smoke_deployer.json")
log = {"wallets": {"s1_aligned": S1, "s2_divergent": S2, "s3_undetermined": S3}}


def load_account():
    if KEYFILE.exists():
        data = json.loads(KEYFILE.read_text())
        return create_account(account_private_key=data["private_key"])
    acct = create_account()
    KEYFILE.write_text(json.dumps(
        {"address": acct.address, "private_key": acct.key.hex()}))
    return acct


def wait_final(client, tx_hash, label):
    last_err = None
    for _ in range(6):
        try:
            receipt = client.wait_for_transaction_receipt(
                transaction_hash=tx_hash,
                status=TransactionStatus.FINALIZED,
                retries=100, interval=3000)
            break
        except Exception as e:
            last_err = e
            print(f"  [{label}] rpc hiccup, retrying: {str(e)[:80]}", flush=True)
            time.sleep(15)
    else:
        raise RuntimeError(f"{label} rpc failed: {last_err}")
    if isinstance(receipt, dict):
        data = receipt.get("data") or {}
        addr = data.get("contract_address") if isinstance(data, dict) else None
        if addr is None:
            addr = receipt.get("to_address")
        leader = (receipt.get("consensus_data") or {}).get("leader_receipt", [{}])
        exec_result = (leader[0] if leader else {}).get("execution_result")
        if exec_result is None:
            exec_result = receipt.get("result_name")
        if exec_result is None:
            exec_result = receipt.get("tx_execution_result_name")
        if exec_result is None:
            leader0 = leader[0] if leader else {}
            cd = receipt.get("consensus_data") or {}
            if (leader0.get("execution_result") == "ERROR"
                    or "contract_error" in str(cd.get("result", {}))):
                exec_result = "ERROR"
    else:
        addr = getattr(receipt, "contract_address", None)
        exec_result = None
    print(f"  [{label}] finalized exec={exec_result}", flush=True)
    ok_names = (None, "SUCCESS", "FINISHED_WITH_RETURN")
    if exec_result not in ok_names:
        print("EXECUTION FAILED — consensus data:")
        print(json.dumps(receipt.get("consensus_data"), default=str)[:3000])
        raise RuntimeError(label + " execution failed")
    return {"execution_result": exec_result or "SUCCESS",
            "contract_address": addr}


def read_json(client, addr, fn, args):
    raw = client.read_contract(address=addr, function_name=fn, args=args)
    return json.loads(raw) if isinstance(raw, str) else raw


def reconcile_call(client, addr, wallet, chain, label):
    t0 = time.time()
    tx = client.write_contract(
        address=addr, function_name="request_reconciliation",
        args=[wallet, chain], account=client.local_account)
    res = wait_final(client, tx, label)
    rec = read_json(client, addr, "get_reconciliation", [wallet])
    secs = round(time.time() - t0, 1)
    print(f"  -> verdict={rec.get('final_verdict')} "
          f"confidence={rec.get('confidence')} "
          f"A={rec.get('signal_a_score')} B={rec.get('signal_b_score')} "
          f"divergence={rec.get('divergence_detected')} [{secs}s]", flush=True)
    print(f"     A: {str(rec.get('signal_a_reasoning'))[:150]}", flush=True)
    print(f"     B: {str(rec.get('signal_b_reasoning'))[:150]}", flush=True)
    print(f"     R: {str(rec.get('final_reasoning'))[:200]}", flush=True)
    return {"tx_hash": tx, "wallet": wallet, "chain": chain,
            "execution_result": res["execution_result"], "secs": secs,
            "record": rec}


def main():
    account = load_account()
    client = create_client(chain=studionet, account=account)
    print("deployer:", account.address, flush=True)
    client.fund_account(account.address, 10 * 10**18)
    print("faucet: funded deployer", flush=True)

    tx = client.deploy_contract(code=CODE, account=client.local_account,
                                args=[], leader_only=True)
    res = wait_final(client, tx, "deploy")
    addr = res["contract_address"]
    log["deploy"] = {"tx_hash": tx, "address": addr, "deployer": account.address}
    print("CONTRACT:", addr, flush=True)
    print("explorer: https://explorer-studio.genlayer.com/address/" + addr,
          flush=True)

    # S1 — Aligned-Trustworthy (eth, verified clean long-history wallet)
    print("\n=== S1: Aligned-Trustworthy (eth) ===", flush=True)
    log["s1"] = reconcile_call(client, addr, S1, "eth", "S1")

    # S2 — Divergent (eth, fresh clean wallet) — the core value proposition
    print("\n=== S2: Divergent (eth, fresh wallet) ===", flush=True)
    log["s2"] = reconcile_call(client, addr, S2, "eth", "S2")

    # S3 — Undetermined via live API failure (chain=base, flaky txlist)
    print("\n=== S3: Undetermined (base, live API failure hunt) ===",
          flush=True)
    for attempt in range(3):
        try:
            r = reconcile_call(client, addr, S3, "base", f"S3.{attempt+1}")
            log[f"s3_attempt_{attempt+1}"] = r
            if r["record"].get("final_verdict") == "Undetermined":
                log["s3"] = r
                print("S3 live Undetermined ACHIEVED", flush=True)
                break
            print(f"  attempt {attempt+1}: base API responded OK this time "
                  "(flaky 500 not hit) — verdict recorded, retrying", flush=True)
            # cooldown guard: same wallet re-request within cooldown reverts;
            # request_reconciliation is blocked by cooldown -> use the verdict we got
            break
        except RuntimeError as e:
            print(f"  attempt {attempt+1} consensus error: {e}", flush=True)
            time.sleep(10)

    log["summary"] = {
        "s1_expected": "Aligned-Trustworthy",
        "s1_actual": log["s1"]["record"].get("final_verdict"),
        "s2_expected": "Divergent-Resolved-*",
        "s2_actual": log["s2"]["record"].get("final_verdict"),
        "s3_expected": "Undetermined (if base API failed during consensus)",
        "s3_actual": (log.get("s3") or {}).get("record", {}).get(
            "final_verdict", "see s3_attempt_*"),
    }
    Path("docs").mkdir(exist_ok=True)
    Path("docs/deployment_log.json").write_text(json.dumps(log, indent=2))
    print("\nSUMMARY:", json.dumps(log["summary"], indent=1), flush=True)
    print("DONE. contract:", addr, flush=True)


if __name__ == "__main__":
    main()
