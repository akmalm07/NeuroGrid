"""The pinned IBM Granite BF16 inference runtime."""

import hashlib
import json
import math
import os
import warnings
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Literal, NotRequired, TypedDict

import numpy as np
import torch
from huggingface_hub import snapshot_download
from safetensors import safe_open
from transformers import AutoTokenizer, DynamicCache, GraniteConfig
from transformers.models.granite.modeling_granite import (
    GraniteDecoderLayer, GraniteRMSNorm, GraniteRotaryEmbedding,
)

from .activation import DTYPE, pack, unpack
from .manifest import CONTEXT, LAST_SHARD, MANIFEST, MODEL, REVISION, SHARD_COUNT, WINDOW

WIDTH = MANIFEST["width"]
VOCAB = MANIFEST["vocab"]
LAYERS_PER_SHARD = MANIFEST["layersPerShard"]
FILES: dict[str, str] = MANIFEST["sha256"]
INDEX = "model.safetensors.index.json"


class Payload(TypedDict, total=False):
    tokens: list[int]
    tensor: str


class PartitionResult(TypedDict):
    tensor: str
    token: NotRequired[int]


class CacheMiss(ValueError):
    """Decode requires the matching prefix state."""


class WindowsWeights:
    """Map individual BF16 tensors using 64-bit file offsets on Windows."""

    def __init__(self, path: Path):
        self.path = path
        with path.open("rb") as handle:
            size = int.from_bytes(handle.read(8), "little")
            if not 1 <= size <= 1_048_576:
                raise ValueError("Invalid safetensors header length")
            self.entries = json.loads(handle.read(size))
        self.base = 8 + size
        self.size = path.stat().st_size

    def get_tensor(self, name: str) -> torch.Tensor:
        entry = self.entries[name]
        shape = tuple(entry["shape"])
        start, end = entry["data_offsets"]
        if (entry["dtype"] != "BF16" or not shape or any(type(dimension) is not int or dimension <= 0 for dimension in shape)
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= self.size - self.base
                or end - start != math.prod(shape) * 2):
            raise ValueError("Invalid safetensors weight range")
        values = np.memmap(self.path, mode="r", dtype="<i2", offset=self.base + start, shape=shape)
        # Inference parameters are frozen and these file-backed pages are read-only.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="The given NumPy array is not writable.*", category=UserWarning)
            return torch.from_numpy(values).view(DTYPE)


def weight_reader(path: Path, stack: ExitStack):
    if os.name == "nt":
        return WindowsWeights(path)
    return stack.enter_context(safe_open(path, framework="pt", device="cpu"))


def checkpoint() -> Path:
    local_dir = None
    if cache := os.environ.get("NEUROGRID_MODEL_CACHE"):
        local_dir = Path(cache) / REVISION
    elif os.name == "nt":
        user_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        local_dir = user_data / "NeuroGrid" / "models" / REVISION
    if local_dir and all((local_dir / name).is_file() for name in FILES):
        return local_dir
    return Path(snapshot_download(
        MODEL, revision=REVISION, allow_patterns=list(FILES), local_dir=local_dir, token=False,
    ))


@lru_cache(maxsize=32)
def hash_file(path: Path, size: int, modified_ns: int) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verified_file(path: Path, name: str) -> Path:
    if name not in FILES:
        raise ValueError("Checkpoint index references an unpinned file")
    file = path / name
    info = file.stat()
    if hash_file(file, info.st_size, info.st_mtime_ns) != FILES[name]:
        raise ValueError(f"Model artifact checksum mismatch ({name})")
    return file


def configure_cpu(threads: int) -> int:
    torch.set_num_threads(max(1, threads))
    return torch.get_num_threads()


class Partition:
    def __init__(self, path: Path, index: int):
        if not 0 <= index < SHARD_COUNT:
            raise ValueError("Invalid partition")
        self.index = index
        self.config = GraniteConfig.from_json_file(str(verified_file(path, "config.json")))
        config = self.config
        if (config.num_hidden_layers, config.hidden_size, config.vocab_size) != (
            SHARD_COUNT * LAYERS_PER_SHARD, WIDTH, VOCAB,
        ):
            raise ValueError("Checkpoint architecture does not match the protocol")
        config._attn_implementation = MANIFEST["attention"]
        self.rotary = GraniteRotaryEmbedding(config)
        weight_map = json.loads(verified_file(path, INDEX).read_text(encoding="utf-8"))["weight_map"]
        start = index * LAYERS_PER_SHARD
        with torch.device("meta"):
            self.layers = [GraniteDecoderLayer(config, layer) for layer in range(start, start + LAYERS_PER_SHARD)]
            if index == LAST_SHARD:
                self.norm = GraniteRMSNorm(WIDTH, eps=config.rms_norm_eps)

        with ExitStack() as stack:
            files = {}

            def weight(name: str) -> torch.Tensor:
                filename = weight_map[name]
                if filename not in files:
                    files[filename] = weight_reader(verified_file(path, filename), stack)
                result = files[filename].get_tensor(name)
                if result.dtype != DTYPE:
                    raise ValueError("Checkpoint weight dtype does not match")
                return result

            for layer, module in enumerate(self.layers, start=start):
                module.load_state_dict({
                    key: weight(f"model.layers.{layer}.{key}") for key in module.state_dict()
                }, assign=True)
                module.eval().requires_grad_(False)
            if index == 0:
                self.embedding = weight("model.embed_tokens.weight")
            if index == LAST_SHARD:
                self.norm.load_state_dict({"weight": weight("model.norm.weight")}, assign=True)
                self.norm.eval().requires_grad_(False)
                self.head = weight("lm_head.weight")
        self.caches: dict[str, tuple[int, int, DynamicCache]] = {}

    @torch.inference_mode()
    def run(
        self, payload: Payload, length: int, *, position: int = 0, job: str = "", epoch: int = 0,
        emit: bool = True,
    ) -> PartitionResult:
        if type(position) is not int or type(length) is not int or not 1 <= length <= WINDOW \
                or not 0 <= position <= CONTEXT - length:
            raise ValueError("Invalid Granite token window")
        if self.index == 0:
            tokens = payload.get("tokens")
            if not isinstance(tokens, list) or len(tokens) != length:
                raise ValueError("Invalid token sequence")
            if any(type(token) is not int or not 0 <= token < VOCAB for token in tokens):
                raise ValueError("Invalid token ID")
            hidden = torch.nn.functional.embedding(torch.tensor([tokens]), self.embedding)
            hidden = hidden * self.config.embedding_multiplier
        else:
            hidden = unpack(payload["tensor"], length)

        if position == 0:
            self.caches.pop(job, None)
            if len(self.caches) >= 4:
                del self.caches[next(iter(self.caches))]
            cache = DynamicCache(config=self.config)
        else:
            previous = self.caches.pop(job, None)
            if not previous or previous[:2] != (epoch, position):
                raise CacheMiss("Missing Granite cache for the requested prefix and epoch")
            cache = previous[2]

        positions = torch.arange(position, position + length)
        keys = torch.arange(position + length)
        blocked = keys[None] > positions[:, None]
        mask = torch.zeros((length, position + length), dtype=DTYPE).masked_fill_(blocked, float("-inf"))
        rotary = self.rotary(hidden, positions[None])
        for layer in self.layers:
            hidden = layer(
                hidden, attention_mask=mask[None, None], position_ids=positions[None],
                position_embeddings=rotary, cache_position=positions, past_key_values=cache, use_cache=True,
            )[0]
        self.caches[job] = (epoch, position + length, cache)
        result: PartitionResult = {"tensor": pack(hidden)}
        if self.index == LAST_SHARD and emit:
            logits = torch.nn.functional.linear(self.norm(hidden[:, -1]), self.head) / self.config.logits_scaling
            result["token"] = int(logits.argmax())
        return result


def tokenizer_for(path: Path):
    for name in FILES:
        if name.endswith(".json") or name.endswith(".jinja"):
            verified_file(path, name)
    return AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)


def tokenize_prompt(tokenizer, prompt: str) -> list[int]:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 32 * 1024:
        raise ValueError("Provide a nonempty prompt of at most 32 KiB")
    tokens = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}], add_generation_prompt=True,
        enable_thinking=MANIFEST["decoding"]["enable_thinking"], tokenize=True, return_dict=False,
    )
    if not 1 <= len(tokens) < CONTEXT:
        raise ValueError("The formatted prompt leaves no room for generation")
    return tokens


def matches_activation(submitted: str, expected: str, length: int) -> bool:
    try:
        value = unpack(submitted, length)
    except (ValueError, TypeError):
        return False
    return bool(torch.allclose(value, unpack(expected, length), rtol=1e-2, atol=1e-2))


class Generation(TypedDict):
    text: str
    tokens: list[int]
    model: str
    revision: str
    execution: Literal["bf16-reference"]


class Granite:
    def __init__(self, path: Path):
        self.tokenizer = tokenizer_for(path)
        self.partitions = [Partition(path, index) for index in range(SHARD_COUNT)]
        self._generation = Lock()

    def _window(self, tokens: list[int], position: int, *, emit: bool) -> PartitionResult:
        result = self.partitions[0].run({"tokens": tokens}, len(tokens), position=position, emit=emit)
        for stage in self.partitions[1:]:
            result = stage.run({"tensor": result["tensor"]}, len(tokens), position=position, emit=emit)
        return result

    def generate(self, prompt: str, *, max_tokens: int = 16) -> Generation:
        tokens = tokenize_prompt(self.tokenizer, prompt)
        if type(max_tokens) is not int or not 1 <= max_tokens <= CONTEXT - len(tokens):
            raise ValueError("Generation length exceeds the available context")
        generated: list[int] = []
        with self._generation:
            try:
                for position in range(0, len(tokens), WINDOW):
                    window = tokens[position:position + WINDOW]
                    result = self._window(window, position, emit=position + len(window) == len(tokens))
                for step in range(max_tokens):
                    token = result["token"]
                    generated.append(token)
                    if token == self.tokenizer.eos_token_id or step + 1 == max_tokens:
                        break
                    result = self._window([token], len(tokens) + step, emit=True)
            finally:
                for stage in self.partitions:
                    stage.caches.pop("", None)
        return {"text": self.tokenizer.decode(generated, skip_special_tokens=True), "tokens": generated,
                "model": MODEL, "revision": REVISION, "execution": "bf16-reference"}
