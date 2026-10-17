# Contributing

Use Python 3.11 and `uv sync --frozen --no-editable`. Keep changes focused and run the smallest checks covering the affected behavior. Production/privacy regressions and selected evaluation safety checks run in CI without provider calls.

- Add a regression for durable-state, pricing, scoring or privacy changes. Use synthetic fixtures, never private benchmark examples.
- Preserve frozen prompts, panels, grading revisions, usage and accepted answers. Changed evaluation settings need a new epoch; changed benchmark/grader protocols need a replacement season.
- Keep model requests local, serial, explicitly free and budget-reserved. Do not add paid fallback or automatic replay of unknown outcomes.
- Update `web/`, then run `leaderboard export`; commit the complete validated `site/` snapshot. CI rejects renderer/artifact mismatches.
- Keep secrets, datasets, responses, local logs and databases outside the checkout. Follow [SECURITY.md](SECURITY.md) for vulnerability reports.

Pull requests should describe the concrete behavior change, the relevant checks and any compatibility limits. Publication happens only after the reusable release checks pass.
