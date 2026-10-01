"""Domain-independent entity extraction using spaCy NER + noun chunks."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ExtractedEntity:
    text: str
    normalized_name: str
    entity_type: str  # spaCy label e.g. PERSON/ORG/GPE, or NOUN_PHRASE
    document: str
    page_number: int
    sentence: str
    start_char: int | None = None
    end_char: int | None = None
    extraction_method: str = "spacy_ner"
    alternatives: list[str] = field(default_factory=list)


_ARTICLES = {"the", "a", "an"}

_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir", "madam"}

# Corporate suffixes stripped from the END for canonical matching only.
# Display name (ExtractedEntity.text) is untouched.
_CORP_SUFFIXES = {
    "corporation", "incorporated", "corp", "inc", "ltd", "limited",
    "llc", "plc", "co", "company", "pvt", "private", "holdings",
    "group", "gmbh", "sarl", "pty", "services", "systems",
}


def normalize_name(text: str) -> str:
    """Canonical form for matching (not display).

    - lowercase, possessive ('s) removed, punctuation -> space
    - leading articles / honorifics stripped (the, Dr., Mr. ...)
    - trailing corporate suffixes stripped (Corp, Inc, Ltd, ...)
    So "The ABC Bank Ltd.", "ABC Bank", "ABC Bank's" -> "abc bank".
    """
    t = (text or "").strip().lower()
    if not t:
        return ""
    # normalize unicode apostrophes/quotes
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    # strip surrounding quotes/brackets
    t = t.strip("\"'()[]{}.,;:!?")
    # remove possessive 's (trailing or internal "acme's" -> "acme")
    t = re.sub(r"'s\b", "", t)
    # punctuation -> space (keeps a-z0-9 + space)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    tokens = t.split()
    # strip leading articles / honorifics
    while tokens and tokens[0] in _ARTICLES | _HONORIFICS:
        tokens.pop(0)
    # strip trailing corp suffixes (handles "pvt ltd", "co inc", ...)
    while tokens and tokens[-1] in _CORP_SUFFIXES:
        tokens.pop()
        # "pvt ltd" -> after popping "ltd", "pvt" also pops in next loop
    t = " ".join(tokens)
    return re.sub(r"\s+", " ", t).strip()


def _clean_display(text: str) -> str:
    """Display text: collapse all whitespace (table newlines) to single spaces."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _sentence_of(span) -> str:
    try:
        return re.sub(r"\s+", " ", span.sent.text).strip()
    except Exception:
        try:
            return re.sub(r"\s+", " ", span.text).strip()
        except Exception:
            return ""


# Production filters: numeric/date/money labels are attributes, not graph nodes.
# Generic boilerplate phrases seen in real PDFs (extend per corpus).
DEFAULT_DROP_TYPES = {"CARDINAL", "ORDINAL", "QUANTITY", "PERCENT", "DATE", "TIME", "MONEY"}

GENERIC_PHRASES = {
    "all names", "real customers", "financial transactions", "fictional",
    "classification synthetic", "all identifiers", "amounts and events",
}


def _is_generic(text: str) -> bool:
    t = (text or "").lower()
    return any(g in t for g in GENERIC_PHRASES)


def extract_entities_from_doc(doc, document: str, page_number: int,
                              use_noun_chunks: bool = True,
                              min_chunk_tokens: int = 1,
                              max_chunk_tokens: int = 4,
                              max_chars: int = 80,
                              drop_types: set[str] | None = None) -> list[ExtractedEntity]:
    entities: list[ExtractedEntity] = []
    seen_spans: list[tuple[int, int]] = []
    drop = drop_types if drop_types is not None else DEFAULT_DROP_TYPES

    for ent in doc.ents:
        if ent.label_ in drop:
            continue
        txt = _clean_display(ent.text)
        if len(txt) > max_chars or _is_generic(txt):
            continue
        sent = _sentence_of(ent)
        entities.append(ExtractedEntity(
            text=txt,
            normalized_name=normalize_name(ent.text),
            entity_type=ent.label_,
            document=document,
            page_number=page_number,
            sentence=sent,
            start_char=ent.start_char,
            end_char=ent.end_char,
            extraction_method="spacy_ner",
        ))
        seen_spans.append((ent.start, ent.end))

    def overlaps(chunk) -> bool:
        return any(chunk.start < e and chunk.end > s for s, e in seen_spans)

    if use_noun_chunks:
        for chunk in doc.noun_chunks:
            n_tokens = len([t for t in chunk if not t.is_punct and not t.is_space])
            if not (min_chunk_tokens <= n_tokens <= max_chunk_tokens):
                continue
            txt = _clean_display(chunk.text)
            if len(txt) < 2 or len(txt) > max_chars:
                continue
            if _is_generic(txt):
                continue
            # skip pronouns / stopword-only chunks
            if all(t.is_stop or t.pos_ == "PRON" for t in chunk if not t.is_punct):
                continue
            if overlaps(chunk):
                continue
            sent = _sentence_of(chunk)
            entities.append(ExtractedEntity(
                text=txt,
                normalized_name=normalize_name(txt),
                entity_type="NOUN_PHRASE",
                document=document,
                page_number=page_number,
                sentence=sent,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                extraction_method="spacy_noun_chunk",
            ))
            seen_spans.append((chunk.start, chunk.end))
    return entities


def extract_entities(segments_with_docs, use_noun_chunks: bool = True,
                     min_chunk_tokens: int = 1,
                     max_chunk_tokens: int = 4,
                     max_chars: int = 80,
                     drop_types: set[str] | None = None) -> list[ExtractedEntity]:
    """segments_with_docs: iterable of (segment, spacy_doc|None)."""
    all_entities: list[ExtractedEntity] = []
    for seg, doc in segments_with_docs:
        if doc is None:
            continue
        all_entities.extend(extract_entities_from_doc(
            doc, seg.document, seg.page_number, use_noun_chunks,
            min_chunk_tokens, max_chunk_tokens, max_chars, drop_types))
    log.info("Extracted %d entity mentions.", len(all_entities))
    return all_entities
