# News Research Agent — Multi-Agent RAG Platform

A production-grade, multi-agent retrieval-augmented research assistant for news
and current events. Submit a question; a team of agents (synthesizer →
claim-extractor → fact-checker) produces a **cited, verified** answer or
**abstains** when the evidence is weak. Deployed as microservices on Kubernetes
with a service mesh, custom-metric autoscaling, progressive canary delivery,
full observability, and an **eval-gated CI/CD pipeline**. The orchestrator is
**genuinely agentic** — Claude directs its own tool-use loop — and tools are
exposed over **MCP** (Model Context Protocol).

Built to be reproducible: the news source (GDELT) needs **no API key**, and
embeddings run locally. The only secret you supply is your Anthropic API key.

---

## Why this is more than a tutorial RAG demo

| Skill | Where it lives |
|---|---|
| Containerization (Docker) | Multi-stage `Dockerfile` per service |
| Orchestration (Kubernetes) | `deploy/helm` — Deployments, Services, HPA, PVC |
| Advanced K8s | Custom-metric HPA, **Linkerd** mesh + mTLS, **Flagger** canary |
| Microservices | 4 independently deployable services + Qdrant |
| API + agentic | Anthropic tool-calling, **model-directed tool-use loop** |
| Agentic control flow | Claude decides search/synth/verify/finish per step |
| MCP | `retrieval-mcp` server; orchestrator is an MCP client |
| Tool / function calling | Forced tool schemas in `services/agent` |
| Context management | Dedup + ranking + token budgeting in orchestrator |
| Reducing hallucination | Separate verifier agent, citation enforcement, abstention |
| Prompt engineering | Versioned prompts in `shared/prompts.py` |
| Structured output | Pydantic-validated tool outputs everywhere |
| Evaluation | RAG metrics service + custom news metrics |
| Monitoring / observability | Prometheus, Grafana, OpenTelemetry traces |
| CI/CD for ML | GitHub Actions with an **eval quality gate** |

Architecture details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Quick start (local, ~5 min)

```bash
cp .env.example .env            # add your ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...

make up                         # build + start all services + observability
make ingest                     # pull a fresh news corpus from GDELT into Qdrant
make research Q="What are the latest developments in AI regulation?"
```

Open:
- Grafana dashboard: http://localhost:3000 (RAG News Agent)
- Prometheus: http://localhost:9090
- Orchestrator API docs: http://localhost:8000/docs

Run the offline eval gate locally:

```bash
make eval
```

---

## Services

| Service | Port | Responsibility |
|---|---|---|
| `orchestrator` | 8000 | Multi-agent loop, context mgmt, hallucination subsystem, metrics |
| `retrieval` | 8001 | GDELT ingestion + Qdrant recency/source-filtered search |
| `agent` | 8002 | Anthropic tool-calling: synthesize / extract claims / fact-check |
| `evaluation` | 8003 | RAG + news metrics, CI gate logic |

---

## Kubernetes deployment

```bash
# Prereqs: a cluster (kind/minikube/GKE/EKS), Linkerd + Flagger installed,
# prometheus-adapter exposing `inflight_requests` as a custom metric.

kubectl create secret generic anthropic-secret \
  --from-literal=api-key=$ANTHROPIC_API_KEY

helm lint deploy/helm
helm upgrade --install news-research-agent deploy/helm \
  --set image.registry=ghcr.io/your-org/news-research-agent \
  --set image.tag=latest
```

What you get:
- **Linkerd** sidecar injection + automatic mTLS between services.
- **HPA** on `agent` keyed to in-flight LLM requests per pod (not CPU).
- **Flagger Canary** on `agent`: 10%→50% traffic shift, auto-promoted only if
  success-rate and latency stay within thresholds, else auto-rollback.

---

## Agentic loop + MCP

The orchestrator hands Claude four tools (`search_news`, `synthesize`,
`fact_check`, `emit_final`) and lets the model decide the sequence: it
re-searches when retrieval is thin, re-synthesizes when claims fail
verification, and abstains when sources can't answer. This is a model-directed
agent, not a fixed pipeline. `search_news` / `fetch_article` are served by an
**MCP server** (`retrieval-mcp`), so tools are decoupled from orchestration and
any MCP client could reuse them. See `docs/ARCHITECTURE.md`.

## Load testing the autoscaler

```bash
pip install locust
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 25 --spawn-rate 5 --run-time 5m --headless
# in another terminal, capture scaling:
./loadtest/hpa_watch.sh
```

`inflight_requests` is exported by the orchestrator and surfaced as a custom pod
metric via prometheus-adapter (`deploy/k8s/prometheus-adapter-values.yaml`); the
`agent` HPA scales on it. Record the run to turn "configured autoscaling" into
"load-tested and watched it scale."

## The eval gate (the ML-specific CI step)

`eval/run_eval.py` runs the golden dataset through the live stack, scores each
answer, and **fails the build** if the non-rotting metrics drop below
threshold (`faithfulness ≥ 0.80`, `citation_precision ≥ 0.75`,
`context_recall ≥ 0.70`). Prompt or model changes are thus treated like code
changes that must pass quality bars. Factual correctness is deliberately *not*
gated — it rots in a news domain, while groundedness does not.

The full pipeline (`.github/workflows/ci-cd.yaml`): lint → test → build & push
images → **eval gate** → Helm deploy with Flagger canary.

---

## Configuration

Key env vars (see `deploy/helm/values.yaml` and `docker-compose.yml`):

- `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`)
- `MAX_CONTEXT_CHARS` — context budget for the orchestrator
- `GATE_FAITHFULNESS` / `GATE_CITATION_PRECISION` / `GATE_CONTEXT_RECALL`
- `RECENCY_DAYS` per request — restrict retrieval to recent articles

---

## Suggested 1–2 week build order

1. Days 1–2: orchestrator + agent tool-calling + retrieval end-to-end.
2. Day 3: multi-agent split + structured output.
3. Day 4: context mgmt + hallucination subsystem.
4. Day 5: Dockerize, compose run.
5. Days 6–7: K8s + Helm + Linkerd mesh.
6. Day 8: observability (OTel traces, Prometheus, Grafana).
7. Day 9: custom-metric HPA + Flagger canary.
8. Day 10: eval-gated CI/CD.

This repo gives you a running spine for steps 1–10 to extend.
