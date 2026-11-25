# NeuroGrid coding agent instructions

NeuroGrid is an existing DeepSpace application for cooperative language model inference. Work in the current scaffold and preserve the checked-in legacy application until a tested migration is ready.

## Read before changing code

1. Read [architecture.md](architecture.md) for the current baseline, target system, and network diagrams.
2. Read [the next-release implementation and test handoff](docs/next-feature.md) for priorities and acceptance criteria. [Animated architecture walkthrough](docs/neurogrid-next-architecture.mp4).
3. Read the [README](README.md) and [installer instructions](packaging/INSTALL.md) to reproduce the legacy behavior and find the existing test commands. Read the applicable DeepSpace guidance and installed SDK types before changing platform code.

## Implementation rules

- **TEST IT.** Follow every applicable release gate in the handoff. Run the relevant automated tests and verify the actual Windows and Linux packages, real inference, receipt validation, and hosted workflows when working on those features. Record commands, environments, revisions, observed results, and any uncompleted checks. A mock or an HTTP 200 is not release evidence.
- Use verified user and worker identities, strict TypeScript and typed Python messages, explicit model revisions, bounded inputs, scoped leases, and idempotent writes. Never rely on client-supplied credit values, worker identity, or verification results without validating the protocol evidence.
- Keep billing, reservations, and ledger transitions atomic. Preserve account balances and audit history across migration. Use explicit sandbox credentials for payment tests.
- Keep production secrets, authentication tokens, node credentials, and local environment files out of git, output, and screenshots. Preserve user work. Ask before deploying to hosted environments or incurring costs when authorization has not been granted.
- Report actual inference model, verification mode, and test evidence. Distinguish a hardware-backed receipt from a mathematical inference proof and list any unverified claims as pending.
