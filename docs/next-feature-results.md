# NeuroGrid next-release audit and test evidence

November 24, 2026. Audited `main` at `09ca93a2a774b74af5c8a213332aa4a5a2a7396b` and tested the settings changes in the working tree. Existing changes to the architecture, handoff, README, installer overview, and product documentation were present before implementation. The settings work uses the existing node package and legacy coordinator.

This report records the earlier settings milestone. See the [Granite candidate evidence](granite-beta-results.md) for the current runtime and release gates.

## Audit

| Area | Entry point and observed baseline | Next-release gap |
| --- | --- | --- |
| Desktop and headless | The Windows Start shortcut and the Linux desktop launcher call `neurogrid_node.desktop`. The old window reads QSettings on launch and writes compute limits at Start. Close hides the window when a tray exists. Sign-in launch uses the Windows Run key or a Linux desktop autostart entry. Both launchers and the headless client use the existing Python worker loop. | Shared durable settings, independent Save and Cancel, and saved versus effective status were missing. |
| Boot | The Windows installer creates a desktop shortcut. The Linux four-role host installer provisions a service account, runs three systemd workers plus the reference, and uses systemd credentials. The single-node CLI runs in the foreground. | Single-contributor services, service enrollment and controls, and no-login reboot evidence on both systems. |
| Model and chat | Chat sends the prompt, a request key, and the selected output limit to the authenticated Hono route. The SQLite coordinator uses SmolLM2 with three ten-layer CPU partitions, a 128-token context, and a 24-token output cap. | Pinned Granite 4.2 8B execution, automatic limits, quote binding, and two-credit Paris replies. |
| Routing | The worker answers coordinator latency challenges through the node ping API. The coordinator selects a fresh eligible worker per shard by recent RTT and assignment history, then retries expired leases. | Trusted edge hints, measured ingress and peer links, capacity-aware selection of complete routes, and KV affinity. |
| Verification | The reference process tokenizes and recomputes each served partition result. The ledger carries a work digest and the UI can verify the ledger hash chain. | Attested runtime admission, linked receipts, independent verification, and service-path verification without inference replay. |
| Payments and ledger | A signed Stripe Checkout event is checked before reaching the coordinator. The coordinator transitions orders, jobs, holds, and ledger entries in SQLite transactions. Legacy purchases transfer treasury inventory. The live tests exercised job idempotency and refunds. | Paid issuance and retirement, concurrent holds, reversals, migration reconciliation, and actual Stripe sandbox events. |

## Desktop and packaged settings

The published v0.1.5 Windows code installed by the Start shortcut and the v0.1.5 Linux release archive both matched the audited desktop source. Its newline-normalized SHA-256 was `1d84c56e47d729268902bc5b42a96d899a56e75805b6460f8e824339da7b7fab`. On both systems the installed window retained an unsaved change to two threads and 35% when reopened from the tray. A fresh process restored the original one thread and 75%.

The candidate preserves legacy QSettings on disk and imports the saved site, threads, duty cycle, and sign-in startup preference into a versioned profile. The desktop and headless CLI share validation and atomic updates. The desktop keyring continues to hold the desktop credential. Profile-based worker processes report effective limits and hold an exclusive local ownership lock. The installed Windows and Linux candidates saved one thread and 45% before Start, restored the values in a fresh desktop process, and returned the same values through the packaged headless launcher. The Linux profile had mode `0600`.

| Candidate archive | SHA-256 |
| --- | --- |
| `NeuroGrid-Node-Windows-x64.zip` | `b24a583cb128b9376ebe8389a2f197271aa74966a4981a65a81adabae6508ff1` |
| `NeuroGrid-Node-Linux-x64.tar.gz` | `b2264a09851a3950ddbcdb2bf2282889bdd5d2ec393874c2f6fb827368a14d0e` |

The source-built candidates retain v0.1.5 package metadata. Windows ran on Windows 11 Home build 26300 with Python 3.12 and the native Qt tray. Linux ran on Ubuntu 24.04.5 with Python 3.12.3, Xvfb, and trayer in a Docker Desktop container. The Linux baseline and upgrade used the packaged installer with separate scratch settings. Windows used the packaged headless installer in a clean directory, installed the desktop extra into that environment, and upgraded the extracted candidate there. The installed user's Windows profile was used only as the migration source.

## Commands and results

| Check | Observed result |
| --- | --- |
| `git status --short --branch`, `git log -8 --oneline`, `git diff --check` | Audited the dirty working tree before changing code. Diff whitespace check passed. |
| `npm run validate`, `npm run lint`, `npm run build`, `npm audit --audit-level=moderate` | TypeScript, 19 Vitest tests, lint, and the build passed. npm audit reported zero known vulnerabilities. TypeScript and Vitest also passed on Node 22.23.5. |
| `npx deepspace test run all` with Node 22.23.5, npm 11.6.2, `NEUROGRID_TEST_URL=http://127.0.0.1:5182` and `NEUROGRID_TEST_INFERENCE=1` | All 13 browser checks and 19 unit tests passed. Used an isolated local Vite and SQLite instance, two saved DeepSpace test accounts, three CPU workers, and one reference verifier. The signed-in inference and history check took 10.7 seconds. The initial Node 25 run was refused. An isolated rerun passed after supplying the complete public JWT verification key and running Vite directly. |
| `py -3.12 packaging/package.py windows` and `py -3.12 packaging/package.py linux`, then packaged installers, Qt checks, `config show`, and `status` | Built and checked both archives. Both installed desktops restored saved limits after quitting and reopening. The legacy keyring behavior was exercised with a missing backend in the Qt test. |
| Installed Python `-m unittest discover -s compute/tests -p "test_*.py" -v` on Windows and Ubuntu | Ten tests passed on each system. Coverage includes migration, Cancel, invalid saves, interrupted replacement, status readback, and worker ownership. The Qt test checked that saved edits left the current worker's effective limits in place. Real SmolLM2 inference matched 24 of 24 greedy tokens against a full model on each system. The reference rejected an invalid activation. |
| `compute/tests/live_check.py --url http://127.0.0.1:5182 --model <cache> --accounts <private fixture>` with the installed Windows candidate | All three real partition workers reported one effective PyTorch thread, the configured 70% duty cycle, and a successful heartbeat. A disconnected worker was replaced and settled four tokens exactly once at the legacy 13-credit rate in 20.9 seconds. A malformed activation was rejected and refunded once in 15.3 seconds. |
| Same live harness with the installed Ubuntu candidate | Passed through a local TCP forward to the isolated Windows coordinator. The first attempt timed out after the workers submitted partition results for three token steps. The retry confirmed the effective limits and heartbeats, replaced a disconnected worker, and settled once in 20.3 seconds. A malformed activation was rejected and refunded once in 15.0 seconds. |
| Native Qt tray and a real job with each installed candidate | Opened **Compute limits** while the desktop worker was serving its partition, saved 45%, observed 70% remain effective for that run, and paused cleanly. Both runs returned “The capital of France is Paris.” Each used eight generated tokens and charged 25 legacy credits. |

The model used above was SmolLM2-135M-Instruct at revision `12fd25f77366fa6b3b4b768ec3050bf629380bac`, using trusted reference recomputation.

## Outstanding release gates

- The single-contributor Windows and Linux boot services remain unimplemented. The Windows test session lacked administrator privileges. The Linux test environment was a container without systemd as PID 1. No unattended reboot, crash recovery, delayed networking, service credential rotation, or uninstall result was collected.
- The Granite adapter, automatic chat limits, receipt-based serving and public verification, complete-route scheduling, and prepaid issuance and settlement remain unimplemented. The local GPU was a 4 GiB GTX 1650 SUPER. No compatible confidential-compute hosts or independent attested participants were available. The paid catalog excludes TwIL-8B.
- The two specified Paris prompts at exactly two credits, the 50-request run, measured route benchmarks, attestation and proof milestones, and hosted verification are pending. The legacy tests used the local coordinator and its existing pricing and verification modes.
- The available Stripe configuration was live mode. The checkout browser test used a test response for UI coverage. Actual sandbox events, concurrent prepaid holds, reversals, and migration reconciliation are pending.
