# OpenCode Free Model Leaderboard

Choose a free OpenCode model for **reasoning** or **Python coding**.

**[View leaderboard ↗](https://1duo.github.io/opencode-free-leaderboard/)** · [JSON](https://1duo.github.io/opencode-free-leaderboard/snapshot.json) · [CSV](https://1duo.github.io/opencode-free-leaderboard/leaderboard.csv)

## Preview locally

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). No credentials, datasets or Docker needed.

```sh
git clone https://github.com/1duo/opencode-free-leaderboard.git
cd opencode-free-leaderboard
uv sync --frozen --no-editable
uv run --frozen --no-editable leaderboard build-site
uv run --frozen --no-editable python -m http.server 8000 --directory .pages-site
```

Open [localhost:8000](http://localhost:8000).

## Run benchmarks & contribute

Use your own setup on macOS, Linux or WSL2 with Docker. [Run evaluations and submit results by PR →](CONTRIBUTING.md)

## Protocol and statistics

80-question screens: 20 GPQA Diamond, 20 LiveBench and 40 LiveCodeBench. Coding adds [24 practical Python tasks](docs/coding-expansion.md). Highest available reasoning; one completion per question; 95% sampling intervals.

Only complete, matching full screens are ranked. Other coverage and community submissions are labeled unranked. Benchmark scores do not establish everyday agent performance.

[Methodology](docs/methodology.md) · [Operations](docs/operations.md) · [Architecture](docs/architecture.md) · [Security](SECURITY.md) · [License](LICENSE)
