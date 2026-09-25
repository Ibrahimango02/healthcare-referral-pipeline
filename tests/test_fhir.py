import json

import httpx

from intake.fhir import build_bundle, post_bundle
from intake.hl7_parser import parse_referral
from intake.models import ExtractedFields, Urgency


def _bundle(sample):
    ref = parse_referral(sample("referral_routine"))
    ext = ExtractedFields(requested_procedure="abdominal ultrasound", body_site="abdomen",
                          urgency=Urgency.ROUTINE, confidence=0.9)
    return build_bundle(ref, ext, None)


def test_bundle_is_idempotent_transaction(sample):
    b = _bundle(sample)
    assert b["type"] == "transaction"
    kinds = [e["resource"]["resourceType"] for e in b["entry"]]
    assert kinds == ["Patient", "Practitioner", "ServiceRequest"]
    # every create is conditional, so retries can't duplicate records
    assert all("ifNoneExist" in e["request"] for e in b["entry"])
    sr = b["entry"][-1]["resource"]
    assert sr["subject"]["reference"] == b["entry"][0]["fullUrl"]
    assert sr["identifier"][0]["value"] == "REF0001"


def test_post_bundle_parses_locations(sample):
    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["resourceType"] == "Bundle"
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": [
            {"response": {"status": "201", "location": "Patient/p1/_history/1"}},
            {"response": {"status": "200", "location": "Practitioner/pr1/_history/1"}},
            {"response": {"status": "201", "location": "ServiceRequest/sr1/_history/1"}},
        ]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = post_bundle(_bundle(sample), "http://fhir.test/fhir", client=client)
    assert (res.patient_ref, res.service_request_ref, res.mode) == ("Patient/p1", "ServiceRequest/sr1", "server")
