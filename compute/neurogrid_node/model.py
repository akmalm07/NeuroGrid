"""CPU-only partitions for one immutable SmolLM2 checkpoint."""

import base64
import os
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import snapshot_download
from safetensors import safe_open
from transformers import AutoTokenizer, LlamaConfig
from transformers.models.llama.modeling_llama import (
    LlamaDecoderLayer,
    LlamaRMSNorm,
    LlamaRotaryEmbedding,
)

from . import REVISION

MODEL = "HuggingFaceTB/SmolLM2-135M-Instruct"
WIDTH = 576
CONTEXT = 128
VOCAB = 49152
MODEL_FILES = (
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "model.safetensors",
)


def checkpoint() -> Path:
    # On Windows the default hub cache may require symlink privileges when
    # multiple downloads probe it concurrently. A local snapshot uses files.
    local_dir = None
    if os.name == "nt":
        user_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        local_dir = user_data / "NeuroGrid" / "models" / REVISION
        if all((local_dir / name).is_file() for name in MODEL_FILES):
            return local_dir
    return Path(snapshot_download(
        MODEL,
        revision=REVISION,
        allow_patterns=list(MODEL_FILES),
        local_dir=local_dir,
        token=False,
    ))


def configure_cpu(threads: int) -> None:
    torch.set_num_threads(max(1, threads))


def pack(tensor: torch.Tensor) -> str:
    data = tensor.detach().contiguous().numpy().astype("<f4", copy=False)
    return base64.b64encode(data.tobytes()).decode("ascii")


def unpack(encoded: str, length: int) -> torch.Tensor:
    if type(length) is not int or not 1 <= length <= CONTEXT:
        raise ValueError("Invalid context length")
    expected_bytes = length * WIDTH * 4
    if not isinstance(encoded, str) or len(encoded) != 4 * ((expected_bytes + 2) // 3):
        raise ValueError("Invalid tensor size")
    values = np.frombuffer(base64.b64decode(encoded, validate=True), dtype="<f4")
    if values.size != length * WIDTH or not np.isfinite(values).all():
        raise ValueError("Invalid activation")
    return torch.from_numpy(values.astype(np.float32, copy=True)).view(1, length, WIDTH)


def load_weights(module, file, prefix: str) -> None:
    module.load_state_dict({key: file.get_tensor(prefix + key) for key in module.state_dict()})
    module.eval().requires_grad_(False)


class Partition:
    def __init__(self, path: Path, index: int):
        if index not in range(3):
            raise ValueError("Invalid partition")
        self.index = index
        config = LlamaConfig.from_pretrained(path, local_files_only=True)
        if (config.num_hidden_layers, config.hidden_size, config.vocab_size) != (30, WIDTH, VOCAB):
            raise ValueError("Checkpoint architecture does not match the protocol")
        config._attn_implementation = "eager"
        self.rotary = LlamaRotaryEmbedding(config)
        self.layers = [LlamaDecoderLayer(config, layer) for layer in range(index * 10, (index + 1) * 10)]
        with safe_open(path / "model.safetensors", framework="pt", device="cpu") as weights:
            for layer, module in enumerate(self.layers, start=index * 10):
                load_weights(module, weights, f"model.layers.{layer}.")
            if index == 0:
                self.embedding = weights.get_tensor("model.embed_tokens.weight").float()
            if index == 2:
                self.norm = LlamaRMSNorm(WIDTH, eps=config.rms_norm_eps)
                load_weights(self.norm, weights, "model.norm.")
                self.head = weights.get_tensor("model.embed_tokens.weight").float()

    @torch.inference_mode()
    def run(self, payload: dict, length: int) -> dict:
        if self.index == 0:
            tokens = payload["tokens"]
            if not isinstance(tokens, list) or len(tokens) != length or not 1 <= length <= CONTEXT:
                raise ValueError("Invalid token sequence")
            if any(type(token) is not int or not 0 <= token < VOCAB for token in tokens):
                raise ValueError("Invalid token ID")
            hidden = torch.nn.functional.embedding(torch.tensor([tokens]), self.embedding)
        else:
            hidden = unpack(payload["tensor"], length)
        positions = torch.arange(length)
        mask = torch.full((length, length), torch.finfo(torch.float32).min).triu(1)[None, None]
        rotary = self.rotary(hidden, positions[None])
        for layer in self.layers:
            hidden = layer(
                hidden, attention_mask=mask, position_embeddings=rotary, cache_position=positions,
            )[0]
        result = {"tensor": pack(hidden)}
        if self.index == 2:
            logits = torch.nn.functional.linear(self.norm(hidden[:, -1]), self.head)
            result["token"] = int(logits.argmax())
        return result


class Verifier:
    def __init__(self, path: Path):
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        self.partitions = [Partition(path, index) for index in range(3)]

    def process(self, work: dict) -> dict:
        if work["kind"] == "prepare":
            messages = [{"role": "user", "content": work["prompt"]}]
            tokens = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True)
            if len(tokens) + work["maxTokens"] > CONTEXT:
                return {"error": "context_limit"}
            return {"tokens": tokens}

        index, length = work["stage"], work["length"]
        expected = self.partitions[index].run(work["input"], length)
        try:
            submitted = unpack(work["candidate"]["tensor"], length)
        except (ValueError, TypeError, KeyError):
            return {"valid": False}
        canonical = unpack(expected["tensor"], length)
        if not torch.allclose(submitted, canonical, rtol=1e-4, atol=2e-4):
            return {"valid": False}
        if index < 2:
            return {"valid": True, "tensor": expected["tensor"]}
        token = expected["token"]
        if work["candidate"].get("token") != token:
            return {"valid": False}
        text = self.tokenizer.decode(work["generated"] + [token], skip_special_tokens=True)
        return {"valid": True, "token": token, "text": text, "eos": token == self.tokenizer.eos_token_id}
