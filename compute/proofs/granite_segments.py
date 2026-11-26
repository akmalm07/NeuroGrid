"""Partition the pinned Granite decode export and measure one real Q18 projection tile."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import ezkl
import numpy as np
import onnx
import onnxruntime
from onnx.utils import Extractor

from granite_layer_proof import peak_memory, read_json
from neurogrid_node import REVISION
from segmented_model import Q, projection
from segmented_receipt import digest_file, write_json

MATRICES = {
    "/layer/self_attn/q_proj/MatMul": (4096, 4096),
    "/layer/self_attn/k_proj/MatMul": (4096, 1024),
    "/layer/self_attn/v_proj/MatMul": (4096, 1024),
    "/layer/self_attn/o_proj/MatMul": (4096, 4096),
    "/layer/mlp/gate_proj/MatMul": (4096, 12800),
    "/layer/mlp/up_proj/MatMul": (4096, 12800),
    "/layer/mlp/down_proj/MatMul": (12800, 4096),
}
NORMALIZED = "/layer/input_layernorm/Mul_1_output_0"


def partition(model, tile_rows: int) -> list[dict]:
    if not 1 <= tile_rows <= 4096:
        raise ValueError("Tile rows must be between 1 and 4096")
    nodes = list(model.graph.node)
    initializers = {tensor.name: tensor for tensor in model.graph.initializer}
    boundary = {value.name for value in model.graph.output}
    group: list[int] = []
    groups: list[list[int]] = []
    for index, node in enumerate(nodes):
        if node.name in MATRICES:
            if group:
                groups.append(group)
                group = []
            groups.append([index])
        else:
            group.append(index)
    if group:
        groups.append(group)
    if {node.name for node in nodes if node.name in MATRICES} != set(MATRICES):
        raise ValueError("The export is missing a Granite projection")
    softmax, = (node for node in nodes if node.op_type == "Softmax")
    if list(softmax.input) != ["neurogrid_centered_logits"]:
        raise ValueError("Export the graph with corrected attention scaling")

    produced = {value: index for index, node in enumerate(nodes) for value in node.output}
    consumers: dict[str, set[int]] = {}
    for index, node in enumerate(nodes):
        for value in node.input:
            consumers.setdefault(value, set()).add(index)

    plan = []
    for group in groups:
        indices = set(group)
        inputs = list(dict.fromkeys(value for i in group for value in nodes[i].input
                                    if value and produced.get(value) not in indices))
        outputs = [value for i in group for value in nodes[i].output
                   if value in boundary or consumers.get(value, set()) - indices]
        node = nodes[group[0]]
        item = {"nodes": group, "inputs": inputs, "outputs": outputs, "operation": "onnx"}
        if len(group) == 1 and node.name in MATRICES:
            fan_in, lanes = MATRICES[node.name]
            if node.op_type != "MatMul" or tuple(initializers[node.input[1]].dims) != (fan_in, lanes):
                raise ValueError(f"Unexpected parameter shape at {node.name}")
            ranges = [[start, min(start + tile_rows, lanes)] for start in range(0, lanes, tile_rows)]
            item.update(operation="projection", operator=node.name, fan_in=fan_in, lanes=lanes, tiles=ranges,
                        weights=node.input[1])
        plan.append(item)
    return plan


def check_partition(export: Path, graph, plan: list[dict]) -> None:
    """Check every cut and row join against the original FP32 graph on the saved decode sample."""
    start = time.perf_counter()
    original = onnxruntime.SessionOptions()
    original.intra_op_num_threads = 1
    session = onnxruntime.InferenceSession(str(export / "layer0.onnx"), original,
                                           providers=["CPUExecutionProvider"])
    arrays = read_json(export / "input.json")["input_data"]
    state = {v.name: np.asarray(items, dtype=np.float32).reshape(v.shape)
             for v, items in zip(session.get_inputs(), arrays, strict=True)}
    expected = session.run(None, state)
    del session
    template = onnx.shape_inference.infer_shapes(graph)
    extractor = Extractor(template)
    initializers = {tensor.name: tensor for tensor in graph.graph.initializer}
    for segment in plan:
        if segment["operation"] == "projection":
            activation = state[segment["inputs"][0]]
            weights = onnx.numpy_helper.to_array(initializers[segment["weights"]])
            parts = [activation @ weights[:, start:end] for start, end in segment["tiles"]]
            state[segment["outputs"][0]] = np.concatenate(parts, axis=-1)
            continue
        inputs = [name for name in segment["inputs"] if name not in initializers]
        extracted = extractor.extract_model(inputs, segment["outputs"])
        part = onnxruntime.InferenceSession(extracted.SerializeToString(), original,
                                           providers=["CPUExecutionProvider"])
        state.update(zip(segment["outputs"], part.run(None, {name: state[name] for name in inputs}), strict=True))
    differences = {}
    for item, ref in zip(graph.graph.output, expected, strict=True):
        result = state[item.name]
        np.testing.assert_allclose(result, ref, atol=2e-4, rtol=2e-3, err_msg=item.name)
        differences[item.name] = float(np.max(np.abs(result - ref)))
    print(json.dumps({"phase": "fp32-partition", "max_abs_error": differences, **peak_memory(),
                      "seconds": round(time.perf_counter() - start, 3)}), flush=True)


def trial(export: Path, graph, out: Path, srs: Path, rows: int) -> None:
    """Use public tensor openings for a bounded native profile of a real weight tile."""
    start = time.perf_counter()
    node = next(node for node in graph.graph.node if node.name == "/layer/self_attn/q_proj/MatMul")
    weights = next(t for t in graph.graph.initializer if t.name == node.input[1])
    matrix = onnx.numpy_helper.to_array(weights)[:, :rows].copy()
    norm = Extractor(onnx.shape_inference.infer_shapes(graph)).extract_model(["activation"], [NORMALIZED])
    del graph
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1
    runtime = onnxruntime.InferenceSession(norm.SerializeToString(), options, providers=["CPUExecutionProvider"])
    sample = read_json(export / "input.json")["input_data"][0]
    x, = runtime.run(None, {"activation": np.array(sample, dtype=np.float32).reshape((1, 1, -1))})
    # Public instances bind these exact lanes; the independently trusted claim pins their values.
    inputs = [x.flatten().tolist(), matrix.flatten().tolist(), [i / (1 << Q) for i in range(16)]]
    native = out / "projection.onnx"
    onnx.save(projection(rows, 4096), native)
    settings, circuit, data, witness = [out / name for name in
                                        ("settings.json", "compiled", "input.json", "witness.json")]
    write_json(data, {"input_data": inputs})
    params = ezkl.PyRunArgs()
    params.input_scale = params.param_scale = Q
    params.logrows = (sum(len(tensor) for tensor in inputs) + rows + 32).bit_length()
    params.decomp_legs = 6
    params.decomp_base = 256
    params.num_inner_cols = 4
    params.check_mode = "safe"
    if not ezkl.gen_settings(str(native), str(settings), params) or not ezkl.compile_circuit(
            str(native), str(circuit), str(settings)):
        raise RuntimeError("Unable to compile projection tile")
    w = ezkl.gen_witness(str(data), str(circuit), str(witness))
    encoded = [[ezkl.float_to_felt(v, Q) for v in tensor] for tensor in inputs]
    if w["inputs"] != encoded or w["outputs"][-1] != encoded[-1]:
        raise ValueError("Numerical witness changed its public inputs")
    expected = x.flatten() @ matrix
    actual = np.array([int(ezkl.felt_to_int(v)) / (1 << Q) for v in w["outputs"][0]])
    np.testing.assert_allclose(actual, expected, atol=3e-4, rtol=2e-3)
    claim = {"inputs": encoded, "outputs": w["outputs"]}
    setup, pk, vk, proof = (Path(__file__).with_name("granite_layer_proof.py").resolve(),
                            out / "tile.pk", out / "tile.vk", out / "proof.json")
    print(json.dumps({"phase": "tile-witness", "rows": rows, "logrows": params.logrows,
                      "max_fp32_error": float(np.max(np.abs(actual - expected))), **peak_memory(),
                      "seconds": round(time.perf_counter() - start, 3)}), flush=True)
    for phase, args in (
        ("setup", (circuit, witness, srs, pk, vk)),
        ("prove", (circuit, witness, srs, pk, proof)),
    ):
        subprocess.run([sys.executable, str(setup), phase, *map(str, args)], check=True)
    independent = out / "independent"
    independent.mkdir()
    for artifact in (settings, vk, proof):
        shutil.copyfile(artifact, independent / artifact.name)
    write_json(independent / "claim.json", claim)
    subprocess.run([sys.executable, str(setup), "verify", proof.name, settings.name, vk.name,
                    str(srs), "claim.json"], cwd=independent, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="Existing centered decode graph and input.json")
    parser.add_argument("out", type=Path, help="New scratch directory")
    parser.add_argument("--tile-rows", default=8, type=int)
    parser.add_argument("--tile-srs", type=Path, help="Optional SRS for a real projection tile")
    parser.add_argument("--trial-rows", choices=(1, 4, 8), type=int, default=1,
                        help="Profile 1, 4 or 8 output rows at a 2^14, 2^15 or 2^16 domain")
    parser.add_argument("--check-partition", action="store_true",
                        help="Numerically validate every segment and tile join against the FP32 layer")
    args = parser.parse_args()
    args.out.mkdir()
    os.environ.setdefault("EZKL_REPO_PATH", str(args.out.resolve()))
    model = args.export / "layer0.onnx"
    graph = onnx.load(model)
    plan = partition(graph, args.tile_rows)
    print(json.dumps({"nodes": len(graph.graph.node), "segments": len(plan),
                      "projection_tiles": sum(len(item.get("tiles", [])) for item in plan),
                      "q": Q, **peak_memory()}), flush=True)
    write_json(args.out / "plan.json", {"revision": REVISION, "q": Q, "graph_sha256": digest_file(model),
                                      "segments": plan})
    if args.check_partition:
        check_partition(args.export, graph, plan)
    if args.tile_srs:
        trial(args.export, graph, args.out.resolve(), args.tile_srs.resolve(strict=True), args.trial_rows)


if __name__ == "__main__":
    main()
