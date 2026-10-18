# Contributing code and benchmark results

Anyone can clone and preview this site without credentials or private benchmark data. Running evaluations needs your own local OpenCode/Docker setup and, for GPQA, your own accepted Hugging Face access. Contributors submit result files through a pull request; they do not need write access or the maintainer's private state.

## Clone and preview

```sh
git clone https://github.com/1duo/opencode-free-leaderboard.git
cd opencode-free-leaderboard
uv sync --frozen --no-editable
uv run --frozen --no-editable leaderboard build-site
python3 -m http.server 8000 --directory .pages-site
```

Open `http://localhost:8000`. `build-site` reads committed public data and `results/`, renders the current `web/` code, and validates a complete artifact. It never opens SQLite, reads credentials, downloads datasets or calls a model. You can contribute code, documentation and UI improvements using this path alone.

## Set up your own runner

Supported manual execution: macOS ARM64/x86-64, Linux ARM64/x86-64 (Ubuntu 24.04 is checked in CI), and Windows through an Ubuntu WSL2 environment. Native Windows Python is not supported. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), Git and a working Docker engine; WSL2 users must enable Docker's WSL integration. Allow roughly 15 GB for datasets, images and backups.

```sh
uv run --frozen --no-editable leaderboard install-client
uv run --frozen --no-editable leaderboard discover
uv run --frozen --no-editable leaderboard status
```

The installer downloads the official OpenCode 1.19.3 archive for your platform, verifies a pinned SHA-256 before execution, checks its version and installs it privately. Your interactive OpenCode installation is unchanged. The benchmark protocol still requires this version; a newer interactive client does not authorize changed benchmark settings.

Local state defaults to `~/Library/Application Support/OpenCodeFreeLeaderboard` on macOS and `${XDG_STATE_HOME:-~/.local/state}/opencode-free-leaderboard` on Linux/WSL2. Override it with the root option `--state /absolute/private/path`. State must stay outside the checkout. Every contributor uses their own state, credentials, provider quotas and Docker engine.

For full screens, accept [GPQA's access terms](https://huggingface.co/datasets/Idavidrein/gpqa), then log in from your own terminal:

```sh
uv run --frozen --no-editable hf auth login
```

`HF_TOKEN` also works. On macOS, `leaderboard auth hf` stores it in Keychain. Zen uses your existing local API credential, `OPENCODE_API_KEY`, or the native public free credential when no key is configured. Credentials never enter result files or CI. Confirm that your provider setup accepts the controlled client; a free catalog listing does not guarantee access. The runner never substitutes a paid route.

## Run a complete panel

For a public-only run without GPQA access:

```sh
uv run --frozen --no-editable leaderboard prepare-panels --public-only
# Use the season ID printed above and an exact eligible model ID from status:
uv run --frozen --no-editable leaderboard doctor
uv run --frozen --no-editable leaderboard pilot --season SEASON_ID --model MODEL_ID
uv run --frozen --no-editable leaderboard run --public-only --season SEASON_ID --model MODEL_ID
```

Repeat the same run command to resume. A public screen needs all 20 LiveBench and 40 LiveCodeBench answers; it is unranked against full screens. `run --limit 10 ...` bounds an invocation. `doctor` checks local execution prerequisites; GitHub and scheduling are optional (`doctor --publication` / `--scheduled`). Missing panels are expected until `prepare-panels` succeeds.

For full 80-question screens, use `prepare-panels`, `pilot --all-models`, then `run`. Full evaluation requires the three-model compatibility pilot and each model's own matching pilot. `lightweight --model MODEL_ID` provides a separate 12-question public check; confirmations extend a completed full cycle with 160 new answers. Preserve accepted responses, frozen panels, objective grades and unknown outcomes. Do not cherry-pick questions, rerun wrong answers or lower reasoning effort to improve a submission.

## Export and open a results PR

Export only after a complete panel. The exporter checks the recorded protocol, current free eligibility, output cap, matching pilot, complete graded panel and saved-response accounting. Commit runner/grader changes before evaluating and exporting so the recorded source revision is reviewable. New benchmark protocols or panels need a separate proposal; a results PR must match an already published panel.

```sh
uv run --frozen --no-editable leaderboard submit-results \
  --model MODEL_ID --contributor YOUR_GITHUB_USERNAME \
  --tier public-screen --season SEASON_ID
```

Supported tiers: `screen` (80), `public-screen` (60), `lightweight` (12), `confirmation` (240), and `coding` (24 [HumanEvalPack C++/Rust/Go tasks](docs/humanevalpack.md), or the separate earlier [Systems C](docs/systems-c.md) and [BigCodeBench](docs/coding-expansion.md) panels). `coding --benchmark humanevalpack --prepare-only` prepares and validates the default expansion; `coding` evaluates its six held-out pilots followed by the 24 scored tasks. Select your local evaluation season with `--season`; the exporter matches its fingerprint to a published panel. The command writes `results/<submission-id>.json` and is idempotent for the same evaluation cycle. It exports only IDs/hashes, per-question grades and accounting, frozen settings, dates, platform and provenance. New cycles capture the clean runner commit when they begin. Legacy cycles retain a null `runner_revision`; `exporter_revision` identifies the exporter and must not be confused with the generation source. Review the file before committing. Keep the private database, prompts, answer keys, raw responses and logs on your machine.

```sh
gh auth login
gh repo fork 1duo/opencode-free-leaderboard --remote --remote-name fork
git switch -c results/YOUR_MODEL
git add results/PRINTED_SUBMISSION_ID.json
git commit -m "Add completed benchmark results for MODEL_ID"
git push -u fork HEAD
gh pr create --repo 1duo/opencode-free-leaderboard --base main
```

GitHub CLI will prompt for the PR details; use the supplied template. **Do not replace `site/` with your local export.** Maintainers keep the existing baseline; the deployment combines it with merged `results/` files. You can preview the combined view with `leaderboard build-site` before opening the PR.

CI validates strict schemas, complete question IDs/hashes, panel fingerprints, cap/usage constraints, highest exposed reasoning settings and provenance fields, then recomputes scores and deterministic intervals. New result records must name the PR's GitHub author; accepted records are immutable. Submit a new evaluation cycle rather than editing a published record. Invalid, duplicate, partial or private-content-bearing submissions fail. Fork PRs have no model/dataset secrets and may need maintainer approval before their first CI run.

Maintainers review eligibility evidence, the source revision, environment and any anomalies before merging. CI checks internal consistency; it cannot independently establish that private answers, provider behavior or submitted grades are authentic. Merged results are displayed as **Community · @contributor · unranked** in the existing Reasoning and Coding views, with a link to the submitted record. They never replace local scores, enter verified ranks or supply top recommendations. Independent reproduction can be evaluated through the normal local runner.

## Code changes

```sh
uv run --frozen --no-editable ruff check src tests grading/worker.py
uv run --frozen --no-editable pytest -q tests/test_contributions.py
node --check web/app.js
uv run --frozen --no-editable leaderboard build-site
```

Run the smallest additional checks covering your change. Use synthetic fixtures, never benchmark examples. Changed evaluation settings need a new epoch; changed benchmark/grader protocols need a replacement season. Preserve privacy and no-replay guards. UI changes need no private state or committed regenerated `site/`: CI and Pages render them from public data. Follow [SECURITY.md](SECURITY.md) for vulnerability reports.

## Publish your own fork

Publication to your fork is optional and separate from contributing upstream. Enable GitHub Pages with **GitHub Actions** as its source. Use `leaderboard --repository YOUR_USERNAME/YOUR_REPO publish` with your own GitHub permissions and private state, or use the public Pages workflow in your fork. `FREEBOARD_REPOSITORY` sets the same default. Site links and the macOS scheduled job retain this repository setting. `build-site --output` can generate an artifact for other static hosts.

Built-in `schedule install` is macOS-only and starts immediately. On Linux/WSL2, invoke `run` manually or from your own scheduler; `daily` additionally publishes and needs your explicitly configured publication repository. No GitHub access or schedule is required for manual evaluation or result-file export.
