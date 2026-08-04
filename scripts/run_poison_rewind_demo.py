"""Run the memory poisoning and rewind demo."""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.demo import (  # noqa: E402
    DEMO_NAMESPACE,
    MemoryBiasedDemoReasoningProvider,
    poison_demo_memory,
    reset_poison_rewind_demo,
    run_demo_agent_turn,
    run_poison_rewind_demo,
)
from hindsight.embeddings import embedding_provider_from_env  # noqa: E402
from hindsight.operations import enqueue_operation, execute_operation, preview_rewind  # noqa: E402
from hindsight.tracing import configure_tracing_from_env  # noqa: E402
from hindsight.trace_contract import decision_influence  # noqa: E402


def main() -> None:
    configure_tracing_from_env(service_name="hindsight-demo")

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    all_cmd = subparsers.add_parser("all", help="run the full clean/poison/rewind sequence")
    all_cmd.add_argument("--namespace", default=DEMO_NAMESPACE)
    all_cmd.add_argument("--keep-existing", action="store_true")

    reset_cmd = subparsers.add_parser("reset", help="archive a prior demo session")
    reset_cmd.add_argument("--namespace", default=DEMO_NAMESPACE)

    poison_cmd = subparsers.add_parser("poison", help="insert the poisoned memory")
    poison_cmd.add_argument("--namespace", default=DEMO_NAMESPACE)

    run_cmd = subparsers.add_parser("run", help="run one deterministic agent turn")
    run_cmd.add_argument("--namespace", default=DEMO_NAMESPACE)
    run_cmd.add_argument("--label", default="manual")

    diagnose_cmd = subparsers.add_parser("diagnose", help="show a decision-to-memory trace")
    diagnose_cmd.add_argument("--decision-id", required=True)

    rewind_cmd = subparsers.add_parser("rewind", help="rewind a namespace to an ISO timestamp")
    rewind_cmd.add_argument("--namespace", default=DEMO_NAMESPACE)
    rewind_cmd.add_argument("--timestamp", required=True)
    rewind_cmd.add_argument("--reason", default="Operator requested demo rewind")

    parser.set_defaults(command="all", namespace=DEMO_NAMESPACE, keep_existing=False)
    args = parser.parse_args()
    command = args.command

    if command == "all":
        result = run_poison_rewind_demo(
            namespace=args.namespace,
            keep_existing=args.keep_existing,
        )
    elif command == "reset":
        reset_poison_rewind_demo(namespace=args.namespace)
        result = {"namespace": args.namespace, "reset": True}
    elif command == "poison":
        result = poison_demo_memory(namespace=args.namespace)
    elif command == "run":
        result = run_demo_agent_turn(
            label=args.label,
            namespace=args.namespace,
            reasoning_provider=MemoryBiasedDemoReasoningProvider(),
        )
    elif command == "diagnose":
        result = decision_influence(
            decision_id=args.decision_id,
        )
    elif command == "rewind":
        from datetime import datetime

        timestamp = datetime.fromisoformat(args.timestamp.replace("Z", "+00:00"))
        preview = preview_rewind(
            namespace=args.namespace,
            target_timestamp=timestamp,
            actor="demo.operator",
            reason=args.reason,
        )
        operation, _ = enqueue_operation(
            preview_id=str(preview["id"]),
            fingerprint=str(preview["fingerprint"]),
            idempotency_key=f"demo-cli:{preview['id']}",
        )
        result = execute_operation(
            operation_id=str(operation["id"]),
            embedding_provider=embedding_provider_from_env(),
            worker_id="demo.cli",
        )
    else:
        parser.error(f"Unsupported command: {command}")

    print(json.dumps(_jsonable(result), indent=2, sort_keys=True, default=str))


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    main()
