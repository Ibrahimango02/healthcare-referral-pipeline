"""Build FHIR R4 resources and write them as one transaction Bundle.

Idempotency: Patient and Practitioner use conditional create (`ifNoneExist`) on
their identifiers, and the ServiceRequest is keyed on the HL7 message control
ID. If the activity is retried after a network failure, the server doesn't
create duplicates.

If FHIR_BASE_URL isn't set, the bundle is written to ./out/ instead so the
pipeline runs with no server.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .models import ExtractedFields, FhirWriteResult, ParsedReferral, ReviewDecision

MRN_SYSTEM = "urn:referral-intake:mrn"  # replace with the facility's real OID/URI
MSG_SYSTEM = "urn:referral-intake:hl7-message-control-id"
NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"


def build_bundle(ref: ParsedReferral, ext: ExtractedFields, review: Optional[ReviewDecision]) -> dict:
    p = ref.patient
    patient_id, prac_id, sr_id = (f"urn:uuid:{uuid.uuid4()}" for _ in range(3))
    mrn_system = f"{MRN_SYSTEM}:{p.assigning_authority}" if p.assigning_authority else MRN_SYSTEM

    patient = {
        "resourceType": "Patient",
        "identifier": [{"system": mrn_system, "value": p.mrn}],
        "name": [{"family": p.family_name, "given": [p.given_name]}],
        "gender": {"F": "female", "M": "male"}.get((p.sex or "").upper(), "unknown"),
    }
    if p.birth_date:
        patient["birthDate"] = p.birth_date.isoformat()
    if p.phone:
        patient["telecom"] = [{"system": "phone", "value": p.phone}]

    entries = [
        {
            "fullUrl": patient_id,
            "resource": patient,
            "request": {
                "method": "POST",
                "url": "Patient",
                "ifNoneExist": f"identifier={mrn_system}|{p.mrn}",
            },
        }
    ]

    requester = None
    prov = ref.referring_provider
    if prov and prov.npi:
        entries.append(
            {
                "fullUrl": prac_id,
                "resource": {
                    "resourceType": "Practitioner",
                    "identifier": [{"system": NPI_SYSTEM, "value": prov.npi}],
                    "name": [{"family": prov.family_name, "given": [prov.given_name]}],
                },
                "request": {
                    "method": "POST",
                    "url": "Practitioner",
                    "ifNoneExist": f"identifier={NPI_SYSTEM}|{prov.npi}",
                },
            }
        )
        requester = {"reference": prac_id}

    priority = (ext.urgency or ref.hl7_priority)
    service_request = {
        "resourceType": "ServiceRequest",
        "identifier": [{"system": MSG_SYSTEM, "value": ref.message_control_id}],
        "status": "active",
        "intent": "order",
        "priority": {"routine": "routine", "urgent": "urgent", "stat": "stat"}.get(
            priority.value if priority else "", "routine"
        ),
        "subject": {"reference": patient_id},
        "code": {"text": ext.requested_procedure or "unspecified"},
        "reasonCode": [
            {"coding": [{"system": d.system, "code": d.code, "display": d.display}]} for d in ref.diagnoses
        ],
        "note": [{"text": ref.clinical_notes}] if ref.clinical_notes else [],
    }
    if requester:
        service_request["requester"] = requester
    if ext.body_site:
        service_request["bodySite"] = [{"text": ext.body_site}]
    if ext.clinical_indication:
        service_request["reasonCode"].append({"text": ext.clinical_indication})
    if review:
        service_request["note"].append(
            {"authorString": review.reviewer, "text": f"Reviewed and approved. {review.notes or ''}".strip()}
        )

    entries.append(
        {
            "fullUrl": sr_id,
            "resource": service_request,
            "request": {
                "method": "POST",
                "url": "ServiceRequest",
                "ifNoneExist": f"identifier={MSG_SYSTEM}|{ref.message_control_id}",
            },
        }
    )
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def post_bundle(bundle: dict, base_url: str, client: Optional[httpx.Client] = None) -> FhirWriteResult:
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.post(
            base_url.rstrip("/"),
            json=bundle,
            headers={"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"},
        )
        resp.raise_for_status()
        body = resp.json()
    finally:
        if own_client:
            client.close()

    # Response entries line up with request entries: Patient first, ServiceRequest last.
    locations = [e.get("response", {}).get("location", "") for e in body.get("entry", [])]
    return FhirWriteResult(
        patient_ref=_strip_history(locations[0]),
        service_request_ref=_strip_history(locations[-1]),
        mode="server",
    )


def _strip_history(location: str) -> str:
    # "Patient/123/_history/1" -> "Patient/123"
    return "/".join(location.split("/")[:2])


def write_bundle_to_file(bundle: dict, message_control_id: str, out_dir: str = "out") -> FhirWriteResult:
    path = Path(out_dir)
    path.mkdir(exist_ok=True)
    target = path / f"{message_control_id}.bundle.json"
    target.write_text(json.dumps(bundle, indent=2))
    return FhirWriteResult(
        patient_ref=f"file:{target}#Patient",
        service_request_ref=f"file:{target}#ServiceRequest",
        mode="file",
    )


def write_referral(ref: ParsedReferral, ext: ExtractedFields, review: Optional[ReviewDecision]) -> FhirWriteResult:
    bundle = build_bundle(ref, ext, review)
    base = os.getenv("FHIR_BASE_URL")
    if base:
        return post_bundle(bundle, base)
    return write_bundle_to_file(bundle, ref.message_control_id)
