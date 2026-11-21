"""Exercise a dedicated, otherwise idle NeuroGrid deployment using two test accounts."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from neurogrid_node.client import GridClient, GridError


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--keep-online", action="store_true", help="Keep the three workers and verifier running after checks")
    args = parser.parse_args()
    origin = args.url.rstrip("/")
    requester, worker = (auth(origin, item) for item in json.loads(args.accounts.read_text(encoding="utf-8-sig")))
    reference_key = os.environ["NEUROGRID_SECRET"].strip()
    children = []
    keys = []

    def account(token):
        status, data, _ = call(origin, "/api/grid/user/account", token=token)
        assert status == 200, f"Could not read account ({status})"
        return data

    def run_agent(role, key):
        env = {**os.environ, "NEUROGRID_SECRET": key, "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen([sys.executable, "-m", "neurogrid_node.client", role,
                                    "--url", origin, "--model", str(args.model.resolve())], env=env)
        children.append(process)
        return process

    def enroll(shard):
        status, node, _ = call(origin, "/api/grid/user/enroll", token=worker, data={"shard": shard})
        assert status == 200, f"Enroll failed: {node}"
        keys.append(node)
        return node

    def network():
        return call(origin, "/api/grid/public/network")[1]

    def start():
        request = {"prompt": "What is 2 + 2?", "maxTokens": 4, "key": str(uuid.uuid4())}
        status, job, _ = call(origin, "/api/grid/user/request", data=request, token=requester)
        assert status == 200, job
        return job["id"], request

    def job_state(job_id, state):
        return next((job for job in account(requester)["jobs"] if job["id"] == job_id and job["status"] == state), None)

    def ready():
        status = network()
        return status["verifier"] and {node["shard"] for node in status["nodes"]
                                       if node["status"] in ["online", "working"]} == {0, 1, 2}

    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=12)

    try:
        assert call(origin, "/api/grid/user/account")[0] == 401
        initial_requester, initial_worker = account(requester)["credits"], account(worker)["credits"]
        assert initial_requester >= 39, "The test requester needs at least 39 credits"
        run_agent("reference", reference_key)
        left, middle, right = (enroll(shard) for shard in range(3))
        node_b, node_c = (run_agent("node", node["secret"]) for node in (middle, right))
        dormant = GridClient(origin, left["secret"])
        dormant.call("claim", {})
        wait_until(ready, "three partitions and verifier")
        assert call(origin, "/api/grid/user/revoke", token=requester, data={"id": left["id"]})[0] == 404
        print("PASS · authentication and node ownership", flush=True)

        # The first participant claims work and disappears without pausing.
        failover_start = time.monotonic()
        job_id, request = start()
        duplicate_status, duplicate_job, _ = call(origin, "/api/grid/user/request", data=request, token=requester)
        assert duplicate_status == 200 and duplicate_job["id"] == job_id
        competing = {**request, "key": str(uuid.uuid4())}
        assert call(origin, "/api/grid/user/request", data=competing, token=requester)[1]["error"] == "network_busy"
        work = wait_until(lambda: dormant.call("claim", {}).get("work"), "first-stage lease")
        backup = enroll(0)
        node_a = run_agent("node", backup["secret"])
        completed = wait_until(lambda: job_state(job_id, "settled"), "verified failover settlement")
        assert completed["count"] == 4 and completed["charge"] == 13
        assert account(requester)["credits"] == initial_requester - 13
        assert account(worker)["credits"] == initial_worker + 12
        try:
            dormant.call("result", {"lease": work["lease"], "tensor": "bad"})
            raise AssertionError("Stale lease accepted")
        except GridError as error:
            assert str(error) == "stale_lease"
        repeated = call(origin, "/api/grid/user/request", data=request, token=requester)[1]
        assert repeated["id"] == job_id and account(worker)["credits"] == initial_worker + 12
        print(f"PASS · disconnected worker replaced, four tokens settled once in {time.monotonic() - failover_start:.1f}s; "
              f"reply {completed['output']!r}", flush=True)

        stop(node_a)
        GridClient(origin, backup["secret"]).call("pause", {})
        dormant.call("claim", {})
        wait_until(ready, "capacity before refund test")
        refund_start = time.monotonic()
        refund_id, _ = start()
        work = wait_until(lambda: dormant.call("claim", {}).get("work"), "lease to reject")
        bad = dormant.call("result", {"lease": work["lease"], "tensor": "bad"})
        assert bad == {"accepted": False, "reason": "malformed_result"}, bad
        refunded = wait_until(lambda: job_state(refund_id, "refunded"), "deadline refund")
        assert refunded["reason"] == "partition_unavailable"
        assert account(requester)["credits"] == initial_requester - 13
        assert account(worker)["credits"] == initial_worker + 12
        blocks = call(origin, "/api/grid/public/ledger")[1]["blocks"]
        assert sum(block["reference"] == job_id and block["kind"] == "settle" for block in blocks) == 1
        assert sum(block["reference"] == refund_id and block["kind"] == "refund" for block in blocks) == 1
        print(f"PASS · malformed activation rejected and credits refunded once in {time.monotonic() - refund_start:.1f}s", flush=True)

        if args.keep_online:
            dormant.call("pause", {})
            run_agent("node", backup["secret"])
            wait_until(ready, "demo capacity")
            print("DEMO READY · 3 worker processes and the verifier on this host. Ctrl+C pauses them.", flush=True)
            while all(process.poll() is None for process in (node_b, node_c)):
                time.sleep(5)
    finally:
        for process in children:
            stop(process)
        for key in keys:
            call(origin, "/api/grid/user/revoke", token=worker, data={"id": key["id"]})


if __name__ == "__main__":
    main()
