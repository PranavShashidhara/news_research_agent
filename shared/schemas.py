"""
Shared Pydantic schemas for structured I/O across all services.

These models are the contract between services. Every cross-service payload
and every LLM structured output is validated against one of these. This is
the backbone of the "structured output handling" requirement: nothing crosses
a boundary as an unvalidated dict.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
class Article(BaseModel):
    """A single retrieved news article chunk with provenance metadata."""

    source_id: str = Field(..., description="Stable ID used for citation.")
    title: str
    url: str
    source_name: str = Field(..., description="Outlet, e.g. 'Reuters'.")
    published_at: Optional[datetime] = None
    chunk_text: str
    score: float = Field(0.0, description="Retrieval similarity score.")


class RetrievalRequest(BaseModel):
    query: str
    top_k: int = 8
    recency_days: Optional[int] = Field(
        None, description="If set, restrict to articles newer than N days."
    )
    sources: Optional[list[str]] = Field(
        None, description="Optional outlet allow-list."
    )


class RetrievalResponse(BaseModel):
    articles: list[Article]
    query: str


# --------------------------------------------------------------------------- #
# Agent / claims
# --------------------------------------------------------------------------- #
class Claim(BaseModel):
    """An atomic factual claim extracted from a draft answer."""

    text: str
    cited_source_ids: list[str] = Field(default_factory=list)


class VerifiedClaim(BaseModel):
    claim: Claim
    supported: bool
    supporting_source_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"
    abstain = "abstain"


class AnswerSentence(BaseModel):
    """A sentence of the final answer, each mapped to its sources."""

    text: str
    source_ids: list[str] = Field(
        default_factory=list,
        description="Sources grounding this sentence. Empty => unsupported.",
    )


class ResearchAnswer(BaseModel):
    """The fully structured final answer returned to the user."""

    question: str
    sentences: list[AnswerSentence]
    sources_used: list[Article] = Field(default_factory=list)
    confidence: Confidence = Confidence.medium
    abstained: bool = False
    notes: str = ""


# --------------------------------------------------------------------------- #
# Orchestration request/response
# --------------------------------------------------------------------------- #
class ResearchRequest(BaseModel):
    question: str
    top_k: int = 8
    recency_days: Optional[int] = 14


class ResearchResponse(BaseModel):
    answer: ResearchAnswer
    trace_id: str
    latency_ms: float
    token_usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
class EvalScores(BaseModel):
    # Generation / faithfulness
    faithfulness: Optional[float] = None
    answer_relevance: Optional[float] = None
    citation_precision: Optional[float] = None
    citation_recall: Optional[float] = None
    context_utilization: Optional[float] = None
    # Retrieval
    context_precision: Optional[float] = None
    context_recall: Optional[float] = None
    hit_rate: Optional[float] = None
    mrr: Optional[float] = None
    ndcg: Optional[float] = None
    # News-specific
    source_freshness: Optional[float] = None
    source_diversity: Optional[float] = None
    abstention_correct: Optional[bool] = None


class EvalRequest(BaseModel):
    question: str
    answer: ResearchAnswer
    retrieved: list[Article]
    ground_truth: Optional[str] = None
    relevant_source_ids: Optional[list[str]] = None


class EvalResponse(BaseModel):
    scores: EvalScores
    passed_gate: bool
    gate_failures: list[str] = Field(default_factory=list)
