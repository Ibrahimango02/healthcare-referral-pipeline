"""Run the worker: python -m intake.worker"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from .activities import parse_hl7, extract_fields, validate_referral, write_fhir
from .workflows import ReferralIntakeWorkflow

TASK_QUEUE = "referral-intake"


async def connect() -> Client:
    return await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )


def build_worker(client: Client, executor: ThreadPoolExecutor) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ReferralIntakeWorkflow],
        activities=[parse_hl7, extract_fields, validate_referral, write_fhir],
        activity_executor=executor,  # activities are sync functions, run in threads
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = await connect()
    with ThreadPoolExecutor(max_workers=10) as executor:
        worker = build_worker(client, executor)
        print(f"worker listening on task queue '{TASK_QUEUE}'")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
