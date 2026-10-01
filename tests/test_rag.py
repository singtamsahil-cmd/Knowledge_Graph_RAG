"""GraphRAG tests: retrieval ranking + prompt grounding (Ollama mocked, no network)."""
import json

from src import rag


REL = {"subject": "ABC Bank", "predicate": "approve", "object": "Home Loan LN30002",
       "relation_type": "APPROVE", "evidence": "ABC Bank approved Home Loan LN30002.",
       "document": "bank.pdf", "page": 2, "confidence": 0.9}
OTHER = {"subject": "Priya Singh", "predicate": "work at", "object": "Meridian Tech",
         "relation_type": "WORK_AT", "evidence": "Priya works at Meridian.",
         "document": "corp.pdf", "page": 1, "confidence": 0.7}


def test_question_tokens_drop_stopwords():
    toks = rag.question_tokens("Who approved the Home Loan?")
    assert {"approved", "home", "loan"} <= toks
    assert "who" not in toks and "the" not in toks


def test_retrieve_from_json_ranks_overlap(tmp_path):
    (tmp_path / "relationships.json").write_text(json.dumps([OTHER, REL]), encoding="utf-8")
    hits = rag.retrieve_from_json("Who approved Home Loan LN30002?", out_dir=tmp_path, top_k=5)
    assert hits and hits[0]["predicate"] == "approve"


def test_retrieve_from_json_no_overlap(tmp_path):
    (tmp_path / "relationships.json").write_text(json.dumps([OTHER]), encoding="utf-8")
    assert rag.retrieve_from_json("Quantum banana orbits?", out_dir=tmp_path) == []


def test_build_context_cites_evidence():
    ctx = rag.build_context([REL])
    assert "ABC Bank" in ctx and "Home Loan LN30002" in ctx
    assert "ABC Bank approved Home Loan LN30002." in ctx


def test_answer_no_facts_returns_dont_know(tmp_path):
    (tmp_path / "relationships.json").write_text(json.dumps([OTHER]), encoding="utf-8")
    (tmp_path / "entities.json").write_text("[]", encoding="utf-8")
    res = rag.answer_question("Quantum banana orbits?",
                              cfg={"ollama": {}, "qdrant": {"enabled": False}},
                              store=None, out_dir=tmp_path)
    assert res["facts"] == [] and "don't know" in res["answer"]


def test_ask_ollama_posts_grounded_prompt(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"response": "ABC Bank did."}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr(rag.urllib.request, "urlopen", fake_urlopen)
    out = rag.ask_ollama("Who approved it?", "CTX", model="m", base_url="http://h:11434")
    assert out == "ABC Bank did."
    assert seen["url"] == "http://h:11434/api/generate"
    assert seen["body"]["stream"] is False
    assert "ONLY" in seen["body"]["system"]  # grounding prompt present


def test_api_ask_route_registered():
    import src.api as api
    assert any(r.path == "/ask" for r in api.app.routes)


def test_cloud_uses_bearer_and_cloud_base(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"response": "hi"}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResp()

    monkeypatch.setattr(rag.urllib.request, "urlopen", fake_urlopen)
    out = rag.ask_ollama("q?", "ctx", model="gemma4:31b",
                         base_url="https://ollama.com/api", api_key="k123")
    assert out == "hi"
    assert seen["url"] == "https://ollama.com/api/api/generate" or \
        seen["url"] == "https://ollama.com/api/generate"
    assert seen["auth"] == "Bearer k123"


def test_cloud_chat_api_shape(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"message": {"content": "hi"}}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr(rag.urllib.request, "urlopen", fake_urlopen)
    out = rag.ask_ollama("q?", "ctx", model="gemma4:31b",
                         base_url="https://ollama.com/api", api_key="k",
                         chat_api=True)
    assert out == "hi"
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["body"]["messages"][0]["role"] == "system"


def test_local_sends_no_auth_header(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"response": "hi"}).encode()

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return FakeResp()

    monkeypatch.setattr(rag.urllib.request, "urlopen", fake_urlopen)
    rag.ask_ollama("q?", "ctx", model="m", base_url="http://localhost:11434")
    assert seen["auth"] in (None, "")


def test_resolve_llm_cloud_defaults_and_key_guard(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    cfg = {"llm": {"provider": "ollama-cloud"}}
    llm = rag.resolve_llm(cfg)
    assert llm["provider"] == "ollama-cloud"
    assert llm["model"] == "gemma4:31b"
    assert llm["base_url"] == "https://ollama.com/api"
    import pytest
    with pytest.raises(ValueError, match="API key"):
        rag.answer_question("q?", cfg=cfg, store=None, out_dir=".")
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        rag.resolve_llm({"llm": {"provider": "nope"}})
