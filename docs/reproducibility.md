# Reproduce and verify proofs

Use the Python extras in the [README](../README.md). Use Go 1.26.8 or newer for the native module and Node 24 for the separate Kimchi baselines. The historical measurements used Windows 11 and a Ryzen 5 5600X.

## Complete small transformer

```sh
neurogrid small-prepare small
neurogrid small-issue small 0 1 small/request.json
neurogrid prove small small/request.json small/bundle.json
neurogrid small-issue small 2 3 small/request-b.json
neurogrid prove small small/request-b.json small/bundle-b.json
```

Install `./compute[verify]` in a clean environment. Copy only `small/public` from the trusted installation and the issued request and bundle to a separate directory. From there, run `neurogrid verify public request.json bundle.json`. Expected replies are `[1, 1]` and `[2, 3]`. Each receipt has 54 proof segments.

## Granite Q, K and V

Build `tensor` from `compute/native/tensor_gkr` with `go build -trimpath -o /absolute/path/to/tensor .` and use `tensor.exe` on Windows. Keep generated artifacts outside the checkout.

1. Export layer zero at token position six using `neurogrid benchmark granite-decode --checkpoint /path/to/checkpoint --output export --position 6 --center-attention-logits`.
2. Use `neurogrid benchmark granite-segments export tile --trial-rows 8 --tile-srs /path/to/kzg16.srs` to generate the exact Halo2 tile and pinned graph plan.
3. For each of `q`, `k` and `v`, run `neurogrid benchmark hybrid-projection case fixture tile --full-export export --projection q`, substituting the projection and output directory.
4. Generate model commitments and local rescale keys with `neurogrid benchmark compact setup /absolute/path/to/tensor fixture model`. Keep `model/public` with the independent verifier.
5. Run `neurogrid benchmark compact issue /absolute/path/to/tensor model/public fixture request`, then `neurogrid benchmark compact stage model fixture request job`.
6. Run `neurogrid benchmark compact prove /absolute/path/to/tensor job --key-pin-file model/rescale.pk.sha256`. For a warm benchmark pass multiple jobs with independently issued scopes after the binary.
7. Run `neurogrid benchmark compact verify /absolute/path/to/tensor model/public request job/projection.proof` using a separate public-artifact copy. Add `--output fixture/rounded.bin` to authenticate delivered Q18 values.

The [architecture record](../architecture.md) describes the setup assumptions and historical WASI and monolithic benchmarks. The Go WASI module and Node runner are optional for reproducing the headless baseline. The compact verifier uses the native executable.

## Test matrix

```sh
python -m unittest discover -s compute/tests -p 'test_*.py' -v
(cd compute/native/tensor_gkr && go test ./... && go vet ./...)
npm ci
npm run type-check
npm test
```

Set these variables for real-fixture tests.

| Variable | Expected data |
| --- | --- |
| `NEUROGRID_MODEL` | Pinned Granite checkpoint for BF16 parity |
| `NEUROGRID_SEGMENTED_CASE` | Small setup directory with both issued requests and both proof bundles |
| `NEUROGRID_HYBRID_FIXTURES` | Real `ref-tile`, `ref-tile-alt` and `bridge8` directories, plus the WASI module if using the headless baseline |
| `NEUROGRID_HYBRID_RESPONSES` | Complete small hybrid `small-one`, `small-two` and `small-isolated` directories |
| `NEUROGRID_GKR_BIN` | Optional native Go executable for the hybrid regression fixtures |
| `NEUROGRID_COMPACT_CASE` | Full projection fixture root with `setup-full`, `full-pinned`, `job-a`, `job-b`, `request-a` and `request-b` |
| `NEUROGRID_COMPACT_BIN` | Native Go executable for compact regressions |
| `NEUROGRID_PROJECTION_SAMPLE` | Checkpoint sample from `neurogrid benchmark granite-projection` for the Kimchi projection tests |

Use new output directories or file names when issuing requests. Trust the installed public model, keys and issued request independently of the proof. A local setup is suitable for reproducing experiments.

## Research refactor validation

Windows runs used the pinned Granite revision `a7042144125ce5ae64f89610e8289647fc000b9a` and generated fresh receipts from independently issued requests. The installed verifier wheel checked both complete small-model responses in a separate environment with EZKL and public files. It had neither Torch nor ONNX installed. The native compact verifier independently checked the real Q, K and V projections and all returned Q18 lanes.

| Run | Proving | Verification |
| --- | ---: | ---: |
| Segmented small, `[0, 1] → [1, 1]` | 88.98 s | Accepted all 54 segments |
| Segmented small, `[2, 3] → [2, 3]` during other validation jobs | 135.34 s | Accepted all 54 segments |
| Hybrid small, `[2, 3] → [2, 3]` with native GKR | 64.35 s | 1.58 s |
| Compact Granite Q, isolated run | 3.51 s | 0.24 s |
| Compact Granite Q, K and V during other validation jobs | 5.60 s, 1.58 s, 1.07 s | 0.37 s, 0.27 s, 0.19 s |

The isolated Q run committed 1.38 GiB of peak memory and transferred 143,114 verification bytes. Both segmented bundles retained 385,536 raw proof bytes, and the hybrid bundle retained 371,968. These samples use the existing trusted proving key setups. The observed slower contended samples are included alongside the previous measurements in the architecture record. No numerical rules or native proof algorithms changed.

All 27 Python proof regressions, three pinned Granite inference regressions, and ten Node proof regressions passed. The Go module built natively and passed vet. Both Go adversarial test groups passed in a headless Go WebAssembly build after Windows Application Control blocked the freshly generated native test harness. The separate archived website build, lint and 25 unit tests passed. Its browser suite reported ten passes, two credential-dependent skips and one failure against the external authentication proxy during the isolated audit.
