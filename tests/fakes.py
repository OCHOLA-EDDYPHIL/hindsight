"""Deterministic provider and tool fakes that never ship with the runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from hindsight.reasoning import ReasoningRequest, ReasoningResponse

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def recommendation_decision(
    recommendation: str = "Inspect dependency health, then throttle retry fanout.",
    *,
    diagnosis: str = "Retry amplification is saturating the downstream processor.",
    citations: list[dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "diagnosis": diagnosis,
            "recalled_memory_citations": citations or [],
            "next_step_kind": "recommendation",
            "tool_call": None,
            "recommendation": recommendation,
            "remediation_action": None,
            "rationale": "The recommendation is bounded and follows the available evidence.",
            "rollback": "Restore the previous retry policy if the service degrades.",
            "verification": ["Confirm latency and queue depth return to their expected range."],
            "safety_constraints": ["Do not mutate infrastructure from the diagnostic workflow."],
        },
        sort_keys=True,
    )


def diagnostic_decision(query_key: str) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "diagnosis": "Current telemetry is required before recommending a mitigation.",
            "recalled_memory_citations": [],
            "next_step_kind": "diagnostic_tool",
            "tool_call": {
                "name": "aws_cloudwatch_diagnostics",
                "query_key": query_key,
            },
            "recommendation": None,
            "remediation_action": None,
            "rationale": "The configured metric can confirm the suspected failure mode.",
            "rollback": "No rollback is required because the diagnostic is read-only.",
            "verification": ["Use the returned datapoints in the next reasoning turn."],
            "safety_constraints": ["Run only the server-configured metric query."],
        },
        sort_keys=True,
    )


def retraction_decision(
    *,
    memory_id: str,
    quote: str,
    reason: str = "Retract recalled guidance that conflicts with the current observation.",
) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "diagnosis": "The cited guidance is unsafe for the observed incident state.",
            "recalled_memory_citations": [{"memory_id": memory_id, "quote": quote}],
            "next_step_kind": "remediation_action",
            "tool_call": None,
            "recommendation": None,
            "remediation_action": {
                "name": "retract_recalled_memory",
                "target_memory_id": memory_id,
                "reason": reason,
            },
            "rationale": "Removing the governed guidance prevents it from directing later runs.",
            "rollback": "Restore a reviewed replacement through the governed correction workflow.",
            "verification": ["Confirm the targeted version is no longer current."],
            "safety_constraints": ["Retract only the cited memory in this namespace."],
        },
        sort_keys=True,
    )


class DeterministicReasoningProvider:
    """Return one fixed JSON response from a test-only reasoning provider."""

    provider_name = "test_deterministic"
    model_name = "test-scripted-v1"

    def __init__(self, *, response_text: str | None = None):
        self._response_text = response_text or recommendation_decision()
        self.requests: list[ReasoningRequest] = []

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        self.requests.append(request)
        return ReasoningResponse(
            text=self._response_text,
            provider=self.provider_name,
            model=self.model_name,
            usage={
                "prompt_characters": len(request.prompt),
                "system_characters": len(request.system or ""),
            },
        )


class SequencedReasoningProvider(DeterministicReasoningProvider):
    """Return scripted responses in order and fail if the agent over-calls."""

    def __init__(self, responses: list[str]):
        if not responses:
            raise ValueError("at least one response is required")
        super().__init__(response_text=responses[0])
        self._responses = list(responses)

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        if not self._responses:
            raise AssertionError("reasoning provider called beyond its scripted responses")
        self._response_text = self._responses.pop(0)
        return super().generate(request)


class FixtureLessonReasoningProvider(DeterministicReasoningProvider):
    """Synthesize a citation-valid lesson from the consolidation test prompt."""

    provider_name = "test_fixture_lesson"
    model_name = "test-fixture-lesson-v1"

    def generate(self, request: ReasoningRequest) -> ReasoningResponse:
        self.requests.append(request)
        prompt = json.loads(request.prompt)
        evidence = prompt["evidence"]
        event_id = next(key for key in evidence if key.startswith("event:"))
        memory_id = next(key for key in evidence if key.startswith("memory:"))
        event_payload = json.loads(evidence[event_id])
        incident = prompt["incident"]
        lesson = {
            "schema_version": 1,
            "title": f"Lesson from {incident['slug']}",
            "claims": [
                {
                    "kind": "situation",
                    "text": str(incident.get("root_cause") or "Resolved incident pattern"),
                    "citations": [{"evidence_id": memory_id, "quote": evidence[memory_id]}],
                },
                {
                    "kind": "safe_action",
                    "text": str(event_payload["action"]),
                    "citations": [{"evidence_id": event_id, "quote": str(event_payload["action"])}],
                },
                {
                    "kind": "diagnostic_check",
                    "text": str(event_payload["observation"]),
                    "citations": [
                        {
                            "evidence_id": event_id,
                            "quote": str(event_payload["observation"]),
                        }
                    ],
                },
            ],
        }
        return ReasoningResponse(
            text=json.dumps(lesson, sort_keys=True),
            provider=self.provider_name,
            model=self.model_name,
            usage={"prompt_characters": len(request.prompt)},
        )


class DeterministicEmbeddingProvider:
    """Stable lexical vectors for database tests only."""

    provider_name = "test_deterministic"
    model_name = "test-stable-hash-v1"
    capability = "lexical_hash"
    encoder_revision = "test-hashed-unigram-tf-v1"

    def __init__(self, *, dimensions: int = 1024):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            vector[0] = 1.0
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    def embed_document(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


class FakeCloudWatchDiagnostics:
    """Consume the real call budget while returning scripted observations."""

    name = "aws_cloudwatch_diagnostics"

    def __init__(self, observations: dict[str, dict[str, Any] | Exception]):
        self._observations = observations
        self.calls: list[str] = []

    @property
    def query_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._observations))

    def observe(self, query_key: str, *, budget: Any) -> dict[str, Any]:
        budget.consume()
        self.calls.append(query_key)
        observation = self._observations[query_key]
        if isinstance(observation, Exception):
            raise observation
        return dict(observation)
