"""Run every task on both models and decide.

A candidate is judged against the model in use today, not against a score: it regresses
when it fails a task the current model passes. A task both fail is information, not a
blocker. A task counts as passed only if every repeat passes: a model that is right four
times in five is not one to roll out on.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from evalgate.golden import Task, failed_checks
from evalgate.models import Model

Prices = dict[str, dict[str, float]]  # model → {"input": $/1M tokens, "output": $/1M}


@dataclass
class Side:
    """One model on one task."""

    failed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failed and not self.error

    @property
    def cell(self) -> str:
        return "error" if self.error else ("fail" if self.failed else "pass")


@dataclass
class TaskResult:
    task: str
    owner: str
    about_tools: bool
    current: Side
    candidate: Side

    @property
    def regression(self) -> bool:
        return self.current.passed and not self.candidate.passed

    @property
    def improvement(self) -> bool:
        return self.candidate.passed and not self.current.passed and not self.current.error

    @property
    def inconclusive(self) -> bool:
        return bool(self.current.error or self.candidate.error)


@dataclass
class Totals:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    cost_usd: float | None = 0.0  # None: no price for this model

    @property
    def cost_per_task_usd(self) -> float | None:
        return None if self.cost_usd is None or not self.calls else self.cost_usd / self.calls

    @property
    def median_latency_ms(self) -> int:
        return round(statistics.median(self.latencies_ms)) if self.latencies_ms else 0


@dataclass
class Report:
    current: str
    candidate: str
    runs: int
    results: list[TaskResult]
    totals: dict[str, Totals]
    breaches: list[str] = field(default_factory=list)  # cost or latency past its limit

    @property
    def regressions(self) -> list[TaskResult]:
        return [r for r in self.results if r.regression]

    def passed(self, side: str) -> int:
        return sum(getattr(r, side).passed for r in self.results)

    def tool_passed(self, side: str) -> tuple[int, int]:
        tool_tasks = [r for r in self.results if r.about_tools]
        return sum(getattr(r, side).passed for r in tool_tasks), len(tool_tasks)

    @property
    def verdict(self) -> str:
        if self.regressions:
            return "regression"
        if self.breaches:
            return "over budget"
        return "inconclusive" if any(r.inconclusive for r in self.results) else "pass"

    @property
    def exit_code(self) -> int:
        return {"pass": 0, "regression": 1, "over budget": 1, "inconclusive": 2}[self.verdict]


def cost(prices: Prices, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    price = prices.get(model)
    if price is None:
        return None
    return (prompt_tokens * price["input"] + completion_tokens * price["output"]) / 1_000_000


def evaluate(
    tasks: list[Task],
    current: str,
    candidate: str,
    model: Model,
    runs: int = 1,
    prices: Prices | None = None,
    max_cost_ratio: float | None = None,
    max_latency_ratio: float | None = None,
) -> Report:
    prices = prices or {}
    totals = {name: Totals() for name in {current, candidate}}

    def run(name: str, task: Task) -> Side:
        failed: set[str] = set()
        for _ in range(runs):
            out = model(name, task)
            if out.error:
                return Side(error=out.error)
            total = totals[name]
            total.calls += 1
            total.prompt_tokens += out.prompt_tokens
            total.completion_tokens += out.completion_tokens
            total.latencies_ms.append(out.latency_ms)
            spent = cost(prices, name, out.prompt_tokens, out.completion_tokens)
            total.cost_usd = (
                None if spent is None or total.cost_usd is None else total.cost_usd + spent
            )
            failed |= set(failed_checks(task, out))
        return Side(failed=sorted(failed))

    results = []
    for task in tasks:
        now = run(current, task)
        then = now if candidate == current else run(candidate, task)
        results.append(TaskResult(task.id, task.owner, task.about_tools, now, then))

    report = Report(current, candidate, runs, results, totals)
    cur, cand = totals[current], totals[candidate]
    if max_cost_ratio is not None and cur.cost_per_task_usd and cand.cost_per_task_usd is not None:
        ratio = cand.cost_per_task_usd / cur.cost_per_task_usd
        if ratio > max_cost_ratio:
            report.breaches.append(
                f"cost per task is {ratio:.2f}x the current model's (limit {max_cost_ratio:g}x)"
            )
    if max_latency_ratio is not None and cur.median_latency_ms and cand.median_latency_ms:
        ratio = cand.median_latency_ms / cur.median_latency_ms
        if ratio > max_latency_ratio:
            report.breaches.append(
                f"median latency is {ratio:.2f}x the current model's (limit {max_latency_ratio:g}x)"
            )
    return report
