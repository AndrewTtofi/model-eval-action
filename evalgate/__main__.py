"""`python -m evalgate`: compare a candidate model with the current one on golden tasks.

    python -m evalgate --golden golden.yaml --current fast --candidate fast-next \\
        --base-url https://gateway.example/v1 --tools tools.json --prices prices.yaml
    python -m evalgate --golden golden.yaml --offline \\
        --current stub/baseline --candidate stub/regressed

The API key comes from the environment (EVAL_API_KEY), never from the command line.

Exit code: 0 no regressions; 1 the candidate fails a task the current model passes, or
costs or takes more than a limit allows; 2 the comparison could not be made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from evalgate import golden, render
from evalgate.models import HttpModel, Model, offline
from evalgate.run import Prices, evaluate


def _prices(path: str | None) -> Prices:
    if not path:
        return {}
    raw = yaml.safe_load(Path(path).read_text()) or {}
    prices: Prices = {}
    for model, price in (raw.get("models") or raw).items():
        prices[str(model)] = {"input": float(price["input"]), "output": float(price["output"])}
    return prices


def _headers(pairs: list[str]) -> dict[str, str]:
    headers = {}
    for pair in pairs:
        name, sep, value = pair.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"a header is 'Name: value', got {pair!r}")
        headers[name.strip().lower()] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evalgate", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--golden", required=True, help="the golden tasks (YAML)")
    parser.add_argument("--current", required=True, help="the model in use today")
    parser.add_argument("--candidate", required=True, help="the model being considered")
    parser.add_argument("--base-url", help="an OpenAI-compatible API, up to and including /v1")
    parser.add_argument("--tools", help="tool definitions to offer, as an OpenAI-format JSON list")
    parser.add_argument("--prices", help="YAML: model → input and output $ per 1M tokens")
    parser.add_argument("--header", action="append", default=[], help="an extra request header")
    parser.add_argument("--runs", type=int, default=1, help="repeats per task; every one must pass")
    parser.add_argument(
        "--max-cost-ratio",
        type=float,
        help="fail if the candidate costs more than this many times the current model per task",
    )
    parser.add_argument(
        "--max-latency-ratio",
        type=float,
        help="fail if its median latency is more than this many times the current model's",
    )
    parser.add_argument(
        "--offline", action="store_true", help="deterministic stubs, no key, no spend"
    )
    parser.add_argument("--markdown", help="write the report here as Markdown")
    parser.add_argument("--json", help="write the report here as JSON")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    try:
        tasks = golden.load(args.golden)
        prices = _prices(args.prices)
        tools: list[dict[str, Any]] = json.loads(Path(args.tools).read_text()) if args.tools else []
        headers = _headers(args.header)
    except (OSError, ValueError, KeyError, TypeError, ValidationError, yaml.YAMLError) as exc:
        print(f"cannot evaluate: {exc}", file=sys.stderr)
        return 2
    if args.offline:
        model: Model = offline
    elif not args.base_url:
        print("cannot evaluate: --base-url is required unless --offline", file=sys.stderr)
        return 2
    else:
        model = HttpModel(args.base_url, os.environ.get("EVAL_API_KEY", ""), tools, headers)

    report = evaluate(
        tasks,
        args.current,
        args.candidate,
        model,
        args.runs,
        prices,
        args.max_cost_ratio,
        args.max_latency_ratio,
    )
    text = render.markdown(report, args.offline)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(render.plain(report, args.offline), indent=2))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
