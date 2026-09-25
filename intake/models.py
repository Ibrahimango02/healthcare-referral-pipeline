"""Data models passed between workflow steps.

Everything that crosses a Temporal boundary (workflow <-> activity) is a Pydantic
model, serialized with Temporal's Pydantic data converter.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Urgency(str, Enum):
    ROUTINE = "routine"
    URGENT = "urgent"
    STAT = "stat"


class Patient(BaseModel):
    mrn: str
    assigning_authority: Optional[str] = None
    family_name: str
    given_name: str
    birth_date: Optional[date] = None
    sex: Optional[str] = None
    phone: Optional[str] = None


class Provider(BaseModel):
    npi: Optional[str] = None
    family_name: str
    given_name: str


class Diagnosis(BaseModel):
    code: str
    display: Optional[str] = None
    system: str = "http://hl7.org/fhir/sid/icd-10"


class ParsedReferral(BaseModel):
    """What we could read deterministically from the HL7 v2 message."""

    message_control_id: str
    sending_facility: Optional[str] = None
    patient: Patient
    referring_provider: Optional[Provider] = None
    hl7_priority: Optional[Urgency] = None  # from RF1-2, may be missing
    specialty: Optional[str] = None  # from RF1-3
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    clinical_notes: str = ""  # free text from NTE segments


class ExtractedFields(BaseModel):
    """What the LLM pulled out of the free-text clinical notes."""

    requested_procedure: Optional[str] = None
    body_site: Optional[str] = None
    clinical_indication: Optional[str] = None
    urgency: Optional[Urgency] = None
    suspected_conditions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    extractor: str = "unknown"  # "anthropic:<model>" or "heuristic"


class ValidationResult(BaseModel):
    errors: list[str] = Field(default_factory=list)  # hard failures -> reject
    review_reasons: list[str] = Field(default_factory=list)  # soft -> human review

    @property
    def is_rejected(self) -> bool:
        return bool(self.errors)

    @property
    def needs_review(self) -> bool:
        return not self.errors and bool(self.review_reasons)


class ReviewDecision(BaseModel):
    approved: bool
    reviewer: str
    notes: Optional[str] = None
    # Reviewer corrections; any field set here overrides the LLM output.
    corrections: Optional[dict] = None


class IntakeStatus(str, Enum):
    PARSING = "parsing"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    AWAITING_REVIEW = "awaiting_review"
    WRITING_FHIR = "writing_fhir"
    COMPLETED = "completed"
    REJECTED = "rejected"


class FhirWriteResult(BaseModel):
    patient_ref: str
    service_request_ref: str
    mode: str  # "server" or "file"


class IntakeResult(BaseModel):
    message_control_id: str
    status: IntakeStatus
    fhir: Optional[FhirWriteResult] = None
    validation: Optional[ValidationResult] = None
    review: Optional[ReviewDecision] = None
    detail: Optional[str] = None


class ValidateInput(BaseModel):
    referral: ParsedReferral
    extracted: ExtractedFields


class WriteInput(BaseModel):
    referral: ParsedReferral
    extracted: ExtractedFields
    review: Optional[ReviewDecision] = None
