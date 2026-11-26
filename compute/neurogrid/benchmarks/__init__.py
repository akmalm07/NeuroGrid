"""Reproducible inference and proving benchmarks."""

from importlib import import_module

COMMANDS = {
    "granite-decode": "benchmarks.granite_decode",
    "granite-audit": "benchmarks.granite_audit",
    "granite-segments": "benchmarks.granite_segments",
    "granite-projection": "benchmarks.granite_projection",
    "hybrid-projection": "benchmarks.hybrid_projection",
    "segmented": "proofs.segmented",
    "small-hybrid": "proofs.hybrid_response",
    "compact": "proofs.compact",
    "halo2": "proofs.halo2",
}


def run(name: str, arguments: list[str]) -> None:
    """Run the selected backend with its documented benchmark arguments."""
    if name not in COMMANDS:
        raise ValueError(f"Unknown benchmark {name!r}")
    import_module(f"neurogrid.{COMMANDS[name]}").main(arguments)
