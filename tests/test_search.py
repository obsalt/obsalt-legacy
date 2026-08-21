from __future__ import annotations

from obsalt.domain.enums import Provider
from obsalt.pipeline import IngestPipeline
from obsalt.search.embeddings import HashingEmbedder, cosine, tokenize
from obsalt.search.index import VectorIndex
from obsalt.store import MemoryStore
from tests.conftest import load_fixture


def test_tokenize_stems_refunds() -> None:
    assert "refund" in tokenize("customers asking about refunds")


def test_similar_transcripts_rank_higher() -> None:
    embedder = HashingEmbedder()
    refund = embedder.embed("I want a refund for my order this charge is wrong")
    query = embedder.embed("customers asking about refunds")
    weather = embedder.embed("what's the weather in lisbon today")
    assert cosine(query, refund) > cosine(query, weather)


def test_pipeline_semantic_search_finds_refund_call() -> None:
    store = MemoryStore()
    pipeline = IngestPipeline(store=store)
    refund = pipeline.ingest(Provider.VAPI, load_fixture("vapi_end_of_call.json"), org_id="org")
    other = pipeline.ingest(Provider.RETELL, load_fixture("retell_call_ended.json"), org_id="org")
    hits = store.search("org", "customers asking about refunds", limit=5)
    assert hits
    assert hits[0].call_id == refund.call_id
    assert other.call_id != hits[0].call_id or hits[0].score > 0
