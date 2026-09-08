#!/usr/bin/env python3
"""Shared plumbing for the semantic trade search: paths, the embedding model, and the
Milvus Lite client. Heavy imports (sentence-transformers/torch, pymilvus) are deferred to
call time so the pure helpers and the tests stay importable without them installed."""

from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
INDEX_PATH = REPO_DIR / "trades_index.db"          # Milvus Lite single-file store
META_PATH = REPO_DIR / "ticker_meta.json"          # ticker -> {name, sector, industry}
COLLECTION = "trades"

# bge asks for an instruction prefix on the *query* side only; documents are embedded bare.
# Both sides are L2-normalised so the collection can score with plain cosine similarity.
MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
METRIC = "COSINE"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_MODEL = None


def load_model():
    """Load (once) and return the sentence-transformers embedding model."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(MODEL_NAME)
    return _MODEL


def embed_documents(texts: list[str]):
    return load_model().encode(texts, normalize_embeddings=True,
                               batch_size=64, show_progress_bar=False)


def embed_query(text: str):
    return load_model().encode([QUERY_PREFIX + text], normalize_embeddings=True)


def open_client(load: bool = True):
    """Open the Milvus Lite store. `load=True` pages the collection in for search/query
    (a freshly opened client leaves it in the 'released' state otherwise)."""
    from pymilvus import MilvusClient
    client = MilvusClient(str(INDEX_PATH))
    if load and client.has_collection(COLLECTION):
        client.load_collection(COLLECTION)
    return client
