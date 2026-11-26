# NeuroGrid

NeuroGrid is a research framework for efficient, cryptographically verifiable LLM inference. It uses the original, pinned IBM Granite 4.2 8B weights and a BF16 reference runtime. The proof experiments retain exact Q18 arithmetic, verified tensor openings, and independently issued proof statements.

The segmented small transformer proves two generated tokens, including cached attention and greedy token selection. The compact Granite backend proves the real layer-zero Q, K and V projections. Full Granite attention, authenticated KV transitions, layer proofs, and complete token proofs are open research milestones.

## Start here

Use Python 3.12. Install only the backends you need.

```sh
python -m pip install -e "./compute[proofs]"
python -m pip install -e "./compute[granite,proofs]"
python -m pip install -e "./compute[verify]"
```

The first command installs small-model proving and benchmark tools. The second adds Granite execution and export. The third is for a separate verifier-only environment. For CPU-only Granite, install the matching PyTorch 2.7.1 CPU wheel from the PyTorch index first. Supply `--model` for offline runs. Without a local path, the loader fetches the pinned public revision.

```sh
neurogrid generate "What is the capital of France?" --model /path/to/checkpoint --max-tokens 8
neurogrid small-prepare small
neurogrid small-generate small 0 1
neurogrid small-issue small 0 1 request.json
neurogrid prove small request.json bundle.json
neurogrid verify small/public request.json bundle.json
neurogrid benchmark --help
```

`generate` reports BF16 reference execution. The small-model verifier needs a separately trusted public directory and the original issued request. It performs no inference.

## Programmatic use

```python
from pathlib import Path
from neurogrid.api import issue_small, prove_small, verify_small

root = Path("small")
request = Path("request.json")
bundle = Path("bundle.json")
issue_small(root, [0, 1], request)
prove_small(root, request, bundle)
print(verify_small(root / "public", request, bundle))
```

Use `load_granite` to reuse a loaded reference model and `verify_compact` to check a projection independently of the checkpoint. `neurogrid.benchmarks.run(name, arguments)` runs the same benchmarks as the CLI. The compact path uses the native Go executable. Node is used for the optional Kimchi and historical headless WASI baselines.

## Layout

| Path | Responsibility |
| --- | --- |
| `compute/neurogrid/models` | Pinned checkpoints, BF16 inference, activation encoding and the small transformer |
| `compute/neurogrid/proofs` | Halo2, segmented, hybrid and compact proof entry points and verifiers |
| `compute/native/tensor_gkr` | GKR, polynomial evaluation openings and Groth16 Q18 rescaling |
| `compute/neurogrid/benchmarks` | Granite export, numerical audit and projection fixture generation |
| `compute/tests` | Reference parity and adversarial proof regressions |
| `research/kimchi` | Original Node prover and independent verifier baselines |

See the [reproduction guide](docs/reproducibility.md) for fixture creation and the full test matrix. The [architecture record](architecture.md) retains the monolithic, segmented, hybrid and compact benchmark results. The last website revision is available at the Git tag `website-last-working`.
