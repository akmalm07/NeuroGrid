"""Canonical BF16 activation encoding for partitioned inference."""

import base64

import numpy as np
import torch

from .manifest import MANIFEST, WINDOW

WIDTH = MANIFEST["width"]
DTYPE = torch.bfloat16


def pack(tensor: torch.Tensor) -> str:
    words = tensor.detach().contiguous().view(torch.int16).numpy().astype("<i2", copy=False)
    return base64.b64encode(words).decode("ascii")


def unpack(encoded: str, length: int) -> torch.Tensor:
    if type(length) is not int or not 1 <= length <= WINDOW:
        raise ValueError("Invalid activation window")
    expected_bytes = length * WIDTH * 2
    if not isinstance(encoded, str) or len(encoded) != 4 * ((expected_bytes + 2) // 3):
        raise ValueError("Invalid tensor size")
    values = np.frombuffer(base64.b64decode(encoded, validate=True), dtype="<i2")
    if values.nbytes != expected_bytes:
        raise ValueError("Invalid activation")
    tensor = torch.from_numpy(values.astype(np.int16, copy=True)).view(DTYPE).reshape(1, length, WIDTH)
    if not torch.isfinite(tensor).all():
        raise ValueError("Invalid activation")
    return tensor
