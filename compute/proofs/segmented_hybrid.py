"""Use the public GKR/Halo2 bridge inside the pinned small-transformer trace."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import time

import ezkl

from granite_layer_proof import peak_memory, read_json
from hybrid_benchmark import PROTOCOL, gkr, verify
from hybrid_bridge import run
from segmented_receipt import canonical, commit, field, verify_bundle, write_json

PROJECTIONS = {"linear": (4,), "down": (4, 4)}
PROOF_MARKER = "gkr-halo2-v1"
N = 4


def ints(entries: list[str]) -> list[int]:
    return [int(ezkl.felt_to_int(entry)) for entry in entries]


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def dimensions(segment: dict) -> tuple[tuple[int, ...], int]:
    parts = PROJECTIONS[segment["operation"]]
    return parts, sum(parts)


def prior_cache(segment: dict, known: dict) -> str:
    for position in range(segment["position"], -1, -1):
        keys, values = f"p{position}-keys", f"p{position}-values"
        if keys in known and values in known:
            return digest(canonical([known[keys], known[values]]))
    return digest(canonical([known["cache0"], known["cache0"]]))


def scope(segment: dict, request: dict, known: dict, replies: int, left: bytes, right: bytes) -> dict:
    _, k = dimensions(segment)
    cache = prior_cache(segment, known)
    return {"Protocol": PROTOCOL, "Model": request["manifest"], "Request": request["nonce"],
            "Prompt": digest(canonical(request["prompt"])), "Operation": f"{segment['name']}/reply-count-{replies}",
            "Layer": segment["layer"], "Position": segment["position"], "KVBefore": cache, "KVAfter": cache,
            "M": 1, "K": k, "N": N, "Q": 18, "Left": digest(left), "Right": digest(right)}


def prove_response(model: Path, request_path: Path, job: Path, wasm: Path, bridge: Path, replies: int) -> None:
    from segmented_benchmark import prove

    public = model / "public"
    request = read_json(request_path)
    known = read_json(public / "manifest.json")["constants"].copy()
    job.mkdir()
    projections = job / "projections"
    projections.mkdir()

    def prove_segment(segment, witness, values):
        if segment["operation"] not in PROJECTIONS:
            if segment["operation"] == "attention":
                known.update({name: commit(value) for name, value in
                              zip(segment["outputs"][1:], values[1:], strict=True)})
            return None
        parts, k = dimensions(segment)
        entries = read_json(witness)["inputs"][:-1]
        inputs = [lane for entry in entries[:-1] for lane in ints(entry)]
        weights = ints(entries[-1])
        if list(map(len, entries)) != [*parts, k * N]:
            raise ValueError(f"Incorrect projection witness shape at {segment['name']}")
        accumulators = [sum(inputs[row] * weights[row * N + col] for row in range(k)) for col in range(N)]
        left, right = struct.pack(f"<{k}i", *inputs), struct.pack(f"<{k * N}i", *weights)
        folder = projections / segment["name"]
        folder.mkdir()
        for name, data in (("left", left), ("right", right), ("raw", struct.pack(f"<{N}q", *accumulators)),
                           ("rounded", struct.pack(f"<{N}q", *ints(values[0])))):
            (folder / f"{name}.bin").write_bytes(data)
        write_json(folder / "scope.json", scope(segment, request, known, replies, left, right))
        statement = gkr(folder, wasm, "prove")
        report = run(bridge, folder, statement, prove=True)
        binary_size = (folder / "gkr.proof").stat().st_size + report["proof_bytes"]
        return PROOF_MARKER, binary_size

    os.environ.setdefault("EZKL_REPO_PATH", str(model.resolve()))
    prove(model, request_path, job / "bundle.json", replies=replies, prove_segment=prove_segment)


def opening(path: Path, size: int) -> bytes:
    with path.open("rb") as data:
        result = data.read(size + 1)
    if len(result) != size:
        raise ValueError("Incorrect projection opening length")
    return result


def verify_response(public: Path, request_path: Path, job: Path, wasm: Path, bridge: Path, replies: int) -> dict:
    request = read_json(request_path)
    path = job / "bundle.json"
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Oversize inference bundle")
    bundle = read_json(path)
    projection_count = 0

    def verify_segment(segment, item, known):
        nonlocal projection_count
        if segment["operation"] not in PROJECTIONS:
            return None
        if item["proof"] != PROOF_MARKER:
            raise ValueError(f"Expected a hybrid projection at {segment['name']}")
        parts, k = dimensions(segment)
        folder = job / "projections" / segment["name"]
        left, right, rounded = (opening(folder / name, size) for name, size in (
            ("left.bin", k * 4), ("right.bin", k * N * 4), ("rounded.bin", N * 8)))
        activations = struct.unpack(f"<{k}i", left)
        weights = struct.unpack(f"<{k * N}i", right)
        result = struct.unpack(f"<{N}q", rounded)
        offset = 0
        for name, size in zip(segment["inputs"][:-1], parts, strict=True):
            if known[name] != commit(list(map(field, activations[offset:offset + size]))):
                raise ValueError(f"Projection activation differs from the verified edge at {segment['name']}")
            offset += size
        if known[segment["inputs"][-1]] != commit(list(map(field, weights))):
            raise ValueError(f"Projection weights differ from the pinned model at {segment['name']}")
        if item["outputs"] != [commit(list(map(field, result)))]:
            raise ValueError(f"Projection result differs from the next edge at {segment['name']}")
        expected = scope(segment, request, known, replies, left, right)
        with TemporaryDirectory() as scratch:
            expected_path = Path(scratch) / "scope.json"
            write_json(expected_path, expected)
            report = verify(folder, expected_path, bridge, wasm)
        if report["rounded_sha256"] != digest(rounded):
            raise ValueError(f"Projection result opening changed during verification at {segment['name']}")
        projection_count += 1
        return report["matrix_proof_bytes"] + report["proof_bytes"]

    start = time.perf_counter()
    result = verify_bundle(public, request, bundle, replies=replies, verify_segment=verify_segment)
    return result | {"phase": "hybrid_response_verify", "hybrid_projections": projection_count,
                     "seconds": time.perf_counter() - start, **peak_memory()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prove", "verify"))
    parser.add_argument("model", type=Path, help="Prover root or independently trusted segmented public directory")
    parser.add_argument("request", type=Path, help="Independently issued request")
    parser.add_argument("job", type=Path)
    parser.add_argument("wasm", type=Path)
    parser.add_argument("bridge", type=Path, help="Bridge prover root or independently trusted public directory")
    parser.add_argument("--tokens", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.phase == "prove":
        prove_response(args.model, args.request, args.job, args.wasm, args.bridge, args.tokens)
    else:
        print(json.dumps(verify_response(args.model, args.request, args.job, args.wasm, args.bridge, args.tokens)),
              flush=True)


if __name__ == "__main__":
    main()
