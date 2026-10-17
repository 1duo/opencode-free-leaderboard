"""Container-only C compiler/test supervisor. Task programs never run on the host."""
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
from pathlib import Path


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def grade(item, text):
    if item['benchmark'] != 'systems_c':
        sys.path.insert(0, '/opt')
        from worker import grade as legacy_grade
        return legacy_grade(item, text)
    blocks = re.findall(r'```(?:c|C)\s*\n(.*?)```', text, re.S)
    if len(blocks) != 1 or len(blocks[0].encode()) > 128 * 1024:
        return 0
    if not re.fullmatch(r'[a-z_]+\.h', item['header_name']) or not re.fullmatch(r'[0-9a-f]{64}', item['framework_hash']):
        raise ValueError('Invalid frozen interface or framework')
    framework = Path('/opt/systems-frameworks') / item['framework_hash']
    if not (framework / 'unity.c').is_file():
        raise RuntimeError('Frozen test framework missing')
    with tempfile.TemporaryDirectory(dir='/work') as folder:
        work = Path(folder)
        (work / item['header_name']).write_text(item['header'])
        (work / 'answer.c').write_text(blocks[0])
        (work / 'tests.c').write_text(item['test'])
        (work / 'test-framework').symlink_to(framework, target_is_directory=True)
        env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=1:abort_on_error=1',
               'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=0'}
        command = ['gcc', '-std=c99', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-pedantic',
            '-Wmissing-declarations', '-DUNITY_SUPPORT_64', '-DUNITY_INCLUDE_DOUBLE', '-fsanitize=address,undefined',
            '-fno-sanitize-recover=all', '-fno-common', '-fno-omit-frame-pointer',
            '-I.', str(framework / 'unity.c'), 'tests.c', 'answer.c', '-lm', '-o', 'check']
        with (work / 'compile.log').open('w+') as log:
            try:
                built = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=log,
                                       timeout=30, preexec_fn=limits)
            except subprocess.TimeoutExpired:
                return 0
            if built.returncode:
                return 0
        # Only the executable and public interface need survive execution.
        (work / 'tests.c').unlink()
        (work / 'answer.c').unlink()
        with (work / 'run.log').open('w+') as log:
            try:
                ran = subprocess.run([str(work / 'check')], cwd=work, env=env, stdout=log,
                    stderr=log, timeout=5, preexec_fn=limits)
            except subprocess.TimeoutExpired:
                return 0
            log.seek(0)
            output = log.read(1024 * 1024)
        summaries = re.findall(r'(?m)^(\d+) Tests (\d+) Failures (\d+) Ignored\s*$', output)
        return int(ran.returncode == 0 and summaries == [(str(item['test_count']), '0', '0')])


if __name__ == '__main__':
    request = json.load(sys.stdin)
    print(json.dumps({'score': float(grade(request['item'], request['text']))}))
