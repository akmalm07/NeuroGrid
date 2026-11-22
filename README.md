# NeuroGrid

NeuroGrid runs **SmolLM2-135M-Instruct** across three volunteer CPU nodes. Each node hosts ten transformer layers. A trusted reference recomputes each partition result, and a successful request pays the workers from the requester's compute credits. The web app shows the current network, your balance, and a verifiable history of credit movement.

[Live website](https://neurogrid.app.space) · [Demo video](https://github.com/akmalm07/NeuroGrid/releases/download/v0.1.5/neurogrid-demo.mp4) · [Desktop downloads](https://github.com/akmalm07/NeuroGrid/releases) · [Architecture](architecture.md)

## Try the website

| Page | What to do |
| --- | --- |
| **Home** | Choose **Try NeuroGrid** to chat or **Start Computing** to run a node. |
| **Chat** | Sign in with GitHub or Google, choose a maximum token count, and send a short prompt. Your balance and recent requests appear on the page. New accounts receive 100 credits while the treasury can fund starter grants. |
| **Network** | Check that one worker is online for each partition and the reference verifier is online. The request pipeline and worker statuses update as the network runs. |
| **Ledger** | Inspect reservations, verified settlements, refunds, and purchases. Use **Verify ledger** to replay the displayed chain from genesis, or **Export JSON** to keep a copy. |
| **Downloads** | Install and pair a Windows or Linux node. Create a separate key for each installation and revoke keys you no longer use. |

Chat handles one request at a time. It has a 128-token total context and generates up to 24 tokens per request. Each prompt starts a fresh context. The small model can give incomplete or inaccurate answers. Use prompts you are comfortable sharing with volunteer workers.

## Run a compute node

Download and extract the installer for Windows 10/11 x86-64 or Linux x86-64 (Ubuntu 24.04 was tested). Install Python 3.12, with the Python Launcher on Windows or the distribution's Python 3.12 venv package on Linux. Allow 4 GB of RAM and 4 GB of disk space. The package uses pinned CPU PyTorch and Transformers versions and downloads the model on first use.

- **Windows**. In the extracted folder run `powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-NeuroGrid.ps1`. Open **NeuroGrid Node** from the Start menu.
- **Linux**. In the extracted folder run `./install-neurogrid.sh`. Open **NeuroGrid Node** from the application menu.

Sign in on Downloads and create a key using automatic assignment or a selected partition. Paste the site URL and key into the node, set a CPU thread count and duty cycle, and click **Start**. The window and system tray show the node's status. **Pause** stops new work. **Quit** exits the process. **Launch and resume when I sign in** is opt-in, and auto-resume needs a saved key in the operating-system keyring. Earnings are credited to the account that owns the key and shown on Chat.

For a headless installation, pass `-Headless` to the Windows installer or `--headless` to the Linux installer. The foreground CLI prints its partition and work status. Ctrl+C stops it.

| Platform | Help | Run after setting `NEUROGRID_SECRET` in that shell |
| --- | --- | --- |
| Windows | `.\neurogrid-agent.cmd --help` | `.\neurogrid-agent.cmd node --url https://neurogrid.app.space --threads 2 --duty 50` |
| Linux | `./neurogrid-agent --help` | `./neurogrid-agent node --url https://neurogrid.app.space --threads 2 --duty 50` |

See the [installation guide](packaging/INSTALL.md) for commands that prompt for your key without echoing it, startup behavior, and upgrade instructions. Release assets include SHA-256 checksums and require a network connection for dependency installation.

## Run the full stack locally

The minimum inference setup is **one local web server command, three Python CLI workers, and one trusted Python verifier**. DeepSpace starts Vite and the local Cloudflare Worker runtime together. Use a browser for Chat. The desktop GUI, a GPU, and Stripe are optional for this setup. All four Python roles can share one machine, virtual environment, and model cache. Each worker requires its own node key.

### 1. Run the web app

Use a Node version supported by the pinned DeepSpace SDK. The audit used **Node 22.23.5 and npm 11.6.2**. Node 24 is also supported.

```sh
npm ci
npx deepspace auth whoami
# If you are signed out, run npx deepspace auth login.
npm run dev
```

Open **http://127.0.0.1:5173**. The app's SQLite state is local and separate from production. DeepSpace still provides authentication over the network. The loopback URL avoids Windows IPv6 fallback delays on a server listening on IPv4.

For an isolated local configuration on your DeepSpace app, create it with `npx deepspace secrets configs create dev`. Generate a high-entropy verifier secret and pipe it into `npx deepspace secrets set NEUROGRID_VERIFIER_SECRET --config=dev --stdin`. Retain it in your password manager for the verifier process. Start the server with `npm run dev -- --secrets-config=dev`. DeepSpace materializes local Worker bindings when it starts. Keep `.env`, `.dev.vars`, keys, authentication files, and test credentials out of Git.

### 2. Install the Python package

On Windows PowerShell from the repository root, create and activate a Python 3.12 venv.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On Linux use `python3.12 -m venv .venv` and `source .venv/bin/activate`. Once the environment is active, install CPU PyTorch and the source package.

```sh
python -m pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.7.1+cpu"
python -m pip install -e ./compute
```

Use `python -m pip install -e "./compute[desktop]"` for the optional PySide6 interface. You can pass `--model /path/to/checkpoint` to the CLI if you already have the pinned snapshot on disk.

### 3. Start the four CPU processes

Set `NEUROGRID_SECRET` to the **verifier** credential in its own terminal.

```sh
neurogrid-agent reference --url http://127.0.0.1:5173 --threads 2
```

Sign in on the local Downloads page and create one key for each partition. In three separate terminals, set `NEUROGRID_SECRET` to that terminal's **node** key and run:

```sh
neurogrid-agent node --url http://127.0.0.1:5173 --threads 2
```

When Network shows three online workers and an online verifier, open Chat and try **What is the capital of France?** A maximum of 16 tokens was enough for the audited response. After it settles, open Ledger and verify the chain. Leave the local web server running if you want the website after stopping its CPU clients. Inference resumes when all four clients are available again.

## How it works

| Component | Source | Responsibility |
| --- | --- | --- |
| React website | [`src/pages`](src/pages) | Public pages, authenticated chat, balances, node enrollment, and downloads |
| DeepSpace Worker | [`worker.ts`](worker.ts), [`src/neurogrid/routes.ts`](src/neurogrid/routes.ts) | Hono HTTP routes, DeepSpace JWT verification, RecordRoom routing, and Stripe Checkout |
| Coordinator | [`src/neurogrid/coordinator.ts`](src/neurogrid/coordinator.ts) | SQLite-backed Durable Object that owns node keys, scheduling, jobs, balances, reservations, and ledger blocks |
| CPU node and verifier | [`compute/neurogrid_node`](compute/neurogrid_node) | Load the pinned checkpoint, run the partitions on CPU, verify results, and provide CLI and desktop entry points |
| Installer release workflow | [`.github/workflows/node-release.yml`](.github/workflows/node-release.yml) | Package and test Windows and Linux archives, then publish assets and checksums when a version tag is pushed |

The verifier tokenizes the prompt, then the coordinator assigns a lease for each partition in turn. For each new token, the three workers run embeddings and layers 1–10, layers 11–20, and layers 21–30 plus the output head. The verifier compares tensors and greedy tokens, and the coordinator forwards the verified activation. The scheduler prefers recent low-latency polling workers. A timed-out lease can go to another worker. SQLite commits credit changes with the corresponding ledger record.

DeepSpace supplies auth, hosting, RecordRoom subscriptions, and runtime bindings for Durable Objects. The public sync record signals revision changes. Browsers fetch the authoritative snapshot over HTTP and poll if sync is interrupted. Stripe supplies optional hosted checkout. Hugging Face serves the pinned model artifacts. StoryNest, ThreadHunt, and RunningMap are DeepSpace examples and are outside the runtime.

## Credit mechanics and Stripe

Genesis places **100,000 credits** in the treasury. Those same credits circulate among the treasury, accounts, and reserves. Credits can pay for inference and cannot be redeemed for cash or transferred between users. Before generation the coordinator reserves `1 + 3 × maximum output tokens`. At settlement it charges one credit per verified partition step plus one treasury credit, pays the node owners, and releases unused reserves. A failed job returns the full reservation with no worker rewards.

When Stripe is enabled, users can quote **100–10,000 whole credits** in Chat at **100 credits per $1 USD**, starting at **$1.00**. They sign in before opening hosted Checkout. A purchase transfers treasury credits into the buyer's account. A signed event with the expected currency and amount settles the order exactly once. A confirmed expiration or failed asynchronous payment releases the reservation. The browser success page only shows payment status messaging.

Set these Worker secrets through DeepSpace. Use a separate Stripe sandbox for local testing.

| Secret | Purpose |
| --- | --- |
| `NEUROGRID_VERIFIER_SECRET` | Shared credential between the coordinator and the operator's reference process |
| `NEUROGRID_STRIPE_KEY` | Stripe restricted API key with Checkout Session creation permission |
| `NEUROGRID_STRIPE_WEBHOOK` | Signing secret for the Stripe event destination |
| `NEUROGRID_STRIPE_ORIGIN` | Exact application origin without a trailing slash |

Live mode requires the configured HTTPS site. Stripe sandbox keys can use an exact local origin, such as `http://127.0.0.1:5173`. Configure webhook forwarding into that same local server. In hosted Stripe, use `/api/grid/stripe/webhook` on the exact HTTPS application origin, snapshot event payloads, and these event types:

- `checkout.session.completed`
- `checkout.session.expired`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`

Checkout order retries reuse a Stripe idempotency key for at most 23 hours. If a pending order remains afterward, use its ledger reference to locate the Stripe session and replay its terminal event after reviewing its payment status. An order without a matching Stripe session requires manual reconciliation. Refunds, disputes, tax configuration, and Stripe key rotation are operator responsibilities. [Stripe fulfillment guide](https://docs.stripe.com/checkout/fulfillment)

## Run the tests

```sh
npm run validate
npm run lint
npm run build
npm audit
python -m unittest discover -s compute/tests -p "test_*.py" -v
npx playwright install chromium
```

The Python inference suite compares token IDs with an independently loaded full Hugging Face model. `NEUROGRID_MODEL` can point to its local snapshot. For the live protocol checks, use an idle local or staging instance with two DeepSpace test accounts and no other workers. Create the accounts with `npx deepspace test accounts create` and put only their `email` and `password` values in a JSON array **outside the repository**. Use `npx deepspace test accounts create --help` for the options.

In a Python terminal set `NEUROGRID_SECRET` to the instance's verifier key. The harness starts the reference and workers, tests disconnection and invalid activations, checks credit settlement and node ownership, and revokes its temporary keys on exit. Add `--keep-online` if you want it to leave the three workers and verifier running until you press Ctrl+C.

```sh
python compute/tests/live_check.py --url http://127.0.0.1:5173 --model /absolute/path/to/checkpoint --accounts /private/test-accounts.json --keep-online
```

Run browser tests against the existing local web server. Set these variables in a separate terminal and run the full suite.

```powershell
$env:NEUROGRID_TEST_URL = `http://127.0.0.1:5173`
$env:NEUROGRID_TEST_ACCOUNTS = `C:\private\test-accounts.json`
$env:NEUROGRID_TEST_INFERENCE = `1`
npx playwright test --config tests/playwright.config.ts --workers 1
```

In Bash, export the same variables with your local credential path. The two-account test and live prompt test require the credentials file. The live prompt test also requires active CPU clients and enough requester credits. Omit `NEUROGRID_TEST_INFERENCE` to run only browser tests that can work without model capacity.

### MVP audit results

- The build, TypeScript checks, lint, 19 Vitest tests, and three Python tests passed. npm's audit reported zero known advisories for the resolved dependencies.
- Windows local CPU inference matched **24 of 24 greedy tokens** across three prompts against the full model. The verifier rejected an invalid activation.
- The local integration used two authenticated accounts, three Python worker processes, and one verifier on the same Windows host. A disconnected worker's lease was replaced, and four generated tokens settled exactly once in **20.3 seconds**. A malformed result was rejected and refunded exactly once in **15.0 seconds**.
- All **13 local browser checks** passed, including real DeepSpace WebSocket frames, separate account permissions, the credit quote, a real prompt, persisted chat history, sign-out, and ledger replay. The signed-in prompt and reload check took **12.6 seconds**. The demo records the actual response **The capital of France is Paris.**
- The deployed app passed **11 browser smoke checks** covering the public pages, live WebSocket frames, ledger verification, and the guest API boundary. The two signed-in scenarios ran against the local stack.
- Isolated Stripe Worker and SQLite checks accepted a signed simulated $2.53 payment once, even after event replay. An order below $1 was rejected. The production key is disabled on loopback. No real card was charged during this audit.
- Both v0.1.5 release jobs passed headless installation, menu registration, GUI launch, and CPU shard execution. The published Windows and Linux archive checksums matched on the audit computer. [Release workflow run](https://github.com/akmalm07/NeuroGrid/actions/runs/44971170678)

## Deploy and operate

For your own deployment, register a DeepSpace app and update its name and immutable app ID in `wrangler.toml` and `src/constants.ts`. Configure the verifier secret in the intended DeepSpace secrets config. After the build and tests pass, deploy and confirm the Network and Ledger pages.

```sh
npx deepspace deploy
```

Run the trusted verifier against the deployed HTTPS URL and keep at least one worker per partition online for a public demo. The hosted control plane can stay online independently of its CPU clients. When a partition is missing, Chat waits for capacity. View Worker logs with `npx deepspace logs` and release history with `npx deepspace releases`. [DeepSpace documentation](https://docs.deep.space/)

A single coordinator controls identities, scheduling, verification, and the hash chain. Hashes detect changes relative to a known chain head. Reference recomputation checks numerical agreement and cannot establish the physical machine that ran a shard. SQLite is enough for this trust model. Private chat history stays in the coordinator, while volunteer processes receive token inputs or intermediate activations. See [architecture.md](architecture.md) for the checkpoint, timeouts, and trust details.
