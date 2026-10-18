# Systems C

A custom **24-task C99 subset of [Exercism's C track](https://github.com/exercism/c)** adds explicit memory handling and compiled-language coverage to LiveCodeBench's Python algorithms. Its MIT-licensed data and CPU grader require no dataset login, GPU or paid evaluation service. It is not an official Exercism leaderboard or a complete systems-engineering assessment.

This earlier panel remains available with `coding --benchmark systems-c`. The default expansion is now [HumanEvalPack](humanevalpack.md); results from different extensions remain separate.

| Domain | Scored tasks | Held-out pilot |
|---|---:|---:|
| Memory ownership, pointers and data structures | 6 | 1 |
| Bits, byte encodings and checksums | 6 | 1 |
| Strings and parsing | 6 | 1 |
| Numeric algorithms and representations | 6 | 1 |

The fixed population is declared in `src/freeboard/systems.py`: seven exercises per domain. Seed `20261008` selects the held-out task independently of source order and model output; the other six form the scored panel. Dataset and test-framework revision: `exercism/c@7cf6fe4098bc2e550e3877c54173eda3314a8bfe`. Reference or grader failures block preparation without replacing tasks.

## Protocol

- Preserve upstream exercise instructions and appendices. Supply the official example header as a fixed interface when the starter header is incomplete; no implementation or test answers enter the prompt. Request one complete fenced C source file implementing that interface.
- Enable every upstream test by removing its `TEST_IGNORE()` marker. Assert the expected full test count, zero failures and zero skipped tests. Enable Unity's 64-bit and double-precision assertions.
- Compile with GCC 12.2.0, C99, optimization `-O1`, upstream warning flags as errors, AddressSanitizer and UndefinedBehaviorSanitizer. Leaks, memory errors and undefined behavior fail the answer. Compiler/runtime packages are pinned; the frozen image ID and package-list hash are recorded in the manifest.
- Validate all 28 reference implementations before generation. The upstream `list-ops` reference calls `memcpy` with a null source for an empty list. A reference-only wrapper skips zero-length copies; model answers receive no wrapper or sanitizer exemption. The normalized reference contributes to the frozen question hash.
- Verify synthetic correct, incorrect, runtime timeout, heap-boundary, leak, undefined-behavior and early-exit behavior. A syntax error, sanitizer failure, failed test or program timeout scores zero. Container/infrastructure errors remain pending.
- Run serially in disposable Linux containers: no network or credentials, non-root, read-only root, dropped capabilities, one CPU, 1 GiB RAM and 64 processes. A 128 MiB executable scratch mount permits compiled programs; all other scratch mounts retain `noexec`. Compilation has 30 seconds, the full test executable five seconds, and the outer container 180 seconds. Reference code and model prompts are omitted from the runtime grading request.
- Keep the existing one-completion, highest-exposed reasoning, 4,096 combined output-token and no-tools protocol. Saved answers resume by local grading; unknown requests are never automatically replayed.

## Scoring and contribution

After complete coverage, **Coding = 50% LiveCodeBench pass@1 + 50% Systems C pass@1**. Reuse the existing 40 LiveCodeBench answers only for the same model epoch and generation settings. Initial promotion or replacement requires complete, fresh panels for the same currently free comparison cohort. Until then, previous scores remain intact. Earlier BigCodeBench seasons remain separate.

Each component retains its count and evaluation date. Confidence intervals use 10,000 deterministic stratified bootstrap draws; comparisons use paired draws on matching question IDs. Broad domains and execution checks improve coverage, but do not establish concurrency, OS integration, assembly, GPU optimization, production security or practical agent suitability. Public exercises may have appeared in model training.

```sh
uv run --frozen --no-editable leaderboard prepare-panels --public-only
uv run --frozen --no-editable leaderboard coding --benchmark systems-c --prepare-only
uv run --frozen --no-editable leaderboard coding --benchmark systems-c
# For one model or a bounded invocation:
uv run --frozen --no-editable leaderboard coding --benchmark systems-c --model MODEL_ID --limit 8
```

A fresh setup also needs the matching model compatibility pilot described in [CONTRIBUTING.md](../CONTRIBUTING.md). Repeating the command resumes durable work. Commit runner and grader changes before generation. After the 24 tasks finish, export with `submit-results --model MODEL_ID --tier coding --season LOCAL_SEASON_ID --contributor YOUR_GITHUB_USERNAME` and submit the sanitized file by PR. Use the local season ID printed by `coding --prepare-only`; the exporter matches its source/panel fingerprint to the published season. CI requires matching published grader sources and panel hashes, all domains, binary grades, usage limits and the highest exposed reasoning setting. Contributors record their own immutable image ID; a rebuild on another host can produce a different ID. Community results remain labeled and unranked, with execution claims awaiting independent reproduction.

The local grader and all references passed on 2026-10-17. Model generation remains subject to OpenCode's access policy; changing a dataset does not bypass that policy. No C score is claimed before valid, complete model answers exist.
