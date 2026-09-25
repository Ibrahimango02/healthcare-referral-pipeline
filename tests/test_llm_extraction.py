"""The real LLM path, with the Anthropic client replaced by a fake."""
from types import SimpleNamespace

import anthropic

from intake import extraction
from intake.hl7_parser import parse_referral
from intake.models import Urgency


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
            "requested_procedure": "CT head without contrast",
            "body_site": "head",
            "clinical_indication": "Thunderclap headache with confusion; rule out hemorrhage.",
            "urgency": "stat",
            "suspected_conditions": ["subarachnoid hemorrhage"],
            "missing_information": [],
            "confidence": 0.92,
        })])


def test_llm_extraction_uses_forced_tool_call(monkeypatch, sample):
    fake = FakeMessages()
    monkeypatch.setattr(anthropic, "Anthropic", lambda: SimpleNamespace(messages=fake))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    ext = extraction.extract_fields(parse_referral(sample("referral_needs_review")))

    assert fake.kwargs["tool_choice"] == {"type": "tool", "name": "record_referral_fields"}
    assert "worst of his life" in fake.kwargs["messages"][0]["content"]
    assert ext.urgency == Urgency.STAT
    assert ext.requested_procedure == "CT head without contrast"
    assert ext.extractor.startswith("anthropic:")
