"""Local inference and proof APIs. Verification loads only its selected backend."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .serialization import read_json

if TYPE_CHECKING:
    from .models.granite import Generation, Granite


def load_granite(checkpoint: Path | None = None, *, threads: int = 4) -> "Granite":
    """Load pinned Granite weights for deterministic BF16 reference inference."""
    from .models.granite import Granite, checkpoint as locate_checkpoint, configure_cpu

    if type(threads) is not int or threads < 1:
        raise ValueError("Thread count must be positive")
    configure_cpu(threads)
    return Granite(checkpoint if checkpoint is not None else locate_checkpoint())


def generate(prompt: str, *, checkpoint: Path | None = None, max_tokens: int = 16,
             threads: int = 4) -> "Generation":
    return load_granite(checkpoint, threads=threads).generate(prompt, max_tokens=max_tokens)


def prepare_small(root: Path) -> None:
    """Compile and generate local test keys for the segmented small transformer."""
    from .proofs.segmented import prepare, setup

    os.environ.setdefault("EZKL_REPO_PATH", str(root.resolve()))
    prepare(root)
    setup(root)


def generate_small(root: Path, prompt: list[int]) -> list[int]:
    """Run the pinned small-model witness evaluator and return the generated tokens."""
    from .proofs.segmented import request_for, witness_trace

    os.environ.setdefault("EZKL_REPO_PATH", str(root.resolve()))
    manifest = read_json(root / "public" / "manifest.json")
    for step in witness_trace(root, manifest, request_for(root, prompt)):
        if len(step) == 2:
            return step[0]
    raise RuntimeError("Incomplete small-transformer trace")


def issue_small(root: Path, prompt: list[int], request: Path) -> None:
    """Write an independently issued prompt, nonce and trusted model fingerprint."""
    from .proofs.segmented import issue

    issue(root, prompt, request)


def prove_small(root: Path, request: Path, bundle: Path) -> dict:
    """Produce every proof for the small-transformer request."""
    from .proofs.segmented import prove

    os.environ.setdefault("EZKL_REPO_PATH", str(root.resolve()))
    return prove(root, request, bundle, verbose=False)


def verify_small(public: Path, request: Path, bundle: Path) -> dict:
    """Verify the full response using separately trusted artifacts and request."""
    from .proofs.receipts import verify_bundle

    if bundle.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Proof bundle exceeds the verification size limit")
    return verify_bundle(public, read_json(request), read_json(bundle))


def verify_compact(binary: Path, public: Path, request: Path, receipt: Path,
                   *, output: Path | None = None) -> dict:
    """Verify a compact Granite projection without the model weights."""
    from .proofs.compact import verify

    return verify(binary.resolve(), public, request, receipt, output)
