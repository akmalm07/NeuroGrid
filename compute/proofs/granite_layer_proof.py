"""Setup, prove, and verify a benchmark EZKL circuit in separate processes."""

import argparse
import json
import os
import re
import sys
import time
from math import prod
from pathlib import Path

import ezkl


def peak_memory() -> dict[str, int | None]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD), ("faults", wintypes.DWORD),
                ("peak_working_set", ctypes.c_size_t), ("remaining", ctypes.c_size_t * 6),
                ("peak_commit", ctypes.c_size_t),
            ]

        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.size = ctypes.sizeof(counters)
        if not get_memory(current_process(), ctypes.byref(counters), counters.size):
            raise OSError("GetProcessMemoryInfo failed")
        return {"peak_resident_bytes": counters.peak_working_set, "peak_commit_bytes": counters.peak_commit}

    import resource

    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"peak_resident_bytes": maximum if sys.platform == "darwin" else maximum * 1024,
            "peak_commit_bytes": None}


def peak_resident_bytes() -> int:
    return int(peak_memory()["peak_resident_bytes"] or 0)


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


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
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
    args = parser.parse_args()
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
