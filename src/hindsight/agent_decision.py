"""Strict decision contract for the incident agent."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hindsight.causal_evidence import CausalEvidenceError, strict_json_loads


AGENT_DECISION_SCHEMA_VERSION = 2
CONTROLLED_ACTION_DECISION_SCHEMA_VERSION = 3
MAX_MODEL_TURNS = 4
MAX_DIAGNOSTIC_CALLS = 3
CLOUDWATCH_DIAGNOSTIC_TOOL = "aws_cloudwatch_diagnostics"
MIN_CITATION_QUOTE_LENGTH = 12
PAYMENTS_OPERATIONAL_ACTION_CONTRACT = "payments_retry_amplification.v1"
PAYMENTS_OPERATIONAL_ACTION_CATALOG_ID = "payments_retry_amplification.actions.v1"
PrimaryOperationalAction = Literal["scale_workers", "throttle_retries", "inspect_only"]
PAYMENTS_OPERATIONAL_ACTIONS: tuple[PrimaryOperationalAction, ...] = (
    "scale_workers",
    "throttle_retries",
    "inspect_only",
)
_ACTION_DIRECTIVES = {
    "scale_workers": "Scale payment workers.",
    "throttle_retries": "Throttle retry fanout.",
    "inspect_only": "Inspect current telemetry.",
}
CONTROLLED_ACTION_DIAGNOSIS = (
    "The recorded telemetry and governed memory support one catalog classification."
)
CONTROLLED_ACTION_RATIONALE = (
    "This classification is evidence for operator review and executes no operational change."
)
CONTROLLED_ACTION_SELECTION_RATIONALES = (
    "Recorded evidence supports this catalog selection.",
    "The current telemetry supports the selected catalog action.",
    "The recorded evidence is consistent with this catalog classification.",
)
CONTROLLED_ACTION_ROLLBACK = (
    "No rollback is required because this workflow executes no operational change."
)
CONTROLLED_ACTION_VERIFICATION = (
    "Review the recorded telemetry, memory versions, and action fingerprint before acting."
)
CONTROLLED_ACTION_SAFETY_CONSTRAINT = "Treat this as recommendation evidence only."


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


class OperationalActionParameters(BaseModel):
    """The controlled catalog currently permits no free-form action parameters."""

    model_config = ConfigDict(extra="forbid")


class OperationalAction(BaseModel):
    """One model-produced, comparison-safe operator action classification."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    catalog_id: Literal["payments_retry_amplification.actions.v1"]
    contract: Literal["payments_retry_amplification.v1"]
    action_id: PrimaryOperationalAction
    disposition: Literal["recommend"]
    parameters: OperationalActionParameters

    @model_validator(mode="after")
    def validate_server_catalog(self) -> OperationalAction:
        catalog = operational_action_catalog(self.contract)
        if self.catalog_id != catalog["catalog_id"]:
            raise ValueError("operational action catalog does not match its contract")
        if self.action_id not in catalog["actions"]:
            raise ValueError("operational action is not in the server-owned catalog")
        return self


class ControlledActionSelection(BaseModel):
    """The complete model-authored surface for a controlled terminal action."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action_id: PrimaryOperationalAction
    disposition: Literal["recommend"]
    parameters: OperationalActionParameters
    rationale: str = Field(min_length=1, max_length=4_000)


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


class AgentDecisionV3(AgentDecisionV2):
    """A controlled recommendation with an explicit operational action."""

    schema_version: Literal[3]
    operational_action: OperationalAction | None

    @model_validator(mode="after")
    def validate_operational_action(self) -> AgentDecisionV3:
        if self.next_step_kind == "recommendation":
            if self.operational_action is None:
                raise ValueError("recommendation requires one operational action")
        elif self.operational_action is not None:
            raise ValueError("non-recommendation branches forbid operational action")
        return self


AGENT_DECISION_JSON_SCHEMA: dict[str, Any] = AgentDecisionV2.model_json_schema()
CONTROLLED_ACTION_DECISION_JSON_SCHEMA: dict[str, Any] = AgentDecisionV3.model_json_schema()
CONTROLLED_ACTION_SELECTION_JSON_SCHEMA: dict[str, Any] = (
    ControlledActionSelection.model_json_schema()
)
_PROVIDER_BRANCH_FIELDS = ("tool_call", "recommendation", "remediation_action")


def controlled_action_selection_provider_schema(*, contract: str) -> dict[str, Any]:
    """Return the exact four-field schema exposed to a controlled terminal call."""

    catalog = operational_action_catalog(contract)
    schema = deepcopy(CONTROLLED_ACTION_SELECTION_JSON_SCHEMA)
    schema["properties"]["action_id"]["enum"] = list(catalog["actions"])
    schema["properties"]["rationale"]["enum"] = list(
        CONTROLLED_ACTION_SELECTION_RATIONALES
    )
    _replace_provider_consts(schema)
    return schema


def parse_controlled_action_selection(
    text: str,
    *,
    contract: str,
) -> ControlledActionSelection:
    """Parse the narrow model selection and reject contradictory rationale."""

    operational_action_catalog(contract)
    try:
        selection = ControlledActionSelection.model_validate(strict_json_loads(text))
    except (CausalEvidenceError, ValidationError, TypeError) as exc:
        raise AgentDecisionError(
            "model response did not satisfy ControlledActionSelectionV1"
        ) from exc
    _validate_controlled_selection_rationale(selection)
    return selection


def controlled_action_selection_from_payload(
    payload: Mapping[str, Any],
) -> ControlledActionSelection:
    """Validate a persisted four-field controlled selection."""

    selection = ControlledActionSelection.model_validate(payload)
    _validate_controlled_selection_rationale(selection)
    return selection


def canonicalize_operational_action(
    selection: ControlledActionSelection | Mapping[str, Any],
    *,
    contract: str,
) -> OperationalAction:
    """Inject server-owned catalog identity around one model selection."""

    validated = (
        selection
        if isinstance(selection, ControlledActionSelection)
        else controlled_action_selection_from_payload(selection)
    )
    catalog = operational_action_catalog(contract)
    return OperationalAction.model_validate(
        {
            "catalog_id": catalog["catalog_id"],
            "contract": catalog["contract"],
            "action_id": validated.action_id,
            "disposition": validated.disposition,
            "parameters": validated.parameters.model_dump(mode="json"),
        }
    )


def controlled_decision_from_selection(
    selection: ControlledActionSelection | Mapping[str, Any],
    *,
    contract: str,
) -> AgentDecisionV3:
    """Create the canonical internal decision without trusting model directive prose."""

    validated = (
        selection
        if isinstance(selection, ControlledActionSelection)
        else controlled_action_selection_from_payload(selection)
    )
    action = canonicalize_operational_action(validated, contract=contract)
    return AgentDecisionV3(
        schema_version=CONTROLLED_ACTION_DECISION_SCHEMA_VERSION,
        diagnosis=CONTROLLED_ACTION_DIAGNOSIS,
        recalled_memory_citations=[],
        next_step_kind="recommendation",
        tool_call=None,
        recommendation=operational_action_directive(action),
        remediation_action=None,
        operational_action=action,
        rationale=validated.rationale,
        rollback=CONTROLLED_ACTION_ROLLBACK,
        verification=[CONTROLLED_ACTION_VERIFICATION],
        safety_constraints=[CONTROLLED_ACTION_SAFETY_CONSTRAINT],
    )


def controlled_action_selection_from_decision(
    decision: AgentDecisionV3,
) -> ControlledActionSelection:
    """Project the original four model-authored fields from an internal decision."""

    if decision.operational_action is None:
        raise AgentDecisionError("controlled decision omitted its operational action")
    return ControlledActionSelection(
        action_id=decision.operational_action.action_id,
        disposition=decision.operational_action.disposition,
        parameters=decision.operational_action.parameters,
        rationale=decision.rationale,
    )


def agent_decision_provider_schema(
    *,
    recalled_memory_ids: set[str],
    allowed_query_keys: set[str],
    diagnostic_calls_used: int,
    diagnostic_observation_available: bool,
    model_turn: int,
    operational_action_contract: str | None = None,
) -> dict[str, Any]:
    """Narrow the provider schema to the decision branch allowed for this turn."""

    if type(model_turn) is not int or not 1 <= model_turn <= MAX_MODEL_TURNS:
        raise ValueError(f"model_turn must be between one and {MAX_MODEL_TURNS}")
    if (
        type(diagnostic_calls_used) is not int
        or not 0 <= diagnostic_calls_used <= MAX_DIAGNOSTIC_CALLS
    ):
        raise ValueError(f"diagnostic_calls_used must be between zero and {MAX_DIAGNOSTIC_CALLS}")

    if operational_action_contract not in {None, PAYMENTS_OPERATIONAL_ACTION_CONTRACT}:
        raise ValueError("unsupported operational action contract")
    controlled_action = operational_action_contract is not None
    if controlled_action and (
        not allowed_query_keys
        or diagnostic_observation_available
        or diagnostic_calls_used >= MAX_DIAGNOSTIC_CALLS
        or model_turn >= MAX_MODEL_TURNS
    ):
        raise ValueError(
            "controlled AgentDecisionV3 is diagnostic-only; use ControlledActionSelectionV1"
        )
    schema = deepcopy(
        CONTROLLED_ACTION_DECISION_JSON_SCHEMA if controlled_action else AGENT_DECISION_JSON_SCHEMA
    )
    schema.pop("anyOf", None)
    properties = schema["properties"]
    definitions = schema["$defs"]
    required = schema["required"]

    controlled_recommendations: list[str] | None = None
    if controlled_action:
        catalog = operational_action_catalog(operational_action_contract)
        action_properties = definitions["OperationalAction"]["properties"]
        action_properties["catalog_id"]["enum"] = [catalog["catalog_id"]]
        action_properties["contract"]["enum"] = [catalog["contract"]]
        action_properties["action_id"]["enum"] = list(catalog["actions"])
        controlled_recommendations = list(catalog["directives"].values())
        properties["diagnosis"]["enum"] = [CONTROLLED_ACTION_DIAGNOSIS]
        properties["rationale"]["enum"] = [CONTROLLED_ACTION_RATIONALE]
        properties["rollback"]["enum"] = [CONTROLLED_ACTION_ROLLBACK]
        properties["verification"].update(
            {
                "minItems": 1,
                "maxItems": 1,
                "items": {"type": "string", "enum": [CONTROLLED_ACTION_VERIFICATION]},
            }
        )
        properties["safety_constraints"].update(
            {
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "string",
                    "enum": [CONTROLLED_ACTION_SAFETY_CONSTRAINT],
                },
            }
        )

    def recommendation_schema() -> dict[str, Any]:
        field_schema: dict[str, Any] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 4_000,
        }
        if controlled_recommendations is not None:
            field_schema["enum"] = controlled_recommendations
        return field_schema

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
    action_available = (
        not controlled_action
        and bool(recalled_ids)
        and (not query_keys or diagnostic_observation_available)
    )
    if action_available:
        definitions["RetractRecalledMemoryAction"]["properties"]["target_memory_id"]["enum"] = (
            recalled_ids
        )

    if diagnostic_available and not diagnostic_observation_available:
        properties["next_step_kind"]["enum"] = ["diagnostic_tool"]
        require_field("tool_call", {"$ref": "#/$defs/DiagnosticToolCall"})
        omit_field("recommendation")
        omit_field("remediation_action")
        if controlled_action:
            omit_field("operational_action")
    elif diagnostic_available and diagnostic_observation_available:
        properties["next_step_kind"]["enum"] = [
            "diagnostic_tool",
            "recommendation",
            *(["remediation_action"] if action_available else []),
        ]
        allow_optional_field("tool_call", {"$ref": "#/$defs/DiagnosticToolCall"})
        allow_optional_field(
            "recommendation",
            recommendation_schema(),
        )
        if action_available:
            allow_optional_field(
                "remediation_action",
                {"$ref": "#/$defs/RetractRecalledMemoryAction"},
            )
        else:
            omit_field("remediation_action")
        if controlled_action:
            allow_optional_field(
                "operational_action",
                {"$ref": "#/$defs/OperationalAction"},
            )
    else:
        properties["next_step_kind"]["enum"] = [
            "recommendation",
            *(["remediation_action"] if action_available else []),
        ]
        omit_field("tool_call")
        if action_available:
            allow_optional_field(
                "recommendation",
                recommendation_schema(),
            )
            allow_optional_field(
                "remediation_action",
                {"$ref": "#/$defs/RetractRecalledMemoryAction"},
            )
        else:
            require_field(
                "recommendation",
                recommendation_schema(),
            )
            omit_field("remediation_action")
        if controlled_action:
            require_field(
                "operational_action",
                {"$ref": "#/$defs/OperationalAction"},
            )
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
        payload = strict_json_loads(text)
    except (CausalEvidenceError, TypeError):
        return text
    if not isinstance(payload, dict):
        return text
    normalized = dict(payload)
    for field in _PROVIDER_BRANCH_FIELDS:
        normalized.setdefault(field, None)
    if normalized.get("schema_version") == CONTROLLED_ACTION_DECISION_SCHEMA_VERSION:
        normalized.setdefault("operational_action", None)
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
    operational_action_contract: str | None = None,
) -> AgentDecisionV2 | AgentDecisionV3:
    """Parse and enforce constraints that cannot be represented in JSON Schema."""

    try:
        payload = strict_json_loads(text)
        if operational_action_contract is None:
            decision: AgentDecisionV2 | AgentDecisionV3 = AgentDecisionV2.model_validate(payload)
            schema_name = "AgentDecisionV2"
        elif operational_action_contract == PAYMENTS_OPERATIONAL_ACTION_CONTRACT:
            decision = AgentDecisionV3.model_validate(payload)
            schema_name = "AgentDecisionV3"
        else:
            raise ValueError("unsupported operational action contract")
    except (CausalEvidenceError, ValidationError, TypeError) as exc:
        schema_name = (
            "AgentDecisionV3" if operational_action_contract is not None else "AgentDecisionV2"
        )
        raise AgentDecisionError(f"model response did not satisfy {schema_name}") from exc

    if operational_action_contract is not None and decision.next_step_kind != "diagnostic_tool":
        raise AgentDecisionError(
            "controlled AgentDecisionV3 is diagnostic-only; use ControlledActionSelectionV1"
        )

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
    if isinstance(decision, AgentDecisionV3) and decision.operational_action is not None:
        _validate_controlled_action_text(decision)

    return decision


def agent_decision_from_payload(payload: Mapping[str, Any]) -> AgentDecisionV2 | AgentDecisionV3:
    """Load current decisions while preserving resumability of V1 checkpoints."""

    if payload.get("schema_version") == 3:
        decision = AgentDecisionV3.model_validate(payload)
        if decision.operational_action is not None:
            _validate_controlled_action_text(decision)
        return decision
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
    decision: AgentDecisionV1 | AgentDecisionV2 | AgentDecisionV3,
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


def operational_action_fingerprint(
    action: OperationalAction | Mapping[str, Any],
) -> str:
    """Return a stable identity for a validated operational action."""

    validated = (
        action
        if isinstance(action, OperationalAction)
        else OperationalAction.model_validate(action)
    )
    return f"operational_action:{_digest(validated.model_dump(mode='json'))}"


def operational_action_catalog(contract: str) -> dict[str, Any]:
    """Return the immutable server-owned selection catalog for one contract."""

    if contract != PAYMENTS_OPERATIONAL_ACTION_CONTRACT:
        raise ValueError("unsupported operational action contract")
    return {
        "catalog_id": PAYMENTS_OPERATIONAL_ACTION_CATALOG_ID,
        "contract": PAYMENTS_OPERATIONAL_ACTION_CONTRACT,
        "actions": list(PAYMENTS_OPERATIONAL_ACTIONS),
        "directives": dict(_ACTION_DIRECTIVES),
    }


def operational_action_directive(action: OperationalAction | Mapping[str, Any]) -> str:
    """Render the directive from the server catalog, never from model prose."""

    validated = (
        action
        if isinstance(action, OperationalAction)
        else OperationalAction.model_validate(action)
    )
    return _ACTION_DIRECTIVES[validated.action_id]


def _validate_controlled_action_text(decision: AgentDecisionV3) -> None:
    action = decision.operational_action
    assert action is not None
    if (
        decision.recommendation != operational_action_directive(action)
        or decision.diagnosis != CONTROLLED_ACTION_DIAGNOSIS
        or decision.rollback != CONTROLLED_ACTION_ROLLBACK
        or decision.verification != [CONTROLLED_ACTION_VERIFICATION]
        or decision.safety_constraints != [CONTROLLED_ACTION_SAFETY_CONSTRAINT]
    ):
        raise AgentDecisionError("recommendation prose contradicts server-owned operational action")
    selection = controlled_action_selection_from_decision(decision)
    _validate_controlled_selection_rationale(selection)


def _validate_controlled_selection_rationale(
    selection: ControlledActionSelection,
) -> None:
    """Accept only neutral explanatory prose that cannot encode a directive."""

    if selection.rationale not in CONTROLLED_ACTION_SELECTION_RATIONALES:
        raise AgentDecisionError(
            "controlled action rationale is outside server-approved explanatory prose"
        )


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
