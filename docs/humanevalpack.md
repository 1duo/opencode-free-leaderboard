# HumanEvalPack

A custom **24-task C++, Rust and Go subset of [HumanEvalPack](https://huggingface.co/datasets/bigcode/humanevalpack)** adds compiled-language generation and bug fixing to LiveCodeBench's Python algorithms. The MIT-licensed dataset is public: no dataset login, GPU or paid grading service is required. These are subset results, not official HumanEvalPack leaderboard scores or a complete systems-engineering assessment.

| Language | Generation | Repair | Held-out pilot |
|---|---:|---:|---:|
| C++17 | 4 | 4 | 2 |
| Rust 2021 | 4 | 4 | 2 |
| Go | 4 | 4 | 2 |

Dataset revision: `bigcode/humanevalpack@9a41762f73a8cb23bb5811b73d5aab164efcf378`. Seed `20261008` orders problem families shared by all three languages. Six fixed language/task strata each receive five families: the first is held out, the remaining four are scored. **All thirty families are distinct**, including across languages and generation/repair. Selection precedes reference validation and model answers; failures never trigger replacement sampling.

## Protocol

- Preserve the upstream instruction, public starter, function signature and supplied helpers. Repair questions additionally include the upstream buggy implementation. Hidden tests and canonical solutions never enter prompts. Each task receives one complete source-file answer in a matching fenced block. Repair is the initial task, not a second attempt at an earlier generation.
- Use the complete upstream functional tests. Compile C++ with GCC 12.2.0 and C++17; Rust with rustc 1.63.0 and edition 2021; Go with Go 1.19.8. These deliberately frozen toolchains match the older benchmark; they do not test the newest language features. Rust uses rand 0.8.5, regex 1.7.1 and md5 0.7.0; Go tests use testify 1.8.1. Compiler packages and direct libraries are version-pinned in the Dockerfile. The immutable image ID, installed package list and resolved Rust lockfile hash are recorded.
- Assemble Go implementations and tests in one file, as in the upstream harness. A pinned Go AST helper deduplicates shared imports and removes unused setup-only imports; it never adds missing model imports, repairs submitted functions or changes assertions. Preserve the original setup and tests in private provenance. The helper's source hash is frozen in the manifest.
- Before inference, every selected reference must pass and every selected repair task's buggy implementation must fail. Correct, compile-failure, timeout and early-exit fixtures must pass independently for all three languages. C++ requires the full upstream test entry point to return; Rust and Go require the expected non-skipped test counts. No response-selected retries or model-based grading.
- Execute serially in disposable Linux containers: no network or credentials, non-root, read-only root, dropped capabilities, one CPU, 1 GiB RAM and 64 processes. One 128 MiB executable scratch mount permits compiled binaries; other scratch mounts remain non-executable. Compilation is bounded to 60 seconds, tests to five seconds and the container to 180 seconds. Reference solutions, buggy oracles and prompts are omitted from runtime grading requests. Compilation/test failures score zero; infrastructure failures remain pending.
- Keep the existing highest-exposed reasoning, 4,096 combined output-token, one-completion and no-tools settings. Freeze a new model epoch for changed client/settings. Regrade saved answers locally; never automatically replay unknown requests.

## Scoring

After complete comparable coverage, **Coding = 50% LiveCodeBench pass@1 + 50% HumanEvalPack pass@1**. HumanEvalPack weights the six equally sized language/task strata equally. Reuse LiveCodeBench answers only from the same model epoch and generation settings. Promotion requires complete fresh panels across the same currently free comparison cohort. Until then, existing scores keep their dates and coverage. Earlier Systems C and BigCodeBench seasons remain separate; extension panels are not pooled or double-counted.

Intervals use 10,000 deterministic stratified bootstrap draws, with paired draws on matching panels for model comparisons. These older public tasks may have appeared in model training. They test functional correctness in compiled languages; they do not establish memory safety, concurrency, OS integration, performance or practical agent suitability.

## Run and contribute

```sh
uv run --frozen --no-editable leaderboard prepare-panels --public-only
# Complete a matching model compatibility pilot first; see CONTRIBUTING.md.
uv run --frozen --no-editable leaderboard coding --benchmark humanevalpack --prepare-only
uv run --frozen --no-editable leaderboard coding --benchmark humanevalpack
# Optional bounded run for one model:
uv run --frozen --no-editable leaderboard coding --model MODEL_ID --limit 6
```

HumanEvalPack is the default `coding` panel. Preparation downloads public data, freezes the panel and validates its image and oracles without contacting models. Commit runner/grader changes before generation. Omit `--model` to select all compatible, currently free models; repeat to resume. The six pilot answers never enter scores. An OpenCode access rejection remains blocked and cannot become a failed capability answer. Dataset availability does not establish provider permission or bypass client restrictions.

The 2026-10-18 access check received empty HTTP 403 `FreeTierError` rejections for all five previously scored models. The provider identified the current network/IP as blocked; this is independent of HumanEvalPack access. The frozen C++, Rust and Go reference smoke checks pass locally. Keep this validated panel queued until provider access is restored. [Systems C is an already implemented fallback](coding-candidates.md), but shares the same generation prerequisite.

After the complete 24-task screen, use `submit-results --model MODEL_ID --tier coding --season LOCAL_SEASON_ID --contributor YOUR_GITHUB_USERNAME` for a sanitized results PR. Source/panel fingerprints must match a published validated panel; contributors retain their own immutable image IDs. CI recomputes grades' aggregates and intervals, checks complete coverage and frozen settings, and excludes prompts, tests and answers. Community results remain labeled and unranked until independently reproduced.
