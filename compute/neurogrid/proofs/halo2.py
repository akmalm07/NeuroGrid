"""Setup, prove, and verify a benchmark EZKL circuit in separate processes."""

import argparse
import json
import os
import re
import time
from math import prod
from pathlib import Path

import ezkl

from neurogrid.metrics import peak_memory
from neurogrid.serialization import read_json


def public_fields(settings: dict, claim: dict) -> list[str]:
    args = settings["run_args"]
    if (args["input_visibility"], args["output_visibility"], args["param_visibility"], args["check_mode"]) != (
            "Public", "Public", "Fixed", "SAFE"):
        raise ValueError("Benchmark requires fixed weights and safe checks on public inputs and outputs")
    if set(claim) != {"inputs", "outputs"}:
        raise ValueError("Expected a claim with inputs and outputs")
    for kind in ("input", "output"):
        tensors = claim[f"{kind}s"]
        if not isinstance(tensors, list) or len(tensors) != len(settings[f"model_{kind}_scales"]):
            raise ValueError(f"Unexpected number of claimed {kind} tensors")
    fields = []
    for tensor, shape in zip(claim["inputs"] + claim["outputs"], settings["model_instance_shapes"], strict=True):
        if not isinstance(tensor, list) or len(tensor) != prod(shape):
            raise ValueError("Claimed tensor size differs from the trusted circuit settings")
        if any(not isinstance(field, str) or not re.fullmatch(r"[0-9a-f]{64}", field) for field in tensor):
            raise ValueError("Invalid field encoding in the claim")
        fields.extend(tensor)
    return fields


def verify_proof(proof: Path, settings: Path, vk: Path, srs: Path, claim: Path) -> None:
    expected = public_fields(read_json(settings), read_json(claim))
    if read_json(proof)["instances"] != [expected]:
        raise ValueError("Proof public inputs or outputs differ from the independently supplied claim")
    try:
        verified = ezkl.verify(str(proof), str(settings), str(vk), str(srs))
    except RuntimeError as error:
        raise ValueError("EZKL rejected the cryptographic proof") from error
    if not verified:
        raise ValueError("EZKL rejected the cryptographic proof")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    prove = commands.add_parser("prove")
    verify = commands.add_parser("verify")
    for command, names in (
        (setup, ("circuit", "witness", "srs", "pk", "vk")),
        (prove, ("circuit", "witness", "srs", "pk", "proof")),
        (verify, ("proof", "settings", "vk", "srs", "claim")),
    ):
        for name in names:
            command.add_argument(name, type=Path)
    args = parser.parse_args(argv)
    # The Windows wheel otherwise expects a Unix HOME when initializing setup.
    srs = str(args.srs.resolve(strict=True))
    os.environ.setdefault("EZKL_REPO_PATH", str(args.srs.resolve().parent))

    start = time.perf_counter()
    report = {"phase": args.command, "status": "failed"}
    try:
        if args.command == "setup":
            if not ezkl.setup(str(args.circuit), str(args.vk), str(args.pk), srs, str(args.witness)):
                raise RuntimeError("EZKL key setup failed")
        elif args.command == "prove":
            proof = ezkl.prove(str(args.witness), str(args.circuit), str(args.pk), str(args.proof), srs)
            report["proof_bytes"] = len(bytes.fromhex(proof["proof"][2:]))
            report["proof_file_bytes"] = args.proof.stat().st_size
        else:
            verify_proof(args.proof, args.settings, args.vk, args.srs, args.claim)
        report["status"] = "passed"
    finally:
        report.update(seconds=round(time.perf_counter() - start, 3), **peak_memory())
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
