"""Strict decision contract for the incident agent."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


AGENT_DECISION_SCHEMA_VERSION = 2
MAX_MODEL_TURNS = 4
MAX_DIAGNOSTIC_CALLS = 3
CLOUDWATCH_DIAGNOSTIC_TOOL = "aws_cloudwatch_diagnostics"
MIN_CITATION_QUOTE_LENGTH = 12


class AgentDecisionError(RuntimeError):
    """Raised when a model response cannot be accepted safely."""


class LegacyMemoryCitation(BaseModel):
    """A citation accepted from a durable AgentDecisionV1 checkpoint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    memory_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=2_000)


class MemoryCitation(LegacyMemoryCitation):
    """A claim tied to one recalled memory version."""

    quote: str = Field(
        min_length=MIN_CITATION_QUOTE_LENGTH,
        max_length=2_000,
        description="A meaningful verbatim excerpt from the recalled memory version.",
    )

    @field_validator("quote")
    @classmethod
    def validate_meaningful_quote(cls, value: str) -> str:
        if len(" ".join(value.split())) < MIN_CITATION_QUOTE_LENGTH:
            raise ValueError("citation quote is too short after whitespace normalization")
        return value


class DiagnosticToolCall(BaseModel):
    """A server-scoped diagnostic query selected by the model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Literal["aws_cloudwatch_diagnostics"]
    query_key: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9_.:-]*$")


class RetractRecalledMemoryAction(BaseModel):
    """The only model-selected mutation supported by the incident agent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Literal["retract_recalled_memory"]
    target_memory_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class AgentDecisionV1(BaseModel):
    """Legacy recommendation decision retained for checkpoint compatibility."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1]
    diagnosis: str = Field(min_length=1, max_length=4_000)
    recalled_memory_citations: list[LegacyMemoryCitation] = Field(max_length=8)
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


class AgentDecisionV2(BaseModel):
    """One bounded, mutually exclusive reasoning step from the configured model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[2]
    diagnosis: str = Field(min_length=1, max_length=4_000)
    recalled_memory_citations: list[MemoryCitation] = Field(max_length=8)
    next_step_kind: Literal["diagnostic_tool", "recommendation", "remediation_action"]
    tool_call: DiagnosticToolCall | None
    recommendation: str | None = Field(max_length=4_000)
    remediation_action: RetractRecalledMemoryAction | None
    rationale: str = Field(min_length=1, max_length=4_000)
    rollback: str = Field(min_length=1, max_length=2_000)
    verification: list[str] = Field(min_length=1, max_length=8)
    safety_constraints: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_next_step(self) -> AgentDecisionV2:
        if any(not item.strip() for item in self.verification):
            raise ValueError("verification entries must not be empty")
        if any(not item.strip() for item in self.safety_constraints):
            raise ValueError("safety constraint entries must not be empty")
        if self.next_step_kind == "diagnostic_tool":
            if (
                self.tool_call is None
                or self.recommendation is not None
                or self.remediation_action is not None
            ):
                raise ValueError(
                    "diagnostic_tool requires tool_call and forbids terminal decisions"
                )
        elif self.next_step_kind == "recommendation":
            if (
                self.tool_call is not None
                or not (self.recommendation or "").strip()
                or self.remediation_action is not None
            ):
                raise ValueError(
                    "recommendation requires recommendation text and forbids other branches"
                )
        elif (
            self.tool_call is not None
            or self.recommendation is not None
            or self.remediation_action is None
        ):
            raise ValueError("remediation_action requires one action and forbids other branches")
        return self


AGENT_DECISION_JSON_SCHEMA: dict[str, Any] = AgentDecisionV2.model_json_schema()
_PROVIDER_BRANCH_FIELDS = ("tool_call", "recommendation", "remediation_action")


def agent_decision_provider_schema(
    *,
    recalled_memory_ids: set[str],
    allowed_query_keys: set[str],
    diagnostic_calls_used: int,
    diagnostic_observation_available: bool,
    model_turn: int,
) -> dict[str, Any]:
    """Narrow the provider schema to the decision branch allowed for this turn."""

    if type(model_turn) is not int or not 1 <= model_turn <= MAX_MODEL_TURNS:
        raise ValueError(f"model_turn must be between one and {MAX_MODEL_TURNS}")
    if (
        type(diagnostic_calls_used) is not int
        or not 0 <= diagnostic_calls_used <= MAX_DIAGNOSTIC_CALLS
    ):
        raise ValueError(f"diagnostic_calls_used must be between zero and {MAX_DIAGNOSTIC_CALLS}")

    schema = deepcopy(AGENT_DECISION_JSON_SCHEMA)
    schema.pop("anyOf", None)
    properties = schema["properties"]
    definitions = schema["$defs"]
    required = schema["required"]

    def omit_field(name: str) -> None:
        properties.pop(name, None)
        if name in required:
            required.remove(name)

    def require_field(name: str, field_schema: dict[str, Any]) -> None:
        properties[name] = field_schema
        if name not in required:
            required.append(name)

    def allow_optional_field(name: str, field_schema: dict[str, Any]) -> None:
        properties[name] = field_schema
        if name in required:
            required.remove(name)

    recalled_ids = sorted(recalled_memory_ids)
    citations = properties["recalled_memory_citations"]
    if recalled_ids:
        citations["maxItems"] = min(int(citations["maxItems"]), len(recalled_ids))
        definitions["MemoryCitation"]["properties"]["memory_id"]["enum"] = recalled_ids
    else:
        citations["maxItems"] = 0

    query_keys = sorted(allowed_query_keys)
    if query_keys:
        definitions["DiagnosticToolCall"]["properties"]["query_key"]["enum"] = query_keys
    diagnostic_available = (
        bool(query_keys)
        and diagnostic_calls_used < MAX_DIAGNOSTIC_CALLS
        and model_turn < MAX_MODEL_TURNS
    )
    action_available = bool(recalled_ids) and (not query_keys or diagnostic_observation_available)
    if action_available:
        definitions["RetractRecalledMemoryAction"]["properties"]["target_memory_id"]["enum"] = (
            recalled_ids
        )

    if diagnostic_available and not diagnostic_observation_available:
        properties["next_step_kind"]["enum"] = ["diagnostic_tool"]
        require_field("tool_call", {"$ref": "#/$defs/DiagnosticToolCall"})
        omit_field("recommendation")
        omit_field("remediation_action")
    elif diagnostic_available and diagnostic_observation_available:
        properties["next_step_kind"]["enum"] = [
            "diagnostic_tool",
            "recommendation",
            *(["remediation_action"] if action_available else []),
        ]
        allow_optional_field("tool_call", {"$ref": "#/$defs/DiagnosticToolCall"})
        allow_optional_field(
            "recommendation",
            {"type": "string", "minLength": 1, "maxLength": 4_000},
        )
        if action_available:
            allow_optional_field(
                "remediation_action",
                {"$ref": "#/$defs/RetractRecalledMemoryAction"},
            )
        else:
            omit_field("remediation_action")
    else:
        properties["next_step_kind"]["enum"] = [
            "recommendation",
            *(["remediation_action"] if action_available else []),
        ]
        omit_field("tool_call")
        if action_available:
            allow_optional_field(
                "recommendation",
                {"type": "string", "minLength": 1, "maxLength": 4_000},
            )
            allow_optional_field(
                "remediation_action",
                {"$ref": "#/$defs/RetractRecalledMemoryAction"},
            )
        else:
            require_field(
                "recommendation",
                {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4_000,
                },
            )
            omit_field("remediation_action")
    _replace_provider_consts(schema)
    return schema


def _replace_provider_consts(value: Any) -> None:
    """Use the literal form supported by Gemini structured outputs."""

    if isinstance(value, dict):
        if "const" in value:
            value["enum"] = [value.pop("const")]
        for nested in value.values():
            _replace_provider_consts(nested)
    elif isinstance(value, list):
        for nested in value:
            _replace_provider_consts(nested)


def normalize_agent_decision_provider_text(text: str) -> str:
    """Restore omitted branch siblings before the strict decision parser runs."""

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(payload, dict):
        return text
    normalized = dict(payload)
    for field in _PROVIDER_BRANCH_FIELDS:
        normalized.setdefault(field, None)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def parse_agent_decision(
    text: str,
    *,
    recalled_memory_ids: set[str],
    recalled_memory_text: Mapping[str, str] | None = None,
    allowed_query_keys: set[str],
    diagnostic_calls_used: int,
    diagnostic_observation_available: bool,
    model_turn: int,
) -> AgentDecisionV2:
    """Parse and enforce constraints that cannot be represented in JSON Schema."""

    try:
        payload = json.loads(text)
        decision = AgentDecisionV2.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise AgentDecisionError("model response did not satisfy AgentDecisionV2") from exc

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
            raise AgentDecisionError("final model turn must produce a terminal decision")
        if diagnostic_calls_used >= MAX_DIAGNOSTIC_CALLS:
            raise AgentDecisionError("diagnostic call budget is exhausted")
        if decision.tool_call.query_key not in allowed_query_keys:
            raise AgentDecisionError("model selected a diagnostic query outside the allowlist")
    elif allowed_query_keys and not diagnostic_observation_available:
        raise AgentDecisionError(
            "a current diagnostic observation is required before a terminal decision"
        )
    if decision.next_step_kind == "remediation_action":
        assert decision.remediation_action is not None
        cited = {citation.memory_id for citation in decision.recalled_memory_citations}
        if decision.remediation_action.target_memory_id not in cited:
            raise AgentDecisionError("remediation target must be cited verbatim")

    return decision


def agent_decision_from_payload(payload: Mapping[str, Any]) -> AgentDecisionV2:
    """Load current decisions while preserving resumability of V1 checkpoints."""

    if payload.get("schema_version") == 2:
        return AgentDecisionV2.model_validate(payload)
    legacy = AgentDecisionV1.model_validate(payload)
    # The V1 payload is already validated against its durable contract. Construct
    # only the nested V2 citation adapters so legacy short quotes remain resumable.
    citations = [
        MemoryCitation.model_construct(memory_id=item.memory_id, quote=item.quote)
        for item in legacy.recalled_memory_citations
    ]
    return AgentDecisionV2.model_construct(
        schema_version=2,
        diagnosis=legacy.diagnosis,
        recalled_memory_citations=citations,
        next_step_kind=legacy.next_step_kind,
        tool_call=legacy.tool_call,
        recommendation=legacy.recommendation,
        remediation_action=None,
        rationale=legacy.rationale,
        rollback=legacy.rollback,
        verification=legacy.verification,
        safety_constraints=legacy.safety_constraints,
    )


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
    decision: AgentDecisionV1 | AgentDecisionV2,
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


def remediation_action_id(
    *,
    run_id: str,
    decision: AgentDecisionV2,
    selection_fingerprint: str,
    observation_fingerprint: str,
) -> str:
    """Return a stable identity for one approval-bound remediation proposal."""

    digest = _digest(
        {
            "run_id": run_id,
            "decision": decision.model_dump(mode="json"),
            "selection_fingerprint": selection_fingerprint,
            "observation_fingerprint": observation_fingerprint,
        }
    )
    return f"remediation_action:{digest}"


def diagnostic_observation_fingerprint(observations: list[dict[str, Any]]) -> str:
    """Hash the exact diagnostic observations considered by a decision."""

    return _digest(observations)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _json_value(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _normalized_text(value: str) -> str:
    return " ".join(value.split())
