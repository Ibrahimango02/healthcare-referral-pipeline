import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"


@pytest.fixture
def sample():
    return lambda name: (SAMPLES / f"{name}.hl7").read_text()


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Tests never call the real LLM or FHIR server, and write output to a temp dir."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FHIR_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
