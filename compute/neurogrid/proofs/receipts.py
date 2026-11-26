"""Verify a segmented inference bundle using only the trusted plan and public verification artifacts."""

import argparse
import base64
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time

import ezkl

from neurogrid.metrics import peak_memory
from neurogrid.serialization import canonical, digest_file, read_json

HEX = re.compile(r"[0-9a-f]{64}")
OPERATION = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SCHEMA = "neurogrid-segmented-q18-v1"


def commit(values: list[str]) -> str:
    digest, = ezkl.poseidon_hash(values)
    return digest


def field(value: int | float, scale: int = 0) -> str:
    return str(ezkl.float_to_felt(value, scale))


def open_tokens(tokens: list[int]) -> list[str]:
    return [commit([field(token)]) for token in tokens]


def context_fields(request: dict, segment: dict) -> list[str]:
    digest = sha256(canonical([SCHEMA, request, segment])).digest()
    return [field(int.from_bytes(digest[i:i + 2], "little")) for i in range(0, len(digest), 2)]


def validate_request(manifest: dict, request: dict) -> None:
    if set(request) != {"nonce", "manifest", "prompt"} or not isinstance(request["nonce"], str):
        raise ValueError("Invalid request")
    if not HEX.fullmatch(request["nonce"]) or request["manifest"] != sha256(canonical(manifest)).hexdigest():
        raise ValueError("Request has the wrong nonce or model manifest")
    validate_tokens(request["prompt"], len(manifest["prompt_edges"]), manifest["vocab"])


def validate_tokens(value, count: int, vocab: int) -> None:
    if not isinstance(value, list) or len(value) != count or any(
            type(token) is not int or not 0 <= token < vocab for token in value):
        raise ValueError("Wrong token count or out-of-range token")


def bounded_manifest(manifest: dict, replies: int | None) -> dict:
    if replies is None:
        return manifest
    if not 1 <= replies <= len(manifest["reply_edges"]):
        raise ValueError("Invalid trusted response length")
    edges = manifest["reply_edges"][:replies]
    end, = (i + 1 for i, seg in enumerate(manifest["segments"]) if edges[-1] in seg["outputs"])
    return manifest | {"reply_edges": edges, "segments": manifest["segments"][:end]}


def verify_bundle(public: Path, request: dict, bundle: dict, *, replies: int | None = None,
                  verify_segment=None) -> dict:
    # 'public' comes from the verifier's trusted model installation, never the untrusted proof bundle.
    manifest = read_json(public / "manifest.json")
    if manifest["schema"] != SCHEMA:
        raise ValueError("Unknown proof protocol")
    validate_request(manifest, request)
    manifest = bounded_manifest(manifest, replies)
    if set(bundle) != {"request", "segments", "reply"} or bundle["request"] != request["nonce"]:
        raise ValueError("Proof bundle belongs to another request")
    if not isinstance(bundle["segments"], list) or len(bundle["segments"]) != len(manifest["segments"]):
        raise ValueError("Expected exactly one proof for every scheduled segment")
    validate_tokens(bundle["reply"], len(manifest["reply_edges"]), manifest["vocab"])

    srs = public / "params.srs"
    if digest_file(srs) != manifest["srs_sha256"]:
        raise ValueError("Unrecognized SRS")
    for operation, keys in manifest["circuits"].items():
        if not OPERATION.fullmatch(operation):
            raise ValueError("Invalid operation name in manifest")
        if digest_file(public / f"{operation}.vk") != keys["vk_sha256"] or (
                digest_file(public / f"{operation}.json") != keys["settings_sha256"]):
            raise ValueError("Verification artifacts differ from the trusted manifest")

    known = manifest["constants"] | dict(zip(manifest["prompt_edges"], open_tokens(request["prompt"]), strict=True))
    proof_bytes = 0
    with TemporaryDirectory() as scratch:
        native_path = Path(scratch) / "proof.json"
        for segment, item in zip(manifest["segments"], bundle["segments"], strict=True):
            if not isinstance(item, dict) or set(item) != {"name", "outputs", "proof"} or item["name"] != segment["name"]:
                raise ValueError("Missing, reordered or unexpected proof")
            outputs = item["outputs"]
            if not isinstance(outputs, list) or len(outputs) != len(segment["outputs"]) or any(
                    not isinstance(v, str) or not HEX.fullmatch(v) for v in outputs):
                raise ValueError("Invalid tensor commitments")
            if any(name in known for name in segment["outputs"]):
                raise ValueError("Tensor edge is produced more than once")
            specialized_size = verify_segment(segment, item, known) if verify_segment else None
            if specialized_size is not None:
                proof_bytes += specialized_size
                known.update(zip(segment["outputs"], outputs, strict=True))
                continue
            if not isinstance(item["proof"], str) or len(item["proof"]) > 512_000:
                raise ValueError("Invalid proof size")
            raw = base64.b64decode(item["proof"], validate=True)
            context = commit(context_fields(request, segment))
            instances = [known[name] for name in segment["inputs"]] + [context, *outputs, context]
            native_path.write_bytes(canonical({"proof": list(raw), "instances": [instances], "version": ezkl.__version__}))
            op = segment["operation"]
            try:
                accepted = ezkl.verify(str(native_path), str(public / f"{op}.json"), str(public / f"{op}.vk"), str(srs))
            except RuntimeError as error:
                raise ValueError(f"Proof rejected at {segment['name']}") from error
            if not accepted:
                raise ValueError(f"Proof rejected at {segment['name']}")
            proof_bytes += len(raw)
            known.update(zip(segment["outputs"], outputs, strict=True))

    expected = [known[name] for name in manifest["reply_edges"]]
    if open_tokens(bundle["reply"]) != expected:
        raise ValueError("Reply tokens do not open the proved commitments")
    return {"segments": len(bundle["segments"]), "proof_bytes": proof_bytes, "reply": bundle["reply"]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("public", type=Path)
    parser.add_argument("request", type=Path)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    if args.bundle.stat().st_size > 16 * 1024 * 1024:
        raise SystemExit("Proof bundle exceeds the benchmark limit")
    start = time.perf_counter()
    result = verify_bundle(args.public, read_json(args.request), read_json(args.bundle))
    print(json.dumps({"phase": "verify", **result, **peak_memory(), "bundle_bytes": args.bundle.stat().st_size,
                      "seconds": round(time.perf_counter() - start, 3)}), flush=True)


if __name__ == "__main__":
    main()
