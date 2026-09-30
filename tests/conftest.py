"""Shared fixtures: synthetic multi-domain PDFs + sample spaCy docs."""
from __future__ import annotations

import pytest

SAMPLE_TEXTS = {
    "business": "Acme Corporation acquired Beta Industries for $500 million on March 3, 2024. The CEO announced the merger in New York.",
    "healthcare": "Dr. Sharma prescribed Metformin to the patient at City Hospital on June 12, 2023. The treatment reduced blood sugar levels.",
    "education": "ABC University appointed Dr. Sharma as the director of its research center. The appointment was announced in September 2022.",
    "research": "The research team published a paper on quantum computing in Nature in 2023. The study was funded by the National Science Foundation.",
    "legal": "The Supreme Court ruled that the defendant breached the contract signed in 2021. The judgment awarded $2 million in damages.",
}


@pytest.fixture(scope="session")
def sample_pdfs(tmp_path_factory):
    """Create one synthetic PDF per domain using reportlab."""
    from reportlab.pdfgen import canvas

    base = tmp_path_factory.mktemp("pdfs")
    paths = {}
    for domain, text in SAMPLE_TEXTS.items():
        p = base / f"{domain}.pdf"
        c = canvas.Canvas(str(p))
        c.drawString(72, 750, text)
        c.showPage()
        c.save()
        paths[domain] = p
    return paths


@pytest.fixture(scope="session")
def nlp_model():
    pytest.importorskip("spacy")
    from src.nlp_processor import get_nlp
    try:
        return get_nlp("en_core_web_lg")
    except OSError:
        pytest.skip("en_core_web_lg not installed")
