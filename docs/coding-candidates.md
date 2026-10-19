# Coding benchmark candidates

Assessment: 2026-10-18. **Keep HumanEvalPack as the default; use the existing Systems C panel if a C-focused extension is preferred.** Both are public, CPU-only and already validated locally. The immediate obstacle is OpenCode's network/IP rejection, which affects generation across datasets. A dataset replacement cannot establish provider access or permission.

| Candidate | Coverage and local feasibility | Decision |
|---|---|---|
| [HumanEvalPack](https://huggingface.co/datasets/bigcode/humanevalpack) | C++, Rust and Go generation/repair; 24 scored tasks and six held-out checks. Public MIT data. All 30 references and 15 repair oracles previously validated. | Retain the frozen default. Three fresh reference smoke checks passed, one per language. |
| [Systems C / Exercism](https://github.com/exercism/c) | 24 C99 tasks and four held-out checks; memory ownership, pointers, byte encodings, parsing and numeric work. Public MIT source; CPU containers with address, leak and undefined-behavior checks. | Best ready fallback for lower-level programming. All 28 references previously validated; four fresh smoke checks passed, one per domain. It is a custom subset, not a standard Exercism leaderboard. |
| [MultiPL-E / MBPP](https://github.com/nuprl/MultiPL-E) | Public translated code-generation tasks and Docker execution, including compiled languages. An MBPP panel would avoid repeating HumanEval families. The upstream repository includes a no-training restriction; verify dataset-specific rights and provider retention before adoption. | Possible future multilingual alternative. No local panel or reference validation yet; no access advantage over HumanEvalPack. |
| [Ag-LiveCodeBench-X](https://github.com/nuprl/Ag-LiveCodeBench-X) | Prepared public dataset and language-specific container protocol; a harder multilingual variant of LiveCodeBench. | Defer until source rights, exact toolchains and reference execution are validated. Shared LiveCodeBench ancestry requires checking question-family overlap before combining scores. |

Systems C needs no new integration or toolchain installation on this machine:

```sh
uv run --frozen --no-editable leaderboard coding --benchmark systems-c --prepare-only
# After provider access is restored and explicitly rechecked:
uv run --frozen --no-editable leaderboard coding --benchmark systems-c
```

The validated fallback is `systems-c-v1`, based on `exercism/c@7cf6fe4098bc2e550e3877c54173eda3314a8bfe`; its [protocol](systems-c.md) is unchanged. HumanEvalPack remains selected, and no replacement results or composite score are claimed. Changing the default would require complete comparable coverage before promotion; never pool extension panels to fill missing answers. Existing Coding scores continue to use LiveCodeBench.

Generated solutions and reference code execute only in the existing frozen, isolated containers. No model requests were needed for this assessment. The access diagnosis comes from preserved provider errors; macOS reported no configured system proxy or connected VPN, and no proxy environment variables were present. That does not exclude filtering or shared egress elsewhere on the network. See [operations](operations.md#interrupted-work) for recovery and support evidence.
