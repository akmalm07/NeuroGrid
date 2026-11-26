"""Export one weight row and a sample activation for the Granite proof benchmark."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import TypedDict

import torch

from neurogrid.models.manifest import REVISION
from neurogrid.models.granite import Partition, tokenize_prompt, tokenizer_for


class ProjectionSample(TypedDict):
    revision: str
    coefficientSha256: str
    coefficients: list[int]
    activation: list[int]
    expected: int


def fixed_point(values: torch.Tensor, bits: int) -> torch.Tensor:
    scaled = torch.round(values.float() * (1 << bits))
    if not torch.isfinite(scaled).all() or (scaled < -32768).any() or (scaled > 32767).any():
        raise ValueError("Projection values exceed the signed 16-bit range")
    return scaled.to(torch.int64)


@torch.inference_mode()
def sample(checkpoint: Path, prompt: str, position: int) -> ProjectionSample:
    tokenizer = tokenizer_for(checkpoint)
    tokens = tokenize_prompt(tokenizer, prompt)
    if not 0 <= position < len(tokens):
        raise ValueError("Token position lies outside the formatted prompt")
    partition = Partition(checkpoint, 0)
    layer = partition.layers[0]
    projection = layer.self_attn.q_proj
    if projection.bias is not None:
        raise ValueError("The projection circuit requires a bias-free projection")
    embedded = partition.embedding[tokens[position]] * partition.config.embedding_multiplier
    activation = fixed_point(layer.input_layernorm(embedded), 6)
    coefficients = fixed_point(projection.weight[0], 12)
    coefficient_bytes = coefficients.numpy().astype("<i2").tobytes()
    return {
        "revision": REVISION,
        "coefficientSha256": hashlib.sha256(coefficient_bytes).hexdigest(),
        "coefficients": coefficients.tolist(),
        "activation": activation.tolist(),
        "expected": int(torch.dot(activation, coefficients)),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--position", type=int, required=True)
    parser.add_argument("--prompt", default="What is the capital of France?")
    args = parser.parse_args(argv)
    torch.set_num_threads(2)
    artifact = sample(args.checkpoint, args.prompt, args.position)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(artifact, stream, separators=(",", ":"))


if __name__ == "__main__":
    main()
