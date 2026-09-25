"""Validation rules. Pure functions, so the same code runs in the Temporal
activity locally and inside the AWS Lambda handler (lambda_handler.py).

errors         -> referral is rejected (can't be scheduled at all)
review_reasons -> a human must look before we write to the FHIR server
"""
from __future__ import annotations

import re

from .models import ExtractedFields, ParsedReferral, ValidationResult

CONFIDENCE_THRESHOLD = 0.8
_ICD10 = re.compile(r"^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$")
_URGENCY_RANK = {"routine": 0, "urgent": 1, "stat": 2}


def npi_is_valid(npi: str) -> bool:
    """NPI check digit: Luhn over '80840' + first 9 digits."""
    if not re.fullmatch(r"\d{10}", npi or ""):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(npi[9])


def validate(ref: ParsedReferral, ext: ExtractedFields) -> ValidationResult:
    errors: list[str] = []
    review: list[str] = []
    p = ref.patient

    # Hard requirements: without these we can't identify the patient.
    if not p.mrn:
        errors.append("missing patient MRN (PID-3)")
    if not p.family_name or not p.given_name:
        errors.append("missing patient name (PID-5)")
    if p.birth_date is None:
        errors.append("missing or invalid date of birth (PID-7)")

    # Soft checks: a human can fix or confirm these.
    prov = ref.referring_provider
    if prov is None:
        review.append("no referring provider (PRD segment)")
    elif not prov.npi or not npi_is_valid(prov.npi):
        review.append(f"referring provider NPI is missing or fails check digit: {prov.npi!r}")

    for dx in ref.diagnoses:
        if not _ICD10.match(dx.code):
            review.append(f"diagnosis code {dx.code!r} is not a valid ICD-10 format")

    if not ext.requested_procedure:
        review.append("could not determine the requested procedure")
    if ext.confidence < CONFIDENCE_THRESHOLD:
        review.append(f"extraction confidence {ext.confidence:.2f} is below {CONFIDENCE_THRESHOLD}")

    # Safety net: the notes sound more urgent than the coded priority.
    if ref.hl7_priority and ext.urgency:
        if _URGENCY_RANK[ext.urgency.value] > _URGENCY_RANK[ref.hl7_priority.value]:
            review.append(
                f"notes suggest '{ext.urgency.value}' but referral is coded '{ref.hl7_priority.value}'"
            )

    for item in ext.missing_information:
        review.append(f"missing information: {item}")

    return ValidationResult(errors=errors, review_reasons=review)
