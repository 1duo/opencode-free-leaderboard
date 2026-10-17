"""Public Exercism C tasks, frozen before inference and compiled only in Docker."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .coding import coding_base
from .config import SEED, digest, now
from .grading import GradingUnavailable, docker_environment
from .panels import upstream

REVISION = '7cf6fe4098bc2e550e3877c54173eda3314a8bfe'
PANEL = 'systems-c-v1'
FILES = ['systems.Dockerfile', 'systems_worker.py']
# A declared population of seven exercises per domain, chosen before any model
# responses. Seeded selection holds one out and scores six. Never swap failures.
POPULATION = {
    'memory': ['circular-buffer', 'linked-list', 'binary-search-tree', 'list-ops',
               'grade-school', 'react', 'series'],
    'encoding': ['variable-length-quantity', 'intergalactic-transmission', 'all-your-base',
                 'binary', 'eliuds-eggs', 'secret-handshake', 'luhn'],
    'parsing': ['run-length-encoding', 'word-count', 'wordy', 'phone-number',
                'matching-brackets', 'roman-numerals', 'nucleotide-count'],
    'numeric': ['binary-search', 'sieve', 'prime-factors', 'rational-numbers',
                'complex-numbers', 'knapsack', 'square-root'],
}
FRAMEWORK = ['unity.c', 'unity.h', 'unity_internals.h']
INSTRUCTIONS = ('Implement the supplied C99 interface in one complete C source file. '
    'Include the provided header by name; do not redefine its types or change its API. '
    'Return exactly one fenced c code block, without main or tests. '
    'Standard C and libm are available. No network, files, tools or repair turns. '
    'The full upstream tests run with AddressSanitizer, leak detection and UndefinedBehaviorSanitizer; '
    'warnings are errors. Free owned allocations through the supplied lifecycle functions.')


def select_systems(source: Path) -> dict[str, list[dict]]:
    panels = {'coding': [], 'coding_pilot': []}
    for stratum, population in sorted(POPULATION.items()):
        for position, slug in enumerate(sorted(population, key=lambda s: digest([SEED, PANEL, s]))):
            directory = source / 'exercises/practice' / slug
            stem = slug.replace('-', '_')
            header_path = directory / '.meta/example.h'
            header = (header_path if header_path.exists() else directory / f'{stem}.h').read_text()
            if re.search(r'\)\s*\{', header):
                raise ValueError('An implementation-bearing header cannot be supplied as an interface')
            instructions = (directory / '.docs/instructions.md').read_text()
            append = directory / '.docs/instructions.append.md'
            if append.exists():
                instructions += '\n\n' + append.read_text()
            test = (directory / f'test_{stem}.c').read_text()
            test = re.sub(r'(?m)^\s*TEST_IGNORE\(\);[^\n]*$', '', test)
            if 'TEST_IGNORE' in test:
                raise ValueError('Upstream tests could not all be enabled')
            count = len(re.findall(r'(?m)^\s*RUN_TEST\(', test))
            if not count:
                raise ValueError('No executable upstream tests')
            framework = {name: (directory / 'test-framework' / name).read_text() for name in FRAMEWORK}
            reference = (directory / '.meta/example.c').read_text()
            if slug == 'list-ops':
                # The upstream empty-list reference calls memcpy(NULL, ..., 0).
                # Correct only the validation oracle; scored programs still face
                # unmodified full tests and the same strict sanitizer checks.
                reference = ('#include <string.h>\nstatic void *reference_copy(void *d, const void *s, size_t n)'
                    '{return n ? memcpy(d, s, n) : d;}\n' + reference.replace('memcpy(', 'reference_copy('))
            item = {'benchmark': 'systems_c', 'stratum': stratum, 'slug': slug,
                'header_name': f'{stem}.h', 'header': header, 'test': test, 'test_count': count,
                'framework_hash': digest(framework), 'reference': reference,
                'messages': [{'role': 'user', 'content': INSTRUCTIONS + '\n\n' + instructions +
                    f'\n\nRequired interface ({stem}.h):\n```c\n{header}\n```'}]}
            item['id'] = f'systems_c:{REVISION[:12]}:{slug}:' + digest(item)[:12]
            panels['coding_pilot' if position == 0 else 'coding'].append(item)
    return panels


def prepare_systems(runner) -> dict:
    base = coding_base(runner.db)
    original = json.loads(base['manifest'])
    source = upstream(runner.settings, 'exercism_c', revision=REVISION, repo='exercism/c')
    panels = select_systems(source)
    public = {tier: [{'id': i['id'], 'hash': digest(i), 'benchmark': i['benchmark'], 'stratum': i['stratum']}
                     for i in items] for tier, items in panels.items()}
    manifest = {**original, 'partial': True, 'language': 'C99', 'panels': public,
        'counts': {'coding': {'systems_c': 24}, 'coding_pilot': {'systems_c': 4}},
        'sources': {**original['sources'], 'exercism_c': REVISION},
        'base_grader_image': original['grader_image'], 'grader_image': None,
        'grading_files': {**original['grading_files'],
                         **{name: digest((runner.checkout / 'grading' / name).read_text()) for name in FILES}},
        'protocol': {'output_cap': 4096, 'temperature': 0, 'tools': False, 'completions': 1,
            'panel': PANEL, 'strata': {'systems_c': {s: 6 for s in POPULATION}},
            'compiler': 'gcc-12.2.0', 'sanitizers': ['address', 'undefined'], 'toolchain_packages_hash': None,
            'purpose': 'CPU systems programming subset; equal-weight coding expansion'}}
    ident = 'coding-' + digest(manifest)[:16]
    saved = runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))
    if saved and saved['validated']:
        runner.grader.build(json.loads(saved['manifest']), runner.checkout)
        return saved
    runner.db.execute('INSERT OR IGNORE INTO seasons VALUES(?,?,?,?,?)', (ident, now(), json.dumps(manifest), 0, 0))
    for tier, items in panels.items():
        for item in items:
            prior = runner.db.one('SELECT question_hash FROM items WHERE id=?', (item['id'],))
            if prior and prior['question_hash'] != digest(item):
                raise ValueError('Frozen systems task changed')
            runner.db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)',
                (item['id'], ident, 'systems_c', item['stratum'], digest(item), json.dumps(item)))
            runner.db.execute('INSERT OR IGNORE INTO panels VALUES(?,?,?)', (ident, tier, item['id']))
    env = docker_environment(runner.settings)
    with tempfile.TemporaryDirectory(dir=runner.settings.state) as folder:
        context = Path(folder)
        for name in FILES:
            shutil.copy(runner.checkout / 'grading' / name, context / name)
        for items in panels.values():
            for item in items:
                path = context / 'frameworks' / item['framework_hash']
                if not path.exists():
                    shutil.copytree(source / 'exercises/practice' / item['slug'] / 'test-framework', path)
        tag = 'freeboard-systems:' + digest(manifest)[:16]
        result = subprocess.run(['docker', 'build', '--build-arg', f"BASE_IMAGE={original['grader_image']}",
            '-f', str(context / 'systems.Dockerfile'), '-t', tag, str(context)],
            capture_output=True, text=True, timeout=600, env=env)
        (runner.settings.state / 'logs/systems-grader-build.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise GradingUnavailable('Systems image build failed; inspect private logs/systems-grader-build.log')
    image = subprocess.check_output(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'],
        text=True, timeout=15, env=env).strip()
    packages = subprocess.check_output(['docker', 'run', '--rm', '--network=none', '--read-only',
        '--entrypoint', 'cat', image, '/opt/systems-packages.txt'], text=True, timeout=15, env=env)
    manifest['protocol']['toolchain_packages_hash'] = digest(packages)
    failed = []
    for items in panels.values():
        for item in items:
            if runner.grader.grade(item, '```c\n' + item['reference'] + '\n```', image) != 1:
                failed.append(item['slug'])
    if failed:
        raise GradingUnavailable(f"Systems references failed: {', '.join(failed)}; no replacement sampling")
    fixture = {'benchmark': 'systems_c', 'header_name': 'fixture.h', 'header': 'int twice(int x);\n',
        'framework_hash': panels['coding'][0]['framework_hash'], 'test_count': 1,
        'test': '#include "test-framework/unity.h"\n#include "fixture.h"\n'
            'void setUp(void){}\nvoid tearDown(void){}\n'
            'static void test_twice(void){TEST_ASSERT_EQUAL_INT(4, twice(2));}\n'
            'int main(void){UNITY_BEGIN();RUN_TEST(test_twice);return UNITY_END();}\n'}
    good = '#include "fixture.h"\nint twice(int x){return x*2;}'
    bad = ['#include "fixture.h"\nint twice(int x){return x;}',
           '#include "fixture.h"\nint twice(int x){while(x){} return x;}',
           '#include "fixture.h"\n#include <stdlib.h>\n'
               'int twice(int x){int *p=malloc(sizeof *p);p[0]=x;int y=p[x];free(p);return y;}',
           '#include "fixture.h"\nint twice(int x){return x << 31;}',
           '#include "fixture.h"\n#include <stdlib.h>\n'
               'int twice(int x){volatile int *p=malloc(sizeof *p);*p=x;return x*2;}',
           '#include "fixture.h"\n#include <stdlib.h>\nint twice(int x){(void)x;exit(0);}']
    if runner.grader.grade(fixture, f'```c\n{good}\n```', image) != 1 or any(
            runner.grader.grade(fixture, f'```c\n{code}\n```', image) != 0 for code in bad):
        raise GradingUnavailable('Systems correct/wrong/timeout/memory/undefined-behavior fixtures failed')
    manifest['grader_image'] = image
    runner.db.execute('UPDATE seasons SET manifest=?,validated=1 WHERE id=?', (json.dumps(manifest), ident))
    return runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))
