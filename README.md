# NeuroGrid

NeuroGrid runs a language model across three volunteer CPU processes. Ask a short question in Chat or install the desktop node to host one ten-layer partition. Verified work earns simulated compute credits.

- [Live website](https://neurogrid.app.space)
- [Windows and Linux releases](https://github.com/akmalm07/NeuroGrid/releases)
- [Model, scheduling and ledger architecture](architecture.md)

## Try it

Open Chat, sign in through GitHub or Google, and choose a prompt. Your account starts with 100 simulated credits while funds are available in the treasury. Chat is available while the trusted verifier and all three partitions are online. Network shows live coverage. Ledger shows credit reservations, verified settlements and refunds.

Use Downloads to pair a node. The Windows and Linux installers register **NeuroGrid Node** in the user's application menu. Both archives also include a headless installer option and a foreground CLI with node and work status. Installation and the first model load need internet access. [Desktop and headless instructions](packaging/INSTALL.md)

## Develop and deploy

Use Node 22.15+ or 24, npm 11.6+, and Python 3.12. To deploy your own copy, register a new DeepSpace app and use its name and immutable ID in `wrangler.toml` and `src/constants.ts`.

1. Run `npm ci` and `npx deepspace auth login`.
2. Generate a random verifier credential and pipe it to `npx deepspace secrets set NEUROGRID_VERIFIER_SECRET --stdin`. Retain it securely for the verifier process.
3. Run `npx deepspace dev start` for local development and `npx deepspace deploy` to deploy.
4. Set up Python with a desktop installer or create a Python 3.12 virtual environment. Install `torch==2.7.1+cpu` from the PyTorch CPU wheel index, then install `./compute` with pip.
5. Set the verifier credential in the reference process environment as `NEUROGRID_SECRET`. Run `neurogrid-agent reference --url https://YOUR-APP.app.space` on a trusted host.
6. Pair three volunteer applications and start one per partition. For separate isolated development processes, run `neurogrid-agent node --url http://localhost:5173` with each process's own node key as `NEUROGRID_SECRET`.

DeepSpace supplies OAuth, SQLite Durable Objects, the RecordRoom subscription and hosting. The integration catalog was reviewed. The pinned CPU model and reference checks use no external inference provider. GitHub publishes installer archives with SHA-256 checksums.

## Stripe Checkout

Checkout is disabled until configured. Signed-in users can still get 100 starter credits while the treasury can fund grants. Paid packs are an optional way to buy access to inference. The DeepSpace Worker creates Checkout Sessions through Stripe and verifies payment events. The Durable Object reserves credits before each session and updates the balance and ledger atomically after payment confirmation.

Configure all four values using the DeepSpace secrets store. Use an isolated DeepSpace app and Stripe sandbox to test before enabling real payments.

| Setting | Value |
| --- | --- |
| `NEUROGRID_STRIPE_KEY` | Stripe secret or restricted key with permission to create Checkout Sessions |
| `NEUROGRID_STRIPE_WEBHOOK` | Endpoint signing secret from Stripe |
| `NEUROGRID_STRIPE_ORIGIN` | Your app's HTTPS origin without a trailing slash |
| `NEUROGRID_STRIPE_PACK` | Pack size and USD cents separated by a colon, for example `500:500` |

In Stripe, point an event destination to `/api/grid/stripe/webhook` on that origin. Subscribe to `checkout.session.completed`, `checkout.session.expired`, `checkout.session.async_payment_succeeded`, and `checkout.session.async_payment_failed`. Complete an end-to-end sandbox purchase, expire an unpaid session, and resend a paid event before going live. Resolve outstanding test reservations in the sandbox.

Before accepting live payments, confirm the compute-credit offering and tax setup with your Stripe account, and establish a support process for payment refunds and disputes. They require operator reconciliation because credits may already have paid a worker. If a checkout request is interrupted, opening it again retries the same order within 23 hours. To resolve an older pending order, locate its reference in Stripe and resend the terminal event. Review orders with no corresponding session manually.

## Tests

- `npm run validate` type-checks the app and runs the ledger tests. `npm run lint` checks the app. `npm run build` produces browser and Worker bundles.
- `python -m unittest discover -s compute/tests -p 'test_*.py' -v` compares the three partitions with full Hugging Face inference and checks invalid activations. It downloads the pinned checkpoint unless `NEUROGRID_MODEL` points to a local copy.
- `compute/tests/live_check.py` exercises the hosted protocol with two DeepSpace test accounts, a trusted verifier and separate worker processes. Use an idle demo instance and two funded test accounts. Supply `--url`, `--model` and `--accounts`, where the accounts file is an external JSON array with two email and password objects. Export the verifier credential in the process environment.
- The release workflow checks the CLI on a headless install, verifies application menu entries, starts the PySide6 window with Qt's offscreen platform, downloads the pinned checkpoint, and evaluates a CPU shard.

### MVP verification

- Local CPU comparison produced 24 matching greedy tokens over three prompts against the independently loaded full model. The Python tests also checked reference rejection of invalid activations.
- Hosted protocol checks used two authenticated test accounts and separate worker processes on one Windows host. A disappeared worker was replaced and the four-token request settled once in 31.7 seconds, including the lease timeout. A malformed activation was rejected and its reservation was refunded once in 15.0 seconds.
- The deployed coordinator rejected a 73-credit request against a balance of 61. The balance and ledger head stayed unchanged.
- The hosted browser suite exercised all five pages, the two primary actions, ledger verification, mobile layout and a signed-in chat request. The sampled four-token browser check, including authentication and a reload that confirmed the response persisted, took 13.8 seconds. Browser WebSocket frames contained the read-only DeepSpace grid status projection.
- Five ledger tests covered settlement, refund, tampering, overdraft and duplicate settlement.
- The Windows and Linux install, launch, and CPU shard tests passed for v0.1.2. [Release workflow results](https://github.com/akmalm07/NeuroGrid/actions/runs/44879807628)

The app accepts one active inference at a time. Compute credits are nonredeemable and spendable on NeuroGrid inference. Since volunteer workers receive model inputs and activations, use prompts you can share.
