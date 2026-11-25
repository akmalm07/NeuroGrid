"""Launch the existing contributor engine with a service-owned profile and key."""

import argparse
import logging
import os
import signal
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event

from .client import GridClient, GridError, run_configured_node
from .profile import ProfileStore


def run_service(
    profile: Path, credential: Path, cache: Path, stop: Event, report: Callable[[str], None],
) -> int:
    store = ProfileStore(profile)
    os.environ["NEUROGRID_MODEL_CACHE"] = str(cache)
    try:
        with store.worker():
            saved = store.read()
            if saved.paused:
                report("Paused by the contributor")
                return 0
            with credential.open("rb") as handle:
                data = handle.read(257)
            if len(data) > 256:
                raise ValueError("Invalid service credential length")
            key = data.decode("ascii").strip()
            if not key.startswith("ng_") or not 1 <= len(key) <= 200:
                raise ValueError("Service enrollment required")
            client = GridClient(saved.site, key)
            run_configured_node(client, stop, store, saved.threads, saved.duty, report=report)
    except GridError as exc:
        if exc.status in (401, 403):
            report("Service enrollment expired or was revoked")
            return 78
        report("Coordinator rejected the service runtime")
        return 1
    except (ValueError, FileNotFoundError):
        report("Cannot read the service profile or enrollment")
        return 78
    except (OSError, RuntimeError):
        report("Contributor service could not start or load its model")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the enrolled boot service")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--credential", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--windows", action="store_true")
    args = parser.parse_args()
    credential = args.credential
    if credential is None:
        credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if not credentials_directory:
            parser.error("No service credential directory was supplied")
        credential = Path(credentials_directory) / "node.key"
    stop = Event()

    def run() -> int:
        if not args.windows:
            return run_service(args.profile, credential, args.cache, stop, lambda message: print(message, flush=True))
        logger = logging.getLogger("neurogrid.contributor")
        logger.setLevel(logging.INFO)
        log = RotatingFileHandler(args.profile.with_name("service.log"), maxBytes=1_048_576,
                                  backupCount=2, encoding="utf-8")
        log.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(log)
        try:
            return run_service(args.profile, credential, args.cache, stop, logger.info)
        finally:
            logger.removeHandler(log)
            log.close()

    if args.windows:
        if os.name != "nt":
            parser.error("The Windows Service Control Manager is unavailable on this OS")
        from .windows_service import dispatch

        raise SystemExit(dispatch(run, stop))
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    raise SystemExit(run())


if __name__ == "__main__":
    main()
