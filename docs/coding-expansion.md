# Coding expansion

The current extension is [HumanEvalPack](humanevalpack.md), covering C++, Rust and Go generation/repair. This document preserves the earlier BigCodeBench panel and its evaluation history. Use `coding --benchmark bigcodebench` to select that panel explicitly. [Systems C](systems-c.md) also remains separate.

**BigCodeBench-Instruct v0.1.4, a custom 24-task CPU subset**, adds practical function and library use to LiveCodeBench's algorithmic Python problems. These are custom-subset results, not official BigCodeBench leaderboard scores.

## Choice

| Candidate | Fit |
|---|---|
| [KernelBench](https://github.com/ScalingIntelligence/KernelBench) | GPU-kernel correctness and speed require a supported GPU and consistent hardware, beyond this macOS CPU sandbox. |
| [EvalPlus](https://github.com/evalplus/evalplus) | Small and CPU-friendly with strengthened tests, but older and closer to existing algorithmic coverage. |
| [BigCodeBench-Instruct](https://github.com/bigcode-project/bigcodebench) | Public Apache-2.0 data and executable practical tasks. A restricted CPU task population avoids its large full environment. |

## Frozen protocol

- Data: `bigcode/bigcodebench`, revision `b74c0d0bf70d2c0bc459be537895cca163007f1a`, split `v0.1.4`.
- Grader: `bigcode-project/bigcodebench`, revision `09dd993f46c3fbf3a799465bb96d524edcb0b199`.
- Twelve standard-library and twelve scientific Python tasks, plus one held-out task per stratum. Seed `20261008`; selection is independent of task order and model answers.
- Before sampling, AST imports of references and tests must fit the standard library plus NumPy, SciPy and SymPy. Socket, HTTP, urllib and subprocess dependencies are excluded. This restricted population does not represent all BigCodeBench libraries.
- Official instruction text is preserved verbatim, preceded by a fixed system message listing the available runtime and offline constraint. Upstream sanitization, calibrated code prefix and full test suites are used. Ground-truth validation follows upstream's raw-reference path.
- Python 3.11; NumPy 2.2.6, SciPy 1.15.3, SymPy 1.14.0 and Matplotlib 3.10.1. Version-pinned dependencies and revision-pinned grader source form a separate immutable Docker image; legacy graders are unchanged.
- Every reference must pass before inference. A validation failure blocks the frozen panel; it does not cause replacement sampling. Correct, incorrect and infinite-loop synthetic fixtures must pass too.
- One accepted completion, highest exposed reasoning, combined 4,096-token cap, no permitted tools or repair turns. Existing matching per-model compatibility and cap evidence is required, followed by this panel's two held-out tasks.
- Containers have no network or credentials, non-root execution, read-only root, temporary scratch space, dropped capabilities, one CPU, 1 GiB resident-memory limit and 64 processes. Upstream virtual-address/data limits remain 30 GiB for normal shared-library mappings; Docker bounds physical memory. Tests have a ten-second deadline; outer container failures remain infrastructure uncertainty.

## Score and promotion

**Coding = 50% LiveCodeBench pass@1 + 50% BigCodeBench subset pass@1.** Existing 40-question LiveCodeBench answers are reused for the same model epoch. Each component keeps its own evaluation date.

Initial promotion requires complete expansion coverage of currently runnable models with full LiveCodeBench panels. Partial answers never become zeros or provisional ranks. Ranks compare identical question panels independently of GPQA access. Historical LiveCodeBench-only scores remain in original screen records. Once promoted, a model awaiting the expansion has no composite score.

Intervals use 10,000 deterministic stratified bootstrap draws with equal benchmark weights. Comparisons use paired draws on matching question IDs. Intervals reflect question sampling, not repeated-generation variation. Older public data can be present in model training; this small subset does not establish tool-using agent performance or GPU-kernel optimization.

## Run and contribute

GPQA access is unnecessary for this expansion. On a fresh setup, first prepare a public grading season with `leaderboard prepare-panels --public-only`, then run `pilot --season PUBLIC_SEASON_ID --all-models`. A completed matching full or public compatibility pilot works. Existing validated setups reuse their grading image.

```sh
uv run --frozen --no-editable leaderboard coding --benchmark bigcodebench --prepare-only
uv run --frozen --no-editable leaderboard coding --benchmark bigcodebench --model MODEL_ID
```

Omit `--model` for all currently compatible free models. `--limit N` bounds an invocation. Repeat to resume saved work without replaying unknown outcomes. Scheduled common screens take priority; `status` reports completion and capacity.

Authentication and native-client access rejections remain blocked across automatic discovery. After resolving access, explicitly run `leaderboard discover --recheck-access MODEL_ID`, then resume `coding`. Rechecking cannot override paid pricing, missing catalog IDs, unverified caps, or unknown request outcomes.

As of 2026-10-17 the panel and every reference solution are validated, but no rankable expansion results are available. This is a benchmark-client access block, not a Hugging Face or grading prerequisite. LiveCodeBench remains the active Coding score.

Signing into OpenCode Console succeeded. The pinned benchmark client's requests for all five previously scored models still returned HTTP 403. The official OpenCode v2.0.32 CLI answered a short synthetic request, but a 256-token cap check automatically continued after truncation: two completions followed by a rejected third request. With its single-step configuration, Beam, DeepSeek and Nemotron returned 403; LongCat timed out with an unknown outcome; Space Bunny answered but did not reach the cap. The [pinned client source](https://github.com/anomalyco/opencode/blob/v2.0.32/packages/core/src/session/runner/llm.ts#L290) also confirms that single-step settings do not disable output-limit continuations. None of these checks establishes a compatible one-completion protocol. Synthetic diagnostics do not enter scores; responses, unknown outcomes and conservative charges remain private. The v2 client has not replaced the validated client or its evaluation epochs.

After a complete 24-question panel, use `submit-results --model MODEL_ID --tier coding --contributor YOUR_GITHUB_USERNAME` for a sanitized result PR. Community results remain labeled and unranked; they do not replace locally reproduced composites.
