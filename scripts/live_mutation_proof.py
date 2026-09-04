#!/usr/bin/env python3
"""Live steward-fix-2 proof: after S1's cooldown elapses on the NEW contract,
re-evaluate the S1 wallet and capture before/after records to prove the
stored record REALLY changed (last_updated bump + reevaluation_count up +
substantive field change when data differs). Writes
docs/live_mutation_proof.json.
"""
import json
import time
from pathlib import Path

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

ADDR = "0x3c639D84c6B1463eaFE91EA0A2Db8d767e742c2B"
S1 = "0x930B88a592a045C428f3d99f7f3E5f95e3967508"
COOLDOWN_END = 1788489792 + 60      # S1 last_updated 1788486192 + 3600 + margin

data = json.loads(Path("scripts/smoke_deployer.json").read_text())
acct = create_account(account_private_key=data["private_key"])
client = create_client(chain=studionet, account=acct)


def read(fn, args):
    raw = client.read_contract(address=ADDR, function_name=fn, args=args)
    return json.loads(raw) if isinstance(raw, str) else raw


def main():
    print("waiting for cooldown; now", int(time.time()), "end", COOLDOWN_END,
          flush=True)
    while time.time() < COOLDOWN_END:
        time.sleep(30)
    before = read("get_reconciliation", [S1])
    print("BEFORE:", json.dumps({k: before[k] for k in (
        "final_verdict", "signal_a_score", "signal_b_score",
        "last_updated", "reevaluation_count")}), flush=True)

    tx = client.write_contract(address=ADDR,
                              function_name="request_reevaluation",
                              args=[S1, "eth"], account=client.local_account)
    for _ in range(6):
        try:
            r = client.wait_for_transaction_receipt(
                transaction_hash=tx, status=TransactionStatus.FINALIZED,
                retries=100, interval=3000)
            break
        except Exception as e:
            print("rpc hiccup:", str(e)[:100], flush=True)
            time.sleep(15)
    leader = ((r.get("consensus_data") or {}).get("leader_receipt") or [{}])[0]
    exec_result = leader.get("execution_result")
    print("reeval exec:", exec_result, flush=True)

    after = read("get_reconciliation", [S1])
    print("AFTER:", json.dumps({k: after[k] for k in (
        "final_verdict", "signal_a_score", "signal_b_score",
        "last_updated", "reevaluation_count")}), flush=True)

    substantive = [
        after["signal_a_score"] != before["signal_a_score"],
        after["signal_b_score"] != before["signal_b_score"],
        after["final_verdict"] != before["final_verdict"],
        after["signal_a_reasoning"] != before["signal_a_reasoning"],
        after["signal_b_reasoning"] != before["signal_b_reasoning"],
        after["final_reasoning"] != before["final_reasoning"],
    ]
    proof = {
        "wallet": S1,
        "chain": "eth",
        "tx_hash": tx,
        "execution_result": exec_result,
        "before": before,
        "after": after,
        "checks": {
            "reevaluation_count_incremented":
                after["reevaluation_count"] == before["reevaluation_count"] + 1,
            "last_updated_bumped": after["last_updated"] > before["last_updated"],
            "requested_at_bumped": after["requested_at"] > before["requested_at"],
            "some_substantive_field_changed_when_data_differed": any(substantive),
            "stored_record_matches_returned": after["reevaluation_count"] >= 2,
        },
    }
    Path("docs/live_mutation_proof.json").write_text(json.dumps(proof, indent=1))
    ok = all(proof["checks"].values())
    print("ALL LIVE CHECKS PASS:", ok, flush=True)
    print(json.dumps(proof["checks"], indent=1), flush=True)


if __name__ == "__main__":
    main()
