"""Exercise concurrent holds and release them on an isolated coordinator."""

import argparse
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from neurogrid_node import SHARD_COUNT

from live_check import auth, call, wait_until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("The concurrency check uses an isolated loopback coordinator")
    requester, contributor = (auth(base, item) for item in json.loads(args.accounts.read_text(encoding="utf-8-sig")))

    def account(token):
        status, data, _ = call(base, "/api/grid/user/account", token=token)
        assert status == 200, f"Could not read account ({status})"
        return data

    def idle():
        state = call(base, "/api/grid/public/network")[1]
        ready = {node["shard"] for node in state["nodes"] if node["status"] == "online"}
        return state["verifier"] and state["active"] is None and len(ready) == SHARD_COUNT

    wait_until(idle, "idle Windows Granite network", timeout=900)
    before = account(requester)["credits"]
    worker_ids = {node["id"] for node in account(contributor)["nodes"]}
    online = {node["id"] for node in call(base, "/api/grid/public/network")[1]["nodes"] if node["status"] == "online"}
    assert online and online <= worker_ids, "The test contributor must own all online fixture workers"
    prompt = "Write a short summary of the repeated text below.\n" + ("word " * 3180)
    quotes = []
    for _ in range(4):
        key = str(uuid.uuid4())

        def draft():
            status, quote, _ = call(base, "/api/grid/user/quote", token=requester, data={"prompt": prompt, "key": key})
            assert status == 200 and quote["status"] != "rejected", quote
            return quote if quote["status"] == "ready" else None

        quote = wait_until(draft, "Standard quote")
        assert quote["profile"] == "standard"
        quotes.append(({"key": key, "quoteId": quote["id"]}, quote["credits"]))
    assert sum(quote[1] for quote in quotes) > before, "The test fixture balance must fit fewer than four holds"

    def submit(item):
        packet, cost = item
        status, job, _ = call(base, "/api/grid/user/request", token=requester, data=packet)
        return status, job, cost

    accepted = {}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, quotes))
        accepted = {job["id"]: cost for status, job, cost in results if status == 200}
        denied = [job["error"] for status, job, _cost in results if status != 200]
        assert len(accepted) >= 2 and denied and set(denied) == {"insufficient_credits"}, (len(accepted), denied)
        assert account(requester)["credits"] == before - sum(accepted.values())
        print(f"PASS {len(accepted)} concurrent holds, {len(denied)} overspend denied", flush=True)
    finally:
        for node_id in online:
            call(base, "/api/grid/user/revoke", token=contributor, data={"id": node_id})

    def refunded():
        jobs = {job["id"]: job for job in account(requester)["jobs"]}
        return all(jobs[job]["status"] == "refunded" and jobs[job]["returned"] == amount
                   for job, amount in accepted.items())

    wait_until(refunded, "full worker-loss refunds", timeout=130)
    assert account(requester)["credits"] == before
    blocks = call(base, "/api/grid/public/ledger")[1]["blocks"]
    assert len(blocks) < 100, "Perform this check on a small local ledger"
    for job in accepted:
        assert sum(block["kind"] == "reserve" and block["reference"] == job for block in blocks) == 1
        assert sum(block["kind"] == "refund" and block["reference"] == job for block in blocks) == 1
    print(f"PASS {len(accepted)} full live-job refunds, each recorded once", flush=True)


if __name__ == "__main__":
    main()
