"""Compare two real worker paths via a local relay with a controlled delay."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from live_check import auth, call, stop_child, wait_until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("The measured relay check uses a loopback coordinator")
    fixture = json.loads(args.accounts.read_text(encoding="utf-8-sig"))[1]
    token = auth(base, fixture)
    status, node, _ = call(base, "/api/grid/user/enroll", token=token, data={"shard": 0})
    assert status == 200, f"Enrollment failed ({status})"

    class Relay(BaseHTTPRequestHandler):
        def forward(self):
            if not self.path.startswith("/api/grid/node/"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 710_000:
                self.send_error(413)
                return
            request = Request(base + self.path, method=self.command,
                              data=self.rfile.read(length) if length else None,
                              headers={name: self.headers[name] for name in ("Content-Type", "Authorization")
                                       if name in self.headers})
            time.sleep(1.25)
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as exc:
                response = exc
            with response:
                payload = response.read(1_500_000)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        do_GET = forward
        do_POST = forward

        def log_message(self, *_args):
            pass

    child = None
    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as relay:
        thread = Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "neurogrid_node.client", "node", "--model", str(args.model),
                 "--url", f"http://127.0.0.1:{relay.server_port}", "--threads", "2"],
                env={**os.environ, "NEUROGRID_SECRET": node["secret"]}, stdout=subprocess.DEVNULL,
            )

            def measures():
                assert child.poll() is None, f"The slow check worker exited ({child.returncode})"
                network = call(base, "/api/grid/public/network")[1]
                if not network["verifier"]:
                    return None
                stage = [part for part in network["nodes"] if part["shard"] == 0
                         and part["status"] in {"online", "working"} and "pingMs" in part]
                slow = next((part for part in stage if part["id"] == node["id"]), None)
                direct = [part for part in stage if part["id"] != node["id"]]
                if slow and direct:
                    fast = min(direct, key=lambda part: part["pingMs"])
                    return fast, slow
                return None

            fast, slow = wait_until(measures, "fresh measured paths for real workers", timeout=180)
            assert slow["pingMs"] - fast["pingMs"] >= 700, (fast["pingMs"], slow["pingMs"])
            key = str(uuid.uuid4())

            def quote_ready():
                status, quote, _ = call(base, "/api/grid/user/quote", token=token,
                                        data={"prompt": "Reply with the word Paris only.", "key": key})
                assert status == 200, quote
                assert quote["status"] != "rejected", quote
                return quote if quote["status"] == "ready" else None

            quote = wait_until(quote_ready, "route check quote", timeout=90)
            status, job, _ = call(base, "/api/grid/user/request", token=token,
                                  data={"key": key, "quoteId": quote["id"]})
            assert status == 200, job
            assert job["route"][0] == fast["id"], "The slower path was selected"
            print(f"PASS selected the {fast['pingMs']} ms path over the {slow['pingMs']} ms relay", flush=True)

            def settled():
                account = call(base, "/api/grid/user/account", token=token)[1]
                return next((item for item in account["jobs"] if item["id"] == job["id"]
                             and item["status"] in {"settled", "refunded"}), None)

            result = wait_until(settled, "end of routed Granite request", timeout=240)
            assert result["status"] == "settled" and "Paris" in result["output"], result
            assert 0 < result["charge"] <= quote["credits"], result
            print("PASS measured route produced a real Granite reply at the quoted two-credit charge", flush=True)
        finally:
            if child:
                stop_child(child)
            call(base, "/api/grid/user/revoke", token=token, data={"id": node["id"]})
            relay.shutdown()


if __name__ == "__main__":
    main()
