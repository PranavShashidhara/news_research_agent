"""
Agent / LLM service.

Wraps the Anthropic API and exposes three structured-output endpoints used by
the orchestrator's multi-agent loop:
  - /synthesize     -> grounded, sentence-cited draft answer
  - /extract_claims -> atomic claims from a draft
  - /fact_check     -> per-claim support verdicts

Structured output is enforced via tool schemas + Pydantic validation. The model
is forced to call the tool (tool_choice), so we always get parseable JSON.
"""
from __future__ import annotations

import os
import sys

import time

import anthropic
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel

sys.path.append("/app")
from shared.schemas import (  # noqa: E402
    Article,
    Claim,
    ResearchAnswer,
    VerifiedClaim,
)
from shared.prompts import (  # noqa: E402
    CLAIM_EXTRACTOR_SYSTEM,
    FACT_CHECKER_SYSTEM,
    SYNTHESIZER_SYSTEM,
)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

app = FastAPI(title="agent")
app.mount("/metrics", make_asgi_app())

# Prometheus metrics
SYNTHESIS_LATENCY = Histogram(
    "agent_synthesis_time_seconds", "Synthesis latency in seconds"
)
CLAIMS_EXTRACTION_LATENCY = Histogram(
    "agent_extract_claims_time_seconds", "Claims extraction latency in seconds"
)
FACTCHECK_LATENCY = Histogram(
    "agent_factcheck_time_seconds", "Fact-check latency in seconds"
)
CLAIMS_EXTRACTED = Counter("agent_claims_extracted_total", "Total claims extracted")

# Rough per-million-token prices for cost accounting on the dashboard.
PRICE_IN = float(os.getenv("PRICE_IN_PER_MTOK", "3.0"))
PRICE_OUT = float(os.getenv("PRICE_OUT_PER_MTOK", "15.0"))


def cost(usage) -> float:
    return (usage.input_tokens / 1e6) * PRICE_IN + (
        usage.output_tokens / 1e6
    ) * PRICE_OUT


def sources_block(articles: list[Article]) -> str:
    lines = []
    for a in articles:
        lines.append(
            f"[{a.source_id}] ({a.source_name}, {a.published_at}) "
            f"{a.title}\n{a.chunk_text}"
        )
    return "\n\n".join(lines)


def call_tool(system: str, user: str, tool: dict) -> tuple[dict, object]:
    """Force a single tool call and return (tool_input, usage)."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    return block.input, resp.usage


# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #
ANSWER_TOOL = {
    "name": "emit_answer",
    "description": "Emit the grounded, sentence-cited research answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "source_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["text", "source_ids"],
                },
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low", "abstain"],
            },
            "abstained": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": ["sentences", "confidence", "abstained"],
    },
}

CLAIMS_TOOL = {
    "name": "emit_claims",
    "description": "Emit atomic claims extracted from the answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "cited_source_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["text", "cited_source_ids"],
                },
            }
        },
        "required": ["claims"],
    },
}

VERDICTS_TOOL = {
    "name": "emit_verdicts",
    "description": "Emit per-claim support verdicts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_text": {"type": "string"},
                        "supported": {"type": "boolean"},
                        "supporting_source_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["claim_text", "supported"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class SynthReq(BaseModel):
    question: str
    articles: list[Article]


class ClaimsReq(BaseModel):
    answer: ResearchAnswer


class FactCheckReq(BaseModel):
    claims: list[Claim]
    articles: list[Article]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": MODEL}


@app.post("/synthesize")
def synthesize(req: SynthReq) -> dict:
    start = time.time()
    user = (
        f"QUESTION: {req.question}\n\nSOURCES:\n{sources_block(req.articles)}"
    )
    data, usage = call_tool(SYNTHESIZER_SYSTEM, user, ANSWER_TOOL)
    answer = ResearchAnswer(
        question=req.question,
        sentences=data["sentences"],
        sources_used=req.articles,
        confidence=data.get("confidence", "medium"),
        abstained=data.get("abstained", False),
        notes=data.get("notes", ""),
    )
    SYNTHESIS_LATENCY.observe(time.time() - start)
    return {
        "answer": answer.model_dump(mode="json"),
        "token_usage": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
        },
        "cost_usd": cost(usage),
    }


@app.post("/extract_claims")
def extract_claims(req: ClaimsReq) -> dict:
    start = time.time()
    joined = " ".join(
        f"{s.text} (cites: {','.join(s.source_ids)})"
        for s in req.answer.sentences
    )
    data, usage = call_tool(
        CLAIM_EXTRACTOR_SYSTEM, f"ANSWER:\n{joined}", CLAIMS_TOOL
    )
    claims = [
        Claim(text=c["text"], cited_source_ids=c.get("cited_source_ids", []))
        for c in data["claims"]
    ]
    CLAIMS_EXTRACTION_LATENCY.observe(time.time() - start)
    CLAIMS_EXTRACTED.inc(len(claims))
    return {
        "claims": [c.model_dump() for c in claims],
        "token_usage": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
        },
        "cost_usd": cost(usage),
    }


@app.post("/fact_check")
def fact_check(req: FactCheckReq) -> dict:
    start = time.time()
    claims_txt = "\n".join(f"- {c.text}" for c in req.claims)
    user = (
        f"CLAIMS:\n{claims_txt}\n\nSOURCES:\n{sources_block(req.articles)}"
    )
    data, usage = call_tool(FACT_CHECKER_SYSTEM, user, VERDICTS_TOOL)
    verified: list[VerifiedClaim] = []
    by_text = {c.text: c for c in req.claims}
    for v in data["verdicts"]:
        base = by_text.get(
            v["claim_text"], Claim(text=v["claim_text"], cited_source_ids=[])
        )
        verified.append(
            VerifiedClaim(
                claim=base,
                supported=v["supported"],
                supporting_source_ids=v.get("supporting_source_ids", []),
                rationale=v.get("rationale", ""),
            )
        )
    FACTCHECK_LATENCY.observe(time.time() - start)
    return {
        "verdicts": [v.model_dump() for v in verified],
        "token_usage": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
        },
        "cost_usd": cost(usage),
    }
