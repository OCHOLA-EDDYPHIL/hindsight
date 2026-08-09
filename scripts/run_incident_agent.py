"""Run or resume the CockroachDB-backed incident agent."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent  # noqa: E402
from hindsight.cloudwatch_diagnostics import (  # noqa: E402
    optional_cloudwatch_diagnostics_from_env,
)
from hindsight.embeddings import embedding_provider_from_env  # noqa: E402
from hindsight.reasoning import reasoning_provider_from_env  # noqa: E402
from hindsight.tracing import configure_tracing_from_env  # noqa: E402


def main() -> None:
    configure_tracing_from_env(service_name="hindsight-agent")

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start an incident thread")
    start.add_argument("--thread-id", required=True)
    start.add_argument("--incident-id", required=True)
    start.add_argument("--input", required=True)
    start.add_argument("--namespace")
    start.add_argument("--service-slug")
    start.add_argument("--severity")
    start.add_argument("--title")
    start.add_argument("--pause-before-act", action="store_true")

    resume = subparsers.add_parser("resume", help="resume an interrupted incident thread")
    resume.add_argument("--thread-id", required=True)
    resume.add_argument("--approve", action="store_true")
    resume.add_argument("--reject", action="store_true")
    resume.add_argument("--recommendation-id", required=True)
    resume.add_argument("--selection-fingerprint", required=True)

    args = parser.parse_args()
    provider = reasoning_provider_from_env()
    embedding_provider = embedding_provider_from_env()
    diagnostic_tool = _configured_diagnostic_tool()

    if args.command == "start":
        result = run_incident_agent(
            IncidentInput(
                user_input=args.input,
                incident_id=args.incident_id,
                namespace=args.namespace,
                service_slug=args.service_slug,
                severity=args.severity,
                title=args.title,
            ),
            thread_id=args.thread_id,
            pause_before_act=args.pause_before_act,
            reasoning_provider=provider,
            embedding_provider=embedding_provider,
            diagnostic_tool=diagnostic_tool,
        )
    else:
        if args.approve == args.reject:
            parser.error("choose exactly one of --approve or --reject")
        result = resume_incident_agent(
            thread_id=args.thread_id,
            approved=args.approve,
            recommendation_id=args.recommendation_id,
            selection_fingerprint=args.selection_fingerprint,
            reasoning_provider=provider,
            embedding_provider=embedding_provider,
            diagnostic_tool=diagnostic_tool,
        )

    print(
        json.dumps(
            {
                "thread_id": result.thread_id,
                "interrupted": result.interrupted,
                "interrupt": result.interrupt,
                "plan": result.plan,
                "proposed_action": result.proposed_action,
                "reflected_memory_id": result.reflected_memory_id,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _configured_diagnostic_tool():
    diagnostics = optional_cloudwatch_diagnostics_from_env()
    if diagnostics is None:
        print(
            "CloudWatch diagnostics disabled; configure HINDSIGHT_AWS_ACCOUNT_ID, "
            "AWS_REGION, and HINDSIGHT_STAGE to enable read-only metric tools.",
            file=sys.stderr,
        )
    return diagnostics


if __name__ == "__main__":
    main()
