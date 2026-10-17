# Coding expansion

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

```sh
uv run --frozen --no-editable leaderboard coding --prepare-only
uv run --frozen --no-editable leaderboard coding --model MODEL_ID
```

Omit `--model` for all currently compatible free models. `--limit N` bounds an invocation. Repeat to resume saved work without replaying unknown outcomes. Scheduled common screens take priority; `status` reports completion and capacity.

After a complete 24-question panel, use `submit-results --model MODEL_ID --tier coding --contributor YOUR_GITHUB_USERNAME` for a sanitized result PR. Community results remain labeled and unranked; they do not replace locally reproduced composites.
