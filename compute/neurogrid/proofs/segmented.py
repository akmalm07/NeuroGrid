"""Prepare, prove and benchmark a sequential Q18 transformer trace with reusable segment keys."""

import argparse
from base64 import b64encode
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import time

import ezkl
import onnx
import onnxruntime
import numpy as np

from neurogrid.metrics import peak_memory
from neurogrid.serialization import read_json
from neurogrid.models.small import MODEL, Q, VOCAB, model_plan, templates
from neurogrid.proofs.receipts import SCHEMA, bounded_manifest, commit, context_fields, field, validate_request, validate_tokens
from neurogrid.serialization import canonical, digest_file, write_json

LOGROWS = 13
LOOKUPS = (-4096, 4096)


def paths(root: Path, operation: str) -> tuple[Path, Path, Path, Path]:
    return (root / "private" / f"{operation}.onnx", root / "private" / f"{operation}.ezkl",
            root / "public" / f"{operation}.json", root / "private" / f"{operation}.pk")


def quantized_constants() -> dict[str, list[str]]:
    constants, _, _ = model_plan()
    return {name: [field(np.float32(v).item(), scale) for v in data] for name, (scale, data) in constants.items()}


def witness_trace(root: Path, manifest: dict, request: dict):
    constants, _, _ = model_plan()
    state = quantized_constants() | {f"prompt{i}": [field(token)] for i, token in enumerate(request["prompt"])}
    reference = {name: np.array(values, dtype=np.float32) for name, (_, values) in constants.items()}
    reference.update({f"prompt{i}": np.array([token], dtype=np.int64) for i, token in enumerate(request["prompt"])})
    remaining = Counter(name for segment in manifest["segments"] for name in segment["inputs"])
    remaining.update(manifest["reply_edges"])
    sessions = {}
    ort_options = onnxruntime.SessionOptions()
    ort_options.intra_op_num_threads = 1
    max_abs = 0.0
    with TemporaryDirectory(dir=root / "private") as scratch:
        location = Path(scratch)
        for i, segment in enumerate(manifest["segments"]):
            operation = segment["operation"]
            model, circuit, settings_path, _ = paths(root, operation)
            settings = read_json(settings_path)
            context = context_fields(request, segment)
            inputs = [state[name] for name in segment["inputs"]] + [context]
            sample = location / "input.json"
            sample.write_bytes(canonical({"input_data": inputs}))
            witness_path = location / f"{i}.witness"
            witness = ezkl.gen_witness(str(sample), str(circuit), str(witness_path))
            outputs = witness["outputs"]
            if witness["inputs"] != inputs or outputs[-1] != context:
                raise ValueError(f"Input field or context was rewritten at {segment['name']}")
            if not LOOKUPS[0] <= witness["min_lookup_inputs"] <= witness["max_lookup_inputs"] <= LOOKUPS[1]:
                raise ValueError(f"Lookup range exceeded at {segment['name']}: "
                                 f"{witness['min_lookup_inputs']}..{witness['max_lookup_inputs']}")
            native_io = witness["processed_inputs"]["poseidon_hash"] + witness["processed_outputs"]["poseidon_hash"]
            if native_io != [commit(values) for values in (*inputs, *outputs)]:
                raise ValueError("EZKL commitment encoding differs from the receipt protocol")
            if operation not in sessions:
                sessions[operation] = onnxruntime.InferenceSession(
                    str(model), ort_options, providers=["CPUExecutionProvider"])
            session = sessions[operation]
            feed = dict(zip(
                [v.name for v in session.get_inputs()],
                [reference[name].reshape(info.shape) for name, info in
                 zip(segment["inputs"], session.get_inputs()[:-1], strict=True)] +
                [np.array([int(ezkl.felt_to_int(v)) / (1 << Q) for v in context], dtype=np.float32)],
                strict=True,
            ))
            expected = session.run(None, feed)[:-1]
            for name, fields, scale, ref in zip(segment["outputs"], outputs[:-1],
                                                settings["model_output_scales"][:-1], expected, strict=True):
                value = np.asarray([int(ezkl.felt_to_int(v)) / (1 << scale) for v in fields]).reshape(ref.shape)
                deviation = float(np.max(np.abs(value - ref)))
                max_abs = max(max_abs, deviation)
                # An independent FP32 trace carries its own state throughout the prompt and reply.
                np.testing.assert_allclose(value, ref, atol=3e-4, rtol=2e-3, err_msg=segment["name"])
                if remaining[name] > 0:
                    reference[name] = ref
                    state[name] = fields
            for name in segment["inputs"]:
                remaining[name] -= 1
                if remaining[name] == 0:
                    del reference[name], state[name]
            yield segment, witness_path, native_io, outputs[:-1]
            witness_path.unlink()
    reply = [int(ezkl.felt_to_int(state[name][0])) for name in manifest["reply_edges"]]
    yield reply, max_abs


def prepare(root: Path) -> None:
    start = time.perf_counter()
    root.mkdir()
    (root / "private").mkdir()
    (root / "public").mkdir()
    constants, plan, replies = model_plan()
    manifest = {
        "schema": SCHEMA, "model": MODEL, "vocab": VOCAB, "prompt_edges": ["prompt0", "prompt1"],
        "reply_edges": replies, "segments": [asdict(step) for step in plan],
        "constants": {name: commit(values) for name, values in quantized_constants().items()},
    }
    for operation, graph in templates().items():
        model, circuit, settings, _ = paths(root, operation)
        onnx.checker.check_model(graph)
        onnx.save(graph, model)
        options = ezkl.PyRunArgs()
        options.input_scale = options.param_scale = Q
        options.decomp_base = 256
        options.decomp_legs = 6
        options.num_inner_cols = 4
        options.logrows = LOGROWS
        options.lookup_range = LOOKUPS
        options.input_visibility = options.output_visibility = "hashed"
        options.check_mode = "safe"
        if not ezkl.gen_settings(str(model), str(settings), options):
            raise RuntimeError(f"Could not create settings for {operation}")
        layout = read_json(settings)
        rows = max(layout["num_rows"], layout["module_sizes"]["poseidon"][0])
        if rows >= 1 << LOGROWS:
            raise ValueError(f"{operation} needs {rows} rows in a {1 << LOGROWS} row domain")
        if not ezkl.compile_circuit(str(model), str(circuit), str(settings)):
            raise RuntimeError(f"Could not compile {operation}")
        print(json.dumps({"operation": operation, "rows": rows,
                          "output_scales": layout["model_output_scales"]}), flush=True)

    # Check type and scale at every graph edge before key generation. Equal field encodings
    # alone would not detect a consumer interpreting the same lanes with a different scale.
    ports = {op: (list(graph.graph.input)[:-1], list(graph.graph.output)[:-1]) for op, graph in templates().items()}
    known = dict.fromkeys(manifest["constants"], None) | {
        "prompt0": ((1,), 0, onnx.TensorProto.INT64), "prompt1": ((1,), 0, onnx.TensorProto.INT64),
    }
    for step in plan:
        inputs, outputs = ports[step.operation]
        settings = read_json(paths(root, step.operation)[2])
        for is_output, names, definitions, scales in (
            (False, step.inputs, inputs, settings["model_input_scales"][:-1]),
            (True, step.outputs, outputs, settings["model_output_scales"][:-1]),
        ):
            for name, definition, scale in zip(names, definitions, scales, strict=True):
                tensor = definition.type.tensor_type
                port = tuple(d.dim_value for d in tensor.shape.dim), scale, tensor.elem_type
                if is_output == (name in known):
                    raise ValueError(f"Unknown input or duplicate output at {step.name}/{name}")
                if name in constants:
                    expected_scale, values = constants[name]
                    if scale != expected_scale or len(values) != np.prod(port[0]):
                        raise ValueError(f"Constant shape or scale mismatch at {step.name}/{name}")
                previous = known.get(name)
                if previous is not None and previous != port:
                    raise ValueError(f"Incompatible tensor shape or scale at {step.name}/{name}: {previous} vs {port}")
                known[name] = port
    request = {"nonce": "0" * 64, "prompt": [0, 1], "manifest": sha256(canonical(manifest)).hexdigest()}
    witnesses = root / "private" / "setup-witnesses"
    witnesses.mkdir()
    for trace in witness_trace(root, manifest, request):
        if len(trace) == 2:
            print(json.dumps({"phase": "prepare", **peak_memory(), "seconds": round(time.perf_counter() - start, 3),
                              "reply": trace[0], "max_fp32_error": trace[1]}), flush=True)
            break
        segment, witness_path, _, _ = trace
        target = witnesses / segment["operation"]
        if not target.exists():
            target.write_bytes(witness_path.read_bytes())
    write_json(root / "private" / "plan.json", manifest)


def setup(root: Path) -> None:
    start = time.perf_counter()
    public = root / "public"
    srs = public / "params.srs"
    if srs.exists():
        raise FileExistsError("Setup already started in this directory")
    ezkl.gen_srs(str(srs), LOGROWS)
    manifest = read_json(root / "private" / "plan.json")
    manifest.update(srs_sha256=digest_file(srs), circuits={})
    for op in sorted({seg["operation"] for seg in manifest["segments"]}):
        _, circuit, settings, pk = paths(root, op)
        vk = public / f"{op}.vk"
        witness = root / "private" / "setup-witnesses" / op
        begin = time.perf_counter()
        if not ezkl.setup(str(circuit), str(vk), str(pk), str(srs), str(witness)):
            raise RuntimeError(f"Key setup failed for {op}")
        manifest["circuits"][op] = {"vk_sha256": digest_file(vk), "settings_sha256": digest_file(settings)}
        print(json.dumps({"operation": op, "keygen_seconds": round(time.perf_counter() - begin, 3),
                          "pk_bytes": pk.stat().st_size}), flush=True)
    write_json(public / "manifest.json", manifest)
    print(json.dumps({"phase": "setup", "key_pairs": len(manifest["circuits"]), **peak_memory(),
                      "seconds": round(time.perf_counter() - start, 3)}), flush=True)


def request_for(root: Path, prompt: list[int]) -> dict:
    manifest = read_json(root / "public" / "manifest.json")
    validate_tokens(prompt, len(manifest["prompt_edges"]), manifest["vocab"])
    return {"nonce": secrets.token_hex(32), "prompt": prompt,
            "manifest": sha256(canonical(manifest)).hexdigest()}


def issue(root: Path, prompt: list[int], target: Path) -> None:
    write_json(target, request_for(root, prompt))


def prove(root: Path, request_file: Path, target: Path, *, replies: int | None = None, prove_segment=None,
          verbose: bool = True) -> dict:
    public = root / "public"
    manifest = read_json(public / "manifest.json")
    request = read_json(request_file)
    validate_request(manifest, request)
    manifest = bounded_manifest(manifest, replies)
    start = time.perf_counter()
    segments = []
    proof_seconds = proof_bytes = 0
    reply = None
    for trace in witness_trace(root, manifest, request):
        if len(trace) == 2:
            reply, max_abs = trace
            break
        segment, witness, instances, values = trace
        begin = time.perf_counter()
        specialized = prove_segment(segment, witness, values) if prove_segment else None
        if specialized is None:
            _, circuit, _, pk = paths(root, segment["operation"])
            result = ezkl.prove(str(witness), str(circuit), str(pk), None, str(public / "params.srs"))
            if result["instances"] != [instances]:
                raise ValueError(f"Proof instances differ from evaluated tensors at {segment['name']}")
            raw = bytes.fromhex(result["proof"][2:])
            encoded, size = b64encode(raw).decode("ascii"), len(raw)
        else:
            encoded, size = specialized
        proof_seconds += time.perf_counter() - begin
        proof_bytes += size
        segments.append({"name": segment["name"], "outputs": [commit(data) for data in values],
                         "proof": encoded})
        if verbose:
            print(json.dumps({"segment": segment["name"], "proof_bytes": size,
                              "prove_seconds": round(time.perf_counter() - begin, 3)}), flush=True)
    if reply is None:
        raise RuntimeError("Incomplete inference trace")
    write_json(target, {"request": request["nonce"], "reply": reply, "segments": segments})
    report = {"phase": "prove", "segments": len(segments), "reply": reply, "proof_bytes": proof_bytes,
              "seconds": round(time.perf_counter() - start, 3), "max_fp32_error": max_abs,
              "prove_seconds": round(proof_seconds, 3), **peak_memory()}
    if verbose:
        print(json.dumps(report), flush=True)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="phase", required=True)
    for name in ("prepare", "setup", "issue", "prove"):
        command = modes.add_parser(name)
        command.add_argument("root", type=Path)
        if name in ("issue", "prove"):
            command.add_argument("target", type=Path)
        if name == "issue":
            command.add_argument("prompt", type=int, nargs=2)
        if name == "prove":
            command.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    os.environ.setdefault("EZKL_REPO_PATH", str(args.root.resolve()))
    if args.phase == "prepare":
        prepare(args.root)
    elif args.phase == "setup":
        setup(args.root)
    elif args.phase == "issue":
        issue(args.root, args.prompt, args.target)
    else:
        prove(args.root, args.request, args.target)


if __name__ == "__main__":
    main()
