import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from freeboard.adapters import parse, payload
from freeboard.budget import Budget, BudgetExhausted, usage_total
from freeboard.config import COUNTS, Settings, now
from freeboard.db import DB
from freeboard.discovery import excluded, parse_evidence
from freeboard.grading import DockerGrader, GradingUnavailable, gpqa_score
from freeboard.panels import sample, prepare
from freeboard.publication import export, validate_site
from freeboard.runner import Runner
from freeboard.scoring import bootstrap, is_complete


@pytest.fixture
def context(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    settings = Settings(state=tmp_path / "private")
    settings.initialize(checkout)
    db = DB(settings.state)
    return db, settings, checkout


def model(db, name="test-free"):
    profile = {"protocol": "chat", "cap_parameter": "max_completion_tokens", "temperature": 0, "cap_verified": True}
    db.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?)", (name, name, "https://opencode.ai/zen/v1/chat/completions", "chat", "eligible", "epoch", 0, json.dumps(profile), now(), None))
    return db.one("SELECT * FROM models WHERE id=?", (name,))


def season(db):
    manifest = {"seed": 20261008, "sources": {}, "livebench_release": "2024-11-25", "livecodebench_release": "release_v6",
                "language": "Python", "counts": COUNTS, "panels": {}, "grader_image": "sha256:fixture"}
    db.execute("INSERT INTO seasons VALUES(?,?,?,?,?)", ("season", now(), json.dumps(manifest), 1, 1))
    return db.one("SELECT * FROM seasons WHERE id='season'")


def add_item(db, ident="q", tier="screen", benchmark="gpqa", content=None):
    value = content or {"benchmark": benchmark, "answer": "A", "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
    db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)", (ident, "season", benchmark, "stratum", "hash", json.dumps(value)))
    db.execute("INSERT OR IGNORE INTO panels VALUES(?,?,?)", ("season", tier, ident))


def job_context(context):
    db, settings, checkout = context
    item_model, active = model(db), season(db)
    runner = Runner(db, settings, checkout)
    cycle = runner.cycle(item_model, active, "screen", "cycle")
    add_item(db)
    runner.add_panel(cycle, "screen")
    job = db.one("SELECT * FROM jobs")
    item = json.loads(db.one("SELECT * FROM items")["content"])
    return runner, job, item_model, item


def response_body(tokens=10):
    return {"choices": [{"message": {"content": "FINAL: A"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": tokens, "total_tokens": 20 + tokens,
                      "completion_tokens_details": {"reasoning_tokens": 3}}}


def test_fail_closed_prices_and_exact_join():
    html = '<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr><tr><td>Alias</td><td>alias</td><td>https://opencode.ai/zen/v1/chat/completions</td></tr></table>'
    prices = '<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th></tr><tr><td>Alias</td><td>Free</td><td>Free</td><td>-</td></tr>{}</table>'
    assert parse_evidence(html)["alias"]["free"] is False
    assert parse_evidence(html + prices.format(""))["alias"]["free"] is True
    contradiction = '<tr><td>Alias</td><td>$0.10</td><td>Free</td><td>-</td></tr>'
    assert parse_evidence(html + prices.format(contradiction))["alias"]["free"] is False
    assert excluded("muse-spark-1.3-contributor-free")


def test_generation_endpoint_and_free_gates():
    value = {"id": "alias", "status": "eligible", "endpoint": "https://opencode.ai/zen/v1/chat/completions",
             "effective_profile": {"protocol": "chat", "cap_parameter": "max_completion_tokens", "temperature": 0}}
    assert payload(value, [], 256)["max_completion_tokens"] == 256
    with pytest.raises(ValueError):
        payload({**value, "status": "paid"}, [], 256)
    with pytest.raises(ValueError):
        payload({**value, "id": "muse-spark-1.3-contributor-free"}, [], 256)
    with pytest.raises(ValueError):
        payload({**value, "endpoint": "https://other.example/chat/completions"}, [], 256)


def test_usage_subsets_not_double_counted():
    assert usage_total({"total_tokens": 100, "input_tokens_details": {"cached_tokens": 90}, "output_tokens_details": {"reasoning_tokens": 40}}) == 100
    assert usage_total({"input_tokens": 60, "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 35}}) == 100
    assert usage_total(None) is None


def test_reservation_reconciliation_and_week_rollover(context, monkeypatch):
    db, settings, _ = context
    budget = Budget(db, settings)
    budget.plan([], 0)
    a = budget.reserve("generation", 500)
    assert budget.summary()["accounted_tokens"] == 500
    budget.finish(a, "received", 200, {"total_tokens": 100})
    assert budget.summary()["accounted_tokens"] == 100
    db.execute("UPDATE budgets SET attempts_limit=1")
    with pytest.raises(BudgetExhausted):
        budget.reserve("generation", 1)
    monkeypatch.setattr("freeboard.budget.week", lambda: "2099-01-05")
    assert budget.summary()["attempts_used"] == 0


def test_ambiguous_request_is_not_replayed(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    runner.client = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("unknown"))))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status FROM jobs")["status"] == "ambiguous"
    assert runner.budget.summary()["accounted_tokens"] > 4096
    runner.db.recover()
    assert len(runner.db.rows("SELECT * FROM attempts")) == 1


def test_saved_response_recovery_without_regeneration(context):
    runner, job, _, _ = job_context(context)
    attempt = runner.budget.reserve("generation", 500, job["id"])
    path = runner.settings.state / "responses" / f"{attempt}.json"
    path.write_text(json.dumps({"protocol": "chat", "body": json.dumps(response_body()), "cap": 4096, "latency": 1}))
    runner.db.recover()
    recovered = runner.db.one("SELECT * FROM jobs")
    assert recovered["status"] == "generated"
    assert len(runner.db.rows("SELECT * FROM attempts")) == 1
    runner.grade(recovered, {"benchmark": "gpqa", "answer": "A"}, "unused")
    assert runner.db.one("SELECT score FROM jobs")["score"] == 1


def test_recovery_after_attempt_commit_before_job_commit(context):
    runner, job, _, _ = job_context(context)
    attempt = runner.budget.reserve("generation", 500, job['id'])
    path = runner.settings.state / "responses" / f"{attempt}.json"
    path.write_text(json.dumps({"protocol": "chat", "body": json.dumps(response_body()), "cap": 4096, "latency": 1}))
    runner.budget.finish(attempt, 'received', 200, response_body()['usage'], str(path))
    assert runner.db.one('SELECT status FROM jobs')['status'] == 'dispatching'
    runner.db.recover()
    assert runner.db.one('SELECT status FROM jobs')['status'] == 'generated'
    assert runner.budget.summary()['reported_tokens'] == 30


def test_explicit_retries_are_bounded(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    monkeypatch.setattr("freeboard.runner.time.sleep", lambda _: None)
    runner.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    runner.generate(job, item_model, item, 4096)
    assert len(runner.db.rows("SELECT * FROM attempts")) == 3
    assert runner.db.one("SELECT status FROM jobs")["status"] == "failed"


def test_quota_is_not_a_capability_failure(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    runner.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429, headers={"Retry-After": "3600"})))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status,score FROM jobs") == {"status": "deferred", "score": None}
    assert runner.db.one("SELECT status FROM models")["status"] == "quota_limited"


def test_malformed_success_never_retried(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    runner.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="invalid")))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status FROM jobs")["status"] == "ambiguous"
    assert len(runner.db.rows("SELECT * FROM attempts")) == 1


def test_nested_sampling_is_deterministic_and_held_out():
    pool = [{"id": str(i), "stratum": "a" if i < 60 else "b"} for i in range(120)]
    large = sample(pool, 60, "confirm", equal=True)
    small = sample(large, 20, "screen", equal=True)
    pilot = sample([i for i in pool if i not in large], 2, "pilot", equal=True)
    assert large == sample(list(reversed(pool)), 60, "confirm", equal=True)
    assert {i["id"] for i in small} <= {i["id"] for i in large}
    assert not {i["id"] for i in pilot} & {i["id"] for i in large}


def test_complete_panels_and_paired_draws():
    rows = [{"benchmark": b, "stratum": "x", "item_id": f"{b}:{i}", "score": i % 2, "status": "graded"}
            for b, n in COUNTS["screen"].items() for i in range(n)]
    assert is_complete(rows, "screen")
    assert not is_complete(rows[:-1], "screen")
    a, b = bootstrap(rows), bootstrap(list(reversed(rows)))
    np.testing.assert_array_equal(a["reasoning"], b["reasoning"])
    np.testing.assert_array_equal(a["coding"] - b["coding"], np.zeros(10000))


def test_confirmation_reuses_same_cycle(context):
    db, settings, checkout = context
    item_model, active = model(db), season(db)
    runner = Runner(db, settings, checkout)
    cycle = runner.cycle(item_model, active, "screen", "cycle")
    for i in range(240):
        add_item(db, str(i), tier="confirmation")
        if i < 80:
            add_item(db, str(i), tier="screen")
    runner.add_panel(cycle, "screen")
    db.execute("UPDATE jobs SET status='graded',score=1")
    db.execute("UPDATE cycles SET completed_at=?", (now(),))
    runner.confirm(item_model["id"])
    assert len(db.rows("SELECT * FROM cycles")) == 1
    assert len(db.rows("SELECT * FROM jobs WHERE status='graded'")) == 80
    assert len(db.rows("SELECT * FROM jobs WHERE status='pending'")) == 160


def test_gpqa_final_answer_and_refusal():
    assert gpqa_score("A", "Reasoning\nFINAL: A") == 1
    assert gpqa_score("A", "I refuse") == 0
    assert gpqa_score("A", "FINAL: B") == 0
    assert gpqa_score("A", "The answer could be A or B") == 0


def test_responses_usage_and_truncation():
    data = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 256, "output_tokens_details": {"reasoning_tokens": 200}}}
    result = parse("responses", data)
    assert result.text == "answer" and result.truncated and result.reasoning_tokens == 200


def test_public_export_omits_private_material(context, tmp_path, monkeypatch):
    db, settings, checkout = context
    model(db)
    season(db)
    add_item(db)
    monkeypatch.setattr("freeboard.scoring.credential", lambda _: None, raising=False)
    # The public renderer receives strict schemas, not rows from the private items table.
    public_checkout = tmp_path / "public"
    public_checkout.mkdir()
    import shutil
    shutil.copytree(checkout / "web", public_checkout / "web")
    export(db, settings, public_checkout)
    validate_site(public_checkout / "site")
    for path in (public_checkout / "site").iterdir():
        assert "PRIVATE_SENTINEL" not in path.read_text()
        assert str(settings.state) not in path.read_text()
    content = json.loads((public_checkout / "site/snapshot.json").read_text())
    content["raw_answers"] = ["PRIVATE_SENTINEL"]
    (public_checkout / "site/snapshot.json").write_text(json.dumps(content))
    with pytest.raises(ValueError):
        validate_site(public_checkout / "site")


def test_container_isolation_and_timeout(context, monkeypatch):
    _, settings, _ = context
    grader = DockerGrader(settings)
    import subprocess
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 180)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("freeboard.grading.subprocess.run", call)
    with pytest.raises(GradingUnavailable):
        grader.grade({"benchmark": "synthetic_code"}, "code", "sha256:fixture")
    assert "--network=none" in calls[0] and "--read-only" in calls[0]
    assert "--cap-drop=ALL" in calls[0] and "--pids-limit=64" in calls[0]
    assert calls[1][1:3] == ["rm", "--force"]


def test_failed_export_preserves_previous_snapshot(context, tmp_path, monkeypatch):
    db, settings, checkout = context
    import shutil
    public = tmp_path / "public"
    public.mkdir()
    shutil.copytree(checkout / "web", public / "web")
    export(db, settings, public)
    previous = (public / "site/snapshot.json").read_text()
    monkeypatch.setattr("freeboard.publication.validate_site", lambda _: (_ for _ in ()).throw(ValueError("invalid staging")))
    with pytest.raises(ValueError):
        export(db, settings, public)
    assert (public / "site/snapshot.json").read_text() == previous


def test_private_state_rejects_public_checkout(tmp_path):
    with pytest.raises(ValueError):
        Settings(state=tmp_path / "private").initialize(tmp_path)


def test_prepared_manifest_and_panels(context, monkeypatch):
    db, settings, checkout = context
    monkeypatch.setattr('freeboard.panels.credential', lambda _: 'test-only')
    pools = {}
    for benchmark, count, strata in [("gpqa", 198, ["biology", "chemistry", "physics"]),
                                      ("livebench", 100, ["spatial", "zebra_puzzle"]),
                                      ("livecodebench", 600, ["easy", "medium", "hard"])]:
        pools[benchmark] = [{"id": f"{benchmark}:{i}", "benchmark": benchmark,
                             "stratum": strata[i % len(strata)],
                             "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
                            for i in range(count)]
    monkeypatch.setattr("freeboard.panels.upstream", lambda settings, name: settings.state / name)
    monkeypatch.setattr("freeboard.panels.gpqa", lambda _: pools["gpqa"])
    monkeypatch.setattr("freeboard.panels.livebench", lambda _: pools["livebench"])
    monkeypatch.setattr("freeboard.panels.livecodebench", lambda *_: pools["livecodebench"])
    result = prepare(db, settings, checkout)
    assert result["counts"] == {"screen": 80, "confirmation": 240, "pilot": 6}
    def panel(tier):
        return {x["item_id"] for x in db.rows("SELECT item_id FROM panels WHERE tier=?", (tier,))}
    assert panel("screen") <= panel("confirmation")
    assert not panel("pilot") & panel("confirmation")
    manifest = json.loads(db.one("SELECT manifest FROM seasons")["manifest"])
    assert set(manifest["grading_files"]) == {"Dockerfile", "worker.py", "requirements.txt"}


def test_reported_cap_overrun_blocks_future_generation(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    runner.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response_body(5000))))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status FROM models")["status"] == "cap_violation"
    assert runner.budget.summary()["reported_tokens"] == 5020
