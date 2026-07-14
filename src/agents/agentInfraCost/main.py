"""CLI entry point for the InfraCost agent (Agent 2).

Usage:
    python main.py run <input.json> [--region us-east-1] [--environment dev] [--out output.json]
    python main.py approve <output.json> --approved-by user@example.com [--out approved.json]
    python main.py reject <output.json> [--out rejected.json]

Can be run directly (`python main.py ...`) or as a module
(`python -m agentInfraCost.main ...` with src/agents on PYTHONPATH, or
`python -m src.agents.agentInfraCost.main ...` with the repo root on
PYTHONPATH) — the bootstrap below makes relative imports work either way.
"""

from __future__ import annotations

if __package__ in (None, ""):  # `python main.py` direct execution
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    __package__ = "agentInfraCost"

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .core.pipeline import DEFAULT_ENVIRONMENT, DEFAULT_REGION, apply_approval, run_pipeline
from .models.exceptions import InfraCostAgentError
from .models.output_models import InfraCostOutput

# Loads GEMINI_API_KEY (if present) from a .env file next to this script,
# regardless of the current working directory the CLI is invoked from.
# Never errors if the file is absent — llm_enrichment.py's fallback mode
# covers that case.
load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _write_output(output: InfraCostOutput, out_path: Optional[str]) -> None:
    """Serializes `output` to JSON (respecting the "lambda" field alias) and
    either writes it to `out_path` or prints it to stdout."""
    payload = output.model_dump(mode="json", by_alias=True)
    text = json.dumps(payload, indent=2)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        logger.info("Wrote output to %s", out_path)
    else:
        print(text)


def _cmd_run(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = run_pipeline(raw, region=args.region, environment=args.environment)
    _write_output(output, args.out)
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.output_file).read_text(encoding="utf-8"))
    output = InfraCostOutput.model_validate(raw)
    approved = apply_approval(output, approved=True, approved_by=args.approved_by)
    _write_output(approved, args.out)
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.output_file).read_text(encoding="utf-8"))
    output = InfraCostOutput.model_validate(raw)
    rejected = apply_approval(output, approved=False)
    _write_output(rejected, args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI's argument parser (kept separate from main() for testability)."""
    parser = argparse.ArgumentParser(
        prog="agentInfraCost", description="DevGuard AI InfraCost Agent (Agent 2)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run the pipeline on an Agent 1 output JSON file"
    )
    run_parser.add_argument("input", help="Path to the repo-analysis agent's output JSON")
    run_parser.add_argument("--region", default=DEFAULT_REGION)
    run_parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    run_parser.add_argument("--out", default=None, help="Write output JSON here instead of stdout")
    run_parser.set_defaults(func=_cmd_run)

    approve_parser = subparsers.add_parser(
        "approve", help="Approve a pending InfraCostOutput JSON file"
    )
    approve_parser.add_argument("output_file", help="Path to a pending InfraCostOutput JSON file")
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument("--out", default=None)
    approve_parser.set_defaults(func=_cmd_approve)

    reject_parser = subparsers.add_parser(
        "reject", help="Reject a pending InfraCostOutput JSON file"
    )
    reject_parser.add_argument("output_file", help="Path to a pending InfraCostOutput JSON file")
    reject_parser.add_argument("--out", default=None)
    reject_parser.set_defaults(func=_cmd_reject)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point. Returns a process exit code (0 success, 1 failure)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InfraCostAgentError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
