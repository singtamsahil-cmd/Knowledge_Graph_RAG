"""Generic dependency-parse-based candidate relationship extraction.

No domain-specific rules. Uses grammatical structure only:
SVO, SVA (attr), prepositional (prep/pobj/obl/case), passive (nsubjpass),
compound noun phrases, and negation (neg).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

SUBJ_DEPS = {"nsubj", "nsubjpass", "csubj", "csubjpass"}
OBJ_DEPS = {"dobj", "obj", "attr", "oprd", "dative"}
PREP_DEPS = {"prep"}
POBJ_DEPS = {"pobj", "obl"}
NEG_DEPS = {"neg"}

EXTRACTION_METHOD = "spacy_dependency"


@dataclass
class CandidateRelation:
    subject_text: str
    predicate: str
    object_text: str
    relation_type: str
    sentence: str
    document: str
    page_number: int
    extraction_method: str = EXTRACTION_METHOD
    review_status: str = "candidate"
    negated: bool = False
    # resolved IDs filled in later by pipeline/graph_builder
    subject_id: str | None = None
    object_id: str | None = None


def normalize_relation(predicate: str) -> str:
    """Generic normalization: lowercase, strip, non-alnum -> underscore, upper."""
    p = (predicate or "").strip().lower()
    p = re.sub(r"[^a-z0-9]+", "_", p).strip("_")
    return p.upper() if p else "RELATED_TO"


def _expand_phrase(token) -> str:
    """Expand a token into its noun-phrase-ish surface form.

    Uses the token's subtree but constrained to a contiguous span around the
    token, excluding verbs/clauses and unrelated content. Falls back to token text.
    """
    try:
        subtree = sorted(list(token.subtree), key=lambda t: t.i)
        # keep only tokens in the same clause-ish window: drop verbs other than token itself,
        # drop punctuation at edges
        kept = [t for t in subtree
                if t.pos_ not in ("VERB", "AUX") or t == token]
        # must be contiguous-ish; take min..max but cap length to avoid swallowing sentences
        if not kept:
            return token.text.strip()
        lo, hi = kept[0].i, kept[-1].i
        if hi - lo > 12:  # too wide -> fall back to compounds/amods only
            parts = [c.text for c in sorted(token.children, key=lambda t: t.i)
                     if c.dep_ in ("compound", "amod", "poss", "det", "nummod") or c == token]
            parts = sorted(set(parts), key=lambda w: token.doc.text.find(w))
            return " ".join(parts).strip() or token.text.strip()
        span = token.doc[lo:hi + 1]
        text = span.text.strip().strip(".,;:()\"'").strip()
        return text if text else token.text.strip()
    except Exception:
        return token.text.strip()


def _has_negation(verb) -> bool:
    for child in verb.children:
        if child.dep_ in NEG_DEPS:
            return True
    return False


def _prep_relations(verb, subj_text: str, sentence: str, document: str,
                    page: int, negated: bool) -> list[CandidateRelation]:
    rels: list[CandidateRelation] = []
    for child in verb.children:
        if child.dep_ in PREP_DEPS:
            prep = child.text
            for pobj in child.children:
                if pobj.dep_ in POBJ_DEPS or pobj.pos_ in ("NOUN", "PROPN", "PRON", "NUM"):
                    if pobj.dep_ not in POBJ_DEPS and pobj.dep_ not in ("pobj", "obl", "obj"):
                        # allow bare nominal children of prep conservatively
                        pass
                    obj_text = _expand_phrase(pobj)
                    if not obj_text or not subj_text:
                        continue
                    predicate = f"{verb.lemma_} {prep}".strip()
                    # attribute-style: "director of research center"
                    rels.append(CandidateRelation(
                        subject_text=subj_text, predicate=predicate,
                        object_text=obj_text,
                        relation_type=normalize_relation(predicate),
                        sentence=sentence, document=document, page_number=page,
                        negated=negated,
                    ))
        # Universal-Dependencies style: obl + case
        if child.dep_ in ("obl", "obj") and child.pos_ in ("NOUN", "PROPN", "PRON"):
            case_markers = [c.text for c in child.children if c.dep_ == "case"]
            prep = case_markers[0] if case_markers else ""
            obj_text = _expand_phrase(child)
            predicate = f"{verb.lemma_} {prep}".strip()
            rels.append(CandidateRelation(
                subject_text=subj_text, predicate=predicate,
                object_text=obj_text,
                relation_type=normalize_relation(predicate),
                sentence=sentence, document=document, page_number=page,
                negated=negated,
            ))
    return rels


def extract_relations_from_sentence(sent, document: str, page_number: int) -> list[CandidateRelation]:
    rels: list[CandidateRelation] = []
    sentence_text = sent.text.strip()
    for token in sent:
        if token.pos_ not in ("VERB", "AUX"):
            continue
        verb = token
        # subjects
        subjects = [c for c in verb.children if c.dep_ in SUBJ_DEPS]
        # handle conjunct verbs sharing subject: "appointed X and founded Y"
        if not subjects and verb.dep_ == "conj" and verb.head.pos_ in ("VERB", "AUX"):
            subjects = [c for c in verb.head.children if c.dep_ in SUBJ_DEPS]
        if not subjects:
            continue
        negated = _has_negation(verb)
        # direct objects / attributes
        objects = [c for c in verb.children if c.dep_ in OBJ_DEPS]
        for subj in subjects:
            # skip clausal subjects that would swallow content
            subj_text = _expand_phrase(subj)
            if not subj_text:
                continue
            for obj in objects:
                obj_text = _expand_phrase(obj)
                if not obj_text or obj_text == subj_text:
                    continue
                predicate = verb.lemma_
                # passive: "X was appointed by Y" -> nsubjpass X, agent Y
                if subj.dep_ == "nsubjpass":
                    # still record X <-verb- ... and look for agent below via prep
                    pass
                rels.append(CandidateRelation(
                    subject_text=subj_text, predicate=predicate,
                    object_text=obj_text,
                    relation_type=normalize_relation(predicate),
                    sentence=sentence_text, document=document, page_number=page_number,
                    negated=negated,
                ))
            # prepositional relations off the verb
            rels.extend(_prep_relations(verb, subj_text, sentence_text, document, page_number, negated))
            # passive agent: "appointed by the board"
            if subj.dep_ == "nsubjpass":
                for child in verb.children:
                    if child.dep_ == "agent":
                        for pobj in child.children:
                            if pobj.dep_ == "pobj":
                                agent = _expand_phrase(pobj)
                                if agent:
                                    rels.append(CandidateRelation(
                                        subject_text=agent, predicate=verb.lemma_,
                                        object_text=subj_text,
                                        relation_type=normalize_relation(verb.lemma_),
                                        sentence=sentence_text, document=document,
                                        page_number=page_number, negated=negated,
                                    ))
    return rels


def extract_relations_from_doc(doc, document: str, page_number: int) -> list[CandidateRelation]:
    rels: list[CandidateRelation] = []
    for sent in doc.sents:
        # skip very short / fragment sentences with no verb
        if len([t for t in sent if not t.is_punct]) < 3:
            continue
        rels.extend(extract_relations_from_sentence(sent, document, page_number))
    return rels


def extract_relationships(segments_with_docs) -> list[CandidateRelation]:
    all_rels: list[CandidateRelation] = []
    for seg, doc in segments_with_docs:
        if doc is None:
            continue
        all_rels.extend(extract_relations_from_doc(doc, seg.document, seg.page_number))
    log.info("Extracted %d candidate relationships.", len(all_rels))
    return all_rels
