"""Localize fixed-point witness error in an exported complete Granite decode layer."""

import argparse
import json
import math
import time
from pathlib import Path

import ezkl
import numpy as np
import onnx
import onnxruntime
from onnx import TensorProto
from onnx.utils import Extractor

# Boundaries in the opset-18 export of check_granite_decode_backend.py.
FRAGMENTS = {
    "input-rms": (("activation",), ("/layer/input_layernorm/Mul_1_output_0",)),
    "q-projection": (("/layer/input_layernorm/Mul_1_output_0",), ("/layer/self_attn/q_proj/MatMul_output_0",)),
    "k-projection": (("/layer/input_layernorm/Mul_1_output_0",), ("/layer/self_attn/k_proj/MatMul_output_0",)),
    "v-projection": (("/layer/input_layernorm/Mul_1_output_0",), ("/layer/self_attn/v_proj/MatMul_output_0",)),
    "rotary": (
        ("/layer/self_attn/Transpose_output_0", "/layer/self_attn/Transpose_1_output_0"),
        ("/layer/self_attn/Add_output_0", "/layer/self_attn/Add_1_output_0"),
    ),
    "kv-append": (
        ("previous_key", "previous_value", "/layer/self_attn/Add_1_output_0", "/layer/self_attn/Transpose_2_output_0"),
        ("next_key", "next_value"),
    ),
    "grouped-attention": (
        ("/layer/self_attn/Add_output_0", "next_key", "next_value"),
        ("/layer/self_attn/Reshape_5_output_0",),
    ),
    "attention-logits": (
        ("/layer/self_attn/Add_output_0", "next_key"), ("/layer/self_attn/Mul_6_output_0",),
    ),
    "softmax": (("/layer/self_attn/Mul_6_output_0",), ("/layer/self_attn/Softmax_output_0",)),
    "output-projection": (("/layer/self_attn/Reshape_5_output_0",), ("/layer/self_attn/o_proj/MatMul_output_0",)),
    "attention-residual": (("activation", "/layer/self_attn/o_proj/MatMul_output_0"), ("/layer/Add_output_0",)),
    "post-attention-rms": (("/layer/Add_output_0",), ("/layer/post_attention_layernorm/Mul_1_output_0",)),
    "gate-projection": (("/layer/post_attention_layernorm/Mul_1_output_0",), ("/layer/mlp/gate_proj/MatMul_output_0",)),
    "up-projection": (("/layer/post_attention_layernorm/Mul_1_output_0",), ("/layer/mlp/up_proj/MatMul_output_0",)),
    "silu-gate": (
        ("/layer/mlp/gate_proj/MatMul_output_0", "/layer/mlp/up_proj/MatMul_output_0"),
        ("/layer/mlp/Mul_output_0",),
    ),
    "down-projection": (("/layer/mlp/Mul_output_0",), ("/layer/mlp/down_proj/MatMul_output_0",)),
    "mlp-residual": (("/layer/Add_output_0", "/layer/mlp/down_proj/MatMul_output_0"), ("output",)),
}


def measure_fragment(model: Path, scratch: Path, arrays: list[np.ndarray],
                     expected: list[np.ndarray], scale: int, columns: int) -> dict:
    sample, settings = scratch / "input.json", scratch / "settings.json"
    circuit, witness = scratch / "fragment.ezkl", scratch / "witness.json"
    sample.write_text(json.dumps({"input_data": [item.flatten().tolist() for item in arrays]}), encoding="utf-8")
    options = ezkl.PyRunArgs()
    options.check_mode = "safe"
    options.input_scale = scale
    options.param_scale = scale
    options.num_inner_cols = columns
    if scale > 7:
        options.decomp_legs = 3

    phase = "layout"
    start = time.perf_counter()
    report: dict = {"scale": scale}
    try:
        if not ezkl.gen_settings(str(model), str(settings), options):
            raise RuntimeError("Layout failed")
        layout = json.loads(settings.read_text(encoding="utf-8"))
        report.update(rows=layout["num_rows"], assignments=layout["total_assignments"])
        phase = "calibration"
        if scale > 7 and not ezkl.calibrate_settings(str(sample), str(model), str(settings), scales=[scale],
                                                     scale_rebase_multiplier=[1], max_logrows=26):
            raise RuntimeError("Calibration failed")
        phase = "compilation"
        if not ezkl.compile_circuit(str(model), str(circuit), str(settings)):
            raise RuntimeError("Compilation failed")
        phase = "witness"
        ezkl.gen_witness(str(sample), str(circuit), str(witness))
        generated = json.loads(witness.read_text(encoding="utf-8"))
        outputs = generated["pretty_elements"]["rescaled_outputs"]
        metrics = []
        for reference, output in zip(expected, outputs, strict=True):
            observed = np.array(output, dtype=np.float64).reshape(reference.shape)
            delta = observed - reference
            metrics.append({
                "max_absolute_error": float(np.max(np.abs(delta))),
                "rmse": math.sqrt(float(np.mean(delta ** 2))),
                "reference_max_abs": float(np.max(np.abs(reference))),
                "parity": bool(np.allclose(observed, reference, rtol=2e-3, atol=2e-4)),
            })
        report.update(phase="witness generated", output_metrics=metrics,
                      lookup_range=[generated["min_lookup_inputs"], generated["max_lookup_inputs"]],
                      range_size=generated["max_range_size"])
    except RuntimeError as error:
        report.update(phase=phase, error=str(error))
    return report | {"seconds": round(time.perf_counter() - start, 3)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True, help="Existing full-layer export directory")
    parser.add_argument("--output", type=Path, required=True, help="New local scratch directory")
    parser.add_argument("--fragments", nargs="+", choices=tuple(FRAGMENTS), required=True)
    parser.add_argument("--scales", nargs="+", type=int, choices=(7, 10, 12, 14, 16, 18), default=[7])
    args = parser.parse_args(argv)
    args.output.mkdir()
    model_path = args.export / "layer0.onnx"
    model = onnx.load(model_path)
    model = onnx.shape_inference.infer_shapes(model)
    extractor = Extractor(model)
    boundaries = dict.fromkeys(name for fragment in args.fragments for side in FRAGMENTS[fragment] for name in side)
    original_outputs = {value.name for value in model.graph.output}
    inputs = list(model.graph.input)
    for value in model.graph.value_info:
        if value.name in boundaries and value.name not in original_outputs:
            model.graph.output.append(value)
    probe_path = args.output / "diagnostic.onnx"
    onnx.save(model, probe_path)
    data = json.loads((args.export / "input.json").read_text(encoding="utf-8"))["input_data"]
    feed = {
        value.name: np.array(items, dtype=np.float32).reshape(
            tuple(d.dim_value for d in value.type.tensor_type.shape.dim))
        for value, items in zip(inputs, data, strict=True)
    }
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_DISABLE_ALL
    session = onnxruntime.InferenceSession(str(probe_path), options, providers=["CPUExecutionProvider"])
    intermediate_names = [name for name in boundaries if name not in feed]
    feed.update(zip(intermediate_names, session.run(intermediate_names, feed), strict=True))
    del session
    probe_path.unlink()

    for fragment in args.fragments:
        names, outputs = FRAGMENTS[fragment]
        graph = extractor.extract_model(list(names), list(outputs))
        if any(value.type.tensor_type.elem_type != TensorProto.FLOAT
               for value in (*graph.graph.input, *graph.graph.output)):
            raise ValueError(f"{fragment} contains a non-FP32 tensor boundary")
        fragment_path = args.output / fragment
        fragment_path.mkdir()
        onnx.save(graph, fragment_path / "fragment.onnx")
    del graph, extractor, model

    with (args.output / "audit.jsonl").open("x", encoding="utf-8") as log:
        for fragment in args.fragments:
            names, outputs = FRAGMENTS[fragment]
            fragment_path = args.output / fragment
            graph_path = fragment_path / "fragment.onnx"
            for scale in args.scales:
                scratch = fragment_path / f"scale-{scale}"
                scratch.mkdir()
                result = measure_fragment(graph_path, scratch, [feed[n] for n in names], [feed[n] for n in outputs],
                                          scale, columns=8)
                line = json.dumps({"fragment": fragment} | result)
                print(line, flush=True)
                print(line, file=log, flush=True)


if __name__ == "__main__":
    main()
