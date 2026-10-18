"""Frozen multilingual HumanEvalPack generation/repair subset; CPU grading only."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pyarrow.parquet as pq

from .coding import coding_base
from .config import SEED, digest, now
from .grading import GradingUnavailable, docker_environment
from .panels import download

DATA_REVISION = '9a41762f73a8cb23bb5811b73d5aab164efcf378'
PANEL = 'humanevalpack-multilingual-v1'
LANGUAGES = {'cpp': 'C++17', 'go': 'Go 1.19', 'rust': 'Rust 2021'}
STRATA = {f'{language}_{task}': 4 for language in LANGUAGES for task in ['generation', 'repair']}
FILES = ['humanevalpack.Dockerfile', 'humanevalpack_worker.py', 'humanevalpack_go.go']
INSTRUCTIONS = ('Return exactly one fenced code block containing the complete source file, '
    'including required imports, the supplied function interface and any public helpers. '
    'Do not include tests or a test runner. No tools, network or repair turns. '
    'For Go include package main; for C++ do not define main. '
    'The standard library is available, together with the imports in the public starter.')


def select_humanevalpack(datasets: dict[str, list[dict]]) -> dict[str, list[dict]]:
    indexed = {}
    for language in LANGUAGES:
        rows = datasets[language]
        indexed[language] = {row['task_id'].split('/')[-1]: row for row in rows}
        if len(indexed[language]) != len(rows):
            raise ValueError('Duplicate HumanEvalPack problem family')
    common = set.intersection(*(set(rows) for rows in indexed.values()))
    families = sorted(common, key=lambda family: digest([SEED, PANEL, family]))
    if len(families) < 30:
        raise ValueError('Insufficient distinct multilingual problem families')
    panels = {'coding': [], 'coding_pilot': []}
    # One held-out plus four scored tasks in each language/task stratum. Each
    # underlying HumanEval problem occurs only once across all thirty tasks.
    for group, (stratum, count) in enumerate(sorted(STRATA.items())):
        language, task = stratum.split('_')
        for position, family in enumerate(families[group * (count + 1):(group + 1) * (count + 1)]):
            raw = indexed[language][family]
            starter = ('package main\n\n' if language == 'go' else '') + raw['prompt']
            supplied = starter + (raw['buggy_solution'] if task == 'repair' else '')
            prompt = raw['instruction'] + '\n\n' + (
                'Repair this implementation to meet the specification. Return the entire corrected file.'
                if task == 'repair' else
                'Finish this public starter. Return the entire completed file.')
            prompt += f'\n\n```{language}\n{supplied}\n```'
            test = raw['test']
            test_count = (len(re.findall(r'(?m)^func Test\w+\(', test)) if language == 'go' else
                          len(re.findall(r'#\[test\]', test)) if language == 'rust' else
                          len(re.findall(r'\bassert\s*\(', test)))
            if not test_count or '#[ignore]' in test or 't.Skip(' in test:
                raise ValueError('Missing or disabled upstream tests')
            item = {'benchmark': 'humanevalpack', 'stratum': stratum, 'language': language,
                'task': task, 'family': family, 'source_id': raw['task_id'],
                'test_setup': raw['test_setup'],
                'test': test, 'test_count': test_count,
                'reference': starter + raw['canonical_solution'],
                'buggy': starter + raw['buggy_solution'],
                'messages': [{'role': 'system', 'content': INSTRUCTIONS},
                             {'role': 'user', 'content': prompt}]}
            item['id'] = f'humanevalpack:{DATA_REVISION[:12]}:{stratum}:{family}:' + digest(item)[:12]
            panels['coding_pilot' if position == 0 else 'coding'].append(item)
    return panels


def prepare_humanevalpack(runner) -> dict:
    base = json.loads(coding_base(runner.db)['manifest'])
    datasets = {language: pq.read_table(download(runner.settings, 'bigcode/humanevalpack',
        f'{language}/test-00000-of-00001.parquet', DATA_REVISION)).to_pylist() for language in LANGUAGES}
    panels = select_humanevalpack(datasets)
    public = {tier: [{'id': i['id'], 'hash': digest(i), 'benchmark': i['benchmark'], 'stratum': i['stratum']}
                     for i in items] for tier, items in panels.items()}
    manifest = {**base, 'partial': True, 'language': 'C++17 / Go 1.19 / Rust 2021', 'panels': public,
        'counts': {'coding': {'humanevalpack': 24}, 'coding_pilot': {'humanevalpack': 6}},
        'sources': {**base['sources'], 'humanevalpack_data': DATA_REVISION},
        'base_grader_image': base['grader_image'], 'grader_image': None,
        'grading_files': {**base['grading_files'],
                         **{name: digest((runner.checkout / 'grading' / name).read_text()) for name in FILES}},
        'protocol': {'output_cap': 4096, 'temperature': 0, 'tools': False, 'completions': 1,
            'panel': PANEL, 'strata': {'humanevalpack': STRATA}, 'toolchain_packages_hash': None,
            'purpose': 'Multilingual generation and repair subset; equal-weight coding expansion'}}
    ident = 'coding-' + digest(manifest)[:16]
    saved = runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))
    if saved and saved['validated']:
        runner.grader.build(json.loads(saved['manifest']), runner.checkout)
        return saved
    if shutil.disk_usage(runner.settings.state).free < 1536 * 1024**2:
        raise GradingUnavailable('HumanEvalPack toolchain preparation needs at least 1.5 GiB free disk space')
    runner.db.execute('INSERT OR IGNORE INTO seasons VALUES(?,?,?,?,?)', (ident, now(), json.dumps(manifest), 0, 0))
    for tier, items in panels.items():
        for item in items:
            prior = runner.db.one('SELECT question_hash FROM items WHERE id=?', (item['id'],))
            if prior and prior['question_hash'] != digest(item):
                raise ValueError('Frozen HumanEvalPack task changed')
            runner.db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)',
                (item['id'], ident, 'humanevalpack', item['stratum'], digest(item), json.dumps(item)))
            runner.db.execute('INSERT OR IGNORE INTO panels VALUES(?,?,?)', (ident, tier, item['id']))
    env = docker_environment(runner.settings)
    # Reuse the local frozen C image's installed GCC layer when available. The
    # selected base ID is recorded; fresh clones can build from the common image.
    compiled_base = runner.db.one("""SELECT manifest FROM seasons WHERE validated=1
        AND json_extract(manifest,'$.protocol.panel')='systems-c-v1'
        ORDER BY created_at DESC LIMIT 1""")
    base_image = json.loads(compiled_base['manifest'])['grader_image'] if compiled_base else base['grader_image']
    manifest['base_grader_image'] = base_image
    tag = 'freeboard-humanevalpack:' + digest(manifest)[:16]
    with tempfile.TemporaryDirectory(dir=runner.settings.state) as folder:
        context = Path(folder)
        for name in FILES:
            shutil.copy(runner.checkout / 'grading' / name, context / name)
        built = subprocess.run(['docker', 'build', '--build-arg', f'BASE_IMAGE={base_image}',
            '-f', str(context / 'humanevalpack.Dockerfile'), '-t', tag, str(context)],
            capture_output=True, text=True, timeout=900, env=env)
        (runner.settings.state / 'logs/humanevalpack-grader-build.log').write_text(built.stdout + built.stderr)
        if built.returncode:
            raise GradingUnavailable('HumanEvalPack image build failed; inspect private logs/humanevalpack-grader-build.log')
    image = subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'],
        text=True, timeout=15, env=env).strip()
    packages = subprocess.check_output(['docker', 'run', '--rm', '--network=none', '--read-only',
        '--entrypoint', 'cat', image, '/opt/humanevalpack-packages.txt'], text=True, timeout=15, env=env)
    manifest['protocol']['toolchain_packages_hash'] = digest(packages)
    failures = []
    for items in panels.values():
        for item in items:
            reference = f"```{item['language']}\n{item['reference']}\n```"
            buggy = f"```{item['language']}\n{item['buggy']}\n```"
            if runner.grader.grade(item, reference, image) != 1:
                failures.append(item['source_id'] + ':reference')
            if item['task'] == 'repair' and runner.grader.grade(item, buggy, image) != 0:
                failures.append(item['source_id'] + ':undetected-bug')
    if failures:
        raise GradingUnavailable('HumanEvalPack validation failed without replacement sampling: ' + ', '.join(failures))
    validate_fixtures(runner.grader, image)
    manifest['grader_image'] = image
    runner.db.execute('UPDATE seasons SET manifest=?,validated=1 WHERE id=?', (json.dumps(manifest), ident))
    return runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))


def validate_fixtures(grader, image: str) -> None:
    fixtures = {
        'cpp': ('int twice(int x){return x*2;}', '#include <cassert>\nint main(){assert(twice(2)==4);}', ''),
        'go': ('package main\nfunc Twice(x int) int { return x * 2 }',
               'func TestTwice(t *testing.T) { if Twice(2) != 4 { t.Fatal("wrong") } }',
               'package main\nimport "testing"\n'),
        'rust': ('fn twice(x:i32)->i32{x*2}', '#[test]\nfn test_twice(){assert_eq!(twice(2),4);}', ''),
    }
    loops = {'cpp': 'int twice(int x){volatile int y=x;while(y){} return x;}',
             'go': 'package main\nfunc Twice(x int) int { for {} }',
             'rust': 'fn twice(_x:i32)->i32{loop{}}'}
    exits = {'cpp': '#include <cstdlib>\nint twice(int x){(void)x;std::exit(0);}',
             'go': 'package main\nimport "os"\nfunc Twice(x int) int { os.Exit(0); return x }',
             'rust': 'fn twice(_x:i32)->i32{std::process::exit(0)}'}
    for language, (correct, test, setup) in fixtures.items():
        item = {'benchmark': 'humanevalpack', 'language': language,
                'test': test, 'test_setup': setup, 'test_count': 1}
        for code, expected in [(correct, 1), ('invalid source code', 0), (loops[language], 0), (exits[language], 0)]:
            if grader.grade(item, f'```{language}\n{code}\n```', image) != expected:
                raise GradingUnavailable(f'HumanEvalPack {language} correctness/compile/timeout/early-exit fixture failed')
