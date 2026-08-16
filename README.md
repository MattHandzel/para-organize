# para-organize — ground-up rebuild (branch: `rewrite`)

This branch is a clean slate for rebuilding the KMS organize stage. The old implementation was removed here (it remains intact on `main` and throughout git history — reference rev for original behavior: `d753672~1`).

**Everything an implementing agent needs is in [`spec/`](spec/README.md).** Start with `spec/README.md`, read docs in order, and follow the build order at the bottom of it. Do not start from the old code; start from the spec, consulting old code only where the spec directs.

Status: **spec complete, awaiting Matt's review. No implementation has begun.**
