# NeuroGrid

NeuroGrid runs a language model across three volunteer CPU processes. Ask a short question in Chat or install the desktop node to host one ten-layer partition. Verified work earns simulated compute credits.

- [Live website](https://neurogrid.app.space)
- [Windows and Linux releases](https://github.com/akmalm07/NeuroGrid/releases)
- [Model, scheduling and ledger architecture](architecture.md)

## Try it

Open Chat, sign in through GitHub or Google, and choose a prompt. Your account starts with 100 simulated credits while funds are available in the treasury. Chat is available while the trusted verifier and all three partitions are online. Network shows live coverage. Ledger shows credit reservations, verified settlements and refunds.

Use Downloads to pair a node. The Windows ZIP and Linux TAR bundle installers and launchers that set up Python 3.12 virtual environments with CPU PyTorch and PySide6. Installation and the first model load need internet access. [Installation instructions](packaging/INSTALL.md)

## Develop and deploy

Use Node 22.15+ or 24, npm 11.6+, and Python 3.12. To deploy your own copy, register a new DeepSpace app and use its name and immutable ID in `wrangler.toml` and `src/constants.ts`.

1. Run `npm ci` and `npx deepspace auth login`.
2. Generate a random verifier credential and pipe it to `npx deepspace secrets set NEUROGRID_VERIFIER_SECRET --stdin`. Retain it securely for the verifier process.
3. Run `npx deepspace dev start` for local development and `npx deepspace deploy` to deploy.
4. Set up Python with a desktop installer or create a Python 3.12 virtual environment. Install `torch==2.7.1+cpu` from the PyTorch CPU wheel index, then install `./compute` with pip.
5. Set the verifier credential in the reference process environment as `NEUROGRID_SECRET`. Run `neurogrid-agent reference --url https://YOUR-APP.app.space` on a trusted host.
6. Pair three volunteer applications and start one per partition. For separate isolated development processes, run `neurogrid-agent node --url http://localhost:5173` with each process's own node key as `NEUROGRID_SECRET`.

DeepSpace supplies OAuth, SQLite Durable Objects, the RecordRoom subscription and hosting. The integration catalog was reviewed. The pinned CPU model and reference checks use no external inference provider. GitHub publishes installer archives with SHA-256 checksums.

## Tests

- `npm run validate` type-checks the app and runs the ledger tests. `npm run lint` checks the app. `npm run build` produces browser and Worker bundles.
- `python -m unittest discover -s compute/tests -p 'test_*.py' -v` compares the three partitions with full Hugging Face inference and checks invalid activations. It downloads the pinned checkpoint unless `NEUROGRID_MODEL` points to a local copy.
- `compute/tests/live_check.py` exercises the hosted protocol with two DeepSpace test accounts, a trusted verifier and separate worker processes. Use an idle demo instance and two funded test accounts. Supply `--url`, `--model` and `--accounts`, where the accounts file is an external JSON array with two email and password objects. Export the verifier credential in the process environment.
- The release workflow extracts the archives, installs their dependencies and starts the PySide6 window on Windows and Linux with Qt's offscreen platform.

### MVP verification

- Local CPU comparison produced 24 matching greedy tokens over three prompts against the independently loaded full model. The Python tests also checked reference rejection of invalid activations.
- Hosted protocol checks used two authenticated test accounts and separate worker processes on one Windows host. A disappeared worker was replaced and the four-token request settled once in 31.7 seconds, including the lease timeout. A malformed activation was rejected and its reservation was refunded once in 15.0 seconds.
- The hosted browser suite exercised all five pages, the two primary actions, ledger verification, mobile layout and a signed-in chat request. The sampled four-token browser check, including authentication and a reload that confirmed the response persisted, took 13.8 seconds. Browser WebSocket frames contained the read-only DeepSpace grid status projection.
- Five ledger tests covered settlement, refund, tampering, overdraft and duplicate settlement.

The app accepts one active inference at a time. Credits have no monetary value. Since volunteer workers receive model inputs and activations, use prompts you can share.
