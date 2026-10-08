"""Runs only inside a disposable Linux container. Never import on the host."""
import contextlib
import io
import json
import subprocess
import sys


def grade(item, text):
    if item["benchmark"] == "livebench":
        if item["stratum"] == "spatial":
            from livebench.process_results.reasoning.spatial.utils import spatial_process_results
            return spatial_process_results(item["answer"], text)
        from livebench.process_results.reasoning.zebra_puzzle.utils import get_zebra_puzzle_evaluator
        return get_zebra_puzzle_evaluator(item["release"])(item["answer"], text)
    from lcb_runner.lm_styles import LMStyle
    from lcb_runner.utils.extraction_utils import extract_code
    code = extract_code(text, LMStyle.OpenAIChat)
    if not code:
        return 0
    if item["benchmark"] == "synthetic_code":
        for stdin, expected in item["tests"]:
            try:
                result = subprocess.run([sys.executable, "-c", code], input=stdin,
                                        capture_output=True, text=True, timeout=6)
            except subprocess.TimeoutExpired:
                return 0
            if result.returncode or result.stdout.strip() != expected.strip():
                return 0
        return 1
    from lcb_runner.benchmarks.code_generation import CodeGenerationProblem
    from lcb_runner.evaluation.compute_code_generation_metrics import evaluate_generations_by_problem
    raw = dict(item["raw"])
    fields = CodeGenerationProblem.__dataclass_fields__
    problem = CodeGenerationProblem(**{k: v for k, v in raw.items() if k in fields})
    results, metadata = evaluate_generations_by_problem(([code], problem.get_evaluation_sample(), False, 6))
    if metadata[0].get("error_code") == -5:
        raise RuntimeError("Upstream harness infrastructure error")
    return int(bool(results[0]) and all(value is True or value == 1 for value in results[0]))


if __name__ == "__main__":
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(io.StringIO()):
        score = grade(request["item"], request["text"])
    print(json.dumps({"score": float(score)}))

