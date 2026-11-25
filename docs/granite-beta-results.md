# Granite 4.2 8B candidate evidence

November 24, 2026. Repository base `09ca93a2a774b74af5c8a213332aa4a5a2a7396b` with local candidate changes. All inference and payments in this report used the isolated beta. The production app, Stripe configuration, and ledger stayed in place.

## Runtime

- The runtime uses `ibm-granite/granite-4.2-8b` at `a7042144125ce5ae64f89610e8289647fc000b9a`. It checks SHA-256 artifact digests and uses BF16 weights and activations, Transformers 4.57.1, PyTorch 2.7.1 CPU, SDPA, and greedy non-thinking decoding. [Pinned model card](https://huggingface.co/ibm-granite/granite-4.2-8b/tree/a7042144125ce5ae64f89610e8289647fc000b9a)
- Windows tests ran on Windows 11 build 26300 with Python 3.12, a Ryzen 5 5600X, 32 GiB RAM, four contributor processes, and a separate local reference process. The final `granite-bf16-greedy-v2` uses 32-token prefill windows. The model files are approximately 17.6 GB.
- The beta checks numerical output through trusted reference recomputation. Hardware-backed attestation and mathematical proof remain separate modes in the target architecture.
- Linux beta tests ran on Ubuntu 24.04.5 in Docker Desktop on the Windows test host.

## Checks

| Check | Observed result |
| --- | --- |
| `npm run validate`, `npm run lint`, `npm run build`, `npm audit --audit-level=moderate` | TypeScript, 23 Vitest tests, lint, and the build passed. npm audit reported zero known vulnerabilities. |
| Windows candidate installer and `python -m unittest discover -s compute/tests -p "test_*.py" -v` with the pinned cache | Headless installation, Qt smoke, and 11 tests passed. The official whole Granite model and sharded cache route produced nine matching greedy tokens across three prompts. Tampered activations and missing KV state were rejected. A Windows native mapping crash was fixed by mapping the verified BF16 tensor ranges with 64-bit offsets. Two additional boot-profile tests passed against both final packages. |
| `compute/tests/live_check.py --failover` on Windows with the real coordinator | The France prompt returned “The capital of France is Paris.” after losing the first worker and using a replacement. It took 86.8 seconds and charged 2 credits once, including replay of the request key. The literal Paris prompt explained that Paris is a city and France's capital and charged 2 credits once. It reached the 64-token output cap. |
| `compute/tests/live_check.py --context-only --extended` with the installed Windows package | A follow-up with 171 formatted input tokens replied “Kyoto” using the saved conversation. The Standard request generated 46 tokens, cost 2 credits, and returned the unused hold. The initial longer-history attempt exposed SQLite memory pressure and was refunded. Moving activations outside job rows and using 32-token windows resolved it in the rerun. |
| `compute/tests/live_routing_check.py` against the installed Windows workers | A real contributor over a controlled-delay loopback relay measured 1,277 ms. The scheduler pinned the direct 35 ms path for the first partition. The request completed with a real Granite reply at 2 credits. |
| `compute/tests/live_holds_check.py` against the same local coordinator | Three concurrent same-account holds were accepted and a fourth was denied for overspend. Revoking the test workers refunded all three holds in full, once each. |
| `npx deepspace test run all` with Node 22.23.5, local test accounts, and real worker capacity | 13 browser checks passed, including the quote, a Granite reply charged at 2 credits, history after reload, and public ledger verification. The signed-in inference test took 1.8 minutes under concurrent local load. The ledger test passed again after the three refunds and paginated back to genesis. |
| Stripe CLI 1.55.14, an isolated test profile, locally forwarded Stripe events | Created a real sandbox Checkout Session. Hosted Checkout presented an interactive verification challenge. The Stripe CLI's Checkout fixture completed a separate test session correlated to the local order. Stripe delivered a signed paid event and the app issued 100 credits once. Two replays, a forged signature, and a signed wrong amount exercised idempotency and validation. A genuine sandbox refund reversed 100 credits once, including two replays. |
| Service package checks | Windows PowerShell syntax parsed and the signed Python host passed Authenticode verification. The SCM dispatcher refused interactive invocation with the expected error 1063. Ubuntu installed both headless and desktop beta clients, passed Qt smoke, `bash -n`, and `systemd-analyze verify`, and produced a real 10,924-character base64 BF16 activation from partition 0. |

| Candidate archive | SHA-256 |
| --- | --- |
| `NeuroGrid-Node-Windows-x64.zip` | `3e6cec6967224527b08d7cd3a9196ac0e38cfaa4f4ece233ddeb1321b83276eb` |
| `NeuroGrid-Node-Linux-x64.tar.gz` | `5071f84b9b510215e63dc1dda8c716716d6d45362906d76a0da4996a9557cf02` |

## Release gates still open

- Windows service installation under an administrator account and unattended Windows and Linux reboots. The Windows session was unprivileged and the Ubuntu container did not boot systemd as PID 1.
- Hardware-backed receipt admission, independent receipt verification without inference replay, and mathematical proof benchmarking. This beta serves inference only on loopback and an explicitly configured staging origin. It can open Stripe sandbox purchases. The production configuration was preserved.
- Real regional, peer, NAT, and co-location routing benchmarks on independent computers. The current scheduler measures complete paths through the coordinator relay. The 35 ms and 1,277 ms observations came from local paths.
- Fifty completed Quick jobs from exactly 100 credits, end-to-end disputed charge reinstatement, and migration against a production ledger snapshot. Unit replay covers scale migration, new issuance, refunds, and spent-credit reversal. TwIL-8B is outside the paid catalog.
