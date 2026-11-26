"""Outbound HTTP protocol used by volunteer nodes and the trusted verifier."""

import argparse
import json
import os
import random
import signal
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import REVISION, RUNTIME, SHARD_COUNT
from .profile import MAX_THREADS, ProfileStore, WorkerStatus, site_origin

USER_AGENT = "NeuroGrid-Node/0.1"
DEFAULT_IDLE_POLL = 2


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GridError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.status = status


class GridClient:
    def __init__(self, origin: str, secret: str, role: str = "node"):
        origin = site_origin(origin)
        if not secret.strip():
            raise ValueError("Enter your node key")
        self.base = f"{origin}/api/grid/{role}"
        self.secret = secret.strip()
        self.opener = build_opener(NoRedirect())

    def call(self, action: str, data: dict | None = None, *, timeout: float = 15) -> dict:
        body = json.dumps({**data, "revision": REVISION, "runtime": RUNTIME}).encode() if data is not None else None
        req = Request(f"{self.base}/{action}", data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.secret}",
            "User-Agent": USER_AGENT,
        })
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return json.loads(response.read(1_500_000))
        except HTTPError as exc:
            try:
                message = json.loads(exc.read(2_000)).get("error", "request_failed")
            except (ValueError, AttributeError):
                message = "request_failed"
            raise GridError(message, exc.code) from None


def run_node(
    client: GridClient, stop: Event, threads: int = 2, duty: int = 100, model_path: Path | None = None,
    report: Callable[[str], None] = print, *, observe: Callable[[WorkerStatus], None] | None = None,
    idle_poll: int = DEFAULT_IDLE_POLL,
) -> None:
    status = WorkerStatus(threads=threads, duty=duty)
    while not stop.is_set():
        try:
            profile = client.call("profile")
            break
        except GridError as exc:
            if exc.status < 500 and exc.status != 429:
                raise
            report(f"Coordinator unavailable · retrying after {exc}")
        except (OSError, ValueError):
            report("Coordinator unreachable · retrying")
        stop.wait(random.uniform(3, 5))
    else:
        return
    if profile.get("revision") != REVISION or profile.get("runtime") != RUNTIME:
        raise ValueError("This application uses a different checkpoint revision")
    shard = profile.get("shard")
    if type(shard) is not int or not 0 <= shard < SHARD_COUNT:
        raise ValueError("Coordinator returned an invalid partition")
    report(f"Loading partition {shard + 1} on CPU")
    status = replace(status, state="loading", shard=shard)
    if observe:
        observe(status)
    from .model import CacheMiss, Partition, checkpoint, configure_cpu

    actual_threads = configure_cpu(threads)
    if observe:
        status = replace(status, threads=actual_threads)
    partition = Partition(model_path or checkpoint(), shard)
    if not stop.is_set():
        report(f"Online · {profile['id']} · hosting partition {profile['shard'] + 1}")
    while not stop.is_set():
        try:
            response = client.call("claim", {"probe": True})
            work = response.get("work")
            status = replace(status, state="computing" if work else "online", last_heartbeat=int(time.time()))
            if observe:
                observe(status)
            if work and not stop.is_set():
                report(f"Computing · partition {partition.index + 1}")
                start = time.monotonic()
                try:
                    result = partition.run(
                        work["input"], work["length"], position=work["position"], job=work["job"],
                        epoch=work["epoch"], emit=work["emit"],
                    )
                except CacheMiss:
                    result = {"error": "cache_miss"}
                compute_time = time.monotonic() - start
                if not stop.is_set():
                    reply = client.call("result", {"lease": work["lease"], **result})
                    if reply.get("accepted"):
                        report(f"Result submitted · hosting partition {partition.index + 1}")
                    else:
                        report(f"Result rejected · {reply.get('reason', 'request_failed')}")
                stop.wait(compute_time * (100 / duty - 1))
            else:
                probe = response.get("probe")
                if isinstance(probe, str) and not stop.is_set():
                    try:
                        client.call("ping", {"probe": probe}, timeout=2)
                    except (GridError, OSError, ValueError):
                        pass  # An optional latency sample must not interrupt work polls.
                stop.wait(0.15 if response.get("active") else idle_poll)
        except GridError as exc:
            if exc.status in (401, 403) or str(exc) == "model_mismatch":
                raise
            if str(exc) != "stale_lease":
                if observe:
                    observe(replace(status, state="disconnected"))
                report(f"Retrying after {exc}")
                stop.wait(3)
        except (OSError, ValueError):
            if observe:
                observe(replace(status, state="disconnected"))
            report("Coordinator unreachable · retrying")
            stop.wait(random.uniform(3, 5))
    try:
        client.call("pause", {})
    except (GridError, OSError, ValueError):
        pass  # Heartbeat expiration releases the node even when disconnected.


def run_configured_node(
    client: GridClient, stop: Event, store: ProfileStore, threads: int, duty: int,
    model_path: Path | None = None, report: Callable[[str], None] = print, *, idle_poll: int = DEFAULT_IDLE_POLL,
) -> None:
    """Run the same inference loop with a local, redacted runtime snapshot."""
    current = WorkerStatus(threads=threads, duty=duty)
    store.report_worker(current)

    def observe(status: WorkerStatus):
        nonlocal current
        elapsed = (status.last_heartbeat or 0) - (current.last_heartbeat or 0)
        if replace(status, last_heartbeat=current.last_heartbeat) != current or elapsed >= 5:
            try:
                store.report_worker(status)
            except OSError:
                return  # A failed status refresh must not interrupt an active lease.
            current = status

    try:
        run_node(client, stop, threads, duty, model_path, report, observe=observe, idle_poll=idle_poll)
    except GridError as exc:
        observe(replace(current, state="reenrollment_required" if exc.status in (401, 403) else "failed"))
        raise
    except (OSError, RuntimeError, ValueError):
        observe(replace(current, state="failed"))
        raise
    else:
        observe(replace(current, state="stopped"))


def run_verifier(client, stop, threads=2, model_path=None, report=print):
    report("Loading trusted reference")
    from .model import Verifier, checkpoint, configure_cpu

    configure_cpu(threads)
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
                stop.wait(0.08 if response.get("active") else 0.5)
        except GridError as exc:
            if exc.status in (401, 403) or str(exc) == "model_mismatch":
                raise
            if str(exc) != "stale_verification":
                report(f"Verifier retrying after {exc}")
                stop.wait(2)
        except (OSError, ValueError):
            stop.wait(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one NeuroGrid CPU process")
    commands = parser.add_subparsers(dest="role", required=True)
    for role in ("node", "reference"):
        run = commands.add_parser(role)
        run.add_argument("--url", required=role == "reference")
        run.add_argument("--model", type=Path)
        run.add_argument("--threads", type=int, choices=range(1, MAX_THREADS + 1))
        run.add_argument("--duty", type=int, choices=range(1, 101), metavar="PERCENT")
        if role == "node":
            run.add_argument("--profile", type=Path, help="Use this saved contributor profile")
            run.add_argument("--idle-poll", type=int, default=DEFAULT_IDLE_POLL, metavar="SECONDS",
                             help="Worker claim interval while the network is idle (2–30 seconds)")
    config = commands.add_parser("config", help="Read or update saved contributor settings")
    config.add_argument("--profile", type=Path)
    status = commands.add_parser("status", help="Read saved settings and the local worker's status")
    status.add_argument("--profile", type=Path)
    actions = config.add_subparsers(dest="action", required=True)
    actions.add_parser("show")
    edit = actions.add_parser("set")
    edit.add_argument("--url", dest="site")
    edit.add_argument("--threads", type=int)
    edit.add_argument("--duty", type=int, metavar="PERCENT")
    pause = edit.add_mutually_exclusive_group()
    pause.add_argument("--pause", dest="paused", action="store_true", default=None)
    pause.add_argument("--resume", dest="paused", action="store_false")
    args = parser.parse_args()
    if args.role == "node" and not DEFAULT_IDLE_POLL <= args.idle_poll <= 30:
        run = commands.choices["node"]
        run.error("--idle-poll must be between 2 and 30 seconds")
    if args.role in {"config", "status"}:
        store = ProfileStore(args.profile)
        try:
            if args.role == "status":
                saved = store.read()
                running, worker = store.worker_status()
                print(json.dumps({
                    "path": str(store.path), "saved": asdict(saved), "running": running,
                    "last_observed": asdict(worker) if worker else None,
                }, indent=2))
                return
            if args.action == "set":
                changes = {field: getattr(args, field) for field in ("site", "threads", "duty", "paused")
                           if getattr(args, field) is not None}
                if not changes:
                    edit.error("Provide at least one setting")
                saved = store.update(changes)
            else:
                saved = store.read()
        except (OSError, ValueError) as exc:
            parser.exit(1, f"NeuroGrid: {exc}\n")
        print(json.dumps({"path": str(store.path), **asdict(saved)}, indent=2))
        return

    secret = os.environ.get("NEUROGRID_SECRET", "")
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())

    def report(message: str) -> None:
        print(message, flush=True)

    try:
        store = None
        if args.role == "node" and (args.profile is not None or args.url is None):
            store = ProfileStore(args.profile)
        with store.worker() if store else nullcontext():
            saved = store.read() if store else None
            if saved and saved.paused:
                report("Paused in the contributor profile · use config set --resume")
                return
            if not secret:
                parser.error("Set NEUROGRID_SECRET in the environment of this process")
            url = args.url if args.url is not None else (saved.site if saved else "")
            client = GridClient(url, secret, args.role)
            threads = args.threads or (saved.threads if saved else 2)
            duty = args.duty or (saved.duty if saved else 100)
            if args.role == "reference":
                run_verifier(client, stop, threads=threads, model_path=args.model, report=report)
            elif store:
                run_configured_node(client, stop, store, threads, duty, model_path=args.model, report=report,
                                    idle_poll=args.idle_poll)
            else:
                run_node(client, stop, threads=threads, duty=duty, model_path=args.model, report=report,
                         idle_poll=args.idle_poll)
    except (GridError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"NeuroGrid: {exc}\n")


if __name__ == "__main__":
    main()
