"""
Evaluation service.

Computes RAG metrics in two families plus news-specific signals, and applies a
CI gate on the domain-agnostic, non-rotting metrics (faithfulness, citation
precision/recall, context precision/recall, abstention correctness).

Faithfulness/relevance reuse the agent's fact-checker via an LLM-as-judge.
Retrieval metrics use the provided relevant_source_ids labels. News metrics are
computed directly from source metadata.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from prometheus_client import Gauge, Histogram, make_asgi_app

sys.path.append("/app")
from shared.schemas import (  # noqa: E402
    Article,
    Claim,
    EvalRequest,
    EvalResponse,
    EvalScores,
)

AGENT_URL = os.getenv("AGENT_URL", "http://agent:8000")

# CI gate thresholds. These are the metrics that DON'T rot in a news domain.
GATE = {
    "faithfulness": float(os.getenv("GATE_FAITHFULNESS", "0.80")),
    "citation_precision": float(os.getenv("GATE_CITATION_PRECISION", "0.75")),
    "context_recall": float(os.getenv("GATE_CONTEXT_RECALL", "0.70")),
}

app = FastAPI(title="evaluation")
app.mount("/metrics", make_asgi_app())

# Prometheus metrics
EVAL_LATENCY = Histogram("eval_evaluate_time_seconds", "Evaluation latency in seconds")
FAITHFULNESS = Gauge("eval_faithfulness_score", "Faithfulness score")
CITATION_PRECISION = Gauge("eval_citation_precision_score", "Citation precision score")
CITATION_RECALL = Gauge("eval_citation_recall_score", "Citation recall score")
CONTEXT_PRECISION = Gauge("eval_context_precision_score", "Context precision score")
CONTEXT_RECALL = Gauge("eval_context_recall_score", "Context recall score")
HIT_RATE = Gauge("eval_hit_rate_score", "Hit rate score")
MRR = Gauge("eval_mrr_score", "Mean reciprocal rank")
NDCG = Gauge("eval_ndcg_score", "NDCG score")
SOURCE_FRESHNESS = Gauge("eval_source_freshness_score", "Source freshness score")
SOURCE_DIVERSITY = Gauge("eval_source_diversity_score", "Source diversity score")
GATE_PASS = Gauge("eval_gate_pass", "Whether evaluation passed the gate (1.0 or 0.0)")


# --------------------------------------------------------------------------- #
# Retrieval metrics (need relevant_source_ids labels)
# --------------------------------------------------------------------------- #
def retrieval_metrics(
    retrieved: list[Article], relevant: list[str]
) -> dict[str, float]:
    rel = set(relevant)
    ids = [a.source_id for a in retrieved]
    if not ids or not rel:
        return {}
    hits = [1 if i in rel else 0 for i in ids]
    n_rel_retrieved = sum(hits)

    precision = n_rel_retrieved / len(ids)
    recall = n_rel_retrieved / len(rel)
    hit_rate = 1.0 if n_rel_retrieved > 0 else 0.0

    # MRR
    mrr = 0.0
    for rank, h in enumerate(hits, start=1):
        if h:
            mrr = 1.0 / rank
            break

    # NDCG (binary relevance)
    dcg = sum(h / math.log2(r + 1) for r, h in enumerate(hits, start=1))
    ideal = sum(
        1 / math.log2(r + 1) for r in range(1, min(len(rel), len(ids)) + 1)
    )
    ndcg = dcg / ideal if ideal else 0.0

    return {
        "context_precision": precision,
        "context_recall": recall,
        "hit_rate": hit_rate,
        "mrr": mrr,
        "ndcg": ndcg,
    }


# --------------------------------------------------------------------------- #
# Generation metrics via LLM-as-judge (reuses the agent fact-checker)
# --------------------------------------------------------------------------- #
def faithfulness_and_citations(req: EvalRequest) -> dict[str, float]:
    sentences = req.answer.sentences
    if not sentences:
        # An abstention is vacuously faithful; citation metrics undefined.
        return {"faithfulness": 1.0}

    claims = [
        Claim(text=s.text, cited_source_ids=s.source_ids) for s in sentences
    ]
    with httpx.Client(timeout=120) as hc:
        resp = hc.post(
            f"{AGENT_URL}/fact_check",
            json={
                "claims": [c.model_dump() for c in claims],
                "articles": [a.model_dump(mode="json") for a in req.retrieved],
            },
        ).json()
    verdicts = resp["verdicts"]

    supported = sum(1 for v in verdicts if v["supported"])
    faithfulness = supported / len(verdicts) if verdicts else 1.0

    # Citation precision: of the source_ids the answer cited, how many the
    # verifier confirmed actually support the claim.
    cited_total, cited_correct, support_total = 0, 0, 0
    for s, v in zip(sentences, verdicts):
        cited = set(s.source_ids)
        confirmed = set(v.get("supporting_source_ids", []))
        cited_total += len(cited)
        cited_correct += len(cited & confirmed)
        support_total += len(confirmed)
    citation_precision = cited_correct / cited_total if cited_total else 0.0
    citation_recall = cited_correct / support_total if support_total else 0.0

    return {
        "faithfulness": faithfulness,
        "citation_precision": citation_precision,
        "citation_recall": citation_recall,
    }


# --------------------------------------------------------------------------- #
# News-specific metrics
# --------------------------------------------------------------------------- #
def news_metrics(retrieved: list[Article]) -> dict[str, float]:
    if not retrieved:
        return {}
    # Freshness: mean of exp(-age_days/14), so ~1.0 for today, decaying.
    now = datetime.now(timezone.utc)
    fresh_vals = []
    for a in retrieved:
        if not a.published_at:
            continue
        pub = a.published_at
        if isinstance(pub, str):
            try:
                pub = datetime.fromisoformat(pub)
            except ValueError:
                continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age = max((now - pub).days, 0)
        fresh_vals.append(math.exp(-age / 14))
    freshness = sum(fresh_vals) / len(fresh_vals) if fresh_vals else 0.0

    # Diversity: distinct outlets / total.
    outlets = {a.source_name for a in retrieved}
    diversity = len(outlets) / len(retrieved)
    return {"source_freshness": freshness, "source_diversity": diversity}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvalResponse)
def evaluate(req: EvalRequest) -> EvalResponse:
    start = time.time()
    scores = EvalScores()

    for k, v in faithfulness_and_citations(req).items():
        setattr(scores, k, v)
    if req.relevant_source_ids:
        for k, v in retrieval_metrics(
            req.retrieved, req.relevant_source_ids
        ).items():
            setattr(scores, k, v)
    for k, v in news_metrics(req.retrieved).items():
        setattr(scores, k, v)

    # Abstention correctness: if there were <2 relevant sources, abstaining is
    # correct; otherwise answering is correct.
    if req.relevant_source_ids is not None:
        should_abstain = len(req.relevant_source_ids) < 2
        scores.abstention_correct = req.answer.abstained == should_abstain

    # Apply CI gate.
    failures: list[str] = []
    for metric, threshold in GATE.items():
        val = getattr(scores, metric)
        if val is not None and val < threshold:
            failures.append(f"{metric}={val:.2f} < {threshold:.2f}")

    # Record metrics
    EVAL_LATENCY.observe(time.time() - start)
    if scores.faithfulness is not None:
        FAITHFULNESS.set(scores.faithfulness)
    if scores.citation_precision is not None:
        CITATION_PRECISION.set(scores.citation_precision)
    if scores.citation_recall is not None:
        CITATION_RECALL.set(scores.citation_recall)
    if scores.context_precision is not None:
        CONTEXT_PRECISION.set(scores.context_precision)
    if scores.context_recall is not None:
        CONTEXT_RECALL.set(scores.context_recall)
    if scores.hit_rate is not None:
        HIT_RATE.set(scores.hit_rate)
    if scores.mrr is not None:
        MRR.set(scores.mrr)
    if scores.ndcg is not None:
        NDCG.set(scores.ndcg)
    if scores.source_freshness is not None:
        SOURCE_FRESHNESS.set(scores.source_freshness)
    if scores.source_diversity is not None:
        SOURCE_DIVERSITY.set(scores.source_diversity)
    GATE_PASS.set(1.0 if not failures else 0.0)

    return EvalResponse(
        scores=scores, passed_gate=not failures, gate_failures=failures
    )
