"""Tests for the public governed-memory identity trace."""

import json
import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def test_public_redaction_removes_nested_aws_account_identifiers():
    from hindsight.redaction import redact_account_identifiers

    secret = "123456789012"
    redacted = redact_account_identifiers(
        {
            "account_id": secret,
            "nested": [
                {
                    "aws_account_id": secret,
                    "region": "us-east-1",
                    "message": f"account {secret} was queried",
                }
            ],
        }
    )

    assert redacted == {
        "nested": [
            {
                "region": "us-east-1",
                "message": "account [redacted-account] was queried",
            }
        ]
    }
    assert secret not in str(redacted)


def test_public_redaction_removes_secret_keys_and_credential_like_prompt_text():
    from hindsight.redaction import redact_account_identifiers

    redacted = redact_account_identifiers(
        {
            "api_key": "do-not-keep",
            "nested": {
                "password": "do-not-keep",
                "AccessKeyId": "do-not-keep",
                "SecretAccessKey": "do-not-keep",
                "GitHubToken": "do-not-keep",
                "awsAccountId": "123456789012",
                "max_output_tokens": 1_024,
                "safe": "visible",
            },
            "prompt": (
                "authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature "
                "x-api-key=opaque-value AKIAABCDEFGHIJKLMNOP "
                '{"apiKey": "sk-proj-exampleSecret123", "password": "hunter2"} '
                "postgresql://operator:database-secret@db.internal/hindsight "
                "ghp_exampleSecret123 sk-proj_anotherSecret123 "
                "xoxb-slackSecret123 glpat-gitlabSecret123 "
                "AIzaGoogleSecretToken1234567890 npm_npmSecret123"
            ),
        }
    )

    assert redacted["nested"] == {"max_output_tokens": 1_024, "safe": "visible"}
    assert "api_key" not in redacted
    for secret in (
        "eyJ",
        "opaque-value",
        "AKIA",
        "hunter2",
        "database-secret",
        "ghp_",
        "sk-proj",
        "xoxb-",
        "glpat-",
        "AIza",
        "npm_",
    ):
        assert secret not in redacted["prompt"]
    assert '"apiKey": "[redacted-secret]"' in redacted["prompt"]
    assert "postgresql://operator:[redacted-secret]@db.internal" in redacted["prompt"]


def test_causal_evidence_document_digest_excludes_download_metadata():
    from hindsight.causal_evidence import canonical_sha256
    from hindsight.trace_contract import causal_evidence_document

    scenario = {
        "scenario_id": "scenario-1",
        "namespace": "tenant-private-namespace",
        "rewind_anchor": "2026-08-14T10:00:00Z",
        "created_at": "2026-08-14T09:00:00Z",
        "completed_at": None,
        "stages": {
            "influenced_decision_id": "decision-before",
            "corrected_decision_id": "decision-after",
        },
        "runs": [],
        "operation": {
            "id": "operation-1",
            "operation_type": "rewind",
            "status": "completed",
            "reason": "password=hunter2",
            "actor": "ghp_exampleSecret123",
            "invalidated_memory_ids": ["memory-1"],
            "restored_memory_ids": [],
        },
        "operation_events": [
            {
                "id": "event-1",
                "operation_id": "operation-1",
                "sequence": 1,
                "status": "completed",
                "summary": "api_key=opaque-value",
            }
        ],
        "operation_effects": [],
        "memories": [
            {
                "id": "memory-1",
                "namespace": "tenant-private-namespace",
                "writer": "postgresql://operator:database-secret@db.internal/app",
                "version_number": 1,
            }
        ],
        "causal_evidence": {
            "proof_states": {
                "repeatable_causal_effect_supported": {
                    "status": "unavailable",
                    "reason": "repeated_trials_not_measured",
                }
            },
            "download": {"sha256": "must-not-be-canonicalized"},
        },
    }

    document = causal_evidence_document(scenario)

    assert canonical_sha256(document).startswith("sha256:")
    assert "download" not in str(document)
    assert document["scenario"]["namespace"] == "[redacted-namespace]"
    for secret in (
        "tenant-private-namespace",
        "hunter2",
        "ghp_",
        "opaque-value",
        "database-secret",
    ):
        assert secret not in str(document)


def test_signature_trace_pairs_latest_pre_rewind_rejection_with_correction():
    from datetime import UTC, datetime, timedelta

    from hindsight.trace_contract import _rejected_run_for_operation

    rewind_completed_at = datetime.now(UTC)
    oldest = {
        "id": "oldest-rejection",
        "status": "rejected",
        "completed_at": rewind_completed_at - timedelta(minutes=3),
    }
    corrected_rejection = {
        "id": "corrected-rejection",
        "status": "rejected",
        "completed_at": rewind_completed_at - timedelta(minutes=1),
    }
    later_rejection = {
        "id": "later-rejection",
        "status": "rejected",
        "completed_at": rewind_completed_at + timedelta(minutes=1),
    }

    selected = _rejected_run_for_operation(
        runs=[oldest, corrected_rejection, later_rejection],
        operation={"completed_at": rewind_completed_at},
    )

    assert selected == corrected_rejection


def test_action_comparison_requires_structured_actions_equivalent_context_and_lineage():
    from copy import deepcopy

    from hindsight.agent_decision import (
        PAYMENTS_OPERATIONAL_ACTION_CATALOG_ID,
        PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        agent_decision_provider_schema,
        controlled_action_selection_provider_schema,
        operational_action_catalog,
        operational_action_fingerprint,
    )
    from hindsight.causal_evidence import (
        GOVERNED_MEMORY_PROMPT_MARKER,
        build_causal_envelope,
        canonical_sha256,
        text_sha256,
    )
    from hindsight.trace_contract import (
        _action_comparison,
        _causal_proof_states,
        _controlled_pair_checks,
        _request_invariant_from_actual,
    )

    prompt = (
        "Checkout p99 is above 2s and the queue is growing. Inspect current telemetry "
        "and recommend one reversible next action."
    )

    def observation(timestamp: str, *, account_id: str) -> dict:
        from datetime import datetime, timedelta

        end = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        start = end - timedelta(seconds=900)
        early = end - timedelta(seconds=120)
        latest = end - timedelta(seconds=60)
        return {
            "id": "observation:stable:1",
            "tool_call_id": "diagnostic:stable:1",
            "schema_version": 1,
            "status": "available",
            "tool": "aws_cloudwatch_diagnostics",
            "query_key": "payments.retry_fanout",
            "query_fingerprint": f"cloudwatch_query:{'e' * 64}",
            "account_id": account_id,
            "region": "us-east-1",
            "metric": {
                "namespace": "Hindsight/Demo",
                "name": "CheckoutLatency",
                "dimensions": [
                    {"name": "Service", "value": "payments-api"},
                    {"name": "Stage", "value": "demo"},
                ],
                "statistic": "Maximum",
                "unit": "Milliseconds",
                "period_seconds": 60,
            },
            "window": {
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "seconds": 900,
            },
            "datapoints": [
                {"timestamp": early.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": 2100.0},
                {"timestamp": latest.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": 2400.0},
            ],
            "datapoint_count": 2,
            "truncated": False,
        }

    def run(
        decision_id: str,
        memory_id: str,
        action: str,
        timestamp: str,
        *,
        report: str = prompt,
        rationale: str = "Recorded evidence supports this catalog selection.",
    ) -> dict:
        payload = {
            "catalog_id": PAYMENTS_OPERATIONAL_ACTION_CATALOG_ID,
            "contract": "payments_retry_amplification.v1",
            "action_id": action,
            "disposition": "recommend",
            "parameters": {},
        }
        catalog = operational_action_catalog("payments_retry_amplification.v1")
        invariant_observation = observation(timestamp, account_id="redacted")
        invariant_observation.pop("account_id")
        ordered_observations = [invariant_observation]
        memory_payload = {"memory_id": memory_id}
        diagnostic_schema = agent_decision_provider_schema(
            recalled_memory_ids={memory_id},
            allowed_query_keys={"payments.retry_fanout"},
            diagnostic_calls_used=0,
            diagnostic_observation_available=False,
            model_turn=1,
            operational_action_contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        )
        selection_schema = controlled_action_selection_provider_schema(
            contract=PAYMENTS_OPERATIONAL_ACTION_CONTRACT
        )
        prompt_invariant = f"prompt before\n{GOVERNED_MEMORY_PROMPT_MARKER}\nprompt after"
        diagnostic_request_configuration = {
            "schema_version": 1,
            "attempt": 1,
            "repair_reason": None,
            "logical_turn": 1,
            "provider": "provider-1",
            "model": "model-1",
            "system": "controlled system",
            "prompt_invariant": prompt_invariant,
            "prompt_invariant_sha256": text_sha256(prompt_invariant),
            "temperature": 0,
            "max_output_tokens": 1024,
            "routing_key": "signature:stable:turn:1",
            "decision_contract": "AgentDecisionV3",
            "response_schema_version": 3,
            "response_json_schema": diagnostic_schema,
        }
        selection_request_configuration = {
            **diagnostic_request_configuration,
            "logical_turn": 2,
            "routing_key": "signature:stable:turn:2",
            "decision_contract": "ControlledActionSelectionV1",
            "response_schema_version": 1,
            "response_json_schema": selection_schema,
        }
        request_configurations = [
            diagnostic_request_configuration,
            selection_request_configuration,
        ]
        memory_prompt_fragment = f"1. {json.dumps(memory_payload, sort_keys=True)}"
        memory_intervention = [
            {
                "ordinal": 1,
                "memory": memory_payload,
                "memory_sha256": canonical_sha256(memory_payload),
                "prompt_fragment_sha256": text_sha256(memory_prompt_fragment),
            }
        ]
        tool_contract = {
            "schema_version": 1,
            "diagnostic_tool": "aws_cloudwatch_diagnostics",
            "observation_schema_version": 1,
            "allowed_query_keys": ["payments.retry_fanout"],
            "max_diagnostic_calls": 3,
        }
        operation_effects = [
            {
                "sequence": 1,
                "effect_type": "closed",
                "source_memory_id": "memory-v2",
                "result_memory_id": None,
                "belief_id": "belief-1",
                "namespace": "namespace-1",
            },
            {
                "sequence": 2,
                "effect_type": "reasserted",
                "source_memory_id": "memory-v1",
                "result_memory_id": "memory-v3",
                "belief_id": "belief-1",
                "namespace": "namespace-1",
            },
        ]
        triage = {
            "incident_id": "incident-1",
            "namespace": "namespace-1",
            "service_slug": "payments-api",
            "severity": "high",
            "title": "Checkout latency",
            "summary": report,
            "prior_chat_messages": 0,
        }
        incident = {
            "incident_id": triage["incident_id"],
            "namespace": triage["namespace"],
            "service_slug": triage["service_slug"],
            "severity": triage["severity"],
            "title": triage["title"],
            "normalized_user_incident": report,
        }
        fixed_context = {
            "normalized_user_incident": report,
            "prompt_templates": {
                name: {"id": f"template-{name}", "sha256": f"sha256:{'d' * 64}"}
                for name in ("triage", "decision", "system")
            },
            "triage_result": triage,
            "ordered_tool_calls": [
                {
                    "id": "diagnostic:stable:1",
                    "tool": "aws_cloudwatch_diagnostics",
                    "query_key": "payments.retry_fanout",
                    "status": "completed",
                }
            ],
            "ordered_observations": ordered_observations,
            "ordered_model_request_configuration": [
                _request_invariant_from_actual(request)
                for request in request_configurations
            ],
            "tool_contract": tool_contract,
            "embedding_profile": {
                "profile_id": "profile-1",
                "provider": "provider-1",
                "model": "embedding-1",
                "dimensions": 3,
                "capability": "semantic",
                "encoder_revision": "test-v1",
                "configuration": {},
                "max_distance": None,
            },
            "release_revision": "a" * 40,
            "action_catalog": operational_action_catalog("payments_retry_amplification.v1"),
            "tenant_id": "tenant-1",
            "namespace": "namespace-1",
            "scenario_id": "scenario-1",
            "replay_anchor": "2026-08-13T10:00:00Z",
            "retrieval_policy": "semantic_strict",
            "retrieval_policy_version": 1,
        }
        prompt_input = prompt_invariant.replace(
            GOVERNED_MEMORY_PROMPT_MARKER,
            memory_prompt_fragment,
        )
        actual_requests = [
            {**request_configuration, "prompt": prompt_input}
            for request_configuration in request_configurations
        ]
        envelope = build_causal_envelope(
            identity={
                "scenario_id": "scenario-1",
                "namespace": "namespace-1",
                "replay_anchor": "2026-08-13T10:00:00Z",
                "scenario_routing_key": "signature:stable",
                "release_revision": "a" * 40,
                "run_id": f"run-{decision_id}",
                "decision_id": decision_id,
            },
            invariant_inputs=fixed_context,
            permitted_intervention={
                "kind": "governed_memory_version_selection.v1",
                "ordered_memory_versions": memory_intervention,
                "selection_fingerprint": f"selection-{memory_id}",
                "expected_changed_prompt_fragments": [text_sha256(memory_prompt_fragment)],
                "correction_operation_id": ("operation-1" if memory_id == "memory-v3" else None),
                "correction_target_timestamp": (
                    "2026-08-13T10:00:00Z" if memory_id == "memory-v3" else None
                ),
                "operation_effects": operation_effects if memory_id == "memory-v3" else [],
                "invalidated_memory_fingerprints": (
                    [canonical_sha256("memory-v2")] if memory_id == "memory-v3" else []
                ),
                "restored_memory_fingerprints": (
                    [canonical_sha256("memory-v3")] if memory_id == "memory-v3" else []
                ),
            },
            actual_decision_inputs={
                "incident": incident,
                "triage": triage,
                "retrieval_policy": "semantic_strict",
                "embedding_profile": fixed_context["embedding_profile"],
                "ordered_governed_memories": memory_intervention,
                "ordered_tool_calls": fixed_context["ordered_tool_calls"],
                "ordered_observations": ordered_observations,
                "ordered_model_requests": actual_requests,
                "tool_contract": tool_contract,
                "action_catalog": catalog,
            },
            rendered_prompt_sha256=[text_sha256(prompt_input), text_sha256(prompt_input)],
            decision_output={
                "action_id": action,
                "disposition": "recommend",
                "parameters": {},
                "rationale": rationale,
            },
        )
        read_at = "2026-08-13T10:05:00Z" if memory_id == "memory-v3" else "2026-08-13T10:02:00Z"
        return {
            "decision_id": decision_id,
            "user_input": report,
            "trace": {
                "reads": [
                    {
                        "memory_id": memory_id,
                        "read_at": read_at,
                        "t_valid": (
                            "2026-08-13T10:04:00Z"
                            if memory_id == "memory-v3"
                            else "2026-08-13T10:01:00Z"
                        ),
                        "t_invalid": (None if memory_id == "memory-v3" else "2026-08-13T10:04:00Z"),
                        "memory_lineage_status": "complete",
                        "incoming_lineage_edge_ids": [f"lineage-{memory_id}"],
                    }
                ]
            },
            "action_trace": {
                "schema_version": 4,
                "mode": "recommendation_only",
                "observations": [observation(timestamp, account_id=f"secret-{decision_id}")],
                "causal_envelope": envelope,
                "recommendation": {
                    "summary": operational_action_catalog(
                        "payments_retry_amplification.v1"
                    )["directives"][action],
                    "rationale": rationale,
                    "operational_action": {
                        **payload,
                        "primary_action": action,
                        "directive": operational_action_catalog("payments_retry_amplification.v1")[
                            "directives"
                        ][action],
                        "consistency_status": "consistent",
                        "fingerprint": operational_action_fingerprint(payload),
                    }
                },
            },
        }

    def validly_mutated(candidate: dict, case: str) -> dict:
        mutated = deepcopy(candidate)
        envelope = deepcopy(mutated["action_trace"]["causal_envelope"])
        invariants = envelope["invariant_inputs"]
        actual = envelope["actual_decision_inputs"]
        memory_block = "\n".join(
            f"{ordinal}. {json.dumps(item['memory'], sort_keys=True)}"
            for ordinal, item in enumerate(
                actual["ordered_governed_memories"],
                start=1,
            )
        )
        if case == "timestamp":
            value = "2026-08-13T09:59:00Z"
            invariants["ordered_observations"][0]["datapoints"][0]["timestamp"] = value
            actual["ordered_observations"][0]["datapoints"][0]["timestamp"] = value
        elif case == "early_value":
            invariants["ordered_observations"][0]["datapoints"][0]["value"] = 2_200
            actual["ordered_observations"][0]["datapoints"][0]["value"] = 2_200
        elif case == "dimensions":
            value = "canary"
            invariants["ordered_observations"][0]["metric"]["dimensions"][1]["value"] = value
            actual["ordered_observations"][0]["metric"]["dimensions"][1]["value"] = value
        elif case == "region":
            invariants["ordered_observations"][0]["region"] = "us-west-2"
            actual["ordered_observations"][0]["region"] = "us-west-2"
        elif case == "unit":
            invariants["ordered_observations"][0]["metric"]["unit"] = "Seconds"
            actual["ordered_observations"][0]["metric"]["unit"] = "Seconds"
        elif case == "statistic":
            invariants["ordered_observations"][0]["metric"]["statistic"] = "Average"
            actual["ordered_observations"][0]["metric"]["statistic"] = "Average"
        elif case == "period":
            invariants["ordered_observations"][0]["metric"]["period_seconds"] = 300
            actual["ordered_observations"][0]["metric"]["period_seconds"] = 300
        elif case == "query_fingerprint":
            value = f"cloudwatch_query:{'7' * 64}"
            invariants["ordered_observations"][0]["query_fingerprint"] = value
            actual["ordered_observations"][0]["query_fingerprint"] = value
        elif case == "truncated":
            invariants["ordered_observations"][0]["truncated"] = True
            actual["ordered_observations"][0]["truncated"] = True
        elif case == "window":
            value = "2026-08-13T09:48:00Z"
            invariants["ordered_observations"][0]["window"]["start"] = value
            actual["ordered_observations"][0]["window"]["start"] = value
            invariants["ordered_observations"][0]["window"]["seconds"] = 840
            actual["ordered_observations"][0]["window"]["seconds"] = 840
        elif case in {"provider", "model"}:
            value = f"changed-{case}"
            invariants["ordered_model_request_configuration"][0][case] = value
            actual["ordered_model_requests"][0][case] = value
        elif case == "action_schema":
            value = {"schema": "controlled-v4"}
            invariants["ordered_model_request_configuration"][0]["response_json_schema"] = value
            actual["ordered_model_requests"][0]["response_json_schema"] = value
        elif case == "selection_schema":
            value = {
                "type": "object",
                "required": ["directive"],
                "properties": {"directive": {"type": "string"}},
            }
            invariants["ordered_model_request_configuration"][1]["response_json_schema"] = value
            actual["ordered_model_requests"][1]["response_json_schema"] = value
        elif case == "selection_only":
            invariants["ordered_model_request_configuration"].pop(0)
            actual["ordered_model_requests"].pop(0)
            for request in (
                invariants["ordered_model_request_configuration"][0],
                actual["ordered_model_requests"][0],
            ):
                request["logical_turn"] = 1
                request["routing_key"] = "signature:stable:turn:1"
        elif case == "turn_gap":
            for request in (
                invariants["ordered_model_request_configuration"][1],
                actual["ordered_model_requests"][1],
            ):
                request["logical_turn"] = 3
                request["routing_key"] = "signature:stable:turn:3"
        elif case == "prompt_template":
            invariants["prompt_templates"]["decision"]["sha256"] = f"sha256:{'9' * 64}"
        elif case == "prompt_suffix":
            configuration = invariants["ordered_model_request_configuration"][0]
            request = actual["ordered_model_requests"][0]
            changed = f"{configuration['prompt_invariant']}\nnon-memory suffix"
            configuration["prompt_invariant"] = changed
            configuration["prompt_invariant_sha256"] = text_sha256(changed)
            request["prompt_invariant"] = changed
            request["prompt_invariant_sha256"] = text_sha256(changed)
            request["prompt"] = changed.replace(
                GOVERNED_MEMORY_PROMPT_MARKER,
                memory_block,
            )
        elif case == "tenant":
            invariants["tenant_id"] = "tenant-2"
        elif case == "policy":
            invariants["retrieval_policy"] = "semantic_then_keyword"
            actual["retrieval_policy"] = "semantic_then_keyword"
        elif case == "embedding":
            invariants["embedding_profile"]["profile_id"] = "profile-2"
            actual["embedding_profile"]["profile_id"] = "profile-2"
        elif case == "query_config":
            changed_contract = deepcopy(invariants["tool_contract"])
            changed_contract["allowed_query_keys"] = ["payments.processor_queue_depth"]
            invariants["tool_contract"] = changed_contract
            actual["tool_contract"] = changed_contract
        elif case == "operation_target":
            envelope["permitted_intervention"]["correction_target_timestamp"] = (
                "2026-08-13T10:01:00Z"
            )
        elif case == "unrelated_memory":
            unrelated_memory = {"memory_id": "memory-unrelated"}
            unrelated_fragment = f"2. {json.dumps(unrelated_memory, sort_keys=True)}"
            unrelated_item = {
                "ordinal": 2,
                "memory": unrelated_memory,
                "memory_sha256": canonical_sha256(unrelated_memory),
                "prompt_fragment_sha256": text_sha256(unrelated_fragment),
            }
            intervention = envelope["permitted_intervention"]
            intervention["ordered_memory_versions"].append(unrelated_item)
            intervention["expected_changed_prompt_fragments"].append(
                unrelated_item["prompt_fragment_sha256"]
            )
            intervention["selection_fingerprint"] = "selection-with-unrelated-memory"
            actual["ordered_governed_memories"].append(unrelated_item)
            request = actual["ordered_model_requests"][0]
            request["prompt"] = request["prompt_invariant"].replace(
                GOVERNED_MEMORY_PROMPT_MARKER,
                f"{memory_block}\n{unrelated_fragment}",
            )
        elif case == "release":
            envelope["identity"]["release_revision"] = "9" * 40
            invariants["release_revision"] = "9" * 40
        else:  # pragma: no cover - test helper exhaustiveness
            raise AssertionError(f"unknown mutation case: {case}")
        mutated["action_trace"]["causal_envelope"] = build_causal_envelope(
            identity=envelope["identity"],
            invariant_inputs=invariants,
            permitted_intervention=envelope["permitted_intervention"],
            actual_decision_inputs=actual,
            rendered_prompt_sha256=[
                text_sha256(request["prompt"])
                for request in actual["ordered_model_requests"]
            ],
            decision_output=envelope["decision_output"],
        )
        return mutated

    seed = {
        "id": "memory-v1",
        "belief_id": "belief-1",
        "version_number": 1,
        "transition_kind": "assertion",
        "t_invalid": "2026-08-13T10:01:00Z",
    }
    stale = {
        "id": "memory-v2",
        "belief_id": "belief-1",
        "version_number": 2,
        "previous_version_id": "memory-v1",
        "transition_kind": "supersession",
        "t_invalid": "2026-08-13T10:04:00Z",
    }
    restored = {
        "id": "memory-v3",
        "belief_id": "belief-1",
        "version_number": 3,
        "previous_version_id": "memory-v2",
        "transition_kind": "rewind_reassertion",
        "created_by_operation_id": "operation-1",
        "t_invalid": None,
    }
    rejected = run("decision-before", "memory-v2", "scale_workers", "2026-08-13T10:02:00Z")
    corrected = run("decision-after", "memory-v3", "throttle_retries", "2026-08-13T10:02:00Z")
    operation = {
        "id": "operation-1",
        "status": "completed",
        "target_timestamp": "2026-08-13T10:00:00Z",
        "invalidated_memory_ids": ["memory-v2"],
        "restored_memory_ids": ["memory-v3"],
    }
    effects = [
        {
            "sequence": 1,
            "effect_type": "closed",
            "source_memory_id": "memory-v2",
            "result_memory_id": None,
            "belief_id": "belief-1",
            "namespace": "namespace-1",
        },
        {
            "sequence": 2,
            "effect_type": "reasserted",
            "source_memory_id": "memory-v1",
            "result_memory_id": "memory-v3",
            "belief_id": "belief-1",
            "namespace": "namespace-1",
        },
    ]

    comparison = _action_comparison(
        rejected=rejected,
        corrected=corrected,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )

    assert comparison["status"] == "changed"
    assert comparison["before"]["primary_action"] == "scale_workers"
    assert comparison["after"]["primary_action"] == "throttle_retries"
    assert comparison["context"] == {
        "prompt_equal": True,
        "normalized_telemetry_equal": True,
    }
    assert comparison["memory_correction_proven"] is True
    assert comparison["controlled_pair"] is True

    same_action_with_paraphrased_rationale = run(
        "decision-after-same-action",
        "memory-v3",
        "scale_workers",
        "2026-08-13T10:02:00Z",
        rationale="The current telemetry supports the selected catalog action.",
    )
    unchanged = _action_comparison(
        rejected=rejected,
        corrected=same_action_with_paraphrased_rationale,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert unchanged["status"] == "unchanged"
    assert unchanged["before"]["fingerprint"] == unchanged["after"]["fingerprint"]
    proof_states = _causal_proof_states(
        rejected=rejected,
        corrected=corrected,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert proof_states == {
        "memory_correction_proven": {
            "status": "proven",
            "reason": "rewind_lineage_and_reads_verified",
        },
        "action_delta_proven": {"status": "proven", "reason": "catalog_action_changed"},
        "controlled_pair_eligible": {
            "status": "proven",
            "reason": "fixed_context_and_memory_delta_verified",
        },
        "repeatable_causal_effect_supported": {
            "status": "unavailable",
            "reason": "repeated_trials_not_measured",
        },
        "service_recovery_proven": {
            "status": "unavailable",
            "reason": "service_recovery_not_measured",
        },
    }
    assert all(
        check["status"] == "matched"
        for check in _controlled_pair_checks(
            rejected=rejected,
            corrected=corrected,
            operation=operation,
            operation_effects=effects,
            memory_correction_proven=True,
        )
    )

    different_prompt = run(
        "decision-after-changed",
        "memory-v3",
        "throttle_retries",
        "2026-08-13T10:02:00Z",
        report="A changed report",
    )
    not_controlled = _action_comparison(
        rejected=rejected,
        corrected=different_prompt,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert not_controlled["status"] == "changed"
    assert not_controlled["controlled_pair"] is False

    changed_early_telemetry = run(
        "decision-after-telemetry-change",
        "memory-v3",
        "throttle_retries",
        "2026-08-13T10:03:00Z",
    )
    telemetry_mismatch = _action_comparison(
        rejected=rejected,
        corrected=changed_early_telemetry,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert telemetry_mismatch["context"]["normalized_telemetry_equal"] is True
    assert telemetry_mismatch["controlled_pair"] is False

    expected_mismatch_reasons = {
        "timestamp": "invariant_inputs_ordered_observations_mismatch",
        "early_value": "invariant_inputs_ordered_observations_mismatch",
        "dimensions": "invariant_inputs_ordered_observations_mismatch",
        "region": "invariant_inputs_ordered_observations_mismatch",
        "unit": "invariant_inputs_ordered_observations_mismatch",
        "statistic": "invariant_inputs_ordered_observations_mismatch",
        "period": "invariant_inputs_ordered_observations_mismatch",
        "query_fingerprint": "invariant_inputs_ordered_observations_mismatch",
        "truncated": "invariant_inputs_ordered_observations_mismatch",
        "window": "invariant_inputs_ordered_observations_mismatch",
        "provider": "invariant_inputs_ordered_model_request_configuration_mismatch",
        "model": "invariant_inputs_ordered_model_request_configuration_mismatch",
        "prompt_template": "invariant_inputs_prompt_templates_mismatch",
        "prompt_suffix": "invariant_inputs_ordered_model_request_configuration_mismatch",
        "tenant": "invariant_inputs_tenant_id_mismatch",
        "policy": "invariant_inputs_retrieval_policy_mismatch",
        "embedding": "invariant_inputs_embedding_profile_mismatch",
        "release": "identity_release_revision_mismatch",
    }
    for mismatch_case, expected_reason in expected_mismatch_reasons.items():
        mismatch_proof = _causal_proof_states(
            rejected=rejected,
            corrected=validly_mutated(corrected, mismatch_case),
            operation=operation,
            operation_effects=effects,
            memories=[seed, stale, restored],
            seed=seed,
            compromised=stale,
        )
        assert mismatch_proof["controlled_pair_eligible"] == {
            "status": "not_proven",
            "reason": expected_reason,
        }

    for invalid_request_case in (
        "action_schema",
        "selection_schema",
        "selection_only",
        "turn_gap",
    ):
        invalid_request_proof = _causal_proof_states(
            rejected=rejected,
            corrected=validly_mutated(corrected, invalid_request_case),
            operation=operation,
            operation_effects=effects,
            memories=[seed, stale, restored],
            seed=seed,
            compromised=stale,
        )
        assert invalid_request_proof["controlled_pair_eligible"] == {
            "status": "unavailable",
            "reason": "causal_envelope_incomplete_or_invalid",
        }

    invalid_query_contract = _causal_proof_states(
        rejected=rejected,
        corrected=validly_mutated(corrected, "query_config"),
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert invalid_query_contract["controlled_pair_eligible"] == {
        "status": "unavailable",
        "reason": "causal_envelope_incomplete_or_invalid",
    }

    operation_target_proof = _causal_proof_states(
        rejected=rejected,
        corrected=validly_mutated(corrected, "operation_target"),
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert operation_target_proof["controlled_pair_eligible"]["status"] == "not_proven"

    unrelated_memory_proof = _causal_proof_states(
        rejected=rejected,
        corrected=validly_mutated(corrected, "unrelated_memory"),
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert unrelated_memory_proof["controlled_pair_eligible"] == {
        "status": "unavailable",
        "reason": "causal_envelope_incomplete_or_invalid",
    }

    missing_lineage = deepcopy(corrected)
    missing_lineage["trace"]["reads"][0]["incoming_lineage_edge_ids"] = []
    invalid_at_read = deepcopy(corrected)
    invalid_at_read["trace"]["reads"][0]["t_invalid"] = "2026-08-13T10:04:30Z"
    for invalid_lineage in (missing_lineage, invalid_at_read):
        invalid_proof = _causal_proof_states(
            rejected=rejected,
            corrected=invalid_lineage,
            operation=operation,
            operation_effects=effects,
            memories=[seed, stale, restored],
            seed=seed,
            compromised=stale,
        )
        assert invalid_proof["memory_correction_proven"]["status"] == "not_proven"
        assert invalid_proof["controlled_pair_eligible"]["status"] == "not_proven"

    tampered = deepcopy(corrected)
    tampered["action_trace"]["recommendation"]["operational_action"]["fingerprint"] = (
        "operational_action:tampered"
    )
    unavailable = _action_comparison(
        rejected=rejected,
        corrected=tampered,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["controlled_pair"] is False

    for field, value in (
        ("summary", "Scale payment workers."),
        ("rationale", "Tampered explanatory prose."),
    ):
        tampered_prose = deepcopy(corrected)
        tampered_prose["action_trace"]["recommendation"][field] = value
        prose_comparison = _action_comparison(
            rejected=rejected,
            corrected=tampered_prose,
            operation=operation,
            operation_effects=effects,
            memories=[seed, stale, restored],
            seed=seed,
            compromised=stale,
        )
        assert prose_comparison["status"] == "unavailable"
        assert prose_comparison["controlled_pair"] is False

    legacy = deepcopy(corrected)
    legacy["action_trace"].pop("causal_envelope")
    legacy_proof = _causal_proof_states(
        rejected=rejected,
        corrected=legacy,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert legacy_proof["controlled_pair_eligible"] == {
        "status": "unavailable",
        "reason": "causal_envelope_incomplete_or_invalid",
    }


@requires_db
def test_decision_trace_exposes_retrieval_profile_version_evidence_and_lineage():
    from hindsight.db import database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.trace_contract import decision_influence, governed_decision_trace

    namespace = f"trace-contract:{uuid4()}"
    decision_id = f"trace-decision:{uuid4()}"
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        source = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeouts caused retry fanout",
            provenance=Provenance(
                "pytest.trace",
                "trace:source",
                "Seed a governed source memory",
            ),
        )
        second_source = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeouts caused retry fanout in payments",
            provenance=Provenance(
                "pytest.trace",
                "trace:second-source",
                "Seed a second governed source memory",
            ),
        )
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query="processor timeouts caused retry fanout",
            decision_id=decision_id,
            reader="pytest.trace",
            purpose="Build an inspectable decision trace",
            limit=2,
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="throttle retry fanout while processor timeouts remain high",
            provenance=Provenance(
                "pytest.trace",
                "trace:child",
                "Derive a child memory from the retrieved source",
            ),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(source["id"]), str(second_source["id"])],
        )
        store.invalidate(
            memory_id=str(source["id"]),
            actor="pytest.trace",
            reason="Exercise invalidated trace rendering",
        )

    trace = governed_decision_trace(decision_id=decision_id, db_url=database_url())

    assert trace is not None
    assert trace["decision"]["id"] == decision_id
    assert str(trace["retrievals"][0]["id"]) == retrieval.retrieval_id
    assert trace["retrievals"][0]["embedding_profile_id"]
    assert trace["retrievals"][0]["embedding_provider"] == "test_deterministic"
    read = trace["reads"][0]
    assert str(read["memory_id"]) == str(source["id"])
    assert read["belief_id"] == source["belief_id"]
    assert read["version_number"] == source["version_number"]
    assert read["embedding_profile_id"] == trace["retrievals"][0]["embedding_profile_id"]
    assert read["memory_producer_decision_id"] == source["producer_decision_id"]
    assert read["memory_status"] == "invalidated"
    assert read["t_invalid"] is not None
    assert read["evidence_ids"]
    assert read["outgoing_lineage_edge_ids"]
    assert len(trace["lineage_edges"]) == 2
    assert {str(edge["child_semantic_memory_id"]) for edge in trace["lineage_edges"]} == {
        str(child["id"])
    }
    assert {edge["producer_decision_id"] for edge in trace["lineage_edges"]} == {decision_id}
    assert len({edge["created_at"] for edge in trace["lineage_edges"]}) == 1
    lineage_ids = [str(edge["id"]) for edge in trace["lineage_edges"]]
    assert lineage_ids == sorted(lineage_ids)

    repeated = governed_decision_trace(decision_id=decision_id, db_url=database_url())
    assert repeated is not None
    assert [str(edge["id"]) for edge in repeated["lineage_edges"]] == lineage_ids

    direct = decision_influence(decision_id=decision_id, db_url=database_url())
    assert direct["decision_id"] == decision_id
    assert direct["count"] == 2
    assert {row["memory"]["content"] for row in direct["memories"]} == {
        source["content"],
        second_source["content"],
    }
    assert direct["trace"]["reads"][0]["belief_id"] == source["belief_id"]

    from hindsight import api

    influence = api.decisions_influence(decision_id)
    assert influence["decision_id"] == decision_id
    assert influence["count"] == 2
    assert {row["memory"]["content"] for row in influence["memories"]} == {
        source["content"],
        second_source["content"],
    }
    assert influence["decision"]["id"] == decision_id
    assert influence["retrievals"][0]["id"] == retrieval.retrieval_id
    assert influence["trace"]["reads"][0]["belief_id"] == str(source["belief_id"])
    assert [str(edge["id"]) for edge in influence["trace"]["lineage_edges"]] == lineage_ids

    from fastapi.testclient import TestClient

    response = TestClient(api.app).get(f"/v1/decisions/{decision_id}/influence")
    assert response.status_code == 200
    assert [edge["id"] for edge in response.json()["trace"]["lineage_edges"]] == lineage_ids


@requires_db
def test_explicit_signature_scenario_returns_partial_identity_state():
    from hindsight.db import database_url
    from hindsight.demo_state import (
        ensure_poison_rewind_incident,
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
    )
    from hindsight.trace_contract import signature_scenario_evidence, signature_scenario_trace

    fixture_id = uuid4()
    incident = ensure_poison_rewind_incident(
        fixture_id=fixture_id,
        db_url=database_url(),
    )
    namespace = reset_poison_rewind_state(
        namespace=f"partial-signature:{uuid4()}",
        session_id=fixture_id,
        incident_id=fixture_id,
        db_url=database_url(),
    )
    rewind_anchor = record_poison_rewind_anchor(
        namespace=namespace,
        db_url=database_url(),
    )

    scenario = signature_scenario_trace(namespace=namespace, db_url=database_url())

    assert scenario is not None
    assert scenario["scenario_id"] == fixture_id
    assert scenario["namespace"] == namespace
    assert scenario["status"] == "active"
    assert scenario["session_status"] == "active"
    assert scenario["completed_at"] is None
    assert scenario["rewind_anchor"] == rewind_anchor
    assert scenario["incident"]["id"] == fixture_id
    assert scenario["incident"]["slug"] == incident["slug"]
    assert scenario["incident"]["service_slug"] == "payments-api"
    assert scenario["runs"] == []
    assert scenario["operation"] is None
    assert scenario["stages"] == {
        "baseline_memory_id": None,
        "compromised_memory_id": None,
        "poison_memory_id": None,
        "influenced_decision_id": None,
        "rewind_operation_id": None,
        "corrected_decision_id": None,
    }
    assert {state["status"] for state in scenario["causal_evidence"]["proof_states"].values()} == {
        "unavailable"
    }
    evidence = signature_scenario_evidence(
        scenario_id=str(fixture_id),
        db_url=database_url(),
    )
    assert evidence is not None
    from hindsight.causal_evidence import canonical_sha256

    assert canonical_sha256(evidence) == scenario["causal_evidence"]["download"]["sha256"]
    assert evidence["scope"] == "recommendation_only"


@requires_db
def test_signature_scenario_resolves_by_scenario_and_decision_identity():
    from hindsight.db import connect, database_url
    from hindsight.demo_state import (
        DEMO_INPUT,
        DEMO_NAMESPACE,
        ensure_poison_rewind_incident,
        poison_demo_memory,
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
        seed_good_demo_memory,
    )
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind
    from hindsight.runs import create_run
    from hindsight.trace_contract import signature_scenario_trace
    from psycopg.types.json import Jsonb

    provider = DeterministicEmbeddingProvider()
    fixture_id = uuid4()
    incident = ensure_poison_rewind_incident(
        fixture_id=fixture_id,
        db_url=database_url(),
    )
    namespace = reset_poison_rewind_state(
        namespace=DEMO_NAMESPACE,
        session_id=fixture_id,
        incident_id=fixture_id,
        db_url=database_url(),
    )
    seed = seed_good_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    rewind_anchor = record_poison_rewind_anchor(
        namespace=namespace,
        db_url=database_url(),
    )
    poison = poison_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    bad, _ = create_run(
        incident_slug=incident["slug"],
        namespace=namespace,
        user_input="poisoned run",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        bad_retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=DEMO_INPUT,
            decision_id=bad["decision_id"],
            reader="pytest.trace",
            purpose="Record the stale guidance that shaped the unsafe decision",
            positive_guidance_only=True,
        )
    assert str(poison["id"]) in {str(row["id"]) for row in bad_retrieval.hits}
    with connect(database_url()) as conn:
        with conn.transaction():
            conn.execute(
                """
                    UPDATE agent_runs
                    SET status = 'rejected', plan = 'rotate certificates',
                        action_approved = false, completed_at = now()
                    WHERE id = %s
                """,
                (bad["id"],),
            )
            conn.execute(
                """
                    INSERT INTO agent_run_events (
                        run_id, sequence, phase, status, summary, metadata
                    )
                    SELECT %s, COALESCE(max(sequence), 0) + 1,
                           'completion', 'rejected', 'Operator rejected recommendation', %s
                    FROM agent_run_events WHERE run_id = %s
                """,
                (
                    bad["id"],
                    Jsonb(
                        {
                            "action_trace": {
                                "mode": "recommendation_only",
                                "approval": {"approved": False, "disposition": "rejected"},
                                "execution": {"status": "not_executed"},
                            }
                        }
                    ),
                    bad["id"],
                ),
            )
    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=rewind_anchor,
        actor="pytest.trace",
        reason="Restore the accepted belief version",
        db_url=database_url(),
    )
    queued, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=str(preview["fingerprint"]),
        idempotency_key=f"trace:{uuid4()}",
        db_url=database_url(),
    )
    completed_operation = execute_operation(
        operation_id=str(queued["id"]),
        embedding_provider=provider,
        worker_id="pytest.trace",
        db_url=database_url(),
    )
    operation = completed_operation["id"]
    corrected, _ = create_run(
        incident_slug=incident["slug"],
        namespace=namespace,
        user_input="corrected run",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        corrected_retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=DEMO_INPUT,
            decision_id=corrected["decision_id"],
            reader="pytest.trace",
            purpose="Record the corrected decision after rewind",
            positive_guidance_only=True,
        )
    assert str(poison["id"]) not in {str(row["id"]) for row in corrected_retrieval.hits}
    with connect(database_url()) as conn:
        with conn.transaction():
            conn.execute(
                """
                    UPDATE agent_runs
                    SET status = 'completed', plan = 'throttle retry fanout',
                        action_approved = true, completed_at = now()
                    WHERE id = %s
                """,
                (corrected["id"],),
            )
            conn.execute(
                """
                    INSERT INTO agent_run_events (
                        run_id, sequence, phase, status, summary, metadata
                    )
                    SELECT %s, COALESCE(max(sequence), 0) + 1,
                           'completion', 'completed', 'Recommendation approved', %s
                    FROM agent_run_events WHERE run_id = %s
                """,
                (
                    corrected["id"],
                    Jsonb(
                        {
                            "action_trace": {
                                "mode": "recommendation_only",
                                "approval": {"approved": True, "disposition": "approved"},
                                "execution": {"status": "recommendation_approved"},
                            }
                        }
                    ),
                    corrected["id"],
                ),
            )

    validation_namespace = reset_poison_rewind_state(
        namespace=f"{DEMO_NAMESPACE}:session:{uuid4().hex}",
        incident_id=fixture_id,
        db_url=database_url(),
    )
    validation_bad, _ = create_run(
        incident_slug=incident["slug"],
        namespace=validation_namespace,
        user_input="validation fixture rejected run",
        db_url=database_url(),
    )
    validation_corrected, _ = create_run(
        incident_slug=incident["slug"],
        namespace=validation_namespace,
        user_input="validation fixture corrected run",
        db_url=database_url(),
    )
    with connect(database_url()) as conn:
        with conn.transaction():
            conn.execute(
                """
                    UPDATE agent_runs SET status = 'rejected', completed_at = now()
                    WHERE id = %s
                """,
                (validation_bad["id"],),
            )
            conn.execute(
                """
                    UPDATE agent_runs
                    SET status = 'completed', action_approved = true,
                        completed_at = now()
                    WHERE id = %s
                """,
                (validation_corrected["id"],),
            )
            conn.execute(
                """
                    INSERT INTO memory_operations (
                        operation_type, actor, reason, namespace,
                        invalidated_memory_ids, restored_memory_ids,
                        idempotency_key, status, request_payload,
                        expected_revisions, applied_revisions, attempt_count,
                        completed_at
                    )
                    VALUES (
                        'rewind', 'pytest.trace', 'Validation fixture', %s,
                        '[]'::JSONB, '[]'::JSONB, %s,
                        'completed', '{}'::JSONB,
                        '{}'::JSONB, '{}'::JSONB, 1, now()
                    )
                """,
                (validation_namespace, f"trace:{uuid4()}"),
            )

    default = signature_scenario_trace(db_url=database_url())
    assert default is not None
    assert default["scenario_id"] == fixture_id
    assert default["namespace"] == namespace
    assert default["status"] == "completed"
    assert default["session_status"] == "active"
    assert default["completed_at"] is not None
    assert default["rewind_anchor"] == rewind_anchor
    assert default["incident"]["slug"] == incident["slug"]
    assert default["incident"]["service_slug"] == "payments-api"
    assert default["stages"]["baseline_memory_id"] == seed["id"]
    assert default["stages"]["compromised_memory_id"] == poison["id"]
    assert default["stages"]["poison_memory_id"] == poison["id"]
    assert default["stages"]["influenced_decision_id"] == bad["decision_id"]
    assert default["stages"]["rewind_operation_id"] == operation
    assert default["stages"]["corrected_decision_id"] == corrected["decision_id"]
    bad_trace = next(run for run in default["runs"] if str(run["id"]) == bad["id"])
    corrected_trace = next(run for run in default["runs"] if str(run["id"]) == corrected["id"])
    assert bad_trace["action_trace"]["mode"] == "recommendation_only"
    assert bad_trace["action_trace"]["approval"]["approved"] is False
    assert bad_trace["action_trace"]["execution"]["status"] == "not_executed"
    poison_read = next(
        read for read in bad_trace["trace"]["reads"] if str(read["memory_id"]) == str(poison["id"])
    )
    assert poison_read["writer"] == "demo.fixture-import"
    assert poison_read["source_ref"] == "demo:stale-runbook-import"
    assert "previously approved payment runbook" in poison_read["justification"]
    assert corrected_trace["action_trace"]["mode"] == "recommendation_only"
    assert corrected_trace["action_trace"]["approval"]["approved"] is True
    assert corrected_trace["action_trace"]["execution"]["status"] == "recommendation_approved"
    assert corrected_trace["created_at"] > default["operation"]["completed_at"]
    assert corrected_trace["trace"]["reads"]
    assert all(
        str(read["memory_id"])
        not in {str(value) for value in default["operation"]["invalidated_memory_ids"]}
        for read in corrected_trace["trace"]["reads"]
    )
    assert (
        next(row for row in default["memories"] if row["id"] == poison["id"])["t_invalid"]
        is not None
    )
    by_scenario = signature_scenario_trace(
        scenario_id=str(default["scenario_id"]),
        db_url=database_url(),
    )
    by_decision = signature_scenario_trace(
        decision_id=corrected["decision_id"],
        db_url=database_url(),
    )
    validation_by_decision = signature_scenario_trace(
        decision_id=validation_corrected["decision_id"],
        db_url=database_url(),
    )
    assert by_scenario is not None and by_scenario["namespace"] == namespace
    assert by_decision is not None and by_decision["namespace"] == namespace
    assert validation_by_decision is not None
    assert validation_by_decision["namespace"] == validation_namespace
    assert validation_by_decision["status"] == "active"
    assert validation_by_decision["stages"]["corrected_decision_id"] is None

    from fastapi.testclient import TestClient
    from hindsight.api import app

    client = TestClient(app)
    public = client.get(
        "/v1/signature-scenarios",
        params={"decision_id": corrected["decision_id"]},
    )
    assert public.status_code == 200
    assert public.json()["scenario_id"] == str(default["scenario_id"])
    assert "content" not in public.json()["memories"][0]
    public_poison_read = next(
        read
        for run in public.json()["runs"]
        for read in (run.get("trace") or {}).get("reads", [])
        if read.get("writer") == "demo.fixture-import"
    )
    assert public_poison_read["source_ref"] == "demo:stale-runbook-import"
    assert "previously approved payment runbook" in public_poison_read["justification"]
    deep_link = client.get(f"/v1/signature-scenarios/{default['scenario_id']}")
    assert deep_link.status_code == 200
    assert deep_link.json()["namespace"] == namespace


def test_trace_selectors_are_mutually_exclusive():
    from hindsight.trace_contract import signature_scenario_trace

    with pytest.raises(ValueError, match="only one"):
        signature_scenario_trace(scenario_id="one", decision_id="two")


@requires_db
def test_missing_trace_identities_return_no_trace():
    from hindsight.trace_contract import (
        governed_decision_trace,
        signature_scenario_trace,
    )

    assert signature_scenario_trace(scenario_id="not-a-uuid") is None
    assert governed_decision_trace(decision_id="missing-decision") is None
