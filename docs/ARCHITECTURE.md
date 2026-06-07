# Architecture (v2 — agentic + MCP)

```
                         ┌─────────────────────────────┐
            user ───────▶│        orchestrator         │
                         │  AGENTIC tool-use loop:      │
                         │  Claude decides each step    │
                         │  (search → synth → check →   │
                         │   re-search/re-synth → final)│
                         └───┬───────────┬──────────────┘
              MCP client     │           │  HTTP (reasoning tools)
                  ┌──────────▼──┐   ┌─────▼──────┐   ┌────────────┐
                  │retrieval-mcp│   │   agent    │   │ evaluation │
                  │ MCP server  │   │ Anthropic  │   │ RAG metrics│
                  │ tools:      │   │ synth +    │   │ + CI gate  │
                  │ search_news │   │ fact_check │   └────────────┘
                  │ fetch_article│  └────────────┘
                  └──────┬──────┘
                         │ HTTP
                  ┌──────▼──────┐   ┌──────────────┐
                  │  retrieval  │──▶│   (Qdrant)   │
                  │ GDELT+Qdrant│   │ vector store │
                  └─────────────┘   └──────────────┘
```

## Agentic control flow (the key v2 change)
The orchestrator no longer runs a fixed sequence. It hands Claude a tool set
(`search_news`, `synthesize`, `fact_check`, `emit_final`) and an objective, then
loops. Claude decides what to do next based on intermediate results:

- thin/off-topic retrieval → it calls `search_news` again with a refined query
- unsupported claims after `fact_check` → it re-searches or re-synthesizes
- sufficient grounded evidence → it calls `emit_final`
- genuinely unanswerable → it calls `emit_final` with `abstained=true`

This is the workflow→agent distinction: predefined code path vs. model-directed
process. `MAX_LOOP_STEPS` bounds the loop; a safety net abstains if it doesn't
converge. `agent_loop_steps` is exported to Prometheus so you can see how many
steps real questions take.

## MCP integration
`retrieval-mcp` is a Model Context Protocol server exposing `search_news` and
`fetch_article` over JSON-RPC. The orchestrator is an MCP client. This decouples
tools from orchestration: the tool server is built once and any MCP-compatible
client can consume it, instead of bespoke per-tool wiring. MCP is the
integration layer above raw function calling — the project uses both.

## Hallucination subsystem (unchanged in spirit, stronger in practice)
- Strict grounding prompt + explicit abstention path.
- Independent verifier (`fact_check`) — separate from the generator.
- Sentence→source citation enforcement, applied both by the agent and again
  server-side in `_finalize`.
- Loop can self-correct: failed verification drives re-search/re-synthesis.
- `hallucination_flags_total`, `abstentions_total` exported to Prometheus.

## Evaluation (unchanged)
- Retrieval: context precision/recall, hit-rate, MRR, NDCG.
- Generation: faithfulness, citation precision/recall (LLM-as-judge).
- News-specific: source freshness, diversity, abstention correctness.
- CI gate on the non-rotting metrics only.

## Autoscaling, proven
`inflight_requests` is exported by the orchestrator and surfaced as a custom pod
metric via prometheus-adapter. The `agent` HPA scales on it. `loadtest/` drives
concurrent traffic so the scaling can be observed and recorded.
