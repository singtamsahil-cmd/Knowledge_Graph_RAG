"""spaCy model loading + batch NLP processing."""
from __future__ import annotations

import logging
from collections.abc import Iterable

log = logging.getLogger(__name__)

_nlp = None


def get_nlp(model: str = "en_core_web_lg"):
    """Load spaCy model once (cached)."""
    global _nlp
    if _nlp is not None:
        return _nlp
    import spacy

    try:
        _nlp = spacy.load(model)
        log.info("Loaded spaCy model '%s'.", model)
    except OSError as exc:
        raise OSError(
            f"spaCy model '{model}' not found. Install it with "
            f"`python -m spacy download {model}`. Original error: {exc}"
        ) from exc
    return _nlp


def reset_nlp_cache():
    global _nlp
    _nlp = None


def process_segments_nlp(segments, model: str = "en_core_web_lg", batch_size: int = 32):
    """Yield (segment, doc) pairs using nlp.pipe for batch processing.

    Empty segments still yield a (segment, None) pair so provenance is never lost.
    """
    nlp = get_nlp(model)
    texts = [s.text for s in segments]
    # nlp.pipe can't handle empty strings well for our bookkeeping, so track indices
    non_empty_idx = [i for i, t in enumerate(texts) if t and t.strip()]
    non_empty_texts = [texts[i] for i in non_empty_idx]
    docs_map: dict[int, object] = {}
    if non_empty_texts:
        for idx, doc in zip(non_empty_idx, nlp.pipe(non_empty_texts, batch_size=batch_size)):
            docs_map[idx] = doc
    for i, seg in enumerate(segments):
        yield seg, docs_map.get(i)
