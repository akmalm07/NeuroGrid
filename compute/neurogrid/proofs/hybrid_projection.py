"""Run and verify the public-opening GKR/Halo2 projection baseline."""

import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

from neurogrid.metrics import peak_child
from neurogrid.proofs.rescale import run
from neurogrid.serialization import read_json

PROTOCOL = "neurogrid-public-q18-gkr-v1"
FILES = ("left.bin", "right.bin", "raw.bin", "rounded.bin")


def _profile_native(job: Path, binary: Path, operations: tuple[str, ...]) -> str:
    statements = {gkr(job, binary, operation) for operation in operations}
    if len(statements) != 1:
        raise ValueError("GKR profile did not preserve the statement")
    return statements.pop()


def gkr(job: Path, binary: Path, *operations: str) -> str:
    if not operations:
        raise ValueError("Select a GKR operation")
    wasi = binary.suffix == ".wasm"
    if not wasi and len(operations) > 1:
        return _profile_native(job, binary, operations)
    if wasi:
        runner = Path(__file__).with_name("wasi.mjs")
        command = ["node", str(runner), str(binary), str(job), *operations]
    else:
        command = [str(binary.resolve()), operations[0], str(job.resolve())]
    start = time.perf_counter()
    with subprocess.Popen(command,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        output, error = process.communicate()
        memory = peak_child(process)
    if process.returncode:
        raise ValueError(error.strip().splitlines()[-1] if error.strip() else "GKR process failed")
    statements = set()
    for line in output.splitlines():
        item = json.loads(line)
        if "statement" in item:
            statements.add(item["statement"])
        print(json.dumps(item), flush=True)
    if len(statements) != 1:
        raise ValueError("GKR did not return a unique statement")
    print(json.dumps({"phase": "node_process" if wasi else "native_process",
                      "seconds": time.perf_counter() - start, **memory}),
          flush=True)
    return statements.pop()


def verify(job: Path, expected_scope: Path, public: Path, binary: Path) -> dict:
    # The scope and bridge manifest come from the independently trusted installation.
    # A private snapshot prevents the two backends from reading different openings.
    with TemporaryDirectory() as folder:
        frozen = Path(folder)
        scope = read_json(expected_scope)
        if read_json(job / "scope.json") != scope:
            raise ValueError("Bundle scope differs from the independent statement")
        shutil.copyfile(expected_scope, frozen / "scope.json")
        sizes = {
            "left.bin": 4 * scope["M"] * scope["K"], "right.bin": 4 * scope["K"] * scope["N"],
            "raw.bin": 8 * scope["M"] * scope["N"], "rounded.bin": 8 * scope["M"] * scope["N"],
        }
        for name in (*FILES, "gkr.proof"):
            size = (job / name).stat().st_size
            if size != sizes.get(name, size) or not 0 < size <= (512 * 1024 * 1024 if name in sizes else 1024 * 1024):
                raise ValueError("Invalid proof or tensor file size")
            shutil.copyfile(job / name, frozen / name)
        (frozen / "bridge").mkdir()
        count, width = scope["M"] * scope["N"], read_json(public / "manifest.json")["width"]
        if width not in (8, 64, 256) or not 1 <= count <= 1 << 22:
            raise ValueError("Invalid trusted bridge dimensions")
        for offset in range(0, count, width):
            name = f"{offset}.json"
            source = job / "bridge" / name
            if not 0 < source.stat().st_size <= 512 * 1024:
                raise ValueError("Invalid bridge proof size")
            shutil.copyfile(source, frozen / "bridge" / name)
        statement = gkr(frozen, binary, "verify")
        return run(public, frozen, statement, prove=False) | {
            "matrix_proof_bytes": (frozen / "gkr.proof").stat().st_size}


def benchmark_result(phase: str, start: float, job: Path, report: dict) -> dict:
    matrix_proof = (job / "gkr.proof").stat().st_size
    openings = sum((job / name).stat().st_size for name in FILES)
    return {"phase": f"hybrid_{phase}", "seconds": time.perf_counter() - start,
            "proof_bytes": report["proof_bytes"] + matrix_proof,
            "serialized_proof_bytes": report["serialized_bytes"] + matrix_proof,
            "public_opening_bytes": openings, "outputs": report["outputs"]}
