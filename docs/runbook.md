# Runbook

For the people who own a workflow that uses this action.

## Add it to a repository

1. Write `evals/golden.yaml`: a handful of tasks the teams who depend on the model would
   recognise as theirs. Give each an `owner`.
2. Export the tool definitions the application offers the model to `evals/tools.json`
   (OpenAI format). Without them the tool checks can't pass.
3. Optionally add `evals/prices.yaml` for the cost line.
4. Add the workflow from the README. Limit its `paths` to the file where the model is
   named, so it runs (and spends) only when the model changes.
5. Store the API key as a repository secret and pass it as `api-key`.
6. First, run it with `offline: 'true'` to check the wiring for free.

## Read the comment

| Verdict | Meaning | What to do |
|---------|---------|------------|
| ✅ No regressions | The candidate passes everything the current model passes | Merge on the usual review |
| ❌ regression | A task the current model passes, the candidate fails | Open "What failed"; ask the task's owner; fix the prompt or the tool description, or don't switch |
| ❌ Over budget | Cost per task or latency is past the limit you set | Decide if the quality is worth it, and raise the limit in the same pull request if so |
| ⚠️ Inconclusive | A task couldn't be run (an outage, a bad key, an unknown model) | Read the error next to the task; re-run |

"Improvement" marks a task the candidate passes and the current model fails.

## When something is wrong

### Every task shows `error: HTTP 401`
The `api-key` secret is missing, wrong, or not available to the workflow (secrets are
not passed to pull requests from forks).

### Every task shows `error: HTTP 404` or `could not be reached`
`base-url` must end where `/chat/completions` begins: `https://host/v1`, not the host
alone and not the full path.

### `error: HTTP 400 (…)` on a model name
The endpoint doesn't know `current` or `candidate` by that name.

### Tool tasks fail on both models
The `tools` file wasn't passed, or the tool's `name` there differs from the `tool_call`
in the golden file. Names are compared exactly.

### A task fails on the current model
Not a blocker by itself: the gate is about regressions. But a task nobody passes isn't
protecting anything; fix the task or the check. A check on wording ("rolled back") fails
correct answers ("a rollback"): check the fact.

### The result changes between runs
Models are not deterministic. Raise `runs`; every repeat must pass, so a flaky task
shows up as a failure instead of passing by luck.

### The cost line says "unpriced"
The model has no entry in the prices file. `max-cost-ratio` is not applied to an
unpriced model.

### The job fails with `cannot evaluate: …`
The golden file, tools file or prices file is missing or malformed. The message names
the problem; run `python -m evalgate --golden … --offline …` locally to iterate.

### No comment appears
The workflow needs `permissions: pull-requests: write`, and comments are only posted on
`pull_request` events. The report is always in the job summary.

## Keeping it useful

- **Add a task when something breaks in production** that a task would have caught.
- **Remove a task nobody would defend.**
- **Review the golden file like code.** Its owners are the reviewers.
- **Pin the action** to a release tag or a commit SHA.
