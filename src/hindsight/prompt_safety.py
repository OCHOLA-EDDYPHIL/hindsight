"""Deterministic prompt-injection screening for durable semantic memory."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

PromptSafetyStatus = Literal["clear", "suspected"]

SCANNER_VERSION = "deterministic.v1"
MAX_SCAN_CHARACTERS = 64_000
MAX_SCAN_NODES = 512
MAX_SCAN_DEPTH = 12
PROMPT_SAFETY_METADATA_KEYS = frozenset(
    {
        "prompt_safety_status",
        "prompt_safety_scanner_version",
        "prompt_safety_reason_codes",
    }
)

_RULES = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,120}"
            r"\b(?:previous|prior|above|system|developer|instructions?|rules?|prompt)\b"
        ),
    ),
    (
        "role_impersonation",
        re.compile(
            r"\b(?:you are now|act as|pretend to be|switch to)\b.{0,120}"
            r"\b(?:system|developer|administrator|unrestricted|jailbreak)\b"
        ),
    ),
    (
        "prompt_disclosure",
        re.compile(
            r"\b(?:reveal|print|show|repeat|expose|dump)\b.{0,120}"
            r"\b(?:system|developer|hidden|initial)\s+(?:prompt|instructions?)\b"
        ),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"\b(?:reveal|print|send|exfiltrate|dump|return)\b.{0,120}"
            r"\b(?:api[-_ ]?keys?|passwords?|secrets?|tokens?|credentials?)\b"
        ),
    ),
    (
        "control_token",
        re.compile(
            r"<\|(?:system|assistant|developer|user|tool|endoftext)[^|]*\|>"
            r"|\[/?(?:inst|system)\]"
            r"|</?(?:system|assistant|developer|tool)>"
        ),
    ),
    (
        "authorization_bypass",
        re.compile(
            r"\b(?:call|invoke|execute|run|use)\b.{0,120}"
            r"\b(?:tool|function|shell|command)\b.{0,120}"
            r"\b(?:without|bypass|ignore)\b.{0,80}"
            r"\b(?:approval|authorization|policy|allowlist)\b"
        ),
    ),
)


@dataclass(frozen=True)
class PromptSafetyAssessment:
    """A bounded scanner result safe to persist without source excerpts."""

    status: PromptSafetyStatus
    scanner_version: str
    reason_codes: tuple[str, ...]

    def metadata(self) -> dict[str, Any]:
        return {
            "prompt_safety_status": self.status,
            "prompt_safety_scanner_version": self.scanner_version,
            "prompt_safety_reason_codes": list(self.reason_codes),
        }


def assess_prompt_safety(
    *,
    content: str,
    metadata: dict[str, Any] | None = None,
    structured_payload: dict[str, Any] | None = None,
    provenance: dict[str, str] | None = None,
) -> PromptSafetyAssessment:
    """Screen every prompt-visible string under strict work limits."""

    fragments: list[str] = []
    reason_codes: set[str] = set()
    scanned_characters = 0
    scanned_nodes = 0
    exhausted = False

    def add_fragment(value: str) -> None:
        nonlocal exhausted, scanned_characters
        remaining = MAX_SCAN_CHARACTERS - scanned_characters
        if remaining <= 0:
            exhausted = True
            reason_codes.add("scan_budget_exceeded")
            return
        fragment = value[:remaining]
        fragments.append(fragment)
        scanned_characters += len(fragment)
        if len(value) > len(fragment):
            exhausted = True
            reason_codes.add("scan_budget_exceeded")

    def walk(value: Any, *, depth: int, ancestors: frozenset[int]) -> None:
        nonlocal exhausted, scanned_nodes
        if exhausted:
            return
        if depth > MAX_SCAN_DEPTH:
            exhausted = True
            reason_codes.add("scan_budget_exceeded")
            return
        scanned_nodes += 1
        if scanned_nodes > MAX_SCAN_NODES:
            exhausted = True
            reason_codes.add("scan_budget_exceeded")
            return
        if isinstance(value, str):
            add_fragment(value)
            return
        if isinstance(value, dict):
            identity = id(value)
            if identity in ancestors:
                exhausted = True
                reason_codes.add("scan_structure_cycle")
                return
            nested_ancestors = ancestors | {identity}
            for key in sorted(value, key=str):
                if isinstance(key, str):
                    add_fragment(key)
                walk(value[key], depth=depth + 1, ancestors=nested_ancestors)
                if exhausted:
                    return
            return
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in ancestors:
                exhausted = True
                reason_codes.add("scan_structure_cycle")
                return
            nested_ancestors = ancestors | {identity}
            for item in value:
                walk(item, depth=depth + 1, ancestors=nested_ancestors)
                if exhausted:
                    return

    for value in (content, metadata or {}, structured_payload or {}, provenance or {}):
        walk(value, depth=0, ancestors=frozenset())
        if exhausted:
            break

    normalized = _normalize("\n".join(fragments))
    for reason_code, pattern in _RULES:
        if pattern.search(normalized):
            reason_codes.add(reason_code)

    resolved_reasons = tuple(sorted(reason_codes))
    return PromptSafetyAssessment(
        status="suspected" if resolved_reasons else "clear",
        scanner_version=SCANNER_VERSION,
        reason_codes=resolved_reasons,
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category == "Cf":
            continue
        if category.startswith("C"):
            characters.append(" ")
        else:
            characters.append(character)
    return " ".join("".join(characters).split())
