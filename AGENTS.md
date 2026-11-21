# ShardNet — Coding agent instructions

## Mission

Ship a working DeepSpace application by Saturday, November 21, 2026, 11:59 PM America/New_York. Target deployment Friday and submission Saturday by 6 PM. ShardNet is a cooperative inference network: independently connected browsers host partitions of a neural network, perform useful inference on demand, and earn simulated compute credits. A hash-linked ledger records verified work and credit settlement. Users spend credits to request inference.

This is a **trusted-coordinator, PoUW-inspired prototype**, not a trustless blockchain, cryptocurrency, or production LLM service. Do not claim hashes prove computation or that browser tabs are independent physical machines. Originality is not an evaluation criterion; correctness and understanding of DeepSpace are.

Read [architecture.md](architecture.md) before implementing. It is the technical source of truth. Do not expand scope without approval.

## First steps

1. Read https://docs.deep.space/get-started/quickstart, https://docs.deep.space/guides/data-storage, https://docs.deep.space/guides/authentication, https://docs.deep.space/guides/external-apis and the actual scaffold/SDK types.
2. Use supported Node 22.15+ or 24. Run `npm create deepspace@latest shardnet`, or scaffold in the current near-empty repo if appropriate. Install the official agent guidance: `npx -y skills@latest add deepdotspace/deepspace-skill -y`. Do not guess SDK signatures.
3. Ask the human to complete `npx deepspace auth login` if needed. Run `npx deepspace dev start`. Deploy the scaffold early with `npx deepspace deploy` once authorized. Never expose auth tokens or commit .dev.vars.
4. Inspect `npx deepspace integrations list` and each selected endpoint with `npx deepspace integrations info <endpoint>`. The exercise recommends at least three integrations; distinguish native SDK capabilities from catalog endpoints. Do not invent calls or count mock calls.
5. Read the core workflow of https://github.com/deepdotspace/threadhunt and the SDK examples. Use platform primitives rather than rebuilding auth, sync, or hosting.

## P0 — submission-critical requirements

- Deployed web app with a digit drawing canvas, sample digits, a clear prediction and confidence distribution, live pipeline visualization, worker dashboard, credit balances and block explorer.
- Use one real offline-trained small classifier: scikit-learn digits dataset (8×8 grayscale, 10 classes), MLP 64→48→32→10. Train/export once using a reproducible Python script; inference in TypeScript with no paid AI or GPU dependency. See architecture.md for preprocessing and exact math. Commit weights/manifest and provenance plus measured held-out accuracy, not invented results.
- Separate three real model shards. Each volunteer browser joins explicitly and executes exactly its assigned shard in a Web Worker, then returns its activation to the server. Use two or more authenticated browser sessions to test cross-client execution. Provide an explicitly labeled single-browser demo with three Web Workers if insufficient independent volunteers are available. Never silently fake network activity.
- Server controls assignments, identities, leases, timeouts, verification, accounts, reservations, rewards, block append and access control. Clients cannot modify balances or ledger records.
- Real-time job and node status through DeepSpace data sync or presence. Persist authoritative tasks, balances, and blocks. Reloading must preserve settlement.
- Verify each shard against trusted deterministic reference inference; reject malformed, incorrect, late, unauthorized, or duplicate results. A job either settles once or refunds once. Show failure states in the UI.
- Use integer, simulated nonredeemable credits. No real money, token issuance beyond demo allocation, wallets, mining or stake.
- Public read-only landing/demo explanation; authenticate before shared writes or compute. Make onboarding/test instructions clear, avoid requiring extension installation.
- Test the deployed app, not merely local development or an HTTP 200. Include README, setup, architecture, model provenance, limitations, and an honest submission note.

## P1 — only after P0 works

Implement demand-funded availability bonuses with a capped budget and scarcity weighting if time allows (formula in architecture.md); otherwise use only computation rewards and display scarcity without pretending availability rewards exist. Optional automatic retry and better worker selection can follow the minimum timeout/refund path.

Native DeepSpace features: auth, data storage/sync, presence (if useful), server actions, deploy. External integration enhancement candidates: provider-backed vision/text explanation of a completed digit prediction, an optional second classifier comparison using a different verified provider, and opt-in completion notification through a supported messaging/email provider. These are NOT core inference and must never be called automatically or block the core. Confirm actual catalog availability, billing, permissions and product value first; prefer a finished app with honest rationale over three artificial paid calls. The recommended three-catalog-integration bar is a stretch goal, not a reason to fabricate usage. If external features are selected, document/test each and enforce limits and explicit consent for paid calls or messaging.

## Non-goals

No LLM, model training on volunteer devices, browser extension, arbitrary code execution, P2P discovery, cryptocurrency, actual payments, zero-knowledge proofs, trustless consensus, Docker cluster, external GPU servers, model marketplace or multiple workloads. Do not implement custom OAuth or websocket system if SDK sync suffices.

## Implementation order / go-no-go checkpoints

| Stage | Exit criterion | Budget (estimate) |
|---|---|---|
| 0 — platform | Scaffold live; auth and protected route work | 1–2 h |
| 1 — model | Three shards compose; held-out metrics and tests recorded | 2–3 h |
| 2 — coordination | Real worker claims and inference; timeout path | 3–4 h |
| 3 — settlement | Atomic debit/refund/reward; tamper-evident ledger tests | 2–3 h |
| 4 — product | Canvas, pipeline, nodes, credits and explorer | 3–4 h |
| 5 — quality | Deploy; two-session test; documentation and submission | 3 h |

If schedule slips, remove P1 first, then UI polish. Never remove verification or transactional settlement to add a flashy feature. Prefer a working clearly labeled local demo plus hosted ledger to a broken network; disclose gaps. Do not report a stage complete until its exit criterion is verified.

## Engineering rules

- TypeScript strict, small pure modules, typed messages, bounds checks, structured error codes. Prefer scaffold conventions. Keep dependencies minimal and pin lockfile.
- Server derives caller identity from verified auth; never trust userId, price, credit amount, worker identity or scores supplied by clients. Use opaque random job/lease IDs. Enforce ownership and role checks, quotas, payload sizes, finite tensors and exact dimensions.
- Model assets are versioned and immutable. No arbitrary uploaded model, JavaScript, shell commands or executables. Use only bundled classifier. Never send secrets to browser.
- Credit state and chain head must be transactionally consistent in one authoritative storage scope. Use supported transaction semantics; if SDK collections cannot atomically update several rows, place authoritative ledger state in one Durable Object SQLite transaction. No cross-room race or pretend atomicity.
- Tests first for math/settlement; integration tests for worker leases and errors. Run scaffold's typecheck/build/test commands and inspect output. Do not disable tests to ship.
- Use git commits at working checkpoints and preserve human changes. Keep .env, .dev.vars, token/session files and test credentials out of git. Do not deploy or incur charges without authorized account/approval.

## Submission acceptance checklist

- [ ] Live URL opens on a fresh browser, onboarding works.
- [ ] Draw or select sample digit; real inference flows across 3 shards and produces prediction.
- [ ] At least two authenticated browser sessions demonstrate separate workers; local fallback labeled.
- [ ] Invalid activation is rejected; timeout releases lease and refunds or retries.
- [ ] Insufficient credits rejected; no negative balances; retries and concurrent settlement do not double-charge.
- [ ] Ledger verifies from genesis; changing a test block breaks validation; refresh preserves balances.
- [ ] User cannot write another user's credits or claim another worker's task.
- [ ] Record measured model accuracy, latency, test output and actual integrations.
- [ ] README explains why a centralized database is sufficient today and why independent validators could justify a chain in a future version.
- [ ] Human can explain model partitions, scheduling, verification, atomic settlement, and limitations; human personally performs live checks.

Submit live URL, repository, and concise note: what works, actual DeepSpace integrations, biggest tradeoff (trusted reference verification vs decentralization), agent contributions, and what the human personally verified. Never claim the human completed checks they did not.
