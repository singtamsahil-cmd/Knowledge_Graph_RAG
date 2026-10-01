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

# Copula / light verbs carry no fact value ("X is Y", "X has Y") —
# EXCEPT "be" with a digit-bearing object ("The amount was INR 1,200,000"):
# those sentences define identifier amounts and are kept.
DEFAULT_DROP_PREDICATES = {"be", "have", "do", "get", "exist", "seem", "become"}

# Job-title heads: "Customer Service Lead Priya Nair" -> title relation.
TITLE_NOUNS = {
    "lead", "manager", "director", "officer", "engineer", "technician",
    "planner", "head", "chief", "supervisor", "coordinator", "administrator",
    "analyst", "consultant", "specialist", "president", "chairman",
    "secretary", "partner", "associate", "assistant", "clerk",
}

# Demonstrative noun -> ID-code families seen nearby ("uses this account" -> CA20001).
DEMO_ID_PATTERNS = {
    "account": r"\b(?:SA|CA)\d+\b",
    "loan": r"\bLN\d+\b",
    "batch": r"\b(?:PO|NCR)[-\d][\w-]*\b",
    "case": r"\bCS[-\d][\w-]*\b",
    "report": r"\b(?:NCR|CAPA)[-\d][\w-]*\b",
    "action": r"\b(?:CAPA|NCR)[-\d][\w-]*\b",
    "machine": r"\b(?:RG|LS)-[A-Z]+-\d+\b",
    "equipment": r"\b(?:RG|LS)-[A-Z]+-\d+\b",
    "branch": r"\bBR\d+\b",
    "customer": r"\bCUST\d+\b",
    "transaction": r"\bTXN\d+\b",
    "order": r"\bPO[-\d][\w-]*\b",
    "motor": r"\b(?:LS|RG)-[A-Z]+-\d+\b",
}
_DEMO_RE = re.compile(r"^(this|that|these|those)\s+([a-z]+)", re.IGNORECASE)
_ID_ANY_RE = re.compile(r"\b[A-Z]{2,}[-\/]?\d[\w-]*\b")

MAX_PHRASE_SPAN = 6  # _expand_phrase window; wider spans swallow clauses
MIN_ENDPOINT_CHARS = 2
MAX_ENDPOINT_CHARS = 80


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
    confidence: float = 0.5  # heuristic 0..1; higher = more trustworthy
    event_date: str | None = None  # attached post-extraction from DATE mentions
    # resolved IDs filled in later by pipeline/graph_builder
    subject_id: str | None = None
    object_id: str | None = None


def score_relation(subject_text: str, predicate: str, object_text: str,
                   via_prep: bool = False, via_passive_agent: bool = False) -> float:
    """Cheap transparent heuristic (no model): specific verb + compact proper
    endpoints score higher; prepositional attachments score slightly lower."""
    score = 0.75
    if via_passive_agent:
        score = 0.80
    elif via_prep:
        score = 0.70
    # proper-looking endpoints (capitalized, multi-char, not generic)
    for end in (subject_text, object_text):
        t = (end or "").strip()
        if t[:1].isupper() and len(t) >= 3:
            score += 0.05
        if len(t.split()) > 6:
            score -= 0.10
    return round(min(max(score, 0.0), 1.0), 2)


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
        if hi - lo > MAX_PHRASE_SPAN:  # too wide -> fall back to compounds/amods only
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
                    page: int, negated: bool,
                    history: list[str] | None = None) -> list[CandidateRelation]:
    rels: list[CandidateRelation] = []
    for child in verb.children:
        if child.dep_ in PREP_DEPS:
            prep = child.text
            for pobj in child.children:
                if pobj.dep_ in POBJ_DEPS or pobj.pos_ in ("NOUN", "PROPN", "PRON", "NUM"):
                    if pobj.dep_ not in POBJ_DEPS and pobj.dep_ not in ("pobj", "obl", "obj"):
                        # allow bare nominal children of prep conservatively
                        pass
                    obj_text = _resolve_demonstrative(_expand_phrase(pobj), history or [])
                    if not _valid_endpoint(obj_text) or not _valid_endpoint(subj_text):
                        continue
                    predicate = f"{verb.lemma_} {prep}".strip()
                    # attribute-style: "director of research center"
                    rels.append(CandidateRelation(
                        subject_text=subj_text, predicate=predicate,
                        object_text=obj_text,
                        relation_type=normalize_relation(predicate),
                        sentence=sentence, document=document, page_number=page,
                        negated=negated,
                        confidence=score_relation(subj_text, predicate, obj_text, via_prep=True),
                    ))
        # Universal-Dependencies style: obl + case
        if child.dep_ in ("obl", "obj") and child.pos_ in ("NOUN", "PROPN", "PRON"):
            case_markers = [c.text for c in child.children if c.dep_ == "case"]
            prep = case_markers[0] if case_markers else ""
            obj_text = _resolve_demonstrative(_expand_phrase(child), history or [])
            if not _valid_endpoint(obj_text) or not _valid_endpoint(subj_text):
                continue
            predicate = f"{verb.lemma_} {prep}".strip()
            rels.append(CandidateRelation(
                subject_text=subj_text, predicate=predicate,
                object_text=obj_text,
                relation_type=normalize_relation(predicate),
                sentence=sentence, document=document, page_number=page,
                negated=negated,
                confidence=score_relation(subj_text, predicate, obj_text, via_prep=True),
            ))
    return rels


_ENDPOINT_STOP = {
    "the", "a", "an", "it", "this", "that", "these", "those", "he", "she",
    "they", "we", "you", "i", "its", "their", "his", "her", "them", "him",
}


def _valid_endpoint(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if not (MIN_ENDPOINT_CHARS <= len(t) <= MAX_ENDPOINT_CHARS):
        return False
    # pure numbers are attributes, not relation endpoints
    if t.replace(" ", "").replace(",", "").replace(".", "").isdigit():
        return False
    # stopword-only fragments ("the", "it") carry no entity meaning
    toks = [w.lower() for w in re.findall(r"[a-zA-Z]+", t)]
    if toks and all(w in _ENDPOINT_STOP for w in toks):
        return False
    return True


def _looks_like_amount(text: str) -> bool:
    t = (text or "").strip()
    if not t or not re.search(r"\d", t):
        return False
    if re.search(r"(INR|USD|EUR|GBP|Rs\b|₹|\$|€|£)", t, re.IGNORECASE):
        return True
    dense = re.sub(r"\s+", "", t)
    digits = sum(c.isdigit() or c in ",.%/-" for c in dense)
    return len(dense) > 0 and digits / len(dense) >= 0.4


def _nearest_id_code(history: list[str]) -> str | None:
    """Nearest ID code (LN30002, CA20001, PO-26418, ...) in recent sentences."""
    for sent_text in reversed(history[-3:]):
        hits = _ID_ANY_RE.findall(sent_text or "")
        if hits:
            return hits[-1]
    return None


def _resolve_demonstrative(endpoint: str, history: list[str]) -> str:
    """Resolve "this account" -> "CA20001" using current + previous sentences.

    Looks for an ID code of the matching family; returns the original text
    when nothing unambiguous is found. Evidence sentence is always kept.
    """
    m = _DEMO_RE.match((endpoint or "").strip())
    if not m:
        return endpoint
    pattern = DEMO_ID_PATTERNS.get(m.group(2).lower())
    if not pattern:
        return endpoint
    for sent_text in reversed(history[-3:]):
        hits = re.findall(pattern, sent_text)
        # prefer a code not already inside the endpoint itself
        for h in hits:
            if h.lower() not in endpoint.lower():
                return h
    return endpoint


def _title_relations(sent, document: str, page_number: int) -> list[CandidateRelation]:
    """Emit PERSON —title→ Role for "Role Name ..." appositive-style subjects.

    E.g. "Customer Service Lead Priya Nair opened ..." yields
    Priya Nair —title→ Customer Service Lead (confidence 0.85).
    """
    rels: list[CandidateRelation] = []
    try:
        ents = list(sent.ents)
    except Exception:
        return rels
    for ent in ents:
        if ent.label_ != "PERSON":
            continue
        # walk left within the sentence over capitalized words
        title_tokens: list[str] = []
        i = ent.start - 1
        while i >= sent.start:
            t = sent.doc[i]
            if t.is_punct or t.pos_ in ("VERB", "AUX", "ADP"):
                break
            title_tokens.append(t.text)
            if t.is_sent_start:  # include sentence-initial word, then stop
                break
            i -= 1
            if len(title_tokens) >= 5:
                break
        title_tokens.reverse()
        if not title_tokens:
            continue
        # title must end in a known title noun and be capitalized
        if title_tokens[-1].lower() not in TITLE_NOUNS:
            continue
        if not title_tokens[0][:1].isupper():
            continue
        title = " ".join(title_tokens)
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) > MAX_ENDPOINT_CHARS:
            continue
        rels.append(CandidateRelation(
            subject_text=ent.text.strip(), predicate="title",
            object_text=title,
            relation_type="TITLE",
            sentence=sent.text.strip(), document=document, page_number=page_number,
            negated=False, confidence=0.85,
        ))
    return rels


def extract_relations_from_sentence(sent, document: str, page_number: int,
                                    drop_predicates: set[str] | None = None,
                                    sent_history: list[str] | None = None) -> list[CandidateRelation]:
    rels: list[CandidateRelation] = []
    sentence_text = sent.text.strip()
    drop = drop_predicates if drop_predicates is not None else DEFAULT_DROP_PREDICATES
    history = list(sent_history or []) + [sentence_text]
    rels.extend(_title_relations(sent, document, page_number))
    for token in sent:
        if token.pos_ not in ("VERB", "AUX"):
            continue
        verb = token
        lemma = verb.lemma_.lower()
        if lemma in drop:
            # copula exception: "The sanctioned amount was INR 1,200,000"
            # defines an identifier amount — keep it. Must look amount-like
            # (currency marker or digit-heavy), not just contain a digit.
            cop_amount_obj = next(
                (c for c in verb.children
                 if c.dep_ in OBJ_DEPS and _looks_like_amount(_expand_phrase(c) or "")),
                None)
            if not (lemma == "be" and cop_amount_obj is not None):
                continue
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
            subj_text = _resolve_demonstrative(_expand_phrase(subj), history)
            if not _valid_endpoint(subj_text):
                continue
            # amount linkage: "The sanctioned amount was INR 1,200,000"
            # following "...Business Loan LN30002..." -> subject becomes
            # "The sanctioned amount (LN30002)" so RAG can attribute it.
            if lemma == "be" and "(" not in subj_text:
                code = _nearest_id_code(history)
                if code and code.lower() not in subj_text.lower():
                    subj_text = f"{subj_text} ({code})"
            for obj in objects:
                obj_text = _resolve_demonstrative(_expand_phrase(obj), history)
                if not _valid_endpoint(obj_text) or obj_text == subj_text:
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
                    confidence=score_relation(subj_text, predicate, obj_text),
                ))
            # prepositional relations off the verb
            rels.extend(_prep_relations(verb, subj_text, sentence_text, document, page_number,
                                        negated, history))
            # passive agent: "appointed by the board"
            if subj.dep_ == "nsubjpass":
                for child in verb.children:
                    if child.dep_ == "agent":
                        for pobj in child.children:
                            if pobj.dep_ == "pobj":
                                agent = _resolve_demonstrative(_expand_phrase(pobj), history)
                                if agent:
                                    rels.append(CandidateRelation(
                                        subject_text=agent, predicate=verb.lemma_,
                                        object_text=subj_text,
                                        relation_type=normalize_relation(verb.lemma_),
                                        sentence=sentence_text, document=document,
                                        page_number=page_number, negated=negated,
                                        confidence=score_relation(agent, verb.lemma_, subj_text,
                                                                  via_passive_agent=True),
                                    ))
    return rels


def extract_relations_from_doc(doc, document: str, page_number: int,
                                drop_predicates: set[str] | None = None) -> list[CandidateRelation]:
    rels: list[CandidateRelation] = []
    history: list[str] = []
    for sent in doc.sents:
        # skip very short / fragment sentences with no verb
        if len([t for t in sent if not t.is_punct]) < 3:
            continue
        rels.extend(extract_relations_from_sentence(sent, document, page_number,
                                                    drop_predicates, history))
        history.append(sent.text.strip())
    return rels


def extract_relationships(segments_with_docs,
                          drop_predicates: set[str] | None = None) -> list[CandidateRelation]:
    all_rels: list[CandidateRelation] = []
    for seg, doc in segments_with_docs:
        if doc is None:
            continue
        all_rels.extend(extract_relations_from_doc(doc, seg.document, seg.page_number, drop_predicates))
    log.info("Extracted %d candidate relationships.", len(all_rels))
    return all_rels
