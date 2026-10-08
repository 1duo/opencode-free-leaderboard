from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings, now
from .db import DB
from .scoring import Snapshot, snapshot

SITE_FILES = {"index.html", "app.js", "style.css", "snapshot.json", "leaderboard.csv", "manifest.json", ".nojekyll"}
SOURCE_ROOTS = {"src", "tests", "grading", "web", "docs", ".github"}
SOURCE_FILES = {"pyproject.toml", "uv.lock", ".gitignore", "README.md", "LICENSE", "AGENTS.md"}


def validate_site(folder: Path) -> Snapshot:
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
    environment = Environment(loader=FileSystemLoader(checkout / "web"), autoescape=select_autoescape(["html"]))
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=checkout) as temp:
        stage = Path(temp) / "site"
        stage.mkdir()
        (stage / "snapshot.json").write_text(report.model_dump_json(indent=2))
        (stage / "manifest.json").write_text(json.dumps([m.model_dump() for m in report.seasons], indent=2))
        (stage / "index.html").write_text(environment.get_template("index.html").render(report=report))
        for name in ["app.js", "style.css"]:
            shutil.copy(checkout / "web" / name, stage / name)
        (stage / ".nojekyll").write_text("")
        fields = ["model_id", "tier", "season", "epoch", "status", "availability", "evaluated_at", "reasoning", "coding", "overall",
                  "gpqa_n", "livebench_n", "livecodebench_n", "accounted_tokens", "reported_tokens", "truncation_rate"]
        with (stage / "leaderboard.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in report.rows + report.history:
                data = {k: getattr(row, k) for k in fields if hasattr(row, k)}
                data.update({k: (row.scores or {}).get(k) for k in ["reasoning", "coding", "overall"]})
                data.update({f"{k}_n": (row.sample_counts or {}).get(k, 0) for k in ["gpqa", "livebench", "livecodebench"]})
                # Keep spreadsheet programs from executing formula-like model names.
                writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in data.items()})
        validate_site(stage)
        target, backup = checkout / "site", Path(temp) / "previous"
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
    return {"snapshot": report.snapshot_id, "site": str(checkout / "site"), "ranked": sum(r.scores is not None for r in report.rows)}


def command(args: list[str], checkout: Path) -> str:
    result = subprocess.run(args, cwd=checkout, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} operation failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


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
        if path.is_file() and (str(rel) in SOURCE_FILES or rel.parts[0] in SOURCE_ROOTS or rel.parts[0] == "site"):
            if path.is_symlink() or path.suffix in {".sqlite3", ".db", ".pyc", ".log"}:
                raise ValueError("Unsafe file in publication tree")
            allowed.append(str(rel))
    unexpected = [p for p in tracked if p.split("/")[0] not in SOURCE_ROOTS | {"site"} and p not in SOURCE_FILES]
    if unexpected:
        raise ValueError("Unexpected tracked files; review before publication")
    command(["git", "add", "--", *sorted(allowed)], checkout)
    staged = command(["git", "diff", "--cached", "--name-only"], checkout)
    if staged:
        command(["git", "commit", "-m", "Publish reproducible free-model leaderboard snapshot"], checkout)
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
    # Configure Pages before pushing so the first workflow can deploy successfully.
    pages = subprocess.run(["gh", "api", f"repos/{settings.repository}/pages"], capture_output=True, text=True)
    if pages.returncode:
        command(["gh", "api", "--method", "POST", f"repos/{settings.repository}/pages", "-f", "build_type=workflow"], checkout)
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
