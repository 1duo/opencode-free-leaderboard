# Operations and recovery

## Preflight

Run commands from the repository root. Use `uv run --frozen --no-editable leaderboard doctor` for bounded local execution checks: SQLite integrity, disk capacity, pinned client, Docker and frozen panels. `doctor --publication` additionally checks GitHub access; `--scheduled` checks the macOS launch agent. GitHub access and scheduling are not needed for manual evaluation. It exits nonzero when a requested prerequisite is missing and never sends inference requests or reads credentials. Provider access is established separately by the compatibility pilot.

`leaderboard status` reports model availability, queue, usage and publication status. `export` and `status` do not rewrite large backups. `--state /absolute/private/path` and `--checkout /path/to/repository` are root options before the command. Private state must stay outside the public checkout.

The generation adapter deliberately requires OpenCode 1.19.3. Do not change the version check to work around an upgrade: v2 uses different configuration, session, usage and permission semantics. Validate those, the combined token cap and no-repair behavior before a protocol revision and new model epochs. Metadata-only `discover --catalog-only` is safe with a changed client. Existing records and scores remain available.

## Pinned client

Run `leaderboard install-client` to install a platform-specific, checksum-pinned executable from the official [OpenCode v1.19.3 release](https://github.com/anomalyco/opencode/releases/tag/v1.19.3). macOS/Linux ARM64 and x86-64 are supported; Windows uses WSL2. The installer checks the SHA-256 before execution and the version before atomically installing `state/bin/opencode-1.19.3` with mode `0700`. The runner checks this path before looking at the interactive executable. Existing valid binaries are reused; invalid ones require explicit preservation/removal. Use `leaderboard doctor` afterward; installing a binary is not a successful provider pilot.

Docker commands respect explicit `DOCKER_HOST`, `DOCKER_CONTEXT` and `DOCKER_CONFIG`. If none selects a host/context, an existing `state/colima/freeboard/docker.sock` is used for the private benchmark engine. This does not change the interactive Docker context or start a new VM.

## Scheduling

The managed scheduled checkout lives at `state/scheduler-checkout`; the private runtime lives at `state/runtime`. After publishing source updates, fast-forward the managed checkout and update its installed package with the frozen lock. Schedule installation prepares the checkout before touching the existing job and restores the old plist when bootstrap fails. Installation is an operator action, not a routine daily step, because RunAtLoad starts work immediately.

The plist is `~/Library/LaunchAgents/ai.opencode.free-leaderboard.plist`. It inherits the installed Docker host/config where configured. Logs live at `state/logs/scheduler.log` and `scheduler-error.log`. Inspect without starting a job:

```sh
launchctl print "gui/$(id -u)/ai.opencode.free-leaderboard"
uv run --frozen --no-editable leaderboard doctor
```

All work holds a nonblocking process lock. A second invocation exits rather than dispatching concurrently. Wake the computer and keep Docker running for scheduled work. Inference has a ten-minute overall deadline. Provider quotas defer work; rejections and infrastructure failures do not lower capability scores.

Manual evaluation and result export work on Linux/WSL2 without launchd. Use your own scheduler for repeated `run` invocations. `daily` includes publication and requires an explicitly configured writable repository. Root `--repository OWNER/REPO` (or `FREEBOARD_REPOSITORY`) controls fork publication, generated links and the installed launch agent's repository argument.

## Interrupted work

Resume the original command. An accepted response is atomically saved and synced before grading. Recovery grades saved responses without generating again. A request with an unknown outcome stays ambiguous and keeps a conservative token charge. Errors after answer text, reasoning, tools or usage are not treated as empty rejections.

A proven native free-quota rejection can resume only when the saved transcript establishes the exact question/model/variant, a single empty assistant turn and zero usage. The provider retry time applies across that model's seasons. The original three-attempt allowance and charges remain.

`retry-missing --season SEASON_ID [--model MODEL_ID]` consumes one explicit operator-authorized recovery for an unanswered full-screen question. It never replaces a durable, scored, withheld or cap-invalid answer. Scheduling never grants this exception. Further exceptional recoveries require separate authorization and audit records; do not reset the existing allowance. Preserve partial and unknown records rather than improving the sample by deletion.

## Grading and seasons

Code executes serially in disposable containers with no network or credentials, non-root UID, read-only root, temporary storage, dropped capabilities, one CPU, 1 GiB memory, 64 processes and bounded time. The pinned official code harness uses six-second test limits; a 180-second outer timeout is infrastructure uncertainty and stays pending. The immutable grader image ID and source hashes belong to each season.

Validate a frozen season without resampling with `prepare-panels --season SEASON_ID`. For a replacement, prepare pinned data/graders, run `pilot --season SEASON_ID` and `run --season SEASON_ID`, then promote the already validated candidate using `prepare-panels --season SEASON_ID --promote` only after every runnable model has a complete replacement screen. Keep old seasons separately.

## Backups and disk pressure

Seven validated daily SQLite backups live at `state/backups`. A new backup needs free space for the complete database plus 64 MiB headroom. Backup writes go to a private temporary directory, receive an integrity check and fsync, then replace the day's successful copy. A failed write preserves previous copies; rotation happens afterward. Temporary backup files never enter the repository.

If a legacy backup has a nonempty WAL, stop leaderboard work and checkpoint it with SQLite before replacement. Never delete a WAL: it may contain committed data. Inspect available disk space before dataset or image setup. Local backups protect logical state; an independent encrypted copy is needed for disk loss. Back up response files, selected datasets and immutable grader images as well.

To restore, stop the launch agent, preserve the existing private state, and restore a validated SQLite backup and its matching response/dataset/image files into a separate private state directory. Run SQLite `PRAGMA quick_check` and `leaderboard --state ... status` before resuming. Missing evidence must remain pending; never regenerate accepted answers because files are missing.

## Publication failures

`publish` validates a staged seven-file site, verifies Pages, stages allowed source paths and pushes a complete commit. Pages creation requires a confirmed HTTP 404; authentication, rate-limit and transient failures stop publication. No model or dataset credentials are available to Actions. The reusable release checks must pass before deployment.

A failed export preserves the previous local site; a failed deployment preserves the live site. A failed push can leave a local commit: fix GitHub access, inspect `git status` and remote history, then rerun `publish`. Do not force-push away published history. `status` reconciles pushed publication records with their exact workflow commit. A successful push alone is not a successful deployment.

The site rejects a mismatched snapshot rather than mixing cached HTML/assets and new data. Reload resolves a deployment-boundary cache mismatch. The page labels overdue scores, hides recommendations when eligibility evidence is older than seven days, and retains original evaluation dates.

## Result pull requests

`submit-results` exports only completed, currently eligible evaluations matching a published frozen panel. Public result files contain grades and accounting, not answers, prompts, response text or private paths. A new cycle records its clean generation-source commit; legacy cycles explicitly retain an unknown commit, separately from the exporter revision. The evaluation's native client, highest reasoning variant, platform and immutable grader image remain reviewable.

Result PRs append records bound to the GitHub PR author; accepted files cannot be edited through the ordinary result-PR path. Maintainers review source/provenance and merge. The public build recomputes community scores and intervals from these files without accessing SQLite or credentials. Community rows are labeled, unranked and never replace the baseline or supply recommendations. CI consistency checks are not independent reproduction of submitted grades.

`leaderboard build-site` creates ignored `.pages-site/` using the committed public baseline, accepted result files and current renderer. Pages uses this path too, so merging a results or UI PR publishes without needing the maintainer's local runner. A local `export` keeps merged community records alongside the private runner's baseline. Full contributor instructions are in [CONTRIBUTING.md](../CONTRIBUTING.md).
