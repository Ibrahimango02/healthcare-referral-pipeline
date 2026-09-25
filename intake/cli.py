"""Command line for starting, inspecting, and reviewing referrals.

  python -m intake.cli submit samples/referral_routine.hl7 [--wait]
  python -m intake.cli status REF0002
  python -m intake.cli review REF0002 --approve --reviewer "dr.lee" \
        --set requested_procedure="CT head without contrast" --notes "Confirmed urgent"
  python -m intake.cli review REF0002 --reject --reviewer "dr.lee" --notes "Send to ER"
  python -m intake.cli result REF0002
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from .hl7_parser import parse_referral
from .models import ReviewDecision
from .worker import TASK_QUEUE, connect
from .workflows import IntakeRequest, ReferralIntakeWorkflow


def workflow_id(mcid: str) -> str:
    return f"referral-{mcid}"


async def submit(path: str, wait: bool) -> None:
    raw = Path(path).read_text()
    # Workflow ID = HL7 message control ID, so a resent duplicate message
    # can't start a second intake for the same referral.
    mcid = parse_referral(raw).message_control_id
    client = await connect()
    try:
        handle = await client.start_workflow(
            ReferralIntakeWorkflow.run,
            IntakeRequest(raw_hl7=raw),
            id=workflow_id(mcid),
            task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        print(f"duplicate: referral {mcid} was already submitted")
        return
    print(f"started {handle.id}")
    if wait:
        result = await handle.result()
        print(result.model_dump_json(indent=2))


async def status(mcid: str) -> None:
    client = await connect()
    handle = client.get_workflow_handle(workflow_id(mcid))
    print(json.dumps(await handle.query(ReferralIntakeWorkflow.get_status), indent=2))


def _parse_sets(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        key, _, value = item.partition("=")
        value = value.strip()
        try:
            out[key.strip()] = json.loads(value)  # numbers, true/false, null
        except json.JSONDecodeError:
            out[key.strip()] = value  # plain strings
    return out


async def review(mcid: str, approve: bool, reviewer: str, notes: str | None, sets: list[str]) -> None:
    client = await connect()
    handle = client.get_workflow_handle(workflow_id(mcid))
    decision = ReviewDecision(approved=approve, reviewer=reviewer, notes=notes,
                              corrections=_parse_sets(sets) or None)
    await handle.signal(ReferralIntakeWorkflow.submit_review, decision)
    print(f"sent review for {mcid}: {'approved' if approve else 'rejected'}")


async def result(mcid: str) -> None:
    client = await connect()
    res = await client.get_workflow_handle_for(ReferralIntakeWorkflow.run, workflow_id(mcid)).result()
    print(res.model_dump_json(indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(prog="intake")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit"); s.add_argument("path"); s.add_argument("--wait", action="store_true")
    st = sub.add_parser("status"); st.add_argument("mcid")
    r = sub.add_parser("review"); r.add_argument("mcid")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true"); g.add_argument("--reject", action="store_true")
    r.add_argument("--reviewer", required=True); r.add_argument("--notes")
    r.add_argument("--set", action="append", dest="sets", help="correction, e.g. requested_procedure='CT head'")
    rs = sub.add_parser("result"); rs.add_argument("mcid")
    a = ap.parse_args()

    if a.cmd == "submit":
        asyncio.run(submit(a.path, a.wait))
    elif a.cmd == "status":
        asyncio.run(status(a.mcid))
    elif a.cmd == "review":
        asyncio.run(review(a.mcid, a.approve, a.reviewer, a.notes, a.sets))
    elif a.cmd == "result":
        asyncio.run(result(a.mcid))


if __name__ == "__main__":
    main()
