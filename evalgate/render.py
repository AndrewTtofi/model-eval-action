"""The report as Markdown for a pull request, and as plain data."""

from __future__ import annotations

from typing import Any

from evalgate.run import Report, Totals

MARKER = "<!-- model-eval-action -->"  # how the action finds its own comment to update
ICON = {"pass": "✅", "fail": "❌", "error": "⚠️"}


def _delta(current: float, candidate: float, lower_is_better: bool = True) -> str:
    if not current:
        return "-"
    change = (candidate - current) / current
    if abs(change) < 0.005:
        return "same"
    better = (change < 0) == lower_is_better
    return f"{change:+.0%} {'🟢' if better else '🔴'}"


def _usd(amount: float | None) -> str:
    if amount is None:
        return "unpriced"
    return f"${amount:.4f}" if amount < 0.1 else f"${amount:.2f}"


def markdown(report: Report, offline: bool = False) -> str:
    cur, cand = report.totals[report.current], report.totals[report.candidate]
    n = len(report.results)
    passed_cur, passed_cand = report.passed("current"), report.passed("candidate")
    tools_cur, tools_n = report.tool_passed("current")
    tools_cand, _ = report.tool_passed("candidate")
    headline = {
        "pass": "✅ No regressions",
        "regression": f"❌ {len(report.regressions)} regression(s)",
        "over budget": "❌ Over budget",
        "inconclusive": "⚠️ Inconclusive: some tasks could not be run",
    }[report.verdict]
    lines = [
        MARKER,
        f"## Model eval: {headline}",
        "",
        f"`{report.current}` (current) → `{report.candidate}` (candidate), "
        f"{n} tasks, {report.runs} run{'s' if report.runs != 1 else ''} each. "
        "A task passes only if every run passes.",
        "",
    ]
    if offline:
        lines += [
            "> **Offline:** deterministic stubs. This checks the harness and the golden "
            "file, not a model.",
            "",
        ]
    quality = "same" if passed_cur == passed_cand else f"{passed_cand - passed_cur:+d} tasks"
    tool_delta = "same" if tools_cur == tools_cand else f"{tools_cand - tools_cur:+d}"
    cost_delta = (
        _delta(cur.cost_per_task_usd, cand.cost_per_task_usd)
        if cur.cost_per_task_usd is not None and cand.cost_per_task_usd is not None
        else "-"
    )
    lines += [
        "| | Current | Candidate | Change |",
        "|---|---|---|---|",
        f"| Tasks passed | {passed_cur}/{n} | {passed_cand}/{n} | {quality} |",
        f"| Tool-use tasks passed | {tools_cur}/{tools_n} | {tools_cand}/{tools_n} | "
        f"{tool_delta} |",
        f"| Cost per task | {_usd(cur.cost_per_task_usd)} | {_usd(cand.cost_per_task_usd)} | "
        f"{cost_delta} |",
        f"| Median latency | {cur.median_latency_ms} ms | {cand.median_latency_ms} ms | "
        f"{_delta(cur.median_latency_ms, cand.median_latency_ms)} |",
        "",
    ]
    for breach in report.breaches:
        lines += [f"**Over budget:** {breach}.", ""]
    lines += ["| Task | Owner | Current | Candidate | |", "|---|---|---|---|---|"]
    for r in report.results:
        note = "**regression**" if r.regression else ("improvement" if r.improvement else "")
        lines.append(
            f"| `{r.task}` | {r.owner} | {ICON[r.current.cell]} {r.current.cell} | "
            f"{ICON[r.candidate.cell]} {r.candidate.cell} | {note} |"
        )
    details = []
    for r in report.results:
        for who, side in (("current", r.current), ("candidate", r.candidate)):
            why = side.error or "; ".join(side.failed)
            if why:
                details.append(f"- `{r.task}` ({who}): {why}")
    if details:
        lines += ["", "<details><summary>What failed</summary>", "", *details, "", "</details>"]
    if report.regressions:
        owners = sorted({r.owner for r in report.regressions})
        lines += ["", f"Owners of the regressed tasks: {', '.join(owners)}."]
    return "\n".join(lines) + "\n"


def _totals(t: Totals) -> dict[str, Any]:
    return {
        "calls": t.calls,
        "prompt_tokens": t.prompt_tokens,
        "completion_tokens": t.completion_tokens,
        "cost_usd": t.cost_usd,
        "cost_per_task_usd": t.cost_per_task_usd,
        "median_latency_ms": t.median_latency_ms,
    }


def plain(report: Report, offline: bool = False) -> dict[str, Any]:
    return {
        "verdict": report.verdict,
        "current": report.current,
        "candidate": report.candidate,
        "runs": report.runs,
        "offline": offline,
        "regressions": [r.task for r in report.regressions],
        "breaches": report.breaches,
        "tasks_passed": {
            "current": report.passed("current"),
            "candidate": report.passed("candidate"),
        },
        "totals": {name: _totals(t) for name, t in report.totals.items()},
        "results": [
            {
                "task": r.task,
                "owner": r.owner,
                "current": r.current.cell,
                "candidate": r.candidate.cell,
                "current_failed": r.current.failed,
                "candidate_failed": r.candidate.failed,
                "current_error": r.current.error,
                "candidate_error": r.candidate.error,
            }
            for r in report.results
        ],
    }
