"""The referral intake workflow.

parse HL7 -> LLM extraction -> validation -> (optional) human review -> FHIR write

Temporal persists the workflow's progress, so if the worker crashes mid-run it
resumes from the last completed step, and a referral waiting for human review
can wait for hours or days without holding any resources.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import (
        extract_fields,
        parse_hl7,
        validate_referral,
        write_fhir,
    )
    from .models import (
        ExtractedFields,
        IntakeResult,
        IntakeStatus,
        ReviewDecision,
        ValidateInput,
        ValidationResult,
        WriteInput,
    )


class IntakeRequest(BaseModel):
    raw_hl7: str
    review_timeout_seconds: int = 24 * 60 * 60


QUICK = RetryPolicy(maximum_attempts=3)
LLM = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0, maximum_attempts=6)
FHIR_WRITE = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(minutes=1))


@workflow.defn
class ReferralIntakeWorkflow:
    def __init__(self) -> None:
        self._status = IntakeStatus.PARSING
        self._decision: Optional[ReviewDecision] = None
        self._validation: Optional[ValidationResult] = None
        self._extracted: Optional[ExtractedFields] = None

    @workflow.run
    async def run(self, req: IntakeRequest) -> IntakeResult:
        referral = await workflow.execute_activity(
            parse_hl7, req.raw_hl7, start_to_close_timeout=timedelta(seconds=10), retry_policy=QUICK
        )
        mcid = referral.message_control_id

        self._status = IntakeStatus.EXTRACTING
        extracted = await workflow.execute_activity(
            extract_fields, referral, start_to_close_timeout=timedelta(seconds=60), retry_policy=LLM
        )
        self._extracted = extracted

        self._status = IntakeStatus.VALIDATING
        result = await workflow.execute_activity(
            validate_referral,
            ValidateInput(referral=referral, extracted=extracted),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=QUICK,
        )
        self._validation = result

        if result.is_rejected:
            self._status = IntakeStatus.REJECTED
            return IntakeResult(message_control_id=mcid, status=self._status, validation=result,
                                detail="; ".join(result.errors))

        if result.needs_review:
            self._status = IntakeStatus.AWAITING_REVIEW
            workflow.logger.info("referral %s needs review: %s", mcid, result.review_reasons)
            try:
                await workflow.wait_condition(
                    lambda: self._decision is not None,
                    timeout=timedelta(seconds=req.review_timeout_seconds),
                )
            except TimeoutError:
                self._status = IntakeStatus.REJECTED
                return IntakeResult(message_control_id=mcid, status=self._status, validation=result,
                                    detail="no review decision before timeout")

            decision = self._decision
            assert decision is not None
            if not decision.approved:
                self._status = IntakeStatus.REJECTED
                return IntakeResult(message_control_id=mcid, status=self._status, validation=result,
                                    review=decision, detail=decision.notes or "rejected by reviewer")
            if decision.corrections:
                extracted = ExtractedFields.model_validate({**extracted.model_dump(), **decision.corrections})
                self._extracted = extracted

        self._status = IntakeStatus.WRITING_FHIR
        written = await workflow.execute_activity(
            write_fhir,
            WriteInput(referral=referral, extracted=extracted, review=self._decision),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=FHIR_WRITE,
        )

        self._status = IntakeStatus.COMPLETED
        return IntakeResult(message_control_id=mcid, status=self._status, fhir=written,
                            validation=result, review=self._decision)

    @workflow.signal
    def submit_review(self, decision: ReviewDecision) -> None:
        if self._status == IntakeStatus.AWAITING_REVIEW and self._decision is None:
            self._decision = decision

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self._status.value,
            "review_reasons": self._validation.review_reasons if self._validation else [],
            "errors": self._validation.errors if self._validation else [],
            "extracted": self._extracted.model_dump(mode="json") if self._extracted else None,
        }
