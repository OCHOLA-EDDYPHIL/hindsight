"""Seed and verify a synthetic terminal benchmark upgrade fixture."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from hindsight.db import database_url


LEGACY_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
EXPERIMENT_ID = UUID("10000000-0000-0000-0000-000000000001")
TRIAL_ID = UUID("10000000-0000-0000-0000-000000000002")
DECISION_ID = "ci-upgrade-terminal-action"


def seed(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        latest = conn.execute("SELECT max(filename) FROM schema_migrations").fetchone()[0]
        if latest != "0018_agent_run_attempt_fencing.sql":
            raise RuntimeError(f"fixture requires schema 0018, found {latest}")
        conn.execute(
            """
                INSERT INTO memory_decisions (
                    id, actor, decision_kind, purpose, namespace,
                    status, sealed_at, metadata
                ) VALUES (
                    %s, 'ci.upgrade', 'benchmark_action',
                    'Preserve a synthetic terminal upgrade trace', 'ci:upgrade',
                    'sealed', now(), %s
                )
            """,
            (DECISION_ID, Jsonb({"fixture": "terminal-upgrade"})),
        )
        conn.execute(
            """
                INSERT INTO benchmark_experiments (
                    id, experiment_kind, status, manifest, manifest_sha256,
                    provider, model, code_sha
                ) VALUES (
                    %s, 'ci_smoke', 'running', %s, 'ci-upgrade-manifest',
                    'deterministic', 'deterministic', 'ci-upgrade'
                )
            """,
            (EXPERIMENT_ID, Jsonb({"fixture": "terminal-upgrade"})),
        )
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    id, experiment_id, variant_id, repetition, arm, namespace,
                    status, started_at
                ) VALUES (
                    %s, %s, 'ci-upgrade-variant', 1, 'no_lesson', 'ci:upgrade',
                    'running', now()
                )
            """,
            (TRIAL_ID, EXPERIMENT_ID),
        )
        conn.execute(
            """
                INSERT INTO benchmark_actions (
                    trial_id, step, decision_id, action, observation,
                    cited_memory_ids, unsafe, recovered, usage
                ) VALUES (
                    %s, 1, %s, 'inspect synthetic dependency', %s,
                    '[]'::JSONB, false, false, '{}'::JSONB
                )
            """,
            (TRIAL_ID, DECISION_ID, Jsonb({"status": "fixture"})),
        )
        conn.execute(
            """
                UPDATE benchmark_trials
                SET status = 'completed', recovered = false,
                    action_count = 1, penalized_action_count = 1,
                    unsafe_action_count = 0, completed_at = now()
                WHERE id = %s
            """,
            (TRIAL_ID,),
        )
        conn.execute(
            """
                UPDATE benchmark_experiments
                SET status = 'completed', completed_at = now()
                WHERE id = %s
            """,
            (EXPERIMENT_ID,),
        )


def _expect_immutable(conn: psycopg.Connection, statement: str, params: tuple[object, ...]) -> None:
    try:
        conn.execute(statement, params)
    except psycopg.Error:
        return
    raise RuntimeError("terminal benchmark fixture unexpectedly allowed mutation")


def verify(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        experiment = conn.execute(
            """
                SELECT tenant_id, status, experiment_kind, manifest_sha256,
                       protocol_authorization_id, execution_authorization_id
                FROM benchmark_experiments WHERE id = %s
            """,
            (EXPERIMENT_ID,),
        ).fetchone()
        trial = conn.execute(
            """
                SELECT tenant_id, status, action_count, unsafe_action_count
                FROM benchmark_trials WHERE id = %s
            """,
            (TRIAL_ID,),
        ).fetchone()
        action = conn.execute(
            """
                SELECT tenant_id, action, observation
                FROM benchmark_actions WHERE trial_id = %s AND step = 1
            """,
            (TRIAL_ID,),
        ).fetchone()
        if experiment != (
            LEGACY_TENANT_ID,
            "completed",
            "ci_smoke",
            "ci-upgrade-manifest",
            None,
            None,
        ):
            raise RuntimeError(f"terminal experiment changed during upgrade: {experiment!r}")
        if trial != (LEGACY_TENANT_ID, "completed", 1, 0):
            raise RuntimeError(f"terminal trial changed during upgrade: {trial!r}")
        if action is None or action[0] != LEGACY_TENANT_ID:
            raise RuntimeError(f"terminal action changed during upgrade: {action!r}")
        if action[1:] != ("inspect synthetic dependency", {"status": "fixture"}):
            raise RuntimeError(f"terminal action payload changed during upgrade: {action!r}")

        _expect_immutable(
            conn,
            "UPDATE benchmark_experiments SET status = 'failed' WHERE id = %s",
            (EXPERIMENT_ID,),
        )
        _expect_immutable(
            conn,
            "UPDATE benchmark_trials SET status = 'invalid' WHERE id = %s",
            (TRIAL_ID,),
        )
        _expect_immutable(
            conn,
            "UPDATE benchmark_actions SET action = 'changed' WHERE trial_id = %s AND step = 1",
            (TRIAL_ID,),
        )
        print(
            json.dumps(
                {
                    "experiment_id": str(EXPERIMENT_ID),
                    "trial_id": str(TRIAL_ID),
                    "tenant_id": str(LEGACY_TENANT_ID),
                    "terminal_guards": "restored",
                },
                sort_keys=True,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "verify"))
    args = parser.parse_args()
    url = database_url()
    if args.command == "seed":
        seed(url)
    else:
        verify(url)


if __name__ == "__main__":
    main()
