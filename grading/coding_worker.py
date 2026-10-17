"""BigCodeBench CPU subset. Executed only inside the frozen disposable container."""
import contextlib
import io
import json
import sys


def grade(item, text):
    if item['benchmark'] != 'bigcodebench':
        from worker import grade as legacy_grade
        return legacy_grade(item, text)
    import bigcodebench.eval as evaluator
    from bigcodebench.sanitize import sanitize
    # The upstream evaluator's configurable task deadline is frozen at ten
    # seconds for this CPU subset, independently of the outer container guard.
    evaluator.TIMEOUT_LIMIT = 10.0
    try:
        code = text if item.get('reference_check') else sanitize(text, item['entry_point'])
    except (SyntaxError, ValueError, IndexError):
        return 0
    if not item.get('reference_check') and item.get('code_prompt'):
        code = item['code_prompt'] + '\n    pass\n' + code
    status, _ = evaluator.untrusted_check(code, item['test'], item['entry_point'],
        max_as_limit=30*1024, max_data_limit=30*1024, max_stack_limit=10,
        min_time_limit=10, gt_time_limit=10)
    return int(status == evaluator.PASS)


if __name__ == '__main__':
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        score = grade(request['item'], request['text'])
    print(json.dumps({'score': float(score)}))
