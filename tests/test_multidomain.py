"""Multi-domain generalization: same engine, 5 unrelated domains, no tuning.

Each doc plants checkable facts. Asserts: key entities found, relations
extracted, alias merged, provenance complete, no crash. Cross-document
identity is verified via deterministic IDs (same canonical -> same node).
"""
import pytest

from src.entity_resolver import deterministic_id

DOMAINS = {
    "healthcare": (
        "Dr. Anita Desai prescribed Metformin to patient Ravi Kumar at "
        "St. Mary's Hospital on 4 April 2026. St Marys Hospital reviewed "
        "the prescription. Ravi Kumar recovered after treatment.",
        ["Anita Desai", "Ravi Kumar", "Metformin"],
        "st marys hospital",  # alias pair must merge
    ),
    "legal": (
        "Judge Arjun Mehta dismissed the petition filed by Orion Labs on "
        "12 May 2026. Orion Labs appealed the decision. The court awarded "
        "costs to the respondent.",
        ["Arjun Mehta", "Orion Labs"],
        None,
    ),
    "research": (
        "Dr. Lena Fischer published a paper on CRISPR diagnostics in Nature "
        "in 2025. The Helix Foundation funded the study. Fischer presented "
        "the results at the Berlin summit.",
        ["Lena Fischer", "Helix Foundation"],
        None,
    ),
    "education": (
        "Greenfield University appointed Dr. Omar Khan as Dean of Engineering. "
        "Omar Khan will lead the robotics programme. The university opened "
        "a new laboratory in September.",
        ["Greenfield University", "Omar Khan"],
        None,
    ),
    "logistics": (
        "TransGlobal Freight shipped containers from Mumbai Port to Hamburg on "
        "9 June 2026. EuroDock Logistics received the shipment. The port "
        "authority inspected the cargo.",
        ["TransGlobal Freight", "EuroDock Logistics"],
        None,
    ),
}


def _make_pdf(path, text):
    import textwrap
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    y = 750
    for line in textwrap.wrap(text, width=90):
        c.drawString(72, y, line)
        y -= 15
    c.showPage()
    c.save()


@pytest.fixture(scope="module")
def domain_results(tmp_path_factory):
    from src.pipeline import run_pipeline
    base = tmp_path_factory.mktemp("multidomain")
    out = {}
    for domain, (text, _, _) in DOMAINS.items():
        pdf = base / f"{domain}.pdf"
        _make_pdf(pdf, text)
        res = run_pipeline(pdf=str(pdf), write_to_neo4j=False, export=False,
                           force=True)
        out[domain] = res
    return out


@pytest.mark.parametrize("domain", list(DOMAINS))
def test_domain_entities_found(domain_results, domain):
    import re
    _, expected, _ = DOMAINS[domain]
    names = " | ".join(
        [e.name for e in domain_results[domain]["entities"]] +
        [a for e in domain_results[domain]["entities"] for a in e.alternative_names]
    ).lower()
    names = re.sub(r"\s+", " ", names)  # PDF line-breaks can split mentions
    for ent in expected:
        assert ent.lower() in names, f"{ent} missing in {domain}: {names[:300]}"


@pytest.mark.parametrize("domain", list(DOMAINS))
def test_domain_relations_with_provenance(domain_results, domain):
    rels = domain_results[domain]["relations"]
    assert len(rels) >= 1, f"no relations in {domain}"
    for r in rels:
        assert r.sentence and r.document and r.predicate
        assert r.subject_id and r.object_id  # linked or placeholder


def test_alias_resolution_healthcare(domain_results):
    ents = domain_results["healthcare"]["entities"]
    matches = [e for e in ents if e.normalized_name == "st marys hospital"]
    assert len(matches) == 1, "St. Mary's / St Marys Hospital must merge"
    assert len(matches[0].mentions) >= 2


def test_cross_document_identity_is_deterministic():
    a = deterministic_id("omar khan", "PERSON")
    b = deterministic_id("omar khan", "PERSON")
    assert a == b  # same canonical mention -> same node across documents


def test_no_duplicate_relations(domain_results):
    for domain, res in domain_results.items():
        keys = [(r.subject_id, r.predicate, r.object_id) for r in res["relations"]]
        assert len(keys) == len(set(keys)), f"dup relations in {domain}"


def test_review_statuses_assigned(domain_results):
    allowed = {"verified", "candidate", "uncertain", "contradicted"}
    for domain, res in domain_results.items():
        for r in res["relations"]:
            assert r.review_status in allowed, r.review_status
