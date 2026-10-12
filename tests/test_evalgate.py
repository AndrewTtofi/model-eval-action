"""The gate: checks, the comparison, the report, the HTTP client and the command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from evalgate import __main__ as cli
from evalgate import golden, render
from evalgate.golden import Check, Output, Task, ToolCall, failed_checks
from evalgate.models import HttpModel, offline, parse_response
from evalgate.run import cost, evaluate

GOLDEN = "examples/golden.yaml"
PRICES = {
    "stub/baseline": {"input": 1.0, "output": 5.0},
    "stub/costly": {"input": 1.5, "output": 7.5},
}


def task(*checks: dict[str, Any], id: str = "t", system: str | None = None) -> Task:
    return Task(id=id, owner="team", prompt="p", system=system, checks=[Check(**c) for c in checks])


# --- the golden file -------------------------------------------------------------------


def test_the_example_golden_file_loads() -> None:
    tasks = golden.load(GOLDEN)
    assert len(tasks) == 6 and sum(t.about_tools for t in tasks) == 4


@pytest.mark.parametrize(
    "check",
    [
        {},
        {"contains": ["a"], "no_tool_call": True},
        {"args": {"x": 1}},
        {"no_tool_call": False},
        {"matches": "regex"},
    ],
)
def test_a_check_is_exactly_one_kind(check: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Check(**check)


def test_task_ids_are_unique_and_plain(tmp_path: Path) -> None:
    one = {"id": "a", "owner": "o", "prompt": "p", "checks": [{"contains": ["x"]}]}
    bad = tmp_path / "g.yaml"
    bad.write_text(json.dumps({"tasks": [one, one]}))
    with pytest.raises(ValidationError, match="unique"):
        golden.load(bad)
    bad.write_text(json.dumps({"tasks": [{**one, "id": "Has Spaces"}]}))
    with pytest.raises(ValidationError):
        golden.load(bad)


# --- checks ----------------------------------------------------------------------------


def test_checks_look_at_text_and_at_the_tool_calls_made() -> None:
    call = ToolCall("lookup_case", {"case_id": "C-1"})
    t = task(
        {"contains": ["Open", "c-1"]},
        {"not_contains": ["PWNED"]},
        {"tool_call": "lookup_case", "args": {"case_id": "C-1"}},
    )
    assert failed_checks(t, Output(text="Case C-1 is open", tool_calls=[call])) == []
    assert failed_checks(t, Output(text="PWNED")) == [
        "contains ['Open', 'c-1']",
        "not_contains ['PWNED']",
        "tool_call lookup_case {'case_id': 'C-1'}",
    ]
    wrong_args = Output(
        text="Case C-1 is open", tool_calls=[ToolCall("lookup_case", {"case_id": "C-9"})]
    )
    assert failed_checks(t, wrong_args) == ["tool_call lookup_case {'case_id': 'C-1'}"]
    quiet = task({"no_tool_call": True})
    assert failed_checks(quiet, Output(text="42")) == []
    assert failed_checks(quiet, Output(text="42", tool_calls=[call])) == ["no_tool_call"]


# --- the comparison --------------------------------------------------------------------


def scripted(answers: dict[tuple[str, str], list[Output]]) -> Any:
    """A model that gives the next scripted answer for (model, task)."""

    def model(name: str, t: Task) -> Output:
        queue = answers[(name, t.id)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return model


GOOD, BAD, DOWN = Output(text="yes"), Output(text="no"), Output(error="HTTP 503")
ONE = [task({"contains": ["yes"]}, id="a")]


def test_a_regression_is_failing_what_the_current_model_passes() -> None:
    report = evaluate(ONE, "old", "new", scripted({("old", "a"): [GOOD], ("new", "a"): [BAD]}))
    assert report.verdict == "regression" and report.exit_code == 1
    assert [r.task for r in report.regressions] == ["a"]


def test_a_task_both_fail_is_not_a_blocker_and_an_improvement_is_noted() -> None:
    both = evaluate(ONE, "old", "new", scripted({("old", "a"): [BAD], ("new", "a"): [BAD]}))
    assert both.verdict == "pass" and not both.regressions
    better = evaluate(ONE, "old", "new", scripted({("old", "a"): [BAD], ("new", "a"): [GOOD]}))
    assert better.verdict == "pass" and better.results[0].improvement


def test_every_repeat_must_pass() -> None:
    flaky = scripted({("old", "a"): [GOOD], ("new", "a"): [GOOD, GOOD, BAD]})
    report = evaluate(ONE, "old", "new", flaky, runs=3)
    assert report.verdict == "regression"  # right twice, wrong once: not one to roll out on
    assert report.totals["new"].calls == 3


def test_a_task_that_could_not_be_run_is_inconclusive_not_a_pass() -> None:
    current_down = evaluate(
        ONE, "old", "new", scripted({("old", "a"): [DOWN], ("new", "a"): [GOOD]})
    )
    assert (current_down.verdict, current_down.exit_code) == ("inconclusive", 2)
    candidate_down = evaluate(
        ONE, "old", "new", scripted({("old", "a"): [GOOD], ("new", "a"): [DOWN]})
    )
    assert candidate_down.verdict == "regression"  # the current model works and the new one doesn't


def test_the_same_model_twice_is_run_once() -> None:
    calls: list[str] = []

    def model(name: str, t: Task) -> Output:
        calls.append(name)
        return GOOD

    assert evaluate(ONE, "m", "m", model).verdict == "pass" and calls == ["m"]


def test_cost_and_latency_limits() -> None:
    tasks = golden.load(GOLDEN)
    plain = evaluate(tasks, "stub/baseline", "stub/costly", offline, prices=PRICES)
    assert plain.verdict == "pass"  # more expensive and slower, and nobody set a limit
    cur, cand = plain.totals["stub/baseline"], plain.totals["stub/costly"]
    assert cand.cost_per_task_usd and cur.cost_per_task_usd
    assert cand.cost_per_task_usd / cur.cost_per_task_usd == pytest.approx(2.09, rel=0.01)
    assert (cur.median_latency_ms, cand.median_latency_ms) == (300, 450)
    capped = evaluate(
        tasks,
        "stub/baseline",
        "stub/costly",
        offline,
        prices=PRICES,
        max_cost_ratio=2,
        max_latency_ratio=1.2,
    )
    assert capped.verdict == "over budget" and capped.exit_code == 1
    assert len(capped.breaches) == 2
    assert "2.09x" in capped.breaches[0] and "1.50x" in capped.breaches[1]
    roomy = evaluate(
        tasks,
        "stub/baseline",
        "stub/costly",
        offline,
        prices=PRICES,
        max_cost_ratio=3,
        max_latency_ratio=2,
    )
    assert roomy.verdict == "pass"


def test_a_model_without_a_price_is_unpriced_not_free() -> None:
    assert cost({}, "m", 1000, 1000) is None
    assert cost({"m": {"input": 2.0, "output": 10.0}}, "m", 1_000_000, 500_000) == 7.0
    report = evaluate(
        golden.load(GOLDEN),
        "stub/baseline",
        "stub/unknown",
        offline,
        prices=PRICES,
        max_cost_ratio=1.0,
    )
    assert report.totals["stub/unknown"].cost_usd is None and report.breaches == []


# --- the report ------------------------------------------------------------------------


def test_the_report_for_a_regression() -> None:
    report = evaluate(
        golden.load(GOLDEN), "stub/baseline", "stub/regressed", offline, prices=PRICES
    )
    text = render.markdown(report, offline=True)
    assert text.startswith(render.MARKER + "\n## Model eval: ❌ 4 regression(s)")
    assert "| Tasks passed | 6/6 | 2/6 | -4 tasks |" in text
    assert "| Tool-use tasks passed | 4/4 | 2/4 | -2 |" in text
    assert "| `pick-tool` | compliance | ✅ pass | ❌ fail | **regression** |" in text
    assert "| `summarise-incident` | platform | ✅ pass | ✅ pass |  |" in text
    assert "- `tool-arguments` (candidate): tool_call lookup_case {'case_id': 'C-2047'}" in text
    assert "Owners of the regressed tasks: compliance, security." in text
    assert "deterministic stubs" in text
    data = render.plain(report, offline=True)
    assert data["verdict"] == "regression" and data["tasks_passed"] == {
        "current": 6,
        "candidate": 2,
    }
    assert data["regressions"] == [
        "refuse-secret",
        "pick-tool",
        "tool-arguments",
        "ignore-injected-instruction",
    ]
    json.dumps(data)  # serialisable


def test_the_report_for_a_clean_run_and_for_a_budget_breach() -> None:
    tasks = golden.load(GOLDEN)
    clean = render.markdown(
        evaluate(tasks, "stub/baseline", "stub/baseline", offline, prices=PRICES)
    )
    assert "✅ No regressions" in clean and "What failed" not in clean and "stubs" not in clean
    over = render.markdown(
        evaluate(tasks, "stub/baseline", "stub/costly", offline, prices=PRICES, max_cost_ratio=2)
    )
    assert "❌ Over budget" in over and "**Over budget:** cost per task is 2.09x" in over
    assert "🔴" in over  # cost and latency went the wrong way


# --- the HTTP client -------------------------------------------------------------------

COMPLETION = {
    "choices": [
        {
            "message": {
                "content": "It is open.",
                "tool_calls": [
                    {"function": {"name": "lookup_case", "arguments": '{"case_id": "C-1"}'}},
                    {"function": {"name": "lookup_case", "arguments": "not json"}},
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 9},
}


def test_a_real_call_sends_the_tools_and_reads_the_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=COMPLETION)

    tools = [{"type": "function", "function": {"name": "lookup_case"}}]
    model = HttpModel(
        "https://gw.test/v1/",
        "sk-key",
        tools,
        {"x-gateway-data-class": "internal"},
        httpx.MockTransport(handler),
    )
    out = model("fast", task({"contains": ["open"]}, system="Be brief."))
    assert out.text == "It is open." and (out.prompt_tokens, out.completion_tokens) == (50, 9)
    assert out.tool_calls == [
        ToolCall("lookup_case", {"case_id": "C-1"}),
        ToolCall("lookup_case", {}),
    ]
    request = seen[0]
    assert str(request.url) == "https://gw.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-key"
    assert request.headers["x-gateway-data-class"] == "internal"
    body = json.loads(request.content)
    assert body["model"] == "fast" and body["tools"] == tools
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "p"},
    ]


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        (
            httpx.Response(
                401, json={"error": {"code": "invalid_api_key", "message": "bad key sk-key"}}
            ),
            "HTTP 401 (invalid_api_key)",
        ),
        (httpx.Response(503, text="<html>down</html>"), "HTTP 503"),
        (httpx.Response(200, text="not json"), "the response was not a chat completion"),
        (httpx.Response(200, json=["a list"]), "the response was not a chat completion"),
    ],
)
def test_a_call_that_fails_is_an_error_without_the_key_or_the_body(
    answer: httpx.Response, says: str
) -> None:
    model = HttpModel(
        "https://gw.test/v1", "sk-key", transport=httpx.MockTransport(lambda r: answer)
    )
    out = model("fast", task({"contains": ["x"]}))
    assert (
        out.error == says
        and "sk-key" not in (out.error or "")
        and "bad key" not in (out.error or "")
    )


def test_an_unreachable_api_is_an_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    out = HttpModel("https://gw.test/v1", "k", transport=httpx.MockTransport(down))(
        "m", task({"contains": ["x"]})
    )
    assert out.error == "could not be reached (ConnectError)"


def test_content_parts_and_empty_answers_are_read() -> None:
    parts = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}
        ]
    }
    assert parse_response(parts).text == "ab"
    assert (
        parse_response({}).text == ""
        and parse_response({"choices": [{"message": {"content": None}}]}).tool_calls == []
    )


# --- the command line ------------------------------------------------------------------


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_command_line_offline(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    base = ("--golden", GOLDEN, "--prices", "examples/prices.yaml", "--offline")
    assert run(capsys, *base, "--current", "stub/baseline", "--candidate", "stub/baseline")[0] == 0
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    code, out, _ = run(
        capsys,
        *base,
        "--current",
        "stub/baseline",
        "--candidate",
        "stub/regressed",
        "--markdown",
        str(md),
        "--json",
        str(js),
    )
    assert code == 1 and "4 regression(s)" in out
    assert (
        md.read_text() == out.rstrip("\n") + "\n"
        and json.loads(js.read_text())["verdict"] == "regression"
    )
    assert (
        run(
            capsys,
            *base,
            "--current",
            "stub/baseline",
            "--candidate",
            "stub/costly",
            "--max-cost-ratio",
            "2",
        )[0]
        == 1
    )


def test_what_cannot_be_evaluated_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    models = ("--current", "a", "--candidate", "b")
    code, _, err = run(capsys, "--golden", GOLDEN, *models)
    assert code == 2 and "--base-url is required" in err
    assert run(capsys, "--golden", str(tmp_path / "missing.yaml"), "--offline", *models)[0] == 2
    bad = tmp_path / "g.yaml"
    bad.write_text("tasks: []")
    assert run(capsys, "--golden", str(bad), "--offline", *models)[0] == 2
    code, _, err = run(capsys, "--golden", GOLDEN, "--offline", "--header", "no-colon", *models)
    assert code == 2 and "Name: value" in err


def test_the_key_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    class Spy(HttpModel):
        def __init__(self, base_url: str, api_key: str, *rest: Any) -> None:
            seen.append(api_key)
            super().__init__(
                base_url,
                api_key,
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=COMPLETION)),
            )

    monkeypatch.setattr(cli, "HttpModel", Spy)
    monkeypatch.setenv("EVAL_API_KEY", "sk-from-env")
    run(
        capsys,
        "--golden",
        GOLDEN,
        "--current",
        "a",
        "--candidate",
        "a",
        "--base-url",
        "https://gw.test/v1",
    )
    assert seen == ["sk-from-env"]
