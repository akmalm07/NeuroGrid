"""Prepare, prove and independently verify the compact Granite projection."""

import argparse
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

from neurogrid.metrics import peak_child


MODEL = ("model.json", "model.commit")
REQUEST = ("scope.json", "activation.commit")
PROVING_KEY = ("rescale.r1cs", "rescale.pk")
KEY_PIN = "rescale.pk.sha256"
OPENINGS = ("left.bin", "right.bin", "raw.bin", "rounded.bin")
RECEIPT = "projection.proof"


def command(binary: Path, operation: str, *jobs: Path, key_pin: str | None = None) -> list[dict]:
    start = time.perf_counter()
    env = os.environ.copy()
    env.pop("NEUROGRID_COMPACT_KEY_PIN", None)
    if key_pin is not None:
        env["NEUROGRID_COMPACT_KEY_PIN"] = key_pin
    with subprocess.Popen([str(binary), operation, *map(str, jobs)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env) as process:
        stdout, stderr = process.communicate()
        peak = peak_child(process)
    if process.returncode:
        message = stderr.strip().splitlines()
        raise ValueError(message[-1] if message else f"{operation} failed with status {process.returncode}")
    reports = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    if len(reports) != len(jobs):
        raise RuntimeError(f"Expected a benchmark report for every {operation} job")
    for report in reports:
        print(json.dumps(report), flush=True)
    print(json.dumps({"phase": f"{operation}_process", "seconds": time.perf_counter() - start,
                      "jobs": len(jobs), **peak}), flush=True)
    return reports


def copy_files(source: Path, target: Path, names: tuple[str, ...]) -> None:
    for name in names:
        shutil.copyfile(source / name, target / name)


def link_files(source: Path, target: Path, names: tuple[str, ...]) -> None:
    for name in names:
        try:
            os.link(source / name, target / name)
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP):
                raise
            shutil.copyfile(source / name, target / name)


def setup(binary: Path, fixture: Path, model: Path) -> None:
    model.mkdir()
    public = model / "public"
    public.mkdir()
    with TemporaryDirectory(dir=model) as path:
        scratch = Path(path)
        copy_files(fixture, scratch, ("scope.json", "right.bin"))
        command(binary, "compact-model", scratch)
        command(binary, "compact-key", scratch)
        private = (*PROVING_KEY, KEY_PIN)
        for name in (*MODEL, "rescale.vk", *private):
            (scratch / name).replace((model if name in private else public) / name)


def issue(binary: Path, model: Path, fixture: Path, request: Path) -> None:
    request.mkdir()
    with TemporaryDirectory() as path:
        scratch = Path(path)
        copy_files(model, scratch, MODEL)
        copy_files(fixture, scratch, ("scope.json", "left.bin"))
        command(binary, "compact-issue", scratch)
        copy_files(scratch, request, REQUEST)


def stage(model: Path, fixture: Path, request: Path, job: Path) -> None:
    job.mkdir()
    copy_files(fixture, job, OPENINGS)
    copy_files(request, job, REQUEST)
    copy_files(model / "public", job, MODEL)
    # Keep the same proving-key inode across requests so one process can cache it.
    link_files(model, job, PROVING_KEY)


def verify(binary: Path, model: Path, request: Path, receipt: Path, output: Path | None = None) -> dict:
    start = time.perf_counter()
    if not 100 <= receipt.stat().st_size < 1 << 20:
        raise ValueError("Invalid compact projection proof size")
    with TemporaryDirectory() as path:
        scratch = Path(path)
        copy_files(model, scratch, (*MODEL, "rescale.vk"))
        copy_files(request, scratch, REQUEST)
        shutil.copyfile(receipt, scratch / RECEIPT)
        if output is not None:
            if not 0 < output.stat().st_size <= 4096 * 8:
                raise ValueError("Invalid returned Q18 tensor size")
            shutil.copyfile(output, scratch / "returned-q18.bin")
        snapshot_seconds = time.perf_counter() - start
        report, = command(binary, "compact-verify", scratch)
    per_request = receipt.stat().st_size + sum((request / item).stat().st_size for item in REQUEST)
    installed = sum((model / item).stat().st_size for item in (*MODEL, "rescale.vk"))
    output_bytes = output.stat().st_size if output is not None else 0
    total = {"phase": "compact_payload", "per_request_bytes": per_request + output_bytes,
             "returned_output_bytes": output_bytes,
             "installed_verifier_data_bytes": installed, "snapshot_seconds": snapshot_seconds,
             "wall_seconds": time.perf_counter() - start}
    print(json.dumps(total), flush=True)
    return report | total


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    for phase, fields in (
        ("setup", ("binary", "fixture", "model")), ("issue", ("binary", "public", "fixture", "request")),
        ("stage", ("model", "fixture", "request", "job")), ("prove", ("binary", "jobs")),
        ("verify", ("binary", "public", "request", "receipt")),
    ):
        sub = phases.add_parser(phase)
        for field in fields:
            if field == "jobs":
                sub.add_argument(field, type=Path, nargs="+")
            else:
                sub.add_argument(field, type=Path)
        if phase == "verify":
            sub.add_argument("--output", type=Path)
        if phase == "prove":
            sub.add_argument("--key-pin-file", type=Path, help="Install-time pin from a trusted setup directory")
    args = parser.parse_args(argv)
    if args.phase == "setup":
        setup(args.binary.resolve(), args.fixture, args.model)
    elif args.phase == "issue":
        issue(args.binary.resolve(), args.public, args.fixture, args.request)
    elif args.phase == "stage":
        stage(args.model, args.fixture, args.request, args.job)
    elif args.phase == "prove":
        pin = args.key_pin_file.read_text(encoding="ascii").strip() if args.key_pin_file else None
        command(args.binary.resolve(), "compact-prove", *[job.resolve() for job in args.jobs], key_pin=pin)
    else:
        verify(args.binary.resolve(), args.public, args.request, args.receipt, args.output)


if __name__ == "__main__":
    main()
