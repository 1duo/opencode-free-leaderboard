from __future__ import annotations

import ast
import csv
import io
import json
import os
import random
import tarfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import httpx

from .config import COUNTS, PINS, SEED, Settings, credential, digest, now
from .db import DB


def upstream(settings: Settings, name: str) -> Path:
    revision = PINS[f"{name}_grader"]
    target = settings.state / "upstream" / f"{name}-{revision}"
    if target.exists():
        return target
    repo = "LiveBench/LiveBench" if name == "livebench" else "LiveCodeBench/LiveCodeBench"
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(f"https://codeload.github.com/{repo}/tar.gz/{revision}")
        response.raise_for_status()
    staging = target.with_name(target.name + ".partial")
    staging.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(*Path(member.name).parts[1:])
            if not relative.parts:
                continue
            dest = staging / relative
            if not dest.resolve().is_relative_to(staging.resolve()) or member.issym() or member.islnk():
                raise ValueError("Unsafe upstream archive")
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                dest.write_bytes(stream.read())
    staging.rename(target)
    return target


def download(settings: Settings, repo: str, filename: str, revision: str, gated=False) -> Path:
    # Use resumable HTTP downloads with the hub's bounded timeouts. The optional Xet
    # downloader can otherwise wait indefinitely on unavailable chunk services.
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    from huggingface_hub import hf_hub_download
    token = credential("hf") if gated else False
    if gated and not token:
        raise RuntimeError("GPQA needs an HF token with accepted dataset access; run auth hf")
    return Path(hf_hub_download(repo_id=repo, repo_type="dataset", filename=filename,
                               revision=revision, token=token,
                               cache_dir=settings.state / "datasets" / "cache"))


def proportional(groups: dict[str, list], count: int) -> dict[str, int]:
    total = sum(map(len, groups.values()))
    if total < count or not total:
        raise ValueError("Not enough questions for requested panel")
    quotas = {k: count * len(v) / total for k, v in groups.items()}
    allocation = {k: int(v) for k, v in quotas.items()}
    for key in sorted(quotas, key=lambda k: (-(quotas[k] - allocation[k]), k))[:count - sum(allocation.values())]:
        allocation[key] += 1
    return allocation


def sample(items: list[dict], count: int, salt: str, equal=False) -> list[dict]:
    groups = {}
    for item in items:
        groups.setdefault(item["stratum"], []).append(item)
    if equal:
        if count % len(groups):
            raise ValueError("Equal strata must divide panel count")
        allocation = {k: count // len(groups) for k in groups}
    else:
        allocation = proportional(groups, count)
    result = []
    for key, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda x: x["id"])
        random.Random(f"{SEED}:{salt}:{key}").shuffle(ordered)
        if len(ordered) < allocation[key]:
            raise ValueError("Insufficient questions in a sampling stratum")
        result.extend(ordered[:allocation[key]])
    return sorted(result, key=lambda x: x["id"])


def gpqa(settings: Settings) -> list[dict]:
    path = download(settings, "Idavidrein/gpqa", "gpqa_diamond.csv", PINS["gpqa"], True)
    result = []
    for row in csv.DictReader(path.open()):
        domain = next((row[k] for k in ["High-level domain", "Subject", "Domain"] if row.get(k)), "")
        stratum = next((x for x in ["biology", "chemistry", "physics"] if x in domain.lower()), None)
        if not stratum:
            raise ValueError("GPQA scientific subject metadata missing or changed")
        ident = "gpqa:" + digest(row)
        choices = [row["Correct Answer"], *(row[f"Incorrect Answer {i}"] for i in range(1, 4))]
        order = list(range(4))
        random.Random(f"{SEED}:{ident}").shuffle(order)
        answer = "ABCD"[order.index(0)]
        prompt = row["Question"] + "\n\n" + "\n".join(f"{letter}. {choices[i]}" for letter, i in zip("ABCD", order))
        prompt += "\n\nExplain your reasoning, then end with exactly: FINAL: A (or B, C, D)."
        result.append({"id": ident, "benchmark": "gpqa", "stratum": stratum,
                       "messages": [{"role": "user", "content": prompt}], "answer": answer})
    return result


def livebench(settings: Settings) -> list[dict]:
    import pyarrow.parquet as pq
    path = download(settings, "livebench/reasoning", "data/test-00000-of-00001.parquet", PINS["livebench_data"])
    rows = pq.read_table(path).to_pylist()
    result = []
    for row in rows:
        release = str(row["livebench_release_date"])[:10]
        removed = str(row.get("livebench_removal_date") or "")[:10]
        if release > "2024-11-25" or (removed and removed <= "2024-11-25"):
            continue
        if row["task"] not in {"spatial", "zebra_puzzle"}:
            continue
        if len(row["turns"]) != 1:
            raise ValueError("LiveBench single-turn protocol changed")
        result.append({"id": "livebench:" + row["question_id"], "benchmark": "livebench",
                       "stratum": row["task"], "answer": row["ground_truth"],
                       "release": release, "messages": [{"role": "user", "content": row["turns"][0]}]})
    return result


def lcb_formatter(path: Path):
    # Execute only the pinned upstream pure prompt function and constants. Never deserialize
    # LCB's pickled private tests on the host; that happens inside the grading container.
    tree = ast.parse((path / "lcb_runner/prompts/code_generation.py").read_text())
    names = {"PromptConstants", "get_generic_question_template_answer"}
    tree.body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    scope = {"CodeGenerationProblem": SimpleNamespace}
    exec(compile(tree, str(path), "exec"), scope)
    return scope["PromptConstants"], scope["get_generic_question_template_answer"]


def livecodebench(settings: Settings, source: Path) -> list[dict]:
    constants, formatter = lcb_formatter(source)
    result = {}
    for filename in ["test.jsonl", *[f"test{i}.jsonl" for i in range(2, 7)]]:
        path = download(settings, "livecodebench/code_generation_lite", filename, PINS["livecodebench_data"])
        for line in path.read_text().splitlines():
            row = json.loads(line)
            ident = f"livecodebench:{row['platform']}:{row['question_id']}"
            result[ident] = {"id": ident, "benchmark": "livecodebench", "stratum": row["difficulty"].lower(),
                             "raw": row, "messages": [
                                 {"role": "system", "content": constants.SYSTEM_MESSAGE_GENERIC},
                                 {"role": "user", "content": formatter(SimpleNamespace(**row))}]}
    return list(result.values())


def prepare(db: DB, settings: Settings, checkout: Path, public_only=False) -> dict:
    if not public_only and not credential('hf'):
        raise RuntimeError('GPQA requires an HF token with accepted dataset access; run auth hf')
    grading_files = {name: digest((checkout / 'grading' / name).read_text())
                     for name in ['Dockerfile', 'worker.py', 'requirements.txt']}
    # Reuse frozen questions when only the grader changes. Never resample because
    # of a setup failure, and never carry answers into a replacement season.
    for prior in db.rows('SELECT * FROM seasons ORDER BY created_at DESC'):
        saved = json.loads(prior['manifest'])
        if (saved['sources'] != PINS or saved['seed'] != SEED or saved['counts'] != COUNTS
                or saved.get('partial', False) != public_only):
            continue
        if saved['grading_files'] == grading_files:
            return {'season': prior['id'], 'reused_frozen_questions': True}
        saved['grading_files'] = grading_files
        saved.pop('grader_image', None)
        ident = ('public-pilot-' if public_only else 's1-') + digest({k: v for k, v in saved.items() if k != 'upstream_paths'})[:16]
        db.execute('INSERT OR IGNORE INTO seasons(id,created_at,manifest) VALUES(?,?,?)', (ident, now(), json.dumps(saved)))
        db.execute('INSERT OR IGNORE INTO panels SELECT ?,tier,item_id FROM panels WHERE season=?', (ident, prior['id']))
        return {'season': ident, 'reused_frozen_questions': True, 'replacement_grader': True}
    if not public_only:
        for prior in db.rows('SELECT * FROM seasons ORDER BY created_at DESC'):
            saved = json.loads(prior['manifest'])
            if not (saved.get('partial') and saved['sources'] == PINS and saved['seed'] == SEED and saved['counts'] == COUNTS):
                continue
            pool = gpqa(settings)
            confirmation = sample(pool, COUNTS['confirmation']['gpqa'], 'gpqa')
            screen = sample(confirmation, COUNTS['screen']['gpqa'], 'gpqa:screen')
            pilot = sample([i for i in pool if i['id'] not in {x['id'] for x in confirmation}], 2, 'gpqa:pilot')
            selected = {'confirmation': confirmation, 'screen': screen, 'pilot': pilot}
            saved.pop('grader_image', None)
            saved.update(partial=False, grading_files=grading_files)
            for tier, values in selected.items():
                saved['panels'][tier].extend({'id': i['id'], 'hash': digest(i), 'benchmark': 'gpqa', 'stratum': i['stratum']} for i in values)
            ident = 's1-' + digest({k: v for k, v in saved.items() if k != 'upstream_paths'})[:16]
            db.execute('INSERT OR IGNORE INTO seasons(id,created_at,manifest) VALUES(?,?,?)', (ident, now(), json.dumps(saved)))
            db.execute('INSERT OR IGNORE INTO panels SELECT ?,tier,item_id FROM panels WHERE season=?', (ident, prior['id']))
            for tier, values in selected.items():
                for item in values:
                    item_id = ident + ':' + item['id']
                    db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)',
                        (item_id, ident, 'gpqa', item['stratum'], digest(item), json.dumps(item)))
                    db.execute('INSERT OR IGNORE INTO panels VALUES(?,?,?)', (ident, tier, item_id))
            return {'season': ident, 'reused_frozen_public_questions': True,
                    'counts': {tier: len(items) for tier, items in saved['panels'].items()}}
    lb_source, lcb_source = upstream(settings, "livebench"), upstream(settings, "livecodebench")
    pools = {"livebench": livebench(settings), "livecodebench": livecodebench(settings, lcb_source)}
    if not public_only:
        pools["gpqa"] = gpqa(settings)
    panels = {tier: [] for tier in COUNTS}
    for benchmark, pool in pools.items():
        confirmation = sample(pool, COUNTS["confirmation"][benchmark], benchmark, equal=benchmark == "livebench")
        screen = sample(confirmation, COUNTS["screen"][benchmark], benchmark + ":screen", equal=benchmark == "livebench")
        held_out = [i for i in pool if i["id"] not in {x["id"] for x in confirmation}]
        pilot = sample(held_out, 2, benchmark + ":pilot", equal=benchmark == "livebench")
        for tier, selected in [("confirmation", confirmation), ("screen", screen), ("pilot", pilot)]:
            panels[tier].extend(selected)
    manifest = {"seed": SEED, "sources": PINS, "livebench_release": "2024-11-25",
                "livecodebench_release": "release_v6", "language": "Python", "counts": COUNTS,
                "partial": public_only,
                "protocol": {"output_cap": settings.max_output_tokens, "temperature": 0,
                             "tools": False, "completions": 1},
                "grading_files": grading_files,
                "upstream_paths": {"livebench": str(lb_source), "livecodebench": str(lcb_source)},
                "panels": {tier: [{"id": i["id"], "hash": digest(i), "benchmark": i["benchmark"],
                                   "stratum": i["stratum"]} for i in items] for tier, items in panels.items()}}
    season = ("public-pilot-" if public_only else "s1-") + digest({k: v for k, v in manifest.items() if k != "upstream_paths"})[:16]
    db.execute("INSERT OR IGNORE INTO seasons(id,created_at,manifest) VALUES(?,?,?)", (season, now(), json.dumps(manifest)))
    for tier, selected in panels.items():
        for item in selected:
            item_id = season + ":" + item["id"]
            db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)",
                       (item_id, season, item["benchmark"], item["stratum"], digest(item), json.dumps(item)))
            db.execute("INSERT OR IGNORE INTO panels VALUES(?,?,?)", (season, tier, item_id))
    return {"season": season, "counts": {k: len(v) for k, v in panels.items()},
            "strata": {k: dict(Counter(i["stratum"] for i in v)) for k, v in panels.items()}}
