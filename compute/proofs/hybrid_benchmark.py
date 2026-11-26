"""Benchmark and independently verify the public-opening Granite GKR/Halo2 path."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

from benchmark_metrics import peak_child
from granite_layer_proof import peak_memory, read_json
from hybrid_bridge import SCALE, prepare, run
from segmented_receipt import digest_file, write_json

PROTOCOL = "neurogrid-public-q18-gkr-v1"
REVISION = "a7042144125ce5ae64f89610e8289647fc000b9a"
FILES = ("left.bin", "right.bin", "raw.bin", "rounded.bin")


def setup_case(root: Path, baseline: Path, export: Path | None, *, batched: bool, projection: str = "q") -> None:
    import ezkl
    import numpy as np

    start = time.perf_counter()
    if projection != "q" and export is None:
        raise ValueError("Key and value projection fixtures require the pinned full export")
    root.mkdir()
    claim = read_json(baseline / "independent" / "claim.json")
    left = np.array([int(ezkl.felt_to_int(value)) for value in claim["inputs"][0]], dtype="<i4").reshape(1, 4096)
    right = np.array([int(ezkl.felt_to_int(value)) for value in claim["inputs"][1]], dtype="<i4").reshape(4096, 8)
    tile = right
    if export is not None:
        import onnx

        export_file = export / "layer0.onnx"
        plan = read_json(baseline / "plan.json")
        if plan["revision"] != REVISION or plan["q"] != 18 or plan["graph_sha256"] != digest_file(export_file):
            raise ValueError("Full export differs from the pinned Granite tile plan")
        graph = onnx.load(export_file)
        node, = (node for node in graph.graph.node if node.name == f"/layer/self_attn/{projection}_proj/MatMul")
        weights, = (tensor for tensor in graph.graph.initializer if tensor.name == node.input[1])
        matrix = onnx.numpy_helper.to_array(weights)
        if matrix.shape != (4096, 4096 if projection == "q" else 1024):
            raise ValueError("Wrong pinned Granite projection shape")
        right = np.empty(matrix.shape, dtype="<i4")
        for i in range(0, 4096, 128):
            values = matrix[i:i + 128].astype(np.float64) * SCALE
            right[i:i + 128] = np.copysign(np.floor(np.abs(values) + 0.5), values)
        if projection == "q":
            np.testing.assert_array_equal(right[:, :8], tile)
    if batched:
        # A deterministic second Q18 row exercises the matrix-matrix path; its negation is
        # a test input, not a second captured Granite activation.
        left = np.concatenate((left, -left))
    calc = time.perf_counter()
    raw = left.astype("<i8") @ right.astype("<i8")
    rounded = (np.sign(raw) * ((np.abs(raw) + SCALE // 2) // SCALE)).astype("<i8")
    if projection == "q":
        np.testing.assert_array_equal(rounded[0, :8], [int(ezkl.felt_to_int(v)) for v in claim["outputs"][0]])
    for name, values in zip(FILES, (left, right, raw, rounded), strict=True):
        values.tofile(root / name)
    digest_start = time.perf_counter()
    scope = {
        "Protocol": PROTOCOL, "Model": REVISION, "Operation": f"layer0.self_attn.{projection}_proj",
        "Request": secrets.token_hex(32), "Layer": 0, "Position": 6, "Q": 18,
        "Prompt": sha256(b"What is the capital of France?").hexdigest(),
        # Projection does not modify the cache. These scopes link to external state.
        "KVBefore": sha256(b"Granite projection diagnostic / cache before").hexdigest(),
        "KVAfter": sha256(b"Granite projection diagnostic / cache after").hexdigest(),
        "M": left.shape[0], "K": left.shape[1], "N": right.shape[1],
        "Left": digest_file(root / FILES[0]), "Right": digest_file(root / FILES[1]),
    }
    write_json(root / "scope.json", scope)
    print(json.dumps({"phase": "prepare_case", "m": scope["M"], "k": scope["K"], "n": scope["N"],
                      "arithmetic_and_write_seconds": digest_start - calc,
                      "commitment_seconds": time.perf_counter() - digest_start,
                      "seconds": time.perf_counter() - start, **peak_memory()}), flush=True)


def gkr(job: Path, wasm: Path, *operations: str) -> str:
    runner = Path(__file__).with_name("tensor_gkr") / "wasi.mjs"
    start = time.perf_counter()
    with subprocess.Popen(["node", str(runner), str(wasm), str(job), *operations],
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
    print(json.dumps({"phase": "node_process", "seconds": time.perf_counter() - start, **memory}),
          flush=True)
    return statements.pop()


def verify(job: Path, expected_scope: Path, public: Path, wasm: Path) -> dict:
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
        statement = gkr(frozen, wasm, "verify")
        return run(public, frozen, statement, prove=False) | {
            "matrix_proof_bytes": (frozen / "gkr.proof").stat().st_size}


def benchmark_result(phase: str, start: float, job: Path, report: dict) -> dict:
    matrix_proof = (job / "gkr.proof").stat().st_size
    openings = sum((job / name).stat().st_size for name in FILES)
    return {"phase": f"hybrid_{phase}", "seconds": time.perf_counter() - start,
            "proof_bytes": report["proof_bytes"] + matrix_proof,
            "serialized_proof_bytes": report["serialized_bytes"] + matrix_proof,
            "public_opening_bytes": openings, "outputs": report["outputs"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="phase", required=True)
    case = modes.add_parser("case")
    case.add_argument("job", type=Path)
    case.add_argument("baseline", type=Path)
    case.add_argument("--full-export", type=Path)
    case.add_argument("--projection", choices=("q", "k", "v"), default="q")
    case.add_argument("--batched", action="store_true")
    setup = modes.add_parser("setup")
    setup.add_argument("keys", type=Path)
    setup.add_argument("--width", type=int, default=8)
    for phase in ("prove", "verify", "profile"):
        mode = modes.add_parser(phase)
        mode.add_argument("job", type=Path)
        mode.add_argument("wasm", type=Path)
        if phase == "prove":
            mode.add_argument("keys", type=Path)
        elif phase == "verify":
            mode.add_argument("public", type=Path)
            mode.add_argument("scope", type=Path)
    args = parser.parse_args()
    if args.phase == "case":
        setup_case(args.job, args.baseline, args.full_export, batched=args.batched, projection=args.projection)
    elif args.phase == "setup":
        os.environ.setdefault("EZKL_REPO_PATH", str(args.keys.resolve()))
        prepare(args.keys, args.width)
    elif args.phase == "profile":
        gkr(args.job, args.wasm, "prove", "verify", "prove", "verify")
    else:
        start = time.perf_counter()
        if args.phase == "prove":
            os.environ.setdefault("EZKL_REPO_PATH", str(args.keys.resolve()))
            statement = gkr(args.job, args.wasm, "prove")
            report = run(args.keys, args.job, statement, prove=True)
        else:
            report = verify(args.job, args.scope, args.public, args.wasm)
        print(json.dumps(report), flush=True)
        print(json.dumps(benchmark_result(args.phase, start, args.job, report)), flush=True)


if __name__ == "__main__":
    main()
