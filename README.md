# OpenCode Free Model Leaderboard

A local, persistent runner for verified free OpenCode Zen endpoints, with a static GitHub Pages leaderboard. Raw datasets, answers, responses, state and credentials stay outside this repository.

## Install

```sh
uv sync --frozen --no-editable
uv run --no-editable leaderboard status
```

Python 3.11 is managed by uv. Install and start [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) before grading. Accept access to [GPQA](https://huggingface.co/datasets/Idavidrein/gpqa) using your Hugging Face account; its terms prohibit publishing examples. Supply credentials in your own terminal, never in a commit or chat:

Use `uv run --no-editable` for commands on macOS if filesystem hidden flags cause editable-install `.pth` files to be skipped by Python. The installed launch agent uses this mode automatically.

```sh
uv run --no-editable leaderboard auth zen
uv run --no-editable leaderboard auth hf
```

Credentials are stored in macOS Keychain through Security.framework. Environment variables `OPENCODE_API_KEY` and `HF_TOKEN` are supported for portable manual runs. The installed launch agent uses Keychain. Configure the Zen account to disable paid access and auto-reload where available; this runner only submits exact IDs with fresh zero-price evidence.

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

The cap probe deliberately requests a long JSON array. A model is cap-verified only after all six responses report bounded output usage and at least one reports truncation at the 256-token probe cap. Cached input and hidden reasoning subfields are never added twice. This checks observed gateway behavior, not an assurance about every future response. A cap violation removes verification and prevents further headline requests. Unsupported parameter errors require an explicit profile change and a new pilot:

```sh
uv run --no-editable leaderboard profile MODEL_ID --cap-parameter max_tokens --omit-temperature
uv run --no-editable leaderboard pilot
```

Profile changes create an evaluation epoch on the next discovery. There is no automatic parameter fallback, model substitution, or paid provider fallback. Muse Spark Contributor endpoints are excluded.

## Routine operation

The `launchd` agent runs at 09:15 local time and at login. Keep this checkout at its installed path and keep Docker running. Daily execution revalidates eligibility and resumes work; health jobs are unique to each model/epoch/week. Models have four stable refresh cohorts. A 28-day deadline depends on provider availability and the computer running.

`run --limit 10` processes at most ten jobs. `confirm MODEL_ID` queues 160 extra questions on the model's completed, current screen cycle. The next run processes headline obligations before confirmation extensions; confirmations do not automatically expand the weekly budget.

Private state defaults to `~/Library/Application Support/OpenCodeFreeLeaderboard/`, with SQLite WAL, response files, pinned datasets and upstream source archives, logs, and seven daily backups. All commands share a nonblocking process lock. Use `--state /absolute/private/path` before the command to override storage; placing it inside the checkout is rejected.

Responses are written atomically before grading. If the runner crashes with a saved response, recovery grades it without regeneration. Requests with no durable response are marked ambiguous; no automatic replay occurs. Authentication errors, quotas, and infrastructure errors do not count as failed benchmark answers. Explicit retryable HTTP failures have at most two retries. Failed/ambiguous jobs remain visible and prevent a complete-panel rank; do not erase them to obtain a better sample.

## Protocol and statistics

| Benchmark | Screen | Confirmation |
|---|---:|---:|
| GPQA Diamond | 20 | 60 |
| LiveBench reasoning | 20 | 60 |
| LiveCodeBench Python generation | 40 | 120 |

Panels are nested and deterministic (seed 20261008). GPQA is stratified by subject; answer ordering is frozen. LiveBench uses the verified 2024-11-25 spatial and zebra-puzzle panel with equal allocation. LiveCodeBench uses code_generation_lite release_v6 with proportional difficulty strata, its pinned generic OpenAI-style prompt and fenced-code extraction, and its official harness in Docker. The grader uses six-second test timeouts, one CPU, 1 GiB memory and a 180-second outer infrastructure timeout. Its resolved image ID is recorded in each validated season.

One completion, no tools, no self-repair, temperature zero where supported, 4,096 maximum generated tokens including reasoning where supported. Invalid formats, refusals, wrong answers and truncations are graded objectively. LiveBench zebra grading preserves upstream fractional credit.

Reasoning equally averages GPQA and LiveBench. Coding is pass@1. Optional overall weights are 40% reasoning and 60% coding. Complete panels alone receive scores; screens and confirmations never share a ranking. Ten thousand deterministic stratified bootstrap resamples estimate question-sampling uncertainty. Identical question IDs use identical draws for paired comparisons. An interval containing zero is unresolved; comparisons are exploratory and not corrected for multiple comparisons.

Weekly minimums are 300 provider HTTP attempts and one million accounted tokens. Capacity scales to outstanding headline questions, six probes per eligible model and 25% retry headroom. Accounted tokens conservatively use UTF-8 prompt bytes plus overhead and the generation cap until actual totals are reported. These estimates are not provider invoices. Metadata calls and failed attempts count toward the HTTP budget. Provider-reported token overruns are recorded even if they exceed a reserved estimate; subsequent requests stop at the budget boundary.

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

See [architecture and acceptance notes](docs/architecture.md).
