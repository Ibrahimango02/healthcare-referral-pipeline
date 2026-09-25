import pytest

from intake.extraction import extract_with_heuristic
from intake.hl7_parser import HL7ParseError, parse_referral
from intake.models import ExtractedFields, Urgency
from intake.validation import npi_is_valid, validate


def test_parses_routine_referral(sample):
    ref = parse_referral(sample("referral_routine"))
    assert ref.message_control_id == "REF0001"
    assert ref.patient.mrn == "MRN12345"
    assert ref.patient.birth_date.isoformat() == "1980-05-12"
    assert ref.referring_provider.npi == "1234567893"
    assert ref.hl7_priority == Urgency.ROUTINE
    assert ref.diagnoses[0].code == "R10.11"
    assert "ultrasound" in ref.clinical_notes


def test_rejects_non_hl7():
    with pytest.raises(HL7ParseError):
        parse_referral("this is not hl7")


@pytest.mark.parametrize("npi,ok", [("1234567893", True), ("1234567890", False), ("12345", False), ("", False)])
def test_npi_check_digit(npi, ok):
    assert npi_is_valid(npi) is ok


def _fields(**kw):
    base = dict(requested_procedure="abdominal ultrasound", urgency=Urgency.ROUTINE, confidence=0.95)
    base.update(kw)
    return ExtractedFields(**base)


def test_clean_referral_passes(sample):
    result = validate(parse_referral(sample("referral_routine")), _fields())
    assert result.errors == [] and result.review_reasons == []


def test_urgency_mismatch_goes_to_review(sample):
    ref = parse_referral(sample("referral_needs_review"))
    result = validate(ref, _fields(requested_procedure="CT head", urgency=Urgency.URGENT))
    assert result.needs_review
    assert any("coded 'routine'" in r for r in result.review_reasons)


def test_low_confidence_goes_to_review(sample):
    result = validate(parse_referral(sample("referral_routine")), _fields(confidence=0.5))
    assert result.needs_review


def test_missing_identifiers_rejected(sample):
    result = validate(parse_referral(sample("referral_malformed")), _fields())
    assert result.is_rejected
    assert any("MRN" in e for e in result.errors)


def test_heuristic_flags_red_flag_symptoms(sample):
    ext = extract_with_heuristic(parse_referral(sample("referral_needs_review")))
    assert ext.urgency == Urgency.URGENT
    assert ext.body_site == "head"
