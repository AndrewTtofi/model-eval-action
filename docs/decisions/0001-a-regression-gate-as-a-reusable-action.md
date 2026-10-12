# 0001 — A regression gate for model changes, as a reusable action

- **Status:** accepted
- **Date:** 2026-10-13

## Context
Changing the model behind an application is usually one line: a model name in a config
file. Everything that depended on the old model's behaviour moves at once, and nothing
in the pull request says whether it still works: whether the new model still calls the
right tool with the right arguments, still leaves tools alone when none is needed, still
ignores instructions hidden in a document.

The harness that answers this was first written inside an AI gateway, calling its
provider adapters in-process. That ties it to one codebase. The same check is wanted in
any repository where a model name can change.

## Options considered
1. **Leave it in the gateway** — works for the gateway's own aliases only.
2. **A vendor's eval product** — more capable, and an account, a dashboard and a second
   place to look. Here the aim is a check that lives in the pull request.
3. **An LLM judge scoring answers** — flexible, but the gate would then depend on a
   model's opinion, which drifts and can be argued with.
4. **A small action:** golden tasks with deterministic checks, run on both models over
   the API every provider and gateway already speaks, reported on the pull request.

## Decision
Option 4.

**It compares, it doesn't score.** A candidate regresses when it fails a task the current
model passes. A task both fail is information, not a blocker: a golden file can be
ahead of every model. There is no pass mark to tune.

**Checks are deterministic and about facts:** strings that must or must not appear, a
tool that must be called (with given arguments), or no tool at all. No judge model.
A check on wording is a mistake: check the fact.

**Real tool calls.** The application's tool definitions are sent with every task, and the
checks read the tool calls the model actually returns.

**Every repeat must pass.** With `runs: 5`, right four times is a failure. A gate for a
rollout should be stricter than an average.

**Three verdicts, three exit codes.** `pass` (0); `regression` or `over budget` (1);
`inconclusive` (2), when a task could not be run at all. An outage is not a pass and is
not the model's fault, so it is neither.

**Cost and latency are reported, and optionally gated.** Cost per task comes from token
counts and a price file the caller owns; a model with no price is "unpriced", never
free. `max-cost-ratio` and `max-latency-ratio` fail the run when the candidate is that
many times worse than the current model.

**It talks OpenAI-format chat completions** to a base URL, so it works against a gateway,
a provider, or a local model server, with extra headers for whatever the endpoint needs.

**Offline stubs ship with it.** `offline: true` replaces the models with deterministic
stand-ins: one that passes, one that regresses, one that costs more. A repository can
check its golden file and its workflow wiring on every pull request with no key and no
spend, and this repository tests the action on itself that way.

**Inputs are data.** The composite action passes every input to its script through the
environment, never by interpolating it into the script text, and the API key reaches the
tool only as an environment variable. Error messages carry the HTTP status and the
provider's error code, never a response body.

**One comment, updated.** The report carries a marker; the action edits its earlier
comment instead of adding another.

## Consequences
- Any repository can gate a model change with a workflow of a dozen lines.
- The golden file is the product. Six real tasks owned by the teams who would notice
  are worth more than sixty generated ones.
- Deterministic checks can't judge prose quality. That is a different tool (sampled
  judging in production, human review), and it is the right trade for a gate.
- A live run costs money on every pull request that triggers it; restrict the workflow's
  `paths` to the file where the model is named.
- The price file is the caller's to keep current.
- **The live path has been exercised only against stand-ins** in this repository's
  tests. The harness it was extracted from has been run between two real local models.
