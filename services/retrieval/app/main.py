"""
Retrieval service.

Owns the vector store (Qdrant). Two responsibilities:
  1. Ingest: pull recent articles from GDELT (no API key required, which keeps
     the whole repo reproducible) -> chunk -> embed -> upsert.
  2. Search: recency- and source-filterable hybrid retrieval.

GDELT's free Doc 2.0 API is used so anyone cloning the repo can run it without
signing up for a key. Embeddings use a small local sentence-transformers model
to avoid an external embedding dependency.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Histogram, make_asgi_app
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

import sys
sys.path.append("/app")
from shared.schemas import Article, RetrievalRequest, RetrievalResponse  # noqa: E402

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "news")
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

app = FastAPI(title="retrieval")
app.mount("/metrics", make_asgi_app())

_embedder: SentenceTransformer | None = None
_client: QdrantClient | None = None

# Prometheus metrics
SEARCH_LATENCY = Histogram("retrieval_search_time_seconds", "Search latency in seconds")
RESULTS_COUNT = Histogram("retrieval_results_count", "Number of results returned per search")
INGEST_DOCS = Counter("retrieval_ingest_documents_total", "Total documents ingested")
INGEST_CHUNKS = Counter("retrieval_ingest_chunks_total", "Total chunks created from ingestion")
INGEST_ERRORS = Counter("retrieval_ingest_errors_total", "Total ingestion errors")


def embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL)
    return _client


def ensure_collection() -> None:
    dim = embedder().get_sentence_embedding_dimension()
    existing = {c.name for c in client().get_collections().collections}
    if COLLECTION not in existing:
        client().create_collection(
            collection_name=COLLECTION,
            vectors_config=models.VectorParams(
                size=dim, distance=models.Distance.COSINE
            ),
        )


def chunk(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    """Simple character chunker with overlap. Good enough for news prose."""
    if not text:
        return []
    out, start = [], 0
    while start < len(text):
        out.append(text[start : start + size])
        start += size - overlap
    return out


@app.on_event("startup")
def _startup() -> None:
    ensure_collection()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _load_sample_corpus() -> dict:
    """Load the bundled offline sample corpus (GDELT-shaped) for demos."""
    path = os.getenv("SAMPLE_CORPUS", "/app/app/sample_corpus.json")
    with open(path) as f:
        return json.load(f)


@app.post("/ingest")
def ingest(query: str = "breaking news", max_records: int = 75) -> dict:
    """Pull recent articles from GDELT and upsert them into Qdrant.

    GDELT's free endpoint rate-limits aggressively (HTTP 429), especially on
    broad queries or rapid repeat calls. We retry with backoff, and if GDELT
    stays unavailable we fall back to a bundled sample corpus so the system is
    always demoable offline. Set ALLOW_SAMPLE_FALLBACK=0 to disable the
    fallback and surface the error instead.
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": str(max_records),
        "format": "json",
        "sort": "DateDesc",
    }

    data = None
    last_err = ""
    for attempt in range(3):
        try:
            with httpx.Client(timeout=60) as hc:
                resp = hc.get(
                    GDELT_URL,
                    params=params,
                    headers={"User-Agent": "news-research-agent/1.0"},
                )
            if resp.status_code == 429:
                last_err = "GDELT rate limit (429)"
                time.sleep(2 ** attempt + 1)  # 2s, 3s, 5s backoff
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.HTTPError, ValueError) as e:
            last_err = str(e)
            time.sleep(2 ** attempt)

    if data is None:
        if os.getenv("ALLOW_SAMPLE_FALLBACK", "1") == "1":
            data = _load_sample_corpus()
            fell_back = True
        else:
            raise HTTPException(
                status_code=502,
                detail=f"GDELT unavailable after retries: {last_err}",
            )
    else:
        fell_back = False

    points: list[models.PointStruct] = []
    articles = data.get("articles", [])
    try:
        emb = embedder()
    except Exception as e:  # model download/load failure
        raise HTTPException(
            status_code=503,
            detail=f"Embedding model not ready: {e}",
        )
    for art in articles:
        title = art.get("title", "")
        url = art.get("url", "")
        domain = art.get("domain", "unknown")
        seendate = art.get("seendate")  # e.g. 20260101T120000Z
        published = None
        if seendate:
            try:
                published = datetime.strptime(
                    seendate, "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        body = f"{title}. {art.get('socialimage','')}".strip()
        for ci, ch in enumerate(chunk(f"{title}. {body}")):
            vec = emb.encode(ch).tolist()
            points.append(
                models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vec,
                    payload={
                        "source_id": f"{domain}-{abs(hash(url)) % 10**8}-{ci}",
                        "title": title,
                        "url": url,
                        "source_name": domain,
                        "published_at": published.isoformat()
                        if published
                        else None,
                        "chunk_text": ch,
                    },
                )
            )
    if points:
        client().upsert(collection_name=COLLECTION, points=points)

    # Record metrics
    INGEST_DOCS.inc(len(articles))
    INGEST_CHUNKS.inc(len(points))

    return {
        "ingested_articles": len(articles),
        "chunks": len(points),
        "source": "sample_fallback" if fell_back else "gdelt",
    }


@app.post("/by_id")
def by_id(payload: dict) -> dict | None:
    """Fetch a single stored chunk by its source_id (used by the MCP server)."""
    source_id = payload.get("source_id", "")
    hits = client().scroll(
        collection_name=COLLECTION,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="source_id", match=models.MatchValue(value=source_id)
                )
            ]
        ),
        limit=1,
    )[0]
    if not hits:
        return None
    p = hits[0].payload
    return Article(
        source_id=p["source_id"],
        title=p["title"],
        url=p["url"],
        source_name=p["source_name"],
        published_at=p.get("published_at"),
        chunk_text=p["chunk_text"],
        score=1.0,
    ).model_dump(mode="json")


@app.post("/search", response_model=RetrievalResponse)
def search(req: RetrievalRequest) -> RetrievalResponse:
    start = time.time()
    vec = embedder().encode(req.query).tolist()

    must: list[models.Condition] = []
    if req.recency_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=req.recency_days)
        must.append(
            models.FieldCondition(
                key="published_at",
                range=models.DatetimeRange(gte=cutoff.isoformat()),
            )
        )
    if req.sources:
        must.append(
            models.FieldCondition(
                key="source_name", match=models.MatchAny(any=req.sources)
            )
        )
    flt = models.Filter(must=must) if must else None

    hits = client().search(
        collection_name=COLLECTION,
        query_vector=vec,
        limit=req.top_k,
        query_filter=flt,
    )
    articles = [
        Article(
            source_id=h.payload["source_id"],
            title=h.payload["title"],
            url=h.payload["url"],
            source_name=h.payload["source_name"],
            published_at=h.payload.get("published_at"),
            chunk_text=h.payload["chunk_text"],
            score=h.score,
        )
        for h in hits
    ]

    # Record metrics
    SEARCH_LATENCY.observe(time.time() - start)
    RESULTS_COUNT.observe(len(articles))

    return RetrievalResponse(articles=articles, query=req.query)
