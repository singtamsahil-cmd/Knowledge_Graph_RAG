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


def normalize_name(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.lower()


def _sentence_of(span) -> str:
    try:
        return span.sent.text.strip()
    except Exception:
        return span.text.strip()


def extract_entities_from_doc(doc, document: str, page_number: int,
                              use_noun_chunks: bool = True,
                              min_chunk_tokens: int = 1,
                              max_chunk_tokens: int = 6) -> list[ExtractedEntity]:
    entities: list[ExtractedEntity] = []
    seen_spans: list[tuple[int, int]] = []

    for ent in doc.ents:
        sent = _sentence_of(ent)
        entities.append(ExtractedEntity(
            text=ent.text.strip(),
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
            txt = chunk.text.strip()
            if len(txt) < 2:
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
                     max_chunk_tokens: int = 6) -> list[ExtractedEntity]:
    """segments_with_docs: iterable of (segment, spacy_doc|None)."""
    all_entities: list[ExtractedEntity] = []
    for seg, doc in segments_with_docs:
        if doc is None:
            continue
        all_entities.extend(extract_entities_from_doc(
            doc, seg.document, seg.page_number, use_noun_chunks,
            min_chunk_tokens, max_chunk_tokens))
    log.info("Extracted %d entity mentions.", len(all_entities))
    return all_entities
