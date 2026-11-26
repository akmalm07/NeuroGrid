# NeuroGrid coding agent instructions

NeuroGrid is a reproducible inference and cryptographic proof research framework. Read [the architecture record](architecture.md) and the [reproducibility guide](docs/reproducibility.md) before changing proof or inference code.

- Work toward an independently verified complete Granite layer. Preserve the checkpoint's pinned weights, reference execution, and the fixed-point semantics of each proof protocol.
- Keep proving keys, witnesses, model checkpoints and private inputs out of Git. Verify from separately trusted public artifacts and issued statements.
- Preserve executable baselines and adversarial regressions where they record meaningful results. Put generated keys, logs, graphs and receipts in scratch directories.
- Run the applicable Python, Go and optional Kimchi tests with real fixtures. Record the actual workload, model revision, timing, peak memory, payload and unverified stages. Distinguish BF16 parity, projection proofs, complete small-model inference proofs and full Granite proofs.
- Preserve user work and avoid force-pushing or rewriting history. Obtain authorization before deploying or incurring costs.
