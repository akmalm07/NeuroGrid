"""Exercise a dedicated, otherwise idle NeuroGrid deployment using two test accounts."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from neurogrid_node import SHARD_COUNT
from neurogrid_node.client import GridClient, GridError
from neurogrid_node.profile import ProfileStore


def call(base, path, *, data=None, token=None, cookie=None):
    body = None if data is None else json.dumps(data).encode()
    headers = {"Origin": base, "Content-Type": "application/json", "User-Agent": "NeuroGrid-Live-Check/0.1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if cookie:
        headers["Cookie"] = cookie
    request = Request(base + path, data=body, headers=headers)
    try:
        with urlopen(request, timeout=12) as response:
            return response.status, json.load(response), response.headers
    except HTTPError as error:
        try:
            data = json.loads(error.read())
        except ValueError:
            data = {"error": "upstream_response"}
        return error.code, data, error.headers


def auth(base, account):
    status, _, headers = call(base, "/api/auth/sign-in/email", data=account)
    assert status == 200, f"Test sign-in failed ({status})"
    cookie = next(item.split(";", 1)[0] for item in headers.get_all("Set-Cookie", [])
                  if ".session_token=" in item)
    status, result, _ = call(base, "/api/auth/token", data={}, cookie=cookie)
    assert status == 200, f"JWT request failed ({status})"
    return result["token"]


def wait_until(check, label, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.4)
    raise AssertionError(f"Timed out waiting for {label}")


def stop_child(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            process.terminate()
        process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--failover", action="store_true")
    parser.add_argument("--context-only", action="store_true", help="Check a follow-up after the saved Kyoto turn")
    parser.add_argument("--extended", action="store_true", help="Also test longer output and additional chat turns")
    parser.add_argument("--keep-online", action="store_true")
    args = parser.parse_args()
    origin = args.url.rstrip("/")
    requester, contributor = (auth(origin, item) for item in json.loads(args.accounts.read_text(encoding="utf-8-sig")))
    reference_key = os.environ["NEUROGRID_SECRET"].strip()
    children = []
    nodes = []
    profiles = TemporaryDirectory()

    def account(token):
        status, data, _ = call(origin, "/api/grid/user/account", token=token)
        assert status == 200, f"Could not read account ({status})"
        return data

    def enroll(shard):
        status, node, _ = call(origin, "/api/grid/user/enroll", token=contributor, data={"shard": shard})
        assert status == 200, f"Enroll failed ({status})"
        nodes.append(node)
        return node

    def launch(role, secret, shard=None):
        env = {**os.environ, "NEUROGRID_SECRET": secret, "PYTHONUNBUFFERED": "1"}
        command = [sys.executable, "-m", "neurogrid_node.client", role, "--model", str(args.model.resolve())]
        store = None
        if shard is None:
            command += ["--url", origin, "--threads", str(args.threads)]
        else:
            store = ProfileStore(Path(profiles.name) / f"node-{len(children)}.json")
            store.update({"site": origin, "threads": args.threads, "duty": 70})
            command += ["--profile", str(store.path)]
        child = subprocess.Popen(command, env=env)
        children.append(child)
        return child, store

    def network():
        return call(origin, "/api/grid/public/network")[1]

    def ready():
        if any(child.poll() is not None for child in children):
            raise RuntimeError(f"A Granite process exited during startup ({[child.poll() for child in children]})")
        grid = network()
        return grid["verifier"] and {node["shard"] for node in grid["nodes"]
                                    if node["status"] in {"online", "working"}} == set(range(SHARD_COUNT))

    def start(prompt, *, expected_profile="quick"):
        key = str(uuid.uuid4())

        def quote_ready():
            status, quote, _ = call(origin, "/api/grid/user/quote", token=requester, data={"prompt": prompt, "key": key})
            assert status == 200, quote
            assert quote["status"] != "rejected", quote
            return quote if quote["status"] == "ready" else None

        quote = wait_until(quote_ready, "token-counted quote")
        assert quote["profile"] == expected_profile
        packet = {"quoteId": quote["id"], "key": key}
        other, _, _ = call(origin, "/api/grid/user/request", token=contributor, data=packet)
        assert other == 409, "A second account used the quote"
        status, job, _ = call(origin, "/api/grid/user/request", token=requester, data=packet)
        assert status == 200, job
        return job["id"], packet, quote["credits"]

    def finish(job_id, packet, original, price, *, expected=None):
        def terminal():
            return next((job for job in account(requester)["jobs"]
                         if job["id"] == job_id and job["status"] in {"settled", "refunded"}), None)

        job = wait_until(terminal, "Granite reply", timeout=540)
        assert job["status"] == "settled", job
        if price == 2:
            assert job["charge"] == 2, job
        if expected:
            assert expected.lower() in job["output"].lower(), job
        assert job["charge"] + job["returned"] == price
        assert account(requester)["credits"] == original - job["charge"]
        duplicate_status, duplicate, _ = call(origin, "/api/grid/user/request", token=requester, data=packet)
        assert duplicate_status == 200 and duplicate["id"] == job_id
        assert account(requester)["credits"] == original - job["charge"]
        blocks = call(origin, "/api/grid/public/ledger")[1]["blocks"]
        if len(blocks) < 100:
            assert sum(block["reference"] == job_id and block["kind"] == "settle" for block in blocks) == 1
        return job

    try:
        assert call(origin, "/api/grid/user/account")[0] == 401
        launch("reference", reference_key)
        workers = [(node, *launch("node", node["secret"], node["shard"]))
                   for node in (enroll(shard) for shard in range(SHARD_COUNT))]
        wait_until(ready, "four Granite partitions and verifier", timeout=180)
        for node, process, store in workers:
            wait_until(lambda: (status := store.worker_status()[1]) and status.last_heartbeat,
                       "the installed profile's heartbeat")
            running, status = store.worker_status()
            assert running and status and status.last_heartbeat and process.poll() is None
            assert (status.threads, status.duty, status.shard) == (args.threads, 70, node["shard"])
        if not args.context_only:
            start_time = time.monotonic()
            before = account(requester)["credits"]
            job, packet, price = start("What is the capital of France?")
            if args.failover:
                first, process, _ = workers[0]
                wait_until(lambda: any(n["id"] == first["id"] and n["status"] == "working" for n in network()["nodes"]),
                           "first Granite partition lease")
                stop_child(process)
                backup = enroll(first["shard"])
                standby, standby_store = launch("node", backup["secret"], backup["shard"])
                workers[0] = (backup, standby, standby_store)
                GridClient(origin, first["secret"]).call("pause", {})
            france = finish(job, packet, before, price, expected="Paris")
            print(f"PASS real Granite France answer {france['output']!r}, 2 credits once "
                  f"({time.monotonic() - start_time:.1f}s)", flush=True)

            before = account(requester)["credits"]
            job, packet, price = start("What is the capital of Paris?")
            paris = finish(job, packet, before, price, expected="France")
            assert "city" in paris["output"].lower(), paris
            print(f"PASS real Granite Paris clarification {paris['output']!r}, 2 credits once", flush=True)

            before = account(requester)["credits"]
            job, packet, price = start("Remember that my chosen city is Kyoto. Confirm in one sentence.")
            finish(job, packet, before, price)
        before = account(requester)["credits"]
        job, packet, price = start("Which city did I tell you to remember? Reply with the city only.")
        remembered = finish(job, packet, before, price, expected="Kyoto")
        print(f"PASS real Granite follow-up used saved context: {remembered['output']!r}", flush=True)

        if args.extended:
            before = account(requester)["credits"]
            job, packet, price = start("In detail, explain why bees matter to food production in exactly thirty words.",
                                       expected_profile="standard")
            longer = finish(job, packet, before, price)
            assert longer["count"] > 24, longer
            print(f"PASS {longer['count']} generated tokens, charged {longer['charge']} within the Standard quote", flush=True)
        if args.keep_online:
            print("DEMO READY · four Granite workers and reference verifier. Ctrl+C stops them.", flush=True)
            while all(item[1].poll() is None for item in workers):
                time.sleep(3)
    finally:
        for child in children:
            stop_child(child)
        try:
            for node in nodes:
                call(origin, "/api/grid/user/revoke", token=contributor, data={"id": node["id"]})
        finally:
            profiles.cleanup()


if __name__ == "__main__":
    main()
