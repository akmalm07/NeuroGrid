"""Outbound HTTP protocol used by volunteer nodes and the trusted verifier."""

import argparse
import json
import os
import time
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import REVISION, Partition, Verifier, checkpoint, configure_cpu


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GridError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.status = status


class GridClient:
    def __init__(self, origin: str, secret: str, role: str = "node"):
        parsed = urlsplit(origin.strip())
        localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (localhost and parsed.scheme == "http")
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Enter the HTTPS site address with no path")
        if not secret.strip():
            raise ValueError("Enter your node key")
        self.base = f"{parsed.scheme}://{parsed.netloc}/api/grid/{role}"
        self.secret = secret.strip()
        self.opener = build_opener(NoRedirect())

    def call(self, action: str, data: dict | None = None) -> dict:
        body = json.dumps({"revision": REVISION, **data}).encode() if data is not None else None
        req = Request(f"{self.base}/{action}", data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.secret}",
        })
        try:
            with self.opener.open(req, timeout=15) as response:
                return json.loads(response.read(900_000))
        except HTTPError as exc:
            try:
                message = json.loads(exc.read(2_000)).get("error", "request_failed")
            except (ValueError, AttributeError):
                message = "request_failed"
            raise GridError(message, exc.code) from None


def run_node(client, stop, threads=2, duty=100, model_path=None, report=print):
    configure_cpu(threads)
    profile = client.call("profile")
    if profile["revision"] != REVISION:
        raise ValueError("This application uses a different checkpoint revision")
    report(f"Loading partition {profile['shard'] + 1} on CPU")
    partition = Partition(model_path or checkpoint(), profile["shard"])
    report(f"Online · hosting partition {profile['shard'] + 1}")
    while not stop.is_set():
        try:
            response = client.call("claim", {})
            work = response.get("work")
            if work and not stop.is_set():
                report(f"Computing · partition {partition.index + 1}")
                start = time.monotonic()
                result = partition.run(work["input"], work["length"])
                compute_time = time.monotonic() - start
                if not stop.is_set():
                    client.call("result", {"lease": work["lease"], **result})
                report(f"Online · hosting partition {partition.index + 1}")
                stop.wait(compute_time * (100 / duty - 1))
            else:
                stop.wait(0.3 if response.get("active") else 5)
        except GridError as exc:
            if exc.status in (401, 403) or str(exc) == "model_mismatch":
                raise
            if str(exc) != "stale_lease":
                report(f"Retrying after {exc}")
                stop.wait(3)
        except (OSError, ValueError, URLError):
            report("Coordinator unreachable · retrying")
            stop.wait(4)
    try:
        client.call("pause", {})
    except (GridError, OSError, URLError):
        pass  # Heartbeat expiration releases the node even when disconnected.


def run_verifier(client, stop, threads=2, model_path=None, report=print):
    configure_cpu(threads)
    report("Loading trusted reference")
    verifier = Verifier(model_path or checkpoint())
    report("Verifier ready")
    while not stop.is_set():
        try:
            response = client.call("claim", {})
            work = response.get("work")
            if work:
                result = verifier.process(work)
                client.call("result", {"task": work["id"], **result})
            else:
                stop.wait(0.18 if response.get("active") else 3)
        except GridError as exc:
            if exc.status in (401, 403) or str(exc) == "model_mismatch":
                raise
            if str(exc) != "stale_verification":
                report(f"Verifier retrying after {exc}")
                stop.wait(2)
        except (OSError, URLError):
            stop.wait(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one NeuroGrid CPU process")
    parser.add_argument("role", choices=["node", "reference"])
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--threads", type=int, choices=range(1, 9), default=2)
    args = parser.parse_args()
    secret = os.environ.get("NEUROGRID_SECRET", "")
    if not secret:
        parser.error("Set NEUROGRID_SECRET in the environment of this process")
    client = GridClient(args.url, secret, args.role)
    try:
        if args.role == "reference":
            run_verifier(client, Event(), threads=args.threads, model_path=args.model)
        else:
            run_node(client, Event(), threads=args.threads, model_path=args.model)
    except KeyboardInterrupt:
        if args.role == "node":
            try:
                client.call("pause", {})
            except (GridError, OSError, URLError):
                pass


if __name__ == "__main__":
    main()
