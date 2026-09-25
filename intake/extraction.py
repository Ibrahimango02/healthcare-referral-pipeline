"""LLM extraction of structured fields from free-text referral notes.

Uses Anthropic tool use to force the model to answer in a fixed JSON schema.
If ANTHROPIC_API_KEY isn't set, falls back to a simple keyword heuristic so the
pipeline still runs locally. The heuristic is deliberately low-confidence, so
its output is routed to human review.
"""
from __future__ import annotations

import os
import re

from .models import ExtractedFields, ParsedReferral, Urgency

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

_TOOL = {
    "name": "record_referral_fields",
    "description": "Record the structured fields extracted from a clinical referral.",
    "input_schema": {
        "type": "object",
        "properties": {
            "requested_procedure": {
                "type": ["string", "null"],
                "description": "The exam or procedure being requested, e.g. 'abdominal ultrasound'. Null if not stated.",
            },
            "body_site": {"type": ["string", "null"]},
            "clinical_indication": {
                "type": ["string", "null"],
                "description": "One-sentence reason for referral in clinical language.",
            },
            "urgency": {
                "type": ["string", "null"],
                "enum": ["routine", "urgent", "stat", None],
                "description": "Clinical urgency implied by the notes, independent of any coded priority.",
            },
            "suspected_conditions": {"type": "array", "items": {"type": "string"}},
            "missing_information": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Information a scheduler would need that the referral does not contain.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Your confidence that the extracted fields are complete and correct.",
            },
        },
        "required": ["requested_procedure", "urgency", "confidence", "suspected_conditions", "missing_information"],
    },
}

_SYSTEM = (
    "You extract structured data from clinical referral notes for an imaging intake team. "
    "Only use information present in the input. Never invent a procedure or diagnosis. "
    "If the notes describe red-flag symptoms, set urgency from the symptoms even if the coded priority says routine."
)


def _prompt(ref: ParsedReferral) -> str:
    dx = ", ".join(f"{d.code} ({d.display})" for d in ref.diagnoses) or "none"
    return (
        f"Coded priority: {ref.hl7_priority.value if ref.hl7_priority else 'not provided'}\n"
        f"Specialty: {ref.specialty or 'not provided'}\n"
        f"Coded diagnoses: {dx}\n"
        f"Clinical notes:\n{ref.clinical_notes or '(none)'}"
    )


def extract_with_llm(ref: ParsedReferral) -> ExtractedFields:
    import anthropic  # imported lazily so the heuristic path has no dependency

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=800,
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL["name"]},
        messages=[{"role": "user", "content": _prompt(ref)}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    return ExtractedFields(**block.input, extractor=f"anthropic:{DEFAULT_MODEL}")


# --- offline fallback -------------------------------------------------------

_PROCEDURES = [
    (r"\b(abdominal|abdomen)\b.*\bultrasound\b|\bultrasound\b.*\babdom", "abdominal ultrasound", "abdomen"),
    (r"\b(ct|cat scan)\b.*\bhead\b|\bhead\b.*\bct\b", "CT head", "head"),
    (r"\bimage (the )?head\b|\bhead imaging\b", None, "head"),  # site known, modality not
    (r"\bmri\b", "MRI", None),
    (r"\bx-?ray\b", "X-ray", None),
]
_RED_FLAGS = [r"worst (headache )?of (his|her|their) life", r"\bconfusion\b", r"\bstat\b", r"\basap\b"]


def extract_with_heuristic(ref: ParsedReferral) -> ExtractedFields:
    text = ref.clinical_notes.lower()
    procedure = site = None
    for pattern, proc, s in _PROCEDURES:
        if re.search(pattern, text):
            procedure, site = proc, s
            break
    urgent = any(re.search(p, text) for p in _RED_FLAGS)
    missing = [] if procedure else ["requested procedure / modality"]
    return ExtractedFields(
        requested_procedure=procedure,
        body_site=site,
        clinical_indication=ref.clinical_notes.split(".")[0][:200] or None,
        urgency=Urgency.URGENT if urgent else (ref.hl7_priority or Urgency.ROUTINE),
        suspected_conditions=[d.display for d in ref.diagnoses if d.display],
        missing_information=missing,
        confidence=0.6 if procedure else 0.3,
        extractor="heuristic",
    )


def extract_fields(ref: ParsedReferral) -> ExtractedFields:
    if os.getenv("ANTHROPIC_API_KEY"):
        return extract_with_llm(ref)
    return extract_with_heuristic(ref)
