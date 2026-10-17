from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings, digest, now
from .db import DB
from .scoring import Snapshot, snapshot

SITE_FILES = {"index.html", "app.js", "style.css", "snapshot.json", "leaderboard.csv", "manifest.json", ".nojekyll"}
SOURCE_ROOTS = {"src", "tests", "grading", "web", "docs", ".github", "results"}
SOURCE_FILES = {"pyproject.toml", "uv.lock", ".gitignore", "README.md", "LICENSE", "AGENTS.md", "SECURITY.md", "CONTRIBUTING.md"}


def public_source(path: str) -> bool:
    if path in SOURCE_FILES:
        return True
    if path == '.github/pull_request_template.md':
        return True
    part = Path(path)
    root = part.parts[0]
    if root == 'site':
        return len(part.parts) == 2 and part.name in SITE_FILES
    if root == 'results':
        return len(part.parts) == 2 and bool(re.fullmatch(r'[0-9a-f]{32}\.json', part.name))
    extensions = {'src': {'.py'}, 'tests': {'.py'}, 'docs': {'.md'},
                  '.github': {'.yml', '.yaml'}, 'web': {'.html', '.css', '.js'}}
    if root == 'grading':
        return path in {'grading/Dockerfile', 'grading/worker.py', 'grading/requirements.txt',
                        'grading/coding.Dockerfile', 'grading/coding_worker.py', 'grading/coding-requirements.txt'}
    return root in extensions and part.suffix in extensions[root]


def validate_site(folder: Path) -> Snapshot:
    if folder.is_symlink():
        raise ValueError('Public export must not be a symlink')
    files = {p.name for p in folder.iterdir()}
    if files != SITE_FILES or any(p.is_symlink() or not p.is_file() for p in folder.iterdir()):
        raise ValueError("Unexpected file in public export")
    report = Snapshot.model_validate_json((folder / "snapshot.json").read_text())
    manifests = json.loads((folder / "manifest.json").read_text())
    if manifests != [m.model_dump() for m in report.seasons]:
        raise ValueError("Manifest does not match sanitized snapshot")
    for manifest in report.seasons:
        for tier, items in manifest.panels.items():
            for item in items:
                if set(item) != {"id", "hash", "benchmark", "stratum"}:
                    raise ValueError("Private benchmark material in manifest")
    # No renderer gets access to the SQLite connection, question content, or response files.
    for text_path in folder.iterdir():
        text = text_path.read_text()
        if "BEGIN PRIVATE KEY" in text or "Authorization: Bearer" in text:
            raise ValueError("Credential-like content in public export")
    return report


def export(db: DB, settings: Settings, checkout: Path) -> dict:
    report = snapshot(db, settings)
    return render_site(with_community(report, checkout), checkout, checkout / 'site')


def with_community(report: Snapshot, checkout: Path) -> Snapshot:
    from .submissions import load_submissions
    results = load_submissions(checkout, [m.model_dump() for m in report.seasons])
    data = report.model_copy(update={'community_results': results, 'published_at': None})
    if results or report.community_results:
        data.snapshot_id = digest(data.model_dump(mode='json', exclude={'snapshot_id', 'published_at'}))[:20]
    return data


def build_site(checkout: Path, output: Path, repository: str | None = None) -> dict:
    # Public-only rendering works in a fresh clone and PR CI without private DB,
    # credentials, Docker, model access or benchmark datasets.
    report = Snapshot.model_validate_json((checkout / 'site/snapshot.json').read_text())
    if repository and repository != report.repository:
        report.snapshot_id = digest([report.snapshot_id, repository])[:20]
        report.repository = repository
    return render_site(with_community(report, checkout), checkout, output)


def render_site(report: Snapshot, checkout: Path, target: Path) -> dict:
    if target.is_symlink():
        raise ValueError('Build output must not be a symlink')
    target = target.resolve()
    root = checkout.resolve()
    if target.is_relative_to(root) and target not in {root / 'site', root / '.pages-site'}:
        raise ValueError('Inside the checkout, build output must be site/ or .pages-site/')
    if target.exists() and (not target.is_dir() or {p.name for p in target.iterdir()} != SITE_FILES):
        raise ValueError('Refusing to replace a directory that is not a complete site artifact')
    target.parent.mkdir(parents=True, exist_ok=True)
    environment = Environment(loader=FileSystemLoader(checkout / "web"), autoescape=select_autoescape(["html"]))
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=target.parent) as temp:
        stage = Path(temp) / "site"
        stage.mkdir()
        (stage / "snapshot.json").write_text(report.model_dump_json(indent=2))
        (stage / "manifest.json").write_text(json.dumps([m.model_dump() for m in report.seasons], indent=2))
        (stage / "index.html").write_text(environment.get_template("index.html").render(report=report))
        for name in ["app.js", "style.css"]:
            shutil.copy(checkout / "web" / name, stage / name)
        (stage / ".nojekyll").write_text("")
        fields = ["model_id", "tier", "transport", "client_version", "season", "epoch", "status", "availability", "evaluated_at", "reasoning_setting", "reasoning_variant", "reasoning", "coding", "overall", "gpqa", "livebench", "livecodebench",
                  "gpqa_n", "livebench_n", "livecodebench_n", "gpqa_evaluated_at", "livebench_evaluated_at", "livecodebench_evaluated_at",
                  "accounted_tokens", "reported_tokens", "truncation_rate", "manual_retry_count", "manual_recovered_questions", "public_only_reason",
                  "cap_limit", "cap_observed", "category", "category_score", "sample_n", "source_tier", "rank", "basis", "category_interval_note", "stale", "refresh_pending", "verification", "contributor", "submission_id"]
        fields += [f'{metric}_ci_{bound}' for metric in ['reasoning','coding','overall','gpqa','livebench','livecodebench'] for bound in ['low','high']]
        fields += ['livebench_interval_note', 'livecodebench_interval_note', 'bigcodebench', 'bigcodebench_n', 'bigcodebench_evaluated_at', 'bigcodebench_ci_low', 'bigcodebench_ci_high']
        with (stage / "leaderboard.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
            writer.writeheader()
            for row in report.rows + report.history:
                data = {k: getattr(row, k) for k in fields if hasattr(row, k)}
                data['transport'] = 'local-opencode'
                data.update({f'cap_{k}': v for k, v in (row.cap_failure or {}).items()})
                data.update({k: (row.scores or {}).get(k) for k in ["reasoning", "coding", "overall", "gpqa", "livebench", "livecodebench"]})
                data.update(row.benchmark_scores)
                data.update({f'{metric}_ci_{bound}': limits[index]
                             for metric, limits in {**(row.intervals or {}), **row.benchmark_intervals}.items()
                             for index, bound in enumerate(['low','high'])})
                data.update({f'{k}_evaluated_at': v for k, v in row.benchmark_evaluated_at.items()})
                data.update({f"{k}_n": row.progress.get(k, 0) for k in ["gpqa", "livebench", "livecodebench"]})
                # Keep spreadsheet programs from executing formula-like model names.
                writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in data.items()})
            for result in report.community_results:
                for category, score in result.scores.items():
                    benchmarks = [b for b in (['livecodebench','bigcodebench'] if category == 'coding' else ['gpqa','livebench']) if b in result.sample_counts]
                    data = {'model_id': result.model_id, 'tier': 'community', 'transport': 'local-opencode',
                        'client_version': '1.19.3', 'season': result.season, 'source_tier': result.tier,
                        'status': 'complete', 'verification': result.verification, 'contributor': result.contributor,
                        'submission_id': result.submission_id, 'evaluated_at': result.evaluated_at,
                        'reasoning_setting': result.reasoning_setting, 'reasoning_variant': result.reasoning_variant,
                        'category': category, 'category_score': score, category: score,
                        'sample_n': sum(result.sample_counts[b] for b in benchmarks),
                        **{f'{b}_n': n for b, n in result.sample_counts.items()}, **result.benchmark_scores}
                    if category in result.intervals:
                        data.update({f'{category}_ci_{bound}': result.intervals[category][index] for index, bound in enumerate(['low', 'high'])})
                    writer.writerow(data)
            models = {r.model_id: r for r in report.rows if r.tier == 'screen'}
            for result in report.coding_evaluations:
                data={'model_id':result.model_id,'epoch':result.epoch,'tier':'coding','source_tier':'coding',
                    'transport':'local-opencode','client_version':'1.19.3','season':result.season,'status':result.status,
                    'category':'coding','category_score':result.score,'bigcodebench':result.score,
                    'bigcodebench_n':result.progress,'sample_n':result.progress,'evaluated_at':result.evaluated_at,
                    'bigcodebench_evaluated_at':result.evaluated_at,'accounted_tokens':result.accounted_tokens,
                    'reported_tokens':result.reported_tokens,'truncation_rate':result.truncation_rate}
                if result.interval:
                    data.update(bigcodebench_ci_low=result.interval[0],bigcodebench_ci_high=result.interval[1])
                writer.writerow({k:"'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for k,v in data.items()})
            for result in report.capabilities:
                model = models[result.model_id]
                data = {'model_id': result.model_id, 'epoch': result.epoch, 'tier': 'capability',
                        'transport': 'local-opencode', 'client_version': model.client_version,
                        'availability': model.availability, 'reasoning_setting': model.reasoning_setting,
                        'reasoning_variant': model.reasoning_variant, 'season': result.season,
                        'status': 'complete' if result.score is not None else model.status,
                        'category': result.category, 'category_score': result.score,
                        result.category: result.score, 'source_tier': result.source_tier,
                        'sample_n': sum(result.sample_counts.values()), 'rank': result.rank,
                        'stale': result.stale, 'refresh_pending': result.refresh_pending,
                        'basis': ' + '.join({'gpqa':'GPQA', 'livebench':'LiveBench', 'livecodebench':'LiveCodeBench', 'bigcodebench':'BigCodeBench'}[b]
                                            for b in result.sample_counts),
                        'evaluated_at': result.evaluated_at, 'category_interval_note': result.interval_note,
                        **result.benchmark_scores, **{f'{b}_n': n for b, n in result.sample_counts.items()}}
                data.update({f'{b}_evaluated_at': stamp for b, stamp in result.benchmark_evaluated_at.items()})
                if result.interval:
                    data.update({f'{result.category}_ci_{bound}': result.interval[index]
                                 for index, bound in enumerate(['low', 'high'])})
                writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in data.items()})
            for row in report.public_screens + report.lightweight:
                data = {k: getattr(row, k) for k in fields if hasattr(row, k)}
                data.update(row.benchmark_scores)
                data.update({f'{metric}_ci_{bound}': limits[index] for metric, limits in row.intervals.items()
                             for index, bound in enumerate(['low','high'])})
                data.update({f'{metric}_interval_note': note for metric, note in getattr(row, 'interval_warnings', {}).items()})
                data.update({f'{k}_n': row.progress[k] for k in row.expected})
                data.update({f'{k}_evaluated_at': v for k, v in row.benchmark_evaluated_at.items()})
                writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in data.items()})
        validate_site(stage)
        backup = Path(temp) / "previous"
        if target.exists():
            if target.is_symlink():
                raise ValueError("Public site directory must not be a symlink")
            target.rename(backup)
        try:
            stage.rename(target)
        except BaseException:
            if backup.exists():
                backup.rename(target)
            raise
    return {"snapshot": report.snapshot_id, "site": str(target),
            "ranked": len({r.model_id for r in report.capabilities if r.rank is not None}),
            'community_submissions': len(report.community_results)}


def command(args: list[str], checkout: Path) -> str:
    try:
        result = subprocess.run(args, cwd=checkout, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f'{args[0]} operation unavailable or timed out; private state is retained') from None
    if result.returncode:
        raise RuntimeError(f"{args[0]} operation failed; check authentication, connectivity and repository permissions")
    return result.stdout.strip()


def ensure_pages(settings: Settings, checkout: Path) -> None:
    route = f'repos/{settings.repository}/pages'
    def inspect() -> dict | None:
        try:
            result = subprocess.run(['gh', 'api', route], cwd=checkout,
                                    capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError('Unexpected Pages response')
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise RuntimeError('Cannot verify GitHub Pages; existing deployment is unchanged') from None
        if result.returncode and data.get('status') == '404':
            return None
        if result.returncode or data.get('build_type') != 'workflow':
            raise RuntimeError('GitHub Pages verification failed; a workflow publication source is required')
        return data
    if inspect() is None:
        # A permission, rate-limit or server error is never evidence of absence.
        command(['gh', 'api', '--method', 'POST', route, '-f', 'build_type=workflow'], checkout)


def publish(db: DB, settings: Settings, checkout: Path, create=False) -> dict:
    # Stage exact paths; never 'git add .' or private directories.
    exported = export(db, settings, checkout)
    validate_site(checkout / "site")
    if not (checkout / ".git").exists():
        command(["git", "init", "-b", "main"], checkout)
    tracked = command(["git", "ls-files"], checkout).splitlines()
    allowed = []
    candidates = [checkout / name for name in SOURCE_FILES if (checkout / name).exists()]
    for name in sorted(SOURCE_ROOTS | {'site'}):
        root = checkout / name
        if root.is_symlink():
            raise ValueError('Publication source directory must not be a symlink')
        if root.exists():
            candidates.extend(root.rglob('*'))
    for path in candidates:
        rel = path.relative_to(checkout)
        if any(part.startswith(".") and part not in {".github", ".gitignore", ".nojekyll"} for part in rel.parts):
            continue
        if any(part in {"__pycache__", ".venv", ".pytest_cache"} for part in rel.parts):
            continue
        if path.is_file():
            if path.is_symlink() or not public_source(str(rel)):
                raise ValueError("Unsafe file in publication tree")
            allowed.append(str(rel))
    staged_paths = command(['git', 'diff', '--cached', '--name-only'], checkout).splitlines()
    unexpected = [p for p in tracked + staged_paths if not public_source(p)]
    if unexpected:
        raise ValueError("Unexpected tracked files; review before publication")
    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=checkout, capture_output=True, text=True)
    expected = f"https://github.com/{settings.repository}.git"
    if origin.returncode:
        exists = subprocess.run(["gh", "repo", "view", settings.repository, "--json", "name"], cwd=checkout, capture_output=True)
        if exists.returncode:
            if not create:
                raise RuntimeError("Public repository does not exist; publish --create-repository")
            command(["gh", "repo", "create", settings.repository, "--public", "--description",
                     "Budget-accounted benchmarks of verified free OpenCode Zen endpoints"], checkout)
        command(["git", "remote", "add", "origin", expected], checkout)
    elif origin.stdout.strip() not in {expected, f"git@github.com:{settings.repository}.git"}:
        raise ValueError("Origin does not match the configured publication repository")
    ensure_pages(settings, checkout)
    command(["git", "add", "--", *sorted(allowed)], checkout)
    staged = command(["git", "diff", "--cached", "--name-only"], checkout)
    if staged:
        command(["git", "commit", "-m", "Publish reproducible free-model leaderboard snapshot"], checkout)
    command(["git", "push", "-u", "origin", "main"], checkout)
    sha = command(["git", "rev-parse", "HEAD"], checkout)
    db.execute("INSERT INTO publications(snapshot_id,created_at,commit_sha,deployment_status) VALUES(?,?,?,?)",
               (exported["snapshot"], now(), sha, "pushed"))
    return {"repository": f"https://github.com/{settings.repository}",
            "page": f"https://{settings.repository.split('/')[0]}.github.io/{settings.repository.split('/')[1]}/",
            "commit": sha, "snapshot": exported["snapshot"], "deployment": "pending"}


def refresh_publications(db: DB, settings: Settings, checkout: Path) -> None:
    pending = db.rows("SELECT * FROM publications WHERE deployment_status='pushed'")
    if not pending:
        return
    try:
        result = subprocess.run(["gh", "run", "list", "--repo", settings.repository,
                                 "--workflow", "pages.yml", "--limit", "20", "--json",
                                 "headSha,status,conclusion"], cwd=checkout, capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode:
        return
    for record in pending:
        run = next((r for r in json.loads(result.stdout) if r["headSha"] == record["commit_sha"]), None)
        if run and run["status"] == "completed":
            db.execute("UPDATE publications SET deployment_status=? WHERE id=?",
                       ("deployed" if run["conclusion"] == "success" else "failed", record["id"]))
