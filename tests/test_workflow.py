"""Runs the real workflow and activities against a local Temporal dev server.

The dev server is downloaded automatically the first time. If you already
have the Temporal CLI installed, point TEMPORAL_CLI_PATH at it to skip that.
"""
import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import pytest_asyncio
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from intake.activities import extract_fields, parse_hl7, validate_referral, write_fhir
from intake.models import IntakeStatus, ReviewDecision
from intake.workflows import IntakeRequest, ReferralIntakeWorkflow

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    kwargs = {"data_converter": pydantic_data_converter}
    if os.getenv("TEMPORAL_CLI_PATH"):
        kwargs["dev_server_existing_path"] = os.environ["TEMPORAL_CLI_PATH"]
    env = await WorkflowEnvironment.start_local(**kwargs)
    yield env
    await env.shutdown()


async def _run(env, raw, review=None, timeout=60):
    queue = f"q-{uuid.uuid4()}"
    with ThreadPoolExecutor(4) as ex:
        async with Worker(env.client, task_queue=queue, workflows=[ReferralIntakeWorkflow],
                          activities=[parse_hl7, extract_fields, validate_referral, write_fhir],
                          activity_executor=ex):
            handle = await env.client.start_workflow(
                ReferralIntakeWorkflow.run, IntakeRequest(raw_hl7=raw, review_timeout_seconds=timeout),
                id=f"test-{uuid.uuid4()}", task_queue=queue)
            if review:
                for _ in range(50):  # wait until the workflow is parked on review
                    if (await handle.query(ReferralIntakeWorkflow.get_status))["status"] == "awaiting_review":
                        break
                    await asyncio.sleep(0.1)
                await handle.signal(ReferralIntakeWorkflow.submit_review, review)
            return await handle.result()


@pytest.mark.asyncio(loop_scope="module")
async def test_heuristic_referral_goes_through_review_then_writes(env, sample, tmp_path):
    decision = ReviewDecision(approved=True, reviewer="dr.lee", notes="looks right",
                              corrections={"requested_procedure": "abdominal ultrasound", "confidence": 1.0})
    res = await _run(env, sample("referral_routine"), review=decision)
    assert res.status == IntakeStatus.COMPLETED
    assert res.fhir.mode == "file"
    assert (tmp_path / "out" / "REF0001.bundle.json").exists()


@pytest.mark.asyncio(loop_scope="module")
async def test_reviewer_can_reject(env, sample):
    res = await _run(env, sample("referral_needs_review"),
                     review=ReviewDecision(approved=False, reviewer="dr.lee", notes="send to ER"))
    assert res.status == IntakeStatus.REJECTED
    assert res.detail == "send to ER"


@pytest.mark.asyncio(loop_scope="module")
async def test_missing_identifiers_rejected_without_review(env, sample):
    res = await _run(env, sample("referral_malformed"))
    assert res.status == IntakeStatus.REJECTED
    assert "MRN" in res.detail


@pytest.mark.asyncio(loop_scope="module")
async def test_review_timeout_rejects(env, sample):
    res = await _run(env, sample("referral_needs_review"), timeout=2)
    assert res.status == IntakeStatus.REJECTED
    assert "timeout" in res.detail


@pytest.mark.asyncio(loop_scope="module")
async def test_garbage_input_fails_fast(env):
    with pytest.raises(WorkflowFailureError):
        await _run(env, "not an hl7 message")
