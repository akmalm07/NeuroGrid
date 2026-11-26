"""Check proof-compiler compatibility with a complete, checkpoint-backed Granite decode layer."""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import onnx
import onnxruntime
import torch
from transformers.cache_utils import DynamicCache

from neurogrid.models.manifest import REVISION, WINDOW
from neurogrid.models.granite import Partition, configure_cpu, tokenize_prompt, tokenizer_for

Tensor = torch.Tensor
DecodeState = tuple[Tensor, Tensor, Tensor]
INPUTS = ("activation", "previous_key", "previous_value")
OUTPUTS = ("output", "next_key", "next_value")


class DecodeLayer(torch.nn.Module):
    """Export a single position with explicit incoming and outgoing KV tensors."""

    def __init__(self, partition: Partition, rotary: tuple[Tensor, Tensor]):
        super().__init__()
        self.layer = partition.layers[0]
        self.config = partition.config
        self.register_buffer("cos", rotary[0])
        self.register_buffer("sin", rotary[1])

    def forward(self, activation: Tensor, previous_key: Tensor, previous_value: Tensor) -> DecodeState:
        cache = DynamicCache(config=self.config)
        cache.update(previous_key, previous_value, 0)
        output = self.layer(activation, past_key_values=cache, use_cache=True,
                            position_embeddings=(self.cos, self.sin))[0]
        return output, cache.layers[0].keys, cache.layers[0].values


def compare(observed: DecodeState | list[np.ndarray], expected: DecodeState) -> None:
    for name, value, reference in zip(OUTPUTS, observed, expected, strict=True):
        np.testing.assert_allclose(np.asarray(value), reference.numpy(), rtol=2e-3, atol=2e-4, err_msg=name)


@torch.inference_mode()
def export_decode(checkpoint: Path, output: Path, position: int,
                  center_attention_logits: bool = False) -> tuple[tuple[str, ...], DecodeState]:
    tokens = tokenize_prompt(tokenizer_for(checkpoint), "What is the capital of France?")
    if not 0 < position < min(len(tokens), WINDOW):
        raise ValueError("Position must be inside the benchmark prompt and have a non-empty cache")

    partition = Partition(checkpoint, 0)
    # FP32 is the compiler input format. The learned checkpoint values are unchanged by conversion.
    first_layer = partition.layers[0].float()
    hidden = partition.embedding[torch.tensor([tokens[:position + 1]])].float()
    hidden = hidden * partition.config.embedding_multiplier
    positions = torch.arange(position + 1)[None]
    prefix_cache = DynamicCache(config=partition.config)
    first_layer(hidden[:, :position], past_key_values=prefix_cache, use_cache=True,
                position_embeddings=partition.rotary(hidden[:, :position], positions[:, :position]))
    decode = DecodeLayer(partition, partition.rotary(hidden[:, position:], positions[:, position:])).eval()
    cache = prefix_cache.layers[0]
    values = (hidden[:, position:], cache.keys, cache.values)
    sdpa_result = decode(*values)

    # The Torch exporter cannot lower the fused GQA kernel. The existing eager kernel
    # expresses the same grouped attention with portable operators for this FP32 check.
    partition.config._attn_implementation = "eager"
    eager_result = decode(*values)
    compare(eager_result, sdpa_result)
    graph = output / "layer0.onnx"
    torch.onnx.export(decode, values, str(graph), input_names=INPUTS, output_names=OUTPUTS,
                      opset_version=18, dynamo=False)
    if center_attention_logits:
        center_logits(graph)

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    cpu = onnxruntime.InferenceSession(str(graph), options, providers=["CPUExecutionProvider"])
    compare(cpu.run(None, dict(zip(INPUTS, (item.numpy() for item in values), strict=True))), eager_result)
    # Also vary incoming activation and cache to check the exported graph consumes them.
    changed = (values[0] * 1.125, values[1] * 0.75, values[2] * 0.875)
    compare(cpu.run(None, dict(zip(INPUTS, (item.numpy() for item in changed), strict=True))), decode(*changed))

    with (output / "input.json").open("x", encoding="utf-8") as stream:
        json.dump({"input_data": [value.flatten().tolist() for value in values]}, stream, separators=(",", ":"))
    model = onnx.load(str(graph), load_external_data=False)
    return tuple(sorted({node.op_type for node in model.graph.node})), eager_result


def center_logits(graph: Path) -> None:
    # Tract carries seven extra fractional bits after Granite's 1/128 attention
    # scale. Subtraction triggers an EZKL rebase before the exponential lookup.
    # Softmax is invariant under subtracting the same value from each row entry.
    model = onnx.load(graph)
    softmax, = (node for node in model.graph.node if node.op_type == "Softmax")
    axis = next((a.i for a in softmax.attribute if a.name == "axis"), -1)
    if axis != -1:
        raise ValueError("Expected Granite softmax to normalize across the final key axis")
    logits = softmax.input[0]
    index = list(model.graph.node).index(softmax)
    axes, maximum, centered = "neurogrid_key_axis", "neurogrid_row_max", "neurogrid_centered_logits"
    softmax.input[0] = centered
    model.graph.initializer.append(onnx.helper.make_tensor(axes, onnx.TensorProto.INT64, [1], [axis]))
    model.graph.node.insert(index, onnx.helper.make_node("ReduceMax", [logits, axes], [maximum], keepdims=1))
    model.graph.node.insert(index + 1, onnx.helper.make_node("Sub", [logits, maximum], [centered]))
    onnx.save(model, graph)


def check_ezkl(output: Path, columns: int, expected: DecodeState, scale: int | None, srs: Path | None,
               *, public_tensors: bool = False, logrows: int | None = None,
               lookup_range: tuple[int, int] | None = None) -> None:
    import ezkl
    from neurogrid.serialization import read_json

    options = ezkl.PyRunArgs()
    # The isolated benchmark publishes the synthetic prompt's tensors so the verifier
    # can pin the exact inputs and outputs without loading weights or private openings.
    public_tensors = public_tensors or srs is not None
    options.input_visibility = "public" if public_tensors else "hashed"
    options.output_visibility = "public" if public_tensors else "hashed"
    options.check_mode = "safe"
    options.num_inner_cols = columns
    if scale is not None:
        # Scale 14 already exceeds the two-limb base-16384 range during calibration.
        options.decomp_legs = 3
    if logrows is not None and lookup_range is not None and scale is not None:
        options.logrows = logrows
        options.lookup_range = lookup_range
        options.input_scale = options.param_scale = scale
    model = str(output / "layer0.onnx")
    settings = output / "ezkl-settings.json"
    start = time.perf_counter()
    if not ezkl.gen_settings(model, str(settings), options):
        raise RuntimeError("EZKL could not generate circuit settings")
    plan = json.loads(settings.read_text(encoding="utf-8"))
    rows = plan["num_rows"]
    print(f"EZKL layout estimate with {columns} columns in {time.perf_counter() - start:.2f} seconds. "
          f"{rows:,} rows, {plan['total_assignments']:,} assignments, "
          f"logrows at least {(rows - 1).bit_length()} before blinding rows.", flush=True)
    if scale is None:
        return

    sample = str(output / "input.json")
    if logrows is None:
        start = time.perf_counter()
        if not ezkl.calibrate_settings(sample, model, str(settings), scales=[scale],
                                       scale_rebase_multiplier=[1], max_logrows=26):
            raise RuntimeError("EZKL could not calibrate safe fixed-point settings")
        print(f"Calibration completed in {time.perf_counter() - start:.2f} seconds.", flush=True)
    calibrated = read_json(settings)
    args = calibrated["run_args"]
    print(f"Input and parameter scales {args['input_scale']}/{args['param_scale']} "
          f"at logrows={args['logrows']}.", flush=True)
    rows = calibrated["num_rows"]
    domain = 1 << args["logrows"]
    if rows >= domain:
        message = f"The layout needs {rows:,} rows before blinding, but the domain has {domain:,}."
        if srs is not None:
            raise RuntimeError(message)
        print(message + " Continuing for witness diagnostics only.", flush=True)

    compiled = output / "layer0.ezkl"
    start = time.perf_counter()
    if not ezkl.compile_circuit(model, str(compiled), str(settings)):
        raise RuntimeError("EZKL could not compile calibrated circuit")
    print(f"Compiled {compiled.stat().st_size:,} bytes in {time.perf_counter() - start:.2f} seconds.", flush=True)
    start = time.perf_counter()
    witness_path = output / "witness.json"
    ezkl.gen_witness(sample, str(compiled), str(witness_path))
    witness = read_json(witness_path)
    print(f"Generated witness in {time.perf_counter() - start:.2f} seconds.", flush=True)
    lower, upper = args["lookup_range"]
    if not lower <= witness["min_lookup_inputs"] <= witness["max_lookup_inputs"] <= upper:
        raise ValueError("Witness exceeds the configured lookup range")
    floats = witness["pretty_elements"]["rescaled_outputs"]
    if public_tensors:
        # Measure the actual public outputs, independent of EZKL's display fields.
        floats = [[ezkl.felt_to_float(field, output_scale) for field in tensor]
                  for tensor, output_scale in zip(witness["outputs"], calibrated["model_output_scales"], strict=True)]
    decoded = [
        np.asarray(values, dtype=np.float64).reshape(reference.shape)
        for values, reference in zip(floats, expected, strict=True)
    ]
    for name, values, reference in zip(OUTPUTS, decoded, expected, strict=True):
        print(f"{name} maximum absolute error: {np.max(np.abs(values - reference.numpy())):.8f}", flush=True)
    compare(decoded, expected)
    print("Calibrated witness output and cache parity passed.", flush=True)
    if srs is not None:
        benchmark_proof(output, srs, witness)


def benchmark_proof(output: Path, srs: Path, witness: dict) -> None:
    import ezkl
    from neurogrid.proofs.halo2 import public_fields
    from neurogrid.serialization import read_json

    settings = output / "ezkl-settings.json"
    plan = read_json(settings)
    sample = read_json(output / "input.json")
    expected_inputs = [
        [ezkl.float_to_felt(item, scale) for item in tensor]
        for tensor, scale in zip(sample["input_data"], plan["model_input_scales"], strict=True)
    ]
    if witness["inputs"] != expected_inputs:
        raise ValueError("Witness input differs from the independently quantized benchmark input")
    claim = {"inputs": expected_inputs, "outputs": witness["outputs"]}
    public_fields(plan, claim)

    runner = "neurogrid.proofs.halo2"
    output = output.resolve()
    circuit, witness_path = output / "layer0.ezkl", output / "witness.json"
    pk, vk, proof = output / "pk.key", output / "vk.key", output / "proof.json"

    def phase(command: str, *paths: Path, cwd: Path | None = None, reject: str | None = None) -> None:
        task = [sys.executable, "-m", runner, command, *(str(path) for path in paths)]
        if reject is None:
            subprocess.run(task, cwd=cwd, check=True)
            return
        result = subprocess.run(task, cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode == 0 or reject not in result.stderr:
            raise RuntimeError(f"Unexpected outcome of rejection check: {result.stdout}{result.stderr}")
        print(f"{paths[0].name} with {paths[-1].name}: {reject}.", flush=True)

    phase("setup", circuit, witness_path, srs, pk, vk)
    phase("prove", circuit, witness_path, srs, pk, proof)

    independent = output / "independent-verifier"
    independent.mkdir()
    for src in (settings, vk, proof):
        shutil.copyfile(src, independent / src.name)
    claim_path = independent / "claim.json"
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    def verify(proof_file: str, claim_file: str, reject: str | None = None) -> None:
        phase("verify", Path(proof_file), Path(settings.name), Path(vk.name), srs, Path(claim_file),
              cwd=independent, reject=reject)

    verify(proof.name, claim_path.name)
    bad_claim = independent / "changed-output.json"
    claim["outputs"][0][0] = ezkl.float_to_felt(1, 0) if claim["outputs"][0][0] == "0" * 64 else "0" * 64
    bad_claim.write_text(json.dumps(claim), encoding="utf-8")
    verify(proof.name, bad_claim.name, "differ from the independently supplied claim")
    modified_instances = independent / "changed-instances.json"
    replaced = read_json(proof)
    replaced["instances"] = [public_fields(plan, claim)]
    modified_instances.write_text(json.dumps(replaced), encoding="utf-8")
    verify(modified_instances.name, bad_claim.name, "rejected the cryptographic proof")
    bad_proof = independent / "changed-proof.json"
    tampered = read_json(proof)
    tampered["proof"][-1] ^= 1
    bad_proof.write_text(json.dumps(tampered), encoding="utf-8")
    verify(bad_proof.name, claim_path.name, "rejected the cryptographic proof")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Path to a new scratch directory")
    parser.add_argument("--position", type=int, default=6)
    parser.add_argument("--ezkl-columns", type=int, choices=(2, 8, 16, 32, 56, 64),
                        help="Also estimate the EZKL circuit layout with the specified column count")
    parser.add_argument("--ezkl-witness-scale", type=int, choices=(7, 10, 12, 14, 16, 18),
                        help="Calibrate, compile, and check the EZKL witness at this fixed-point scale")
    parser.add_argument("--center-attention-logits", action="store_true",
                        help="Rebase fixed-point attention logits by explicitly centering before Softmax")
    parser.add_argument("--public-tensors", action="store_true",
                        help="Use public benchmark tensors in diagnostics, as in proof mode")
    parser.add_argument("--ezkl-logrows", type=int, help="Use an explicit domain with a known lookup range")
    parser.add_argument("--ezkl-lookup-range", type=int, nargs=2, metavar=("LOW", "HIGH"),
                        help="Reuse a measured lookup range instead of calibrating")
    parser.add_argument("--srs", type=Path, help="Trusted Halo2 setup. Also run setup, prove and independent verification")
    args = parser.parse_args(argv)
    if args.ezkl_witness_scale is not None and args.ezkl_columns is None:
        parser.error("--ezkl-witness-scale requires --ezkl-columns")
    if args.srs is not None and args.ezkl_witness_scale is None:
        parser.error("--srs requires --ezkl-witness-scale")
    lookup_range = tuple(args.ezkl_lookup_range) if args.ezkl_lookup_range else None
    if (args.ezkl_logrows is None) != (lookup_range is None):
        parser.error("--ezkl-logrows and --ezkl-lookup-range must be supplied together")
    if args.ezkl_logrows is not None and args.ezkl_witness_scale is None:
        parser.error("--ezkl-logrows requires --ezkl-witness-scale")
    srs = args.srs.resolve(strict=True) if args.srs is not None else None

    configure_cpu(2)
    args.output.mkdir()
    start = time.perf_counter()
    operators, expected = export_decode(args.checkpoint, args.output, args.position, args.center_attention_logits)
    print(f"Granite {REVISION}, layer 0 position {args.position}. CPU export parity passed in "
          f"{time.perf_counter() - start:.2f} seconds.", flush=True)
    print("Operators:", ", ".join(operators), flush=True)
    if args.ezkl_columns is not None:
        check_ezkl(args.output, args.ezkl_columns, expected, args.ezkl_witness_scale, srs,
                   public_tensors=args.public_tensors, logrows=args.ezkl_logrows, lookup_range=lookup_range)


if __name__ == "__main__":
    main()
