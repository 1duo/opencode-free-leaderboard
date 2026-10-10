# OpenCode Free Model Leaderboard

A local, persistent benchmark runner using the installed OpenCode client with verified free Zen models, with a static GitHub Pages leaderboard. Raw datasets, answers, responses, state and credentials stay outside this repository.

## Install

```sh
uv sync --frozen --no-editable
uv run --no-editable leaderboard status
```

Python 3.11 is managed by uv. Start [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) or a working Colima Docker engine before grading. Budget roughly 15 GB of free local disk space for datasets, the grading image and rotating backups. Accept access to [GPQA](https://huggingface.co/datasets/Idavidrein/gpqa) using your Hugging Face account; its terms prohibit publishing examples. Supply credentials in your own terminal, never in a commit or chat:

Use `uv run --no-editable` for commands on macOS if filesystem hidden flags cause editable-install `.pth` files to be skipped by Python. The installed launch agent directly uses the project's virtual environment with an explicit source path, avoiding dependency installation during scheduled runs.

```sh
uv run --no-editable leaderboard auth zen
uv run --no-editable leaderboard auth hf
```

Credentials are stored and read in macOS Keychain through Security.framework using the dedicated `freeboard` account. Environment variables `OPENCODE_API_KEY` and `HF_TOKEN`, an existing OpenCode API credential, and the standard local Hugging Face token file are supported. Without a Zen key, the runner uses [OpenCode's public free-model credential](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/provider.ts). It still requires fresh official zero-price evidence and never substitutes a paid endpoint.

For a reliable scheduled runtime outside the public checkout:

```sh
UV_PROJECT_ENVIRONMENT="$HOME/Library/Application Support/OpenCodeFreeLeaderboard/runtime" uv sync --frozen --no-editable --link-mode copy
```

The launch agent prefers that private runtime when installed. It uses a managed public-source clone at `state/scheduler-checkout`, outside macOS's protected Documents directory. Installing the schedule clones or fast-forwards the already published source and sets an explicit source path. Reinstall the schedule after publishing code updates; scheduled snapshots are committed from the managed clone. The computer and Docker engine must be running.

## First evaluation

```sh
uv run --no-editable leaderboard discover
uv run --no-editable leaderboard prepare-panels
uv run --no-editable leaderboard pilot
uv run --no-editable leaderboard run
uv run --no-editable leaderboard export
uv run --no-editable leaderboard publish --create-repository
uv run --no-editable leaderboard schedule install
```

Discovery must succeed against both the official catalog and pricing page. Pricing parsing is fail-closed: missing or contradictory evidence blocks generation. The pilot selects the first three eligible endpoints lexicographically, with six health probes and six held-out benchmark questions each. Pilot answers never enter rankings.

Generation requires the validated OpenCode version 1.18.31. Each question uses a fresh session through a dedicated loopback server with a fixed benchmark agent. Native file/shell tool schemas remain present for free-tier compatibility; tool use and external paths require permission, every request is rejected, and rejected tool attempts score zero. No permission is granted and no repair turn is allowed. Other tools, plugins, sharing, parallel tasks, title/summary agents, and compaction are disabled. No step limit is set, avoiding the client's maximum-step summary instruction on the first response. The original benchmark system instruction is sent in the system field and its question in user parts; they are never flattened. OpenCode adds its own environment context, so these results measure this client configuration.

The cap probe deliberately requests a long JSON array. Cap verification requires bounded usage for the five other synthetic checks and a synthetic cap check truncated at the 256-token limit. If the initial probe ends early or has an unknown outcome, one separate fixed text-streaming calibration can establish the limit; the original outcome remains recorded and no accepted probe or benchmark question is repeated. Cached input and hidden reasoning subfields are never added twice. This checks observed gateway behavior, not an assurance about every future response. A cap violation removes verification and prevents further headline requests. A client, configuration, or endpoint change creates a new evaluation epoch and requires a new pilot. There is no model substitution or paid provider fallback. Muse Spark Contributor endpoints are excluded.

A native free-quota interruption may resume only when its explicit rejection and saved native transcript prove that no response content was produced. The exact model, variant and question must match. The provider's retry time gates every pending request to that model across seasons, the original conservative charge is retained, and the same three-attempt limit applies. Partial content, unknown outcomes and exhausted retries are never replayed.

While GPQA access is pending, `prepare-panels --public-only` freezes a separate compatibility panel. Run `pilot --season PUBLIC_PILOT_ID` to evaluate six synthetic probes and two held-out questions each from LiveBench and LiveCodeBench per selected model. This pilot cannot be promoted or used for headline rankings. `prepare-panels --season SEASON_ID` validates an already frozen panel without downloading or resampling it. Download caches can be discarded after the selected questions and manifest are stored and backed up; preserve the private SQLite state.

All generation goes through the real OpenCode client; the runner does not post completions directly to Zen. Discovery still reads the official catalog and pricing page. Only an exact, currently verified zero-price Zen ID is selectable. Provider free-tier restrictions can still reject the client configuration and are shown explicitly.

Earlier direct API and native configurations remain in private storage and count against their original budgets, but are excluded from current public scores, pilots, and queues. New answers use protocol revision 6 and separate epochs; no earlier answers are reused. Revision 3 removed every tool schema, which caused avoidable free-tier rejections. Revision 4 also exposed an automatic filesystem denial that could trigger a repair turn. Revision 5 corrected permission handling; revision 6 additionally selects the highest reasoning variant exposed by the installed client.

Discovery reads the installed client's model definitions without generating an answer. Each request explicitly selects the highest advertised reasoning effort (`max`, then `xhigh`, then `high`, as available). The client validates the current definition before dispatch and preserves the selected variant in its session record and the private response journal. The page, JSON and CSV display the setting for each evaluation. Models with no exposed control are labeled **Provider-managed · no selectable level**; their maximum reasoning cannot be verified. Requested effort remains subject to the combined token cap and is not independent evidence of provider internals. Changed controls create a new epoch and require fresh answers.

Once a model's public pilot completes, finish the frozen 20 LiveBench and 40 LiveCodeBench questions independently of GPQA setup:

```sh
uv run --no-editable leaderboard run --public-only --season PUBLIC_PILOT_ID --model MODEL_ID
```

Use `pilot --all-models --season PUBLIC_PILOT_ID` to check the verified free evaluation list. Pilot capacity scales to the selected unfinished work plus already-accounted usage. Then `run --public-only --season PUBLIC_PILOT_ID` finishes screens for all models with successful matching pilots and verified caps. Configured omissions stay outside evaluations and public reports across rediscovery. Private records and budget charges are retained.

These 60-question public screens have separate persistent cycles, component scores and stratified intervals; they never receive headline ranks, reasoning/overall scores, or automatic confirmation extensions. Screens require a verified cap and a matching completed OpenCode pilot. Saved answers resume by grading, and pilot answers are never reused. Adding GPQA creates a new full season with fresh answers rather than silently turning these partial results into headline scores.

## Routine operation

The `launchd` agent runs at 09:15 local time and at login. Keep the private runtime and managed scheduled checkout at their installed paths and keep Docker running. Daily execution revalidates eligibility, completes missing pilots for the active full season, and resumes screens only after each model's matching pilot and health evidence are complete; health jobs are unique to each model/epoch/season/week. Models have four stable refresh cohorts. A 28-day deadline depends on provider availability and the computer running.

While GPQA setup is incomplete, daily execution resumes the latest validated public pilot and screen. It publishes new progress without promoting these subsets into full rankings. Future quota retries wait for both the provider's reset window and the next local invocation.

`run --limit 10` processes at most ten jobs. `confirm MODEL_ID` queues 160 extra questions on the model's completed, current screen cycle. The next run processes headline obligations before confirmation extensions; confirmations do not automatically expand the weekly budget.

Private state defaults to `~/Library/Application Support/OpenCodeFreeLeaderboard/`, with SQLite WAL, response files, isolated OpenCode session/config/cache directories, pinned datasets and upstream source archives, logs, and seven daily backups. All commands share a nonblocking process lock. Use `--state /absolute/private/path` before the command to override storage; placing it inside the checkout is rejected.

Responses are written atomically before grading. If the runner crashes with a saved response, recovery grades it without regeneration. Requests with no durable response are marked ambiguous; no automatic replay occurs. Generation has a ten-minute overall deadline; the event listener has no separate idle read timeout. Listener failure journals record the exception type for private diagnosis. Authentication errors, quotas, and infrastructure errors do not count as failed benchmark answers. Client retries require an explicit retryable HTTP or provider rejection before any response content, with at most two retries. Header, socket and stream failures with unknown outcomes stop without retrying. No repair completions are allowed. Failed/ambiguous jobs remain visible and prevent a complete-panel rank; do not erase them to obtain a better sample. A new scheduled cycle may start after 28 days even if the previous screen is incomplete; its original records remain.

Missing output usage or inconsistent reasoning accounting also blocks headline eligibility, retaining the response without replay. Recovery applies the same cap checks as normal execution. Historical rows retain the configuration used by their evaluation cycle.

## Protocol and statistics

| Benchmark | Screen | Confirmation |
|---|---:|---:|
| GPQA Diamond | 20 | 60 |
| LiveBench reasoning | 20 | 60 |
| LiveCodeBench Python generation | 40 | 120 |

Panels are nested and deterministic (seed 20261008). GPQA is stratified by subject; answer ordering is frozen. LiveBench uses the verified 2024-11-25 spatial and zebra-puzzle panel with equal allocation. LiveCodeBench uses code_generation_lite release_v6 with proportional difficulty strata, its pinned generic OpenAI-style prompt and fenced-code extraction, and its official harness in Docker. The grader uses six-second test timeouts, one CPU, 1 GiB memory and a 180-second outer infrastructure timeout. Its resolved image ID is recorded in each validated season.

One completion, no granted tools, no self-repair, temperature zero where supported, 4,096 maximum generated tokens including reasoning where supported. Invalid formats, rejected tool requests, refusals, wrong answers and truncations are graded objectively. LiveBench zebra grading preserves upstream fractional credit.

Reasoning equally averages GPQA and LiveBench. Coding is pass@1. Optional overall weights are 40% reasoning and 60% coding. Complete panels alone receive scores; screens and confirmations never share a ranking. Ten thousand deterministic stratified bootstrap resamples estimate question-sampling uncertainty. Identical question IDs use identical draws for paired comparisons. An interval containing zero is unresolved; comparisons are exploratory and not corrected for multiple comparisons.

Completed individual benchmark sections also appear as unranked plots and in JSON/CSV, with their own evaluation dates and intervals. Incomplete sections have no score. Headline reasoning, coding and overall scores and ranks still require the entire 80- or 240-question panel; earlier public subsets remain separate.

These small, older public subsets may have appeared in model training. The intervals do not measure contamination, hidden alias changes, provider reporting errors, or repeated-generation variability. Exact point-score ties share a displayed position; unresolved comparisons do not establish a winner. No code review can establish absolute fairness or trustworthiness.

Weekly minimums are 300 recorded client attempts and one million accounted tokens. The initial request is reserved before dispatch; retry events are reserved during client backoff. At most two retries are allowed, and long backoffs abort the session. Counts describe reserved client attempts and observed retries, not a provider HTTP audit. Tokens are client-normalized; cache and reasoning are added once. Unknown outcomes retain their conservative reservation and block further requests to that model in the session. Capacity scales to outstanding headline questions, six probes per eligible model and 25% retry headroom. Accounted tokens conservatively use UTF-8 prompt bytes plus 8,192 tokens of client/environment overhead and the generation cap until actual totals are reported. These estimates are not provider invoices. Explicit discovery calls and failed attempts count toward the dispatch budget. Client-reported token overruns are recorded even if they exceed a reserved estimate; subsequent requests stop at the budget boundary.

## Publication and seasons

The default repository is `1duo/opencode-free-leaderboard`. `publish` validates an explicit public export and commits only allowed source/publication paths; it never stages private state. GitHub Actions deploys the complete Pages artifact and stamps its publication time. Pushing a commit is not confirmation of deployment; check the Pages workflow. A failed workflow preserves the last deployed site.

Public JSON/CSV include aggregate results, dates, token accounting and availability. Public manifests contain question IDs, hashes and strata, not question text or answers. Static rendering receives only a strict public snapshot schema. If setup or discovery is unavailable, the page shows pending status and prerequisites without invented scores.

For a candidate season, update explicit revision pins, run `prepare-panels`, then `pilot --season SEASON` and `run --season SEASON`. Promote with `prepare-panels --promote` only after every runnable endpoint has a complete candidate screen. Old seasons remain archived. Silent changes behind an unchanged alias may be undetectable; endpoint/configuration epochs do not identify the underlying model.

Representative repository/agent tasks are outside this release. Benchmark capability does not establish practical agent suitability.

## Targeted verification

```sh
uv run --no-editable pytest -q tests/test_core.py
uv run --no-editable ruff check src tests grading/worker.py
```

See [architecture and acceptance notes](docs/architecture.md) and the [2026-10-08 code review and pilot execution record](docs/review-2026-10-08.md).
