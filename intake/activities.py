"""Temporal activities: the steps that touch the outside world (LLM, Lambda,
FHIR server). Activities can fail and be retried; the workflow can't do I/O.
"""
from __future__ import annotations

import json
import os
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import extraction, fhir, validation
from .hl7_parser import HL7ParseError, parse_referral
from .models import (
    ExtractedFields,
    FhirWriteResult,
    ParsedReferral,
    ValidateInput,
    ValidationResult,
    WriteInput,
)


@activity.defn
def parse_hl7(raw: str) -> ParsedReferral:
    try:
        return parse_referral(raw)
    except HL7ParseError as e:
        # Retrying won't fix a malformed message, so fail fast.
        raise ApplicationError(str(e), type="HL7ParseError", non_retryable=True) from e


@activity.defn
def extract_fields(referral: ParsedReferral) -> ExtractedFields:
    activity.logger.info("extracting fields for %s", referral.message_control_id)
    # Rate limits and timeouts from the LLM API raise normal exceptions,
    # which the workflow's retry policy retries with backoff.
    return extraction.extract_fields(referral)


@activity.defn
def validate_referral(data: ValidateInput) -> ValidationResult:
    if os.getenv("VALIDATION_BACKEND") == "lambda":
        return _validate_via_lambda(data)
    return validation.validate(data.referral, data.extracted)


def _validate_via_lambda(data: ValidateInput) -> ValidationResult:
    import boto3  # only needed in lambda mode

    client = boto3.client("lambda")
    resp = client.invoke(
        FunctionName=os.environ["VALIDATION_LAMBDA_NAME"],
        Payload=data.model_dump_json().encode(),
    )
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"validation lambda failed: {payload}")
    return ValidationResult.model_validate(payload)


@activity.defn
def write_fhir(data: WriteInput) -> FhirWriteResult:
    return fhir.write_referral(data.referral, data.extracted, data.review)
