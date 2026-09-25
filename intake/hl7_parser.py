"""Deterministic parsing of HL7 v2 referral messages (REF^I12).

Segments used:
  MSH  message header (control ID, sending facility)
  RF1  referral information (priority, specialty)
  PRD  provider data (referring provider, NPI)
  PID  patient identification
  DG1  diagnosis (ICD-10)
  NTE  free-text notes -> handed to the LLM
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import hl7

from .models import Diagnosis, ParsedReferral, Patient, Provider, Urgency


class HL7ParseError(ValueError):
    """The message can't be read at all. Not retryable."""


_PRIORITY_MAP = {"R": Urgency.ROUTINE, "U": Urgency.URGENT, "S": Urgency.STAT}


def _field(seg: hl7.Segment, idx: int, comp: int = 1) -> Optional[str]:
    """1-based field / component access that returns None instead of raising."""
    try:
        value = seg.extract_field(field_num=idx, component_num=comp)
    except (IndexError, KeyError):
        return None
    value = str(value).strip() if value is not None else ""
    return value or None


def _first(msg: hl7.Message, name: str) -> Optional[hl7.Segment]:
    try:
        return msg.segment(name)
    except KeyError:
        return None


def parse_referral(raw: str) -> ParsedReferral:
    # HL7 uses \r as the segment separator; files on disk usually have \n.
    text = raw.replace("\r\n", "\r").replace("\n", "\r").strip()
    try:
        msg = hl7.parse(text)
    except Exception as e:  # hl7 raises a few different exception types
        raise HL7ParseError(f"not a valid HL7 v2 message: {e}") from e

    msh = _first(msg, "MSH")
    pid = _first(msg, "PID")
    if msh is None or pid is None:
        raise HL7ParseError("message is missing MSH or PID segment")

    # MSH is special: MSH-1 is the field separator, so MSH-10 is index 10 here.
    control_id = _field(msh, 10)
    if not control_id:
        raise HL7ParseError("MSH-10 (message control ID) is empty")

    dob_raw = _field(pid, 7)
    dob = None
    if dob_raw:
        try:
            dob = datetime.strptime(dob_raw[:8], "%Y%m%d").date()
        except ValueError:
            dob = None  # validation step will flag it

    patient = Patient(
        mrn=_field(pid, 3, 1) or "",
        assigning_authority=_field(pid, 3, 4),
        family_name=_field(pid, 5, 1) or "",
        given_name=_field(pid, 5, 2) or "",
        birth_date=dob,
        sex=_field(pid, 8),
        phone=_field(pid, 13),
    )

    provider = None
    prd = _first(msg, "PRD")
    if prd is not None:
        provider = Provider(
            family_name=_field(prd, 2, 1) or "",
            given_name=_field(prd, 2, 2) or "",
            npi=_field(prd, 7, 1),
        )

    rf1 = _first(msg, "RF1")
    priority = specialty = None
    if rf1 is not None:
        priority = _PRIORITY_MAP.get((_field(rf1, 2) or "").upper())
        specialty = _field(rf1, 3, 2) or _field(rf1, 3, 1)

    diagnoses = []
    for seg in msg.segments("DG1") if _first(msg, "DG1") is not None else []:
        code = _field(seg, 3, 1)
        if code:
            diagnoses.append(Diagnosis(code=code, display=_field(seg, 3, 2)))

    notes = []
    for seg in msg.segments("NTE") if _first(msg, "NTE") is not None else []:
        note = _field(seg, 3)
        if note:
            notes.append(note)

    return ParsedReferral(
        message_control_id=control_id,
        sending_facility=_field(msh, 4),
        patient=patient,
        referring_provider=provider,
        hl7_priority=priority,
        specialty=specialty,
        diagnoses=diagnoses,
        clinical_notes="\n".join(notes),
    )
