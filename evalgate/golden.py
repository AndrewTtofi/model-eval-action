"""Golden tasks: a prompt, and checks on what a model answers.

    contains: [..]       every string appears in the text (case-insensitive)
    not_contains: [..]   none of the strings appears in the text
    tool_call: name      the model called this tool; with `args`, with those values
    no_tool_call: true   the model answered without calling a tool

Checks look at facts in the answer and at the tool calls actually made, never at wording
or at a model's own opinion of an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Check(BaseModel):
    """One check: exactly one of the four kinds."""

    model_config = ConfigDict(extra="forbid")

    contains: list[str] | None = None
    not_contains: list[str] | None = None
    tool_call: str | None = None
    args: dict[str, Any] | None = None  # with tool_call: argument values it must have
    no_tool_call: bool | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Check:
        kinds = (self.contains, self.not_contains, self.tool_call, self.no_tool_call)
        if sum(k is not None for k in kinds) != 1:
            raise ValueError("a check is one of contains, not_contains, tool_call, no_tool_call")
        if self.args is not None and self.tool_call is None:
            raise ValueError("`args` goes with `tool_call`")
        if self.no_tool_call is False:
            raise ValueError("`no_tool_call` is true or absent; use `tool_call` to require one")
        return self

    @property
    def about_tools(self) -> bool:
        return self.tool_call is not None or self.no_tool_call is not None

    def describe(self) -> str:
        if self.contains is not None:
            return f"contains {self.contains}"
        if self.not_contains is not None:
            return f"not_contains {self.not_contains}"
        if self.tool_call is not None:
            return f"tool_call {self.tool_call}" + (f" {self.args}" if self.args else "")
        return "no_tool_call"


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    owner: str  # the team that vouches for this task, and is asked when it fails
    prompt: str = Field(min_length=1)
    system: str | None = None
    checks: list[Check] = Field(min_length=1)

    @property
    def about_tools(self) -> bool:
        return any(check.about_tools for check in self.checks)


class Golden(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Golden:
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        return self


def load(path: str | Path) -> list[Task]:
    return Golden.model_validate(yaml.safe_load(Path(path).read_text())).tasks


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class Output:
    """What a model answered, in the terms the checks use."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None  # the call failed: there is nothing to check


def failed_checks(task: Task, out: Output) -> list[str]:
    """The checks this answer fails (empty: the task passes)."""
    text = out.text.lower()
    failures = []
    for check in task.checks:
        if check.contains is not None:
            ok = all(s.lower() in text for s in check.contains)
        elif check.not_contains is not None:
            ok = not any(s.lower() in text for s in check.not_contains)
        elif check.tool_call is not None:
            ok = any(
                call.name == check.tool_call
                and all(str(call.args.get(k)) == str(v) for k, v in (check.args or {}).items())
                for call in out.tool_calls
            )
        else:
            ok = not out.tool_calls
        if not ok:
            failures.append(check.describe())
    return failures
