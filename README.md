# OpenCode Free Model Leaderboard

Compare free OpenCode models for reasoning and Python coding on fixed benchmark subsets.

**[Live leaderboard](https://1duo.github.io/opencode-free-leaderboard/)** · [JSON](https://1duo.github.io/opencode-free-leaderboard/snapshot.json) · [CSV](https://1duo.github.io/opencode-free-leaderboard/leaderboard.csv) · [Operations](docs/operations.md)

The local runner keeps datasets, answers, responses, credentials and SQLite state private. GitHub Actions validates and publishes a sanitized static snapshot; it never runs model requests.

## Setup

Use macOS, Python 3.11 managed by [uv](https://docs.astral.sh/uv/), authenticated `gh`, and a running Docker Desktop or Colima engine. Allow roughly 15 GB for initial datasets, the grader and seven backups. Accept the [GPQA access terms](https://huggingface.co/datasets/Idavidrein/gpqa) before preparing full panels.

```sh
uv sync --frozen --no-editable
uv run --frozen --no-editable leaderboard doctor
uv run --frozen --no-editable leaderboard auth hf
# Optional: reuse an existing native OpenCode API credential, or store one:
uv run --frozen --no-editable leaderboard auth zen
```

The validated generation client is **OpenCode 1.19.3, protocol revision 6**. A different version is blocked before inference. OpenCode v2 has a different API and has not passed the benchmark compatibility protocol. Updating your interactive client does not authorize a changed benchmark configuration. Existing evaluations retain their original client versions and epochs.

To keep an independent benchmark client, install the official executable at `state/bin/opencode-1.19.3`. The runner prefers that file and verifies its version before use. See [the pinned client setup](docs/operations.md#pinned-client).

Credentials go into macOS Keychain. `HF_TOKEN`, `OPENCODE_API_KEY`, an existing OpenCode API credential and the standard Hugging Face token file are also supported. The native public free-model credential is used if no Zen key is configured; official zero-price evidence is still required. Never put secrets into a command argument, issue or commit.

## Run locally

```sh
uv run --frozen --no-editable leaderboard discover
uv run --frozen --no-editable leaderboard prepare-panels
uv run --frozen --no-editable leaderboard pilot
uv run --frozen --no-editable leaderboard run
uv run --frozen --no-editable leaderboard status
uv run --frozen --no-editable leaderboard export
```

`pilot` starts with three eligible models in lexicographic order. `pilot --all-models` checks all eligible models. A model needs its own successful held-out pilot and verified token cap before its screen. `run --limit 10` bounds an invocation; the same command resumes durable work. `confirm MODEL_ID` queues a manual 240-question confirmation, reusing that cycle's 80 screen answers and adding 160 questions.

Use `prepare-panels --public-only`, then `pilot --season SEASON_ID --all-models` and `run --public-only --season SEASON_ID` for public-only evaluation. These 60-question screens contain no GPQA and remain unranked against full screens. `lightweight` checks new models on a nested 12-question public subset; larger completed panels replace those values without pooling duplicate questions.

`discover --catalog-only` refreshes pricing and catalog evidence without changing frozen client settings or authorizing inference. Provider access restrictions remain a separate prerequisite. A free catalog listing does not prove that an endpoint accepts this benchmark setup.

## Protocol and statistics

| Component | Screen | Manual confirmation |
|---|---:|---:|
| GPQA Diamond | 20 | 60 |
| LiveBench reasoning | 20 | 60 |
| LiveCodeBench Python | 40 | 120 |
| Total | 80 | 240 |

Panels are deterministic and nested, with seed `20261008`. LiveBench uses spatial and zebra questions active on **2024-11-25**; LiveCodeBench uses `code_generation_lite`, `release_v6`. Data and graders are pinned by revision. Public manifests contain IDs, hashes and strata, never prompts or answer keys.

- **Reasoning:** equal average of GPQA and LiveBench. Public-only results use LiveBench and disclose that coverage.
- **Coding:** LiveCodeBench pass@1.
- **Ranking:** complete, matching full screens only. Other coverage is unranked. An unfinished refresh preserves the prior completed score with its original date and refresh status.
- **Uncertainty:** 10,000 deterministic stratified bootstrap draws; paired comparisons use identical panel draws. A difference interval containing zero is unresolved. Intervals measure question sampling, not repeated-generation variability.

Each question gets one accepted completion, no permitted tools or repair turns, temperature zero where supported, and a combined 4,096-token generation cap. The highest native reasoning variant is selected explicitly; models without a selectable control are labeled provider-managed. Native tool schemas remain present for compatibility, but requests are rejected and score zero. Client environment context is part of the recorded configuration.

GPQA dispatch requires current, exact no-training evidence for the model. Routes without it use public questions only. Contributor models are excluded by default. A changed endpoint, client or effective configuration creates a new epoch with a new pilot; silent changes behind an unchanged alias may be undetectable.

These are small, older public benchmark subsets, potentially present in model training. They measure benchmark capability and do not establish everyday agent suitability. Scores, coverage, dates and uncertainty should be read together.

## Publish and operate

```sh
uv run --frozen --no-editable leaderboard publish --create-repository
UV_PROJECT_ENVIRONMENT="$HOME/Library/Application Support/OpenCodeFreeLeaderboard/runtime" uv sync --frozen --no-editable --link-mode copy
uv run --frozen --no-editable leaderboard schedule install
```

The default public repository is `1duo/opencode-free-leaderboard`. Publication stages only allowed source and public files. Offline production/privacy checks and artifact validation must pass before Pages deployment; a failed check or deployment preserves the last live site. Evaluation dates and deployment time are separate.

The launch agent runs at **09:15 local time daily and at login**, using a managed source checkout outside Documents. Do not reinstall it just to resume work: installation starts it immediately. Full screens target a 28-day refresh in four cohorts. Deadlines depend on provider availability, Docker and the computer running; missed deadlines are retained explicitly.

The weekly plan scales above minimums of 300 attempts and one million accounted tokens. Requests reserve conservative usage before dispatch. Unknown usage retains that reservation. Explicit retries are bounded; unknown or partially answered requests are never automatically replayed. Confirmations use spare capacity.

Private state is under `~/Library/Application Support/OpenCodeFreeLeaderboard/`. Every command uses a process lock. Daily SQLite backups are validated and atomically replaced; seven successful daily copies rotate only after a new copy succeeds. Keep an independent encrypted backup for disk failure recovery.

See [operations and recovery](docs/operations.md), [architecture](docs/architecture.md), [security](SECURITY.md), and the [evaluation audit](docs/review-2026-10-08.md).

## Development

```sh
uv run --frozen --no-editable ruff check src tests grading/worker.py
uv run --frozen --no-editable pytest -q tests/test_production.py tests/test_privacy.py
node --check web/app.js
```

These checks do not call models or need credentials. CI also runs selected evaluation safety regressions and checks the committed site against its renderer. Follow [CONTRIBUTING.md](CONTRIBUTING.md); do not change frozen prompts, grading limits or accepted answers to improve results.
