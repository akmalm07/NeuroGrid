"""Halo2 fixed-point bridge for independently verified public GKR accumulators."""

from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time

import ezkl

from neurogrid.metrics import peak_memory
from neurogrid.serialization import read_json
from neurogrid.proofs.halo2 import verify_proof
from neurogrid.serialization import digest_file, write_json

FIELD = 21888242871839275222246405745257275088548364400416034343698204186575808495617
SCALE = 1 << 18


def felt(n: int) -> str:
    return (int(n) % FIELD).to_bytes(32, "little").hex()


def graph(width: int):
    from onnx import TensorProto as T, helper

    nodes = []
    const = [helper.make_tensor(name, T.INT64, [], [value]) for name, value in
             (("one", 1), ("zero", 0), ("scale", SCALE), ("half", SCALE // 2), ("last", SCALE - 1))]

    def op(kind: str, *inputs: str, name: str | None = None) -> str:
        result = name or f"v{len(nodes)}"
        nodes.append(helper.make_node(kind, inputs, [result]))
        return result

    mag = op("Mul", "raw", "sign")
    rounded = op("Mul", "rounded", "sign")
    op("Sub", op("Sub", op("Add", mag, "half"), op("Mul", rounded, "scale")), "remainder", name="equation")
    op("Sub", op("Mul", "sign", "sign"), "one", name="sign_check")
    cap = op("Max", op("Sub", "remainder", "last"), "zero")
    for item in (mag, rounded, "remainder"):
        cap = op("Add", cap, op("Max", op("Neg", item), "zero"))
    op("Identity", cap, name="range_check")
    op("Identity", "context", name="bound_context")
    inputs = [helper.make_tensor_value_info(name, T.INT64, [width]) for name in ("raw", "rounded", "sign", "remainder")]
    inputs.append(helper.make_tensor_value_info("context", T.INT64, [16]))
    outputs = [helper.make_tensor_value_info(name, T.INT64, [width])
               for name in ("equation", "sign_check", "range_check")]
    outputs.append(helper.make_tensor_value_info("bound_context", T.INT64, [16]))
    return helper.make_model(helper.make_graph(nodes, "q18-rescale-v1", inputs, outputs, initializer=const),
                             opset_imports=[helper.make_opsetid("", 18)], ir_version=8)


def bridge_claim(raw: list[int], rounded: list[int], context: bytes, width: int) -> dict:
    if len(raw) != len(rounded) or not 0 < len(raw) <= width or len(context) != 32:
        raise ValueError("Invalid rescaling shape")
    if any(abs(n) >= 1 << 52 for n in raw) or any(abs(n) >= 1 << 35 for n in rounded):
        raise ValueError("Rescaling input exceeds signed numerical bounds")
    pad = [0] * (width - len(raw))
    raw, rounded = raw + pad, rounded + pad
    signs = [1 if n >= 0 else -1 for n in raw]
    remainders = [abs(n) + SCALE // 2 - sign * q * SCALE for n, q, sign in zip(raw, rounded, signs, strict=True)]
    tag = [int.from_bytes(context[i:i + 2], "little") for i in range(0, 32, 2)]
    return {"inputs": [list(map(felt, data)) for data in (raw, rounded, signs, remainders, tag)],
            "outputs": [[felt(0)] * width for _ in range(3)] + [list(map(felt, tag))]}


def prepare(root: Path, width: int) -> None:
    import onnx

    if width not in (8, 64, 256):
        raise ValueError("Unsupported bridge width")
    start = time.perf_counter()
    root.mkdir()
    public = root / "public"
    public.mkdir()
    model, settings, compiled = root / "bridge.onnx", public / "settings.json", root / "bridge.ezkl"
    onnx.save(graph(width), model)
    params = ezkl.PyRunArgs()
    params.input_scale = params.param_scale = 0
    params.logrows = 11 if width <= 64 else 13
    params.num_inner_cols = 4
    params.decomp_base = 256
    params.decomp_legs = 7
    params.check_mode = "safe"
    params.lookup_range = (-256, 256)
    if not ezkl.gen_settings(str(model), str(settings), params) or not ezkl.compile_circuit(
            str(model), str(compiled), str(settings)):
        raise RuntimeError("Could not compile the Halo2 bridge")
    srs = public / "params.srs"
    ezkl.gen_srs(str(srs), params.logrows)
    zero = bridge_claim([0], [0], bytes(32), width)
    with TemporaryDirectory() as folder:
        data, witness = Path(folder) / "input.json", Path(folder) / "witness.json"
        write_json(data, {"input_data": zero["inputs"]})
        ezkl.gen_witness(str(data), str(compiled), str(witness))
        if not ezkl.setup(str(compiled), str(public / "bridge.vk"), str(root / "bridge.pk"), str(srs), str(witness)):
            raise RuntimeError("Could not generate the Halo2 bridge key")
    write_json(public / "manifest.json", {"width": width, "artifacts": {
        name: digest_file(public / name) for name in ("settings.json", "bridge.vk", "params.srs")}})
    print(json.dumps({"phase": "bridge_setup", "seconds": time.perf_counter() - start, "width": width,
                      **peak_memory()}), flush=True)


def context(statement: str, rounded_hash: str, offset: int, total: int) -> bytes:
    if not re.fullmatch("[0-9a-f]{64}", statement):
        raise ValueError("Malformed GKR statement digest")
    return sha256(b"neurogrid/q18-bridge/v1\0" + bytes.fromhex(statement) + bytes.fromhex(rounded_hash)
                  + offset.to_bytes(8, "little") + total.to_bytes(8, "little")).digest()


def run(root: Path, job: Path, statement: str, *, prove: bool) -> dict:
    import numpy as np

    start = time.perf_counter()
    public = root / "public" if prove else root
    manifest = read_json(public / "manifest.json")
    for name in ("settings.json", "bridge.vk", "params.srs"):
        if digest_file(public / name) != manifest["artifacts"][name]:
            raise ValueError("Unrecognized bridge artifacts")
    scope = read_json(job / "scope.json")
    count = scope["M"] * scope["N"]
    width = manifest["width"]
    if width not in (8, 64, 256) or not 1 <= count <= 1 << 22:
        raise ValueError("Unexpected output size")
    labels = ("raw.bin", "rounded.bin")
    if any((job / name).stat().st_size != count * 8 for name in labels):
        raise ValueError("Incorrect rescale opening size")
    raw, rounded = (np.fromfile(job / name, dtype="<i8") for name in labels)
    proofs = job / "bridge"
    if prove:
        proofs.mkdir()
    size = serialized = 0
    witness_seconds = backend_seconds = 0.0
    digest_start = time.perf_counter()
    rounded_hash = digest_file(job / labels[1])
    commitment_seconds = time.perf_counter() - digest_start
    for offset in range(0, count, width):
        claim = bridge_claim(raw[offset:offset + width].tolist(), rounded[offset:offset + width].tolist(),
                             context(statement, rounded_hash, offset, count), width)
        proof = proofs / f"{offset}.json"
        if prove:
            with TemporaryDirectory() as folder:
                data, witness = Path(folder) / "input.json", Path(folder) / "witness.json"
                write_json(data, {"input_data": claim["inputs"]})
                begin = time.perf_counter()
                evaluated = ezkl.gen_witness(str(data), str(root / "bridge.ezkl"), str(witness))
                witness_seconds += time.perf_counter() - begin
                if evaluated["inputs"] != claim["inputs"] or evaluated["outputs"] != claim["outputs"]:
                    raise ValueError("Halo2 rejected Q18 rescaling inputs")
                begin = time.perf_counter()
                ezkl.prove(str(witness), str(root / "bridge.ezkl"), str(root / "bridge.pk"),
                           str(proof), str(public / "params.srs"))
                backend_seconds += time.perf_counter() - begin
        verify_size = proof.stat().st_size
        if verify_size > 512 * 1024:
            raise ValueError("Oversize rescale proof")
        if not prove:
            with TemporaryDirectory() as folder:
                claim_path = Path(folder) / "claim.json"
                write_json(claim_path, claim)
                begin = time.perf_counter()
                verify_proof(proof, public / "settings.json", public / "bridge.vk", public / "params.srs", claim_path)
                backend_seconds += time.perf_counter() - begin
        encoded = read_json(proof)["proof"]
        size += len(encoded) if isinstance(encoded, list) else len(bytes.fromhex(encoded[2:]))
        serialized += verify_size
    total = time.perf_counter() - start
    return {"phase": "bridge_prove" if prove else "bridge_verify", "seconds": total,
            "witness_seconds": witness_seconds, "halo2_seconds": backend_seconds,
            "orchestration_seconds": total - witness_seconds - backend_seconds,
            "proof_bytes": size, "serialized_bytes": serialized, "commitment_seconds": commitment_seconds,
            "rounded_sha256": rounded_hash, "outputs": count, **peak_memory()}
