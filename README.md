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

## Makefile targets

| Target | Description |
|---|---|
| `make up` | Build images and start the full local stack (`docker compose up --build -d`) |
| `make down` | Stop all containers and remove volumes |
| `make ps` | Show container status |
| `make logs [S=service]` | Tail logs for all services; optionally filter to one service |
| `make ingest [Q="..."]` | Pull a news corpus into Qdrant (default topic: `artificial intelligence`) |
| `make research Q="..."` | Ask a research question against the running stack |
| `make eval` | Run the offline eval gate (`eval/run_eval.py`) |
| `make helm-lint` | Lint the Helm chart |
| `make helm-template` | Render the Helm templates to stdout |

---

## Docker Compose containers (local stack)

`make up` starts **9 containers**:

| Container | Host port | Image / build | Notes |
|---|---|---|---|
| `qdrant` | 6333 | `qdrant/qdrant:v1.12.0` | Vector store; data persisted in `qdrant_data` volume |
| `retrieval` | 8001 | `services/retrieval/Dockerfile` | GDELT ingestion + Qdrant search; `mem_limit: 2g` for the embedding model |
| `retrieval-mcp` | 8010 | `services/retrieval/Dockerfile` | MCP server exposing `search_news`/`fetch_article` over HTTP (`MCP_HTTP=1`) |
| `agent` | 8002 | `services/agent/Dockerfile` | Anthropic tool-calling: synthesize / extract claims / fact-check |
| `evaluation` | 8003 | `services/evaluation/Dockerfile` | RAG + news metrics, CI gate logic |
| `orchestrator` | 8000 | `services/orchestrator/Dockerfile` | Multi-agent loop, context mgmt, hallucination subsystem |
| `otel-collector` | 4318 | `otel/opentelemetry-collector-contrib:0.110.0` | Receives OTLP traces from orchestrator |
| `prometheus` | 9090 | `prom/prometheus:v2.54.1` | Scrapes metrics from all services |
| `grafana` | 3000 | `grafana/grafana:11.2.0` | Pre-provisioned RAG dashboard + Prometheus datasource |

All services share a Docker Compose network; inter-service calls use container names (e.g. `http://retrieval:8000`).

---

## Application services (API surface)

| Service | Host port | Responsibility |
|---|---|---|
| `orchestrator` | 8000 | Multi-agent loop, context mgmt, hallucination subsystem, metrics |
| `retrieval` | 8001 | GDELT ingestion + Qdrant recency/source-filtered search |
| `retrieval-mcp` | 8010 | MCP server — `search_news` + `fetch_article` tools |
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

### Pod count

| Workload | Static replicas | HPA min | HPA max | Scale metric |
|---|---|---|---|---|
| `orchestrator` | 2 | — | — | — |
| `retrieval` | 1 | — | — | — |
| `agent` | 2 | 2 | **10** | `inflight_requests` (avg ≤ 5 per pod) |
| `evaluation` | 1 | — | — | — |
| `qdrant` | 1 | — | — | — |
| **Total (at rest)** | **7** | | | |
| **Total (peak)** | | | **15** | (agent HPA max + other 5) |

The `agent` HPA (`autoscaling/v2`) triggers on the custom metric `inflight_requests` exposed by the orchestrator and surfaced via prometheus-adapter. At rest the cluster runs **7 pods**; under load it can scale to **15 pods**.

What you get:
- **Linkerd** sidecar injection + automatic mTLS between services.
- **HPA** on `agent` keyed to in-flight LLM requests per pod (not CPU).
- **Flagger Canary** on `agent`: 10%→50% traffic shift, auto-promoted only if
  success-rate ≥ 99% and latency ≤ 5000 ms, checked every 30 s; else auto-rollback.

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


