# Architecture and acceptance

`discovery → eligibility → health/cap verification → held-out pilot → fixed screens → optional confirmations → sanitized snapshot → Pages`

## Boundaries

- The SQLite database and response/dataset folders live outside the checkout with restrictive permissions. No API keys are logged or put in container environments.
- Generation uses only the pinned installed OpenCode client and exact currently eligible Zen IDs. A fresh tool-free session preserves system/user roles and includes the client's fixed environment context. The dedicated server binds to loopback with private authentication; global plugins, instructions, skills, and tools are disabled. It cannot select a different model or provider. Redirects are disabled. Discovery must join official endpoint and price rows by exact display name and catalog ID.
- Each initial generation and observed retry receives a committed attempt reservation; retry signals are handled during client backoff. Counts are client-observed dispatches rather than a provider HTTP audit. Failed/unknown usage keeps that reservation; actual usage reconciles it without adding cache or reasoning subsets twice.
- LCB compressed/pickled private tests are deserialized only in the isolated grading container. Pin both source repositories and data snapshots. Prompts come from the pinned upstream function; code extraction and test checking come from upstream inside Docker.
- The runner owns its lock, backup, durable job transitions, retry counters and budget decisions. Unknown network outcomes are not automatically retried.
- Public exports are constructed from allowlisted schema fields; manifests cannot include arbitrary content. GitHub Pages receives only an explicit seven-file site directory. Publication Actions have no model or dataset credentials.

## Release acceptance

Offline targeted checks validate fail-closed pricing, protocols, exclusions, accounting, retries and recovery, nested panel sampling, paired statistics, private export boundaries, and publication validation. Container fixtures must pass before a season is activated. The three-model pilot must complete before headline screens start. Public deployment is accepted separately from benchmark completion.

The current first release may publish an honest setup/pending page while API credentials, dataset access or Docker are missing. That page is not evidence that the pilot or benchmark acceptance checks have passed.

## Operational failures

Provider access can disappear between price verification and dispatch. An OpenCode-only free-tier promotion can also reject the controlled client configuration; switching transport does not guarantee access. Keep the exact provider response status, mark access/quota failures, and stop using the endpoint. No local pricing check can make a contractual guarantee about provider billing. Daily revalidation and explicit model gating minimize that gap.

An interrupted attempt without a response remains ambiguous until an operator can reconcile it from provider evidence. Do not reissue it to improve a score. Container infrastructure timeouts remain ungraded. Pending screens cannot replace older complete scores; older scores remain available with their original dates.

