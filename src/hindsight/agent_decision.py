"""Strict decision contract for the incident agent."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


AGENT_DECISION_SCHEMA_VERSION = 1
MAX_MODEL_TURNS = 4
MAX_DIAGNOSTIC_CALLS = 3
CLOUDWATCH_DIAGNOSTIC_TOOL = "aws_cloudwatch_diagnostics"


class AgentDecisionError(RuntimeError):
    """Raised when a model response cannot be accepted safely."""


class MemoryCitation(BaseModel):
    """A claim tied to one recalled memory version."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    memory_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(
        min_length=1,
        max_length=2_000,
        description="A verbatim excerpt from the recalled memory version.",
    )


class DiagnosticToolCall(BaseModel):
    """A server-scoped diagnostic query selected by the model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Literal["aws_cloudwatch_diagnostics"]
    query_key: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9_.:-]*$")


class AgentDecisionV1(BaseModel):
    """One bounded reasoning step produced by the configured model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1]
    diagnosis: str = Field(min_length=1, max_length=4_000)
    recalled_memory_citations: list[MemoryCitation] = Field(max_length=8)
    next_step_kind: Literal["diagnostic_tool", "recommendation"]
    tool_call: DiagnosticToolCall | None
    recommendation: str | None = Field(max_length=4_000)
    rationale: str = Field(min_length=1, max_length=4_000)
    rollback: str = Field(min_length=1, max_length=2_000)
    verification: list[str] = Field(min_length=1, max_length=8)
    safety_constraints: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_next_step(self) -> AgentDecisionV1:
        if any(not item.strip() for item in self.verification):
            raise ValueError("verification entries must not be empty")
        if any(not item.strip() for item in self.safety_constraints):
            raise ValueError("safety constraint entries must not be empty")
        if self.next_step_kind == "diagnostic_tool":
            if self.tool_call is None or self.recommendation is not None:
                raise ValueError("diagnostic_tool requires tool_call and forbids recommendation")
        elif self.tool_call is not None or not (self.recommendation or "").strip():
            raise ValueError("recommendation requires recommendation text and forbids tool_call")
        return self


AGENT_DECISION_JSON_SCHEMA: dict[str, Any] = AgentDecisionV1.model_json_schema()


def parse_agent_decision(
    text: str,
    *,
    recalled_memory_ids: set[str],
    recalled_memory_text: Mapping[str, str] | None = None,
    allowed_query_keys: set[str],
    diagnostic_calls_used: int,
    diagnostic_observation_available: bool,
    model_turn: int,
) -> AgentDecisionV1:
    """Parse and enforce constraints that cannot be represented in JSON Schema."""

    try:
        payload = json.loads(text)
        decision = AgentDecisionV1.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise AgentDecisionError("model response did not satisfy AgentDecisionV1") from exc

    cited_ids = [citation.memory_id for citation in decision.recalled_memory_citations]
    if len(cited_ids) != len(set(cited_ids)):
        raise AgentDecisionError("a recalled memory may be cited only once per decision")
    unknown_citations = set(cited_ids) - recalled_memory_ids
    if unknown_citations:
        raise AgentDecisionError("model cited memory that was not recalled")
    if recalled_memory_text is not None:
        for citation in decision.recalled_memory_citations:
            source = recalled_memory_text.get(citation.memory_id, "")
            if _normalized_text(citation.quote) not in _normalized_text(source):
                raise AgentDecisionError("model citation is not a quote from recalled memory")

    if decision.next_step_kind == "diagnostic_tool":
        assert decision.tool_call is not None
        if model_turn >= MAX_MODEL_TURNS:
            raise AgentDecisionError("final model turn must produce a recommendation")
        if diagnostic_calls_used >= MAX_DIAGNOSTIC_CALLS:
            raise AgentDecisionError("diagnostic call budget is exhausted")
        if decision.tool_call.query_key not in allowed_query_keys:
            raise AgentDecisionError("model selected a diagnostic query outside the allowlist")
    elif allowed_query_keys and not diagnostic_observation_available:
        raise AgentDecisionError(
            "a current diagnostic observation is required before recommendation"
        )

    return decision


def memory_selection_fingerprint(memories: list[dict[str, Any]]) -> str:
    """Hash the ordered governed memory versions that influenced a decision."""

    selection = []
    for memory in memories:
        metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
        selection.append(
            {
                "memory_id": str(memory.get("memory_id") or memory.get("id") or ""),
                "belief_id": str(memory.get("belief_id") or ""),
                "version_number": memory.get("version_number"),
                "trust_status": str(memory.get("trust_status") or ""),
                "t_valid": _json_value(memory.get("t_valid")),
                "t_invalid": _json_value(memory.get("t_invalid")),
                "profile_id": str(
                    memory.get("embedding_profile_id") or memory.get("profile_id") or ""
                ),
                "operator_disposition": str(metadata.get("operator_disposition") or ""),
                "safety_status": str(metadata.get("safety_status") or ""),
                "contradiction_status": str(metadata.get("contradiction_status") or ""),
                "usage_instruction": str(metadata.get("usage_instruction") or ""),
            }
        )
    return _digest(selection)


def recommendation_id(
    *,
    run_id: str,
    decision: AgentDecisionV1,
    selection_fingerprint: str,
) -> str:
    """Return a stable content identity for an approval-bound recommendation."""

    digest = _digest(
        {
            "run_id": run_id,
            "decision": decision.model_dump(mode="json"),
            "selection_fingerprint": selection_fingerprint,
        }
    )
    return f"recommendation:{digest}"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _json_value(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())
