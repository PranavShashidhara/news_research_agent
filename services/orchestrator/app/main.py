"""
Agentic orchestrator.

Unlike a fixed pipeline, the control flow here is decided by Claude at runtime.
The model is given a set of tools and an objective, and it loops -- choosing to
search, re-search with a refined query, synthesize, verify claims, or abstain --
until it produces a grounded answer or determines it cannot. This is the
difference between a workflow (predefined code path) and an agent (model-directed
process).

Tools are sourced from the retrieval MCP server (discovered, not hardcoded) plus
local reasoning tools (synthesize / fact_check) backed by the agent service. The
orchestrator is therefore an MCP client.

Cross-cutting: OpenTelemetry spans per loop step, Prometheus metrics for latency,
tokens, cost, hallucination flags, abstentions, and agent loop iterations.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import anthropic
import httpx
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

sys.path.append("/app")
from shared.schemas import (  # noqa: E402
    Article,
    AnswerSentence,
    Claim,
    ResearchAnswer,
    ResearchRequest,
    ResearchResponse,
)
from shared.prompts import ORCHESTRATOR_SYSTEM, PROMPT_VERSION  # noqa: E402

RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://retrieval:8000")
AGENT_URL = os.getenv("AGENT_URL", "http://agent:8000")
OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4318/v1/traces")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "9000"))
MAX_LOOP_STEPS = int(os.getenv("MAX_LOOP_STEPS", "8"))

PRICE_IN = float(os.getenv("PRICE_IN_PER_MTOK", "3.0"))
PRICE_OUT = float(os.getenv("PRICE_OUT_PER_MTOK", "15.0"))

provider = TracerProvider(
    resource=Resource.create({"service.name": "orchestrator"})
)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT))
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("orchestrator")

REQS = Counter("research_requests_total", "Total research requests")
HALLUC = Counter("hallucination_flags_total", "Sentences dropped for grounding")
ABSTAIN = Counter("abstentions_total", "Requests that abstained")
LOOPS = Histogram("agent_loop_steps", "Tool-use loop steps per request")
INFLIGHT = Gauge("inflight_requests", "In-flight research requests")
LAT = Histogram("stage_latency_seconds", "Per-stage latency", ["stage"])
COST = Counter("llm_cost_usd_total", "Cumulative LLM cost in USD")

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
app = FastAPI(title="orchestrator")
app.mount("/metrics", make_asgi_app())


def usd(usage) -> float:
    return (usage.input_tokens / 1e6) * PRICE_IN + (
        usage.output_tokens / 1e6
    ) * PRICE_OUT


def dedupe_and_budget(articles: list[Article]) -> list[Article]:
    """Context management: dedupe by source_id, rank by score, trim to budget."""
    seen: set[str] = set()
    ranked = sorted(articles, key=lambda a: a.score, reverse=True)
    kept, total = [], 0
    for a in ranked:
        if a.source_id in seen:
            continue
        if total + len(a.chunk_text) > MAX_CONTEXT_CHARS:
            continue
        seen.add(a.source_id)
        total += len(a.chunk_text)
        kept.append(a)
    return kept


TOOLS = [
    {
        "name": "search_news",
        "description": "Search recent news. Refine the query and retry if "
        "results are thin or off-topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "recency_days": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "synthesize",
        "description": "Draft a sentence-cited answer grounded ONLY in the "
        "articles gathered so far.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fact_check",
        "description": "Verify the current draft's claims against gathered "
        "sources. Returns per-claim support verdicts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "emit_final",
        "description": "Finish. Provide the final grounded answer or abstain "
        "if sources are insufficient.",
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
    },
]


class _State:
    """Mutable scratchpad the agent builds up across loop steps."""

    def __init__(self, question: str):
        self.question = question
        self.articles: dict[str, Article] = {}
        self.draft: ResearchAnswer | None = None
        self.verdicts: list[dict] = []
        self.tokens = {"input": 0, "output": 0}
        self.cost = 0.0

    def article_list(self) -> list[Article]:
        return list(self.articles.values())


def _run_tool(name: str, args: dict, st: _State, hc: httpx.Client) -> str:
    if name == "search_news":
        with LAT.labels("search").time():
            r = hc.post(
                f"{RETRIEVAL_URL}/search",
                json={
                    "query": args["query"],
                    "top_k": args.get("top_k", 8),
                    "recency_days": args.get("recency_days", 14),
                },
            ).json()
        new = [Article(**a) for a in r["articles"]]
        for a in dedupe_and_budget(list(st.articles.values()) + new):
            st.articles[a.source_id] = a
        return json.dumps(
            [
                {"source_id": a.source_id, "title": a.title,
                 "source": a.source_name, "snippet": a.chunk_text[:160]}
                for a in st.article_list()
            ]
        )

    if name == "synthesize":
        with LAT.labels("synthesize").time():
            s = hc.post(
                f"{AGENT_URL}/synthesize",
                json={
                    "question": st.question,
                    "articles": [a.model_dump(mode="json")
                                 for a in st.article_list()],
                },
            ).json()
        st.draft = ResearchAnswer(**s["answer"])
        st.tokens["input"] += s["token_usage"]["input"]
        st.tokens["output"] += s["token_usage"]["output"]
        st.cost += s["cost_usd"]
        return json.dumps(
            {"sentences": [sd.model_dump() for sd in st.draft.sentences],
             "abstained": st.draft.abstained}
        )

    if name == "fact_check":
        if not st.draft:
            return "No draft to check. Call synthesize first."
        claims = [Claim(text=s.text, cited_source_ids=s.source_ids)
                  for s in st.draft.sentences]
        with LAT.labels("fact_check").time():
            f = hc.post(
                f"{AGENT_URL}/fact_check",
                json={
                    "claims": [c.model_dump() for c in claims],
                    "articles": [a.model_dump(mode="json")
                                 for a in st.article_list()],
                },
            ).json()
        st.verdicts = f["verdicts"]
        st.tokens["input"] += f["token_usage"]["input"]
        st.tokens["output"] += f["token_usage"]["output"]
        st.cost += f["cost_usd"]
        unsupported = sum(1 for v in st.verdicts if not v["supported"])
        return json.dumps(
            {"total": len(st.verdicts), "unsupported": unsupported,
             "verdicts": [{"claim": v["claim"]["text"],
                           "supported": v["supported"]} for v in st.verdicts]}
        )

    return f"Unknown tool {name}"


def _finalize(args: dict, st: _State) -> ResearchAnswer:
    sentences = [AnswerSentence(**s) for s in args.get("sentences", [])]
    unsupported = {v["claim"]["text"] for v in st.verdicts
                   if not v["supported"]}
    grounded: list[AnswerSentence] = []
    for s in sentences:
        if not s.source_ids or any(u in s.text for u in unsupported):
            HALLUC.inc()
            continue
        grounded.append(s)
    answer = ResearchAnswer(
        question=st.question,
        sentences=grounded,
        sources_used=st.article_list(),
        confidence=args.get("confidence", "medium"),
        abstained=args.get("abstained", False) or not grounded,
        notes=args.get("notes", ""),
    )
    if answer.abstained:
        ABSTAIN.inc()
        answer.confidence = "abstain"
    return answer


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "prompt_version": PROMPT_VERSION, "model": MODEL}


@app.post("/research", response_model=ResearchResponse)
def research(req: ResearchRequest) -> ResearchResponse:
    REQS.inc()
    INFLIGHT.inc()
    trace_id = str(uuid.uuid4())
    t0 = time.time()
    st = _State(req.question)

    messages = [{"role": "user", "content":
                 f"Research question: {req.question}\n"
                 f"Default recency window: {req.recency_days} days."}]
    final_answer: ResearchAnswer | None = None

    try:
        with tracer.start_as_current_span("agent_loop") as root:
            root.set_attribute("trace_id", trace_id)
            with httpx.Client(timeout=120) as hc:
                steps = 0
                for steps in range(1, MAX_LOOP_STEPS + 1):
                    with tracer.start_as_current_span(f"step_{steps}"):
                        resp = client.messages.create(
                            model=MODEL,
                            max_tokens=2000,
                            system=ORCHESTRATOR_SYSTEM,
                            tools=TOOLS,
                            messages=messages,
                        )
                        st.tokens["input"] += resp.usage.input_tokens
                        st.tokens["output"] += resp.usage.output_tokens
                        st.cost += usd(resp.usage)

                        tool_uses = [b for b in resp.content
                                     if b.type == "tool_use"]
                        if not tool_uses:
                            break

                        messages.append({"role": "assistant",
                                         "content": resp.content})
                        results = []
                        finished = False
                        for tu in tool_uses:
                            if tu.name == "emit_final":
                                final_answer = _finalize(tu.input, st)
                                finished = True
                                results.append({"type": "tool_result",
                                                "tool_use_id": tu.id,
                                                "content": "ok"})
                            else:
                                out = _run_tool(tu.name, tu.input, st, hc)
                                results.append({"type": "tool_result",
                                                "tool_use_id": tu.id,
                                                "content": out})
                        messages.append({"role": "user", "content": results})
                        if finished:
                            break
                LOOPS.observe(steps)

        if final_answer is None:
            ABSTAIN.inc()
            final_answer = ResearchAnswer(
                question=req.question, sentences=[],
                sources_used=st.article_list(),
                confidence="abstain", abstained=True,
                notes="Agent did not converge on a grounded answer.")

        COST.inc(st.cost)
        return ResearchResponse(
            answer=final_answer,
            trace_id=trace_id,
            latency_ms=(time.time() - t0) * 1000,
            token_usage=st.tokens,
            cost_usd=st.cost,
        )
    finally:
        INFLIGHT.dec()
