# NeuroGrid

NeuroGrid runs **IBM Granite 4.2 8B** across four CPU worker processes. Chat uses recent completed turns as context and displays a token-counted quote before Send. The contributor package supports Windows and **Linux beta**.

The local and staging Granite beta uses trusted reference recomputation and supports sandbox payments. Hosted chat remains paused pending independent verification of a complete Granite reply without a serving-path rerun. [Architecture and proof tests](architecture.md#verification-and-trust)

## Chat

Sign in, enter a prompt, select **Get quote**, then confirm the maximum with **Send**. The final charge uses the formatted conversation and the number of model positions computed. It can be fractional and a Quick reply costs up to 2 credits. Failed and empty replies release the full hold.

The coordinator takes up to ten recent completed user-and-assistant pairs from the authenticated account's chat, up to 32 KiB. The pinned tokenizer drops the oldest pairs when the input exceeds its profile and preserves the latest prompt. Quick uses at most 256 formatted input and 64 generated tokens. Standard permits up to 4,096 total context and 512 generated tokens. The server lowers the output budget when necessary to fit the context or balance.

The public ledger keeps prompts, replies, and activations private. Workers and the reference operator receive model inputs and intermediate state.

## Local development

Use Node 22.15+ on the 22 line or Node 24, Python 3.12, and at least 25 GB free for the CPU node and checkpoint. A host running all four workers and the reference also needs substantial RAM. Expect slow generation on CPU.

```sh
npm ci
npx deepspace auth whoami
npm run dev
```

Configure the local verifier secret through DeepSpace and run on loopback. Use a separate, authorized staging app for hosted beta testing. Set `NEUROGRID_REFERENCE_BETA_ORIGIN` to its exact HTTPS origin.

Build both node archives with the existing packager. Follow the [installation guide](packaging/INSTALL.md) for desktop, headless, and boot setup.

```sh
python packaging/package.py windows
python packaging/package.py linux
```

To develop from source, install the CPU runtime and Python package in a Python 3.12 virtual environment.

```sh
python -m pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.7.1+cpu"
python -m pip install -e "./compute[desktop]"
```

Start the reference first. Create four node keys in Downloads, one per partition. Set `NEUROGRID_SECRET` separately for the verifier and each worker, then run the worker command in four terminals. Both commands accept `--model PATH` for an existing pinned checkpoint.

```sh
neurogrid-agent reference --url http://127.0.0.1:5173 --threads 2
neurogrid-agent node --url http://127.0.0.1:5173 --threads 2
```

## Routing and credits

The coordinator pins a complete route per request using fresh coordinator RTT, verified execution time, queue load, failures, and a trusted Cloudflare ingress hint. Activations travel through the coordinator over HTTPS. Workers cache KV state between prefill windows and tokens. Failover advances the lease epoch and rebuilds the prefix on the replacement route. DeepSpace RecordRoom carries product status updates.

The [shared Granite manifest](compute/neurogrid_node/granite.json) pins the checkpoint, file hashes, tokenizer, BF16 format, and deterministic decoding. The ledger verifies accounting history and work digests. Independent attestation and mathematical proofs are separate release gates.

The migration returns open legacy request holds and preserves balances, pending checkout holds, and historic ledger blocks. New entries use 1,000 subunits per credit. The treasury backs starter grants. Confirmed Stripe payments issue credits. Settlement returns any unused hold, distributes up to 70% of the final charge, and retires the rest. Reversals can leave a negative balance when purchased credits were already spent.

Use sandbox `NEUROGRID_STRIPE_KEY`, `NEUROGRID_STRIPE_WEBHOOK`, and the exact `NEUROGRID_STRIPE_ORIGIN` for this beta. Forward signed snapshot events to `/api/grid/stripe/webhook`.

- `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired`, and `checkout.session.async_payment_failed`
- `charge.refunded`, `charge.dispute.funds_withdrawn`, and `charge.dispute.funds_reinstated`

## Tests

```sh
npm run validate
npm run lint
npm run build
npm audit
npx deepspace test run all
python -m unittest discover -s compute/tests -p "test_*.py" -v
```

Set `NEUROGRID_MODEL` to the pinned BF16 checkpoint for the full-model parity tests. Set `NEUROGRID_TEST_URL`, `NEUROGRID_TEST_ACCOUNTS`, and `NEUROGRID_TEST_INFERENCE=1` for signed-in browser tests against a ready local app. Keep the account fixture outside Git.

```sh
python compute/tests/live_check.py --url http://127.0.0.1:5173 --model /path/to/granite --accounts /private/accounts.json --failover --extended
```

Set the isolated verifier key in the test process environment. The harness runs all four workers, exercises failover and conversational context, and revokes temporary node keys. Use `--keep-online` to retain capacity while running browser tests.
