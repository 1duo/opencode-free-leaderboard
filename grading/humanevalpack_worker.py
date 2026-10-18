"""Container-only multilingual compiler and upstream-test supervisor."""
import json
import os
import re
import resource
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def execute(command, work, env, filename, timeout):
    with (work / filename).open('w+') as log:
        try:
            ran = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=log,
                                 timeout=timeout, preexec_fn=limits)
        except subprocess.TimeoutExpired:
            return False, ''
        log.seek(0)
        return ran.returncode == 0, log.read(1024 * 1024)


def grade(item, text):
    if item['benchmark'] != 'humanevalpack':
        sys.path.insert(0, '/opt')
        from worker import grade as legacy_grade
        return legacy_grade(item, text)
    language = item['language']
    aliases = {'cpp': r'cpp|c\+\+', 'go': 'go|golang', 'rust': 'rust|rs'}
    if language not in aliases:
        raise ValueError('Unknown frozen language')
    blocks = re.findall(r'```[^\n]*\n.*?```', text, re.S)
    match = re.fullmatch(r'```(?:' + aliases[language] + r')\s*\n(.*?)```',
                        blocks[0], re.S | re.I) if len(blocks) == 1 else None
    if not match or len(match[1].encode()) > 128 * 1024:
        return 0
    source = match[1]
    with tempfile.TemporaryDirectory(dir='/work') as folder:
        work = Path(folder)
        env = {**os.environ, 'GOMAXPROCS': '1', 'GO111MODULE': 'off',
               'GOPATH': '/usr/share/gocode', 'GOCACHE': str(work / 'cache'),
               'GOTMPDIR': str(work), 'TMPDIR': str(work)}
        if language == 'cpp':
            # Preserve the assertions, then require proof that the test function
            # returned. exit(0), missing tests and a forged exit code cannot pass.
            test, count = re.subn(r'\bint\s+main\s*\(\s*\)', 'int hep_tests()', item['test'])
            if count != 1 or '}' not in test:
                raise ValueError('Unsupported upstream C++ test entry point')
            prefix, suffix = test.rsplit('}', 1)
            token = 'HEP_COMPLETE_' + secrets.token_hex(16)
            test = prefix + '\nreturn 0;\n}' + suffix
            test += '\nint main(){int result=hep_tests(); if(result==0) std::puts("' + token + '"); return result;}\n'
            (work / 'answer.cpp').write_text('#include <cstdio>\n' + source + '\n' + test)
            command = ['g++-12', '-std=c++17', '-O1', 'answer.cpp', '-o', 'check']
        elif language == 'rust':
            (work / 'answer.rs').write_text(source + '\n' + item['test_setup'] + '\n' + item['test'])
            dependencies = Path('/opt/hep-rust/target/release/deps')
            command = ['rustc', '--edition=2021', '--test', '-C', 'opt-level=1',
                       '-C', 'strip=symbols', '-A', 'warnings', 'answer.rs', '-o', 'check',
                       '-L', 'dependency=' + str(dependencies)]
            for crate in ['rand', 'regex', 'md5']:
                libraries = list(dependencies.glob(f'lib{crate}-*.rlib'))
                if len(libraries) != 1:
                    raise RuntimeError('Frozen Rust dependency missing or ambiguous')
                command += ['--extern', crate + '=' + str(libraries[0])]
        else:
            (work / 'answer.go').write_text(source)
            (work / 'tests.go').write_text(item['test_setup'] + '\n' + item['test'])
            assembled, _ = execute(['/opt/hep-go-assemble', 'answer.go', 'tests.go', 'combined_test.go'],
                                    work, env, 'assemble.log', 5)
            if not assembled:
                return 0
            (work / 'answer.go').unlink()
            (work / 'tests.go').unlink()
            command = ['/usr/lib/go-1.19/bin/go', 'test', '-p=1', '-c', '-ldflags=-s -w', '-o', 'check', '.']
        built, _ = execute(command, work, env, 'compile.log', 60)
        if not built:
            return 0
        for path in work.glob('answer.*'):
            path.unlink()
        test_source = work / 'combined_test.go'
        if test_source.exists():
            test_source.unlink()
        arguments = (['-test.v', '-test.timeout=5s', '-test.paniconexit0'] if language == 'go' else
                     ['--test-threads=1'] if language == 'rust' else [])
        passed, output = execute([str(work / 'check'), *arguments], work, env, 'run.log', 5)
        if not passed:
            return 0
        if language == 'cpp':
            lines = output.splitlines()
            return int(lines[-1:] == [token] and lines.count(token) == 1)
        if language == 'rust':
            summary = re.findall(r'test result: ok\. (\d+) passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;', output)
            return int(summary == [str(item['test_count'])])
        expected = re.findall(r'(?m)^func (Test\w+)\(', item['test'])
        observed = re.findall(r'(?m)^--- PASS: (\w+) \(', output)
        return int(len(expected) == item['test_count'] and sorted(expected) == sorted(observed)
                   and output.rstrip().endswith('PASS'))


if __name__ == '__main__':
    request = json.load(sys.stdin)
    print(json.dumps({'score': float(grade(request['item'], request['text']))}))
