"""Run or resume the CockroachDB-backed incident agent demo."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent  # noqa: E402
from hindsight.embeddings import DeterministicEmbeddingProvider  # noqa: E402
from hindsight.reasoning import DeterministicReasoningProvider  # noqa: E402
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
    start.add_argument("--deterministic", action="store_true")

    resume = subparsers.add_parser("resume", help="resume an interrupted incident thread")
    resume.add_argument("--thread-id", required=True)
    resume.add_argument("--approve", action="store_true")
    resume.add_argument("--reject", action="store_true")
    resume.add_argument("--deterministic", action="store_true")

    args = parser.parse_args()
    provider = (
        DeterministicReasoningProvider(response_text="check errors, reduce blast radius, verify")
        if args.deterministic
        else None
    )
    embedding_provider = DeterministicEmbeddingProvider()

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
        )
    else:
        if args.approve and args.reject:
            parser.error("--approve and --reject are mutually exclusive")
        result = resume_incident_agent(
            thread_id=args.thread_id,
            approved=not args.reject,
            reasoning_provider=provider,
            embedding_provider=embedding_provider,
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


if __name__ == "__main__":
    main()
