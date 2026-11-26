"""Command-line inference, proof generation and independent verification."""

import argparse
import json
from pathlib import Path

from . import api, benchmarks


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="neurogrid", description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    generate = modes.add_parser("generate", help="Generate text using pinned Granite BF16 weights")
    generate.add_argument("prompt")
    generate.add_argument("--model", type=Path)
    generate.add_argument("--max-tokens", type=int, default=16)
    generate.add_argument("--threads", type=int, default=4)
    for name in ("small-prepare", "small-generate", "small-issue", "prove"):
        mode = modes.add_parser(name)
        mode.add_argument("root", type=Path)
        if name in ("small-generate", "small-issue"):
            mode.add_argument("prompt", type=int, nargs=2)
        if name in ("small-issue", "prove"):
            mode.add_argument("request", type=Path)
        if name == "prove":
            mode.add_argument("bundle", type=Path)
    verify = modes.add_parser("verify", help="Verify a complete small-transformer proof bundle")
    for name in ("public", "request", "bundle"):
        verify.add_argument(name, type=Path)
    compact = modes.add_parser("verify-projection", help="Verify a compact Granite projection")
    for name in ("binary", "public", "request", "receipt"):
        compact.add_argument(name, type=Path)
    compact.add_argument("--output", type=Path)
    benchmark = modes.add_parser("benchmark", help="Run a reproducible proof benchmark")
    benchmark.add_argument("name", choices=benchmarks.COMMANDS)
    benchmark.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "generate":
                result = api.generate(args.prompt, checkpoint=args.model, max_tokens=args.max_tokens,
                                      threads=args.threads)
                print(json.dumps(result, ensure_ascii=False))
            case "small-prepare":
                api.prepare_small(args.root)
            case "small-generate":
                print(json.dumps({"reply": api.generate_small(args.root, args.prompt)}))
            case "small-issue":
                api.issue_small(args.root, args.prompt, args.request)
            case "prove":
                print(json.dumps(api.prove_small(args.root, args.request, args.bundle)))
            case "verify":
                print(json.dumps(api.verify_small(args.public, args.request, args.bundle)))
            case "verify-projection":
                api.verify_compact(args.binary, args.public, args.request, args.receipt, output=args.output)
            case "benchmark":
                benchmarks.run(args.name, args.arguments)
    except ModuleNotFoundError as error:
        parser.exit(1, f"neurogrid: missing optional backend dependency {error.name!r}. See the installation extras.\n")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        parser.exit(1, f"neurogrid: {error}\n")
