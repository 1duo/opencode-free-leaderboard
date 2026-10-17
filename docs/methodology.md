# Benchmark methodology

| Component | Screen | Manual confirmation |
|---|---:|---:|
| GPQA Diamond | 20 | 60 |
| LiveBench reasoning | 20 | 60 |
| LiveCodeBench Python | 40 | 120 |
| Total | 80 | 240 |

Panels are deterministic and nested, with seed `20261008`. LiveBench uses spatial and zebra questions active on **2024-11-25**; LiveCodeBench uses `code_generation_lite`, `release_v6`. Data and graders are pinned by revision. Public manifests contain IDs, hashes and strata, never prompts or answer keys.

- **Reasoning:** equal average of GPQA and LiveBench. Public-only results use LiveBench and disclose that coverage.
- **Coding:** LiveCodeBench pass@1 until the [24-task BigCodeBench CPU expansion](coding-expansion.md) passes its promotion gate, then the equal-weight average of both complete coding panels. Legacy screen records retain their original LiveCodeBench scores.
- **Ranking:** complete, matching full screens only. Other coverage is unranked. An unfinished refresh preserves the prior completed score with its original date and refresh status.
- **Uncertainty:** 10,000 deterministic stratified bootstrap draws; paired comparisons use identical panel draws. A difference interval containing zero is unresolved. Intervals measure question sampling, not repeated-generation variability.

Each question gets one accepted completion, no permitted tools or repair turns, temperature zero where supported, and a combined 4,096-token generation cap. The highest native reasoning variant is selected explicitly; models without a selectable control are labeled provider-managed. Native tool schemas remain present for compatibility, but requests are rejected and score zero. Client environment context is part of the recorded configuration.

GPQA dispatch requires current, exact no-training evidence for the model. Routes without it use public questions only. Contributor models are excluded by default. A changed endpoint, client or effective configuration creates a new epoch with a new pilot; silent changes behind an unchanged alias may be undetectable.

These are small, older public benchmark subsets, potentially present in model training. They measure benchmark capability and do not establish everyday agent suitability. Scores, coverage, dates and uncertainty should be read together.

Community result files must cover a complete published panel. CI recomputes scores and intervals from typed grades and validates IDs/hashes, settings, caps and accounting. The page labels these rows with the contributor and audit link; they remain unranked and never replace local scores or recommendations. CI cannot independently verify private answers or provider behavior.


[Run evaluations and contribute results](../CONTRIBUTING.md) · [Operations](operations.md)
