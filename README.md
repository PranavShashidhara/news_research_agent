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

## End-to-End Setup (From Scratch)

> **Golden rule:** validate the local Docker Compose stack — including `make eval` — before touching Kubernetes. Everything in the K8s phases depends on a clean local run.

---

### Phase 1 — Prerequisites

Install the required toolchain:

```bash
# Docker Engine (Linux)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/

# Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# kind (local Kubernetes cluster)
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind
```

---

### Phase 2 — Local Docker Compose

Validate everything works before building for Kubernetes.

```bash
# Clone and configure
git clone <your-repo-url> && cd news_research_agent
cp .env.example .env
# Edit .env → set ANTHROPIC_API_KEY=sk-ant-...

# Start all 9 containers
make up

# Wait ~30s for health checks, then verify all containers are healthy
make ps

# Ingest a fresh news corpus from GDELT into Qdrant
make ingest

# Run a test query
make research Q="What are the latest developments in AI regulation?"

# Run the offline eval gate — must pass before proceeding
make eval
```

Open the local dashboards and service endpoints:

| Service | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3000 | RAG News Agent dashboard (default login: admin/admin) |
| Orchestrator API | http://localhost:8000/docs | Interactive Swagger UI |
| Orchestrator (raw) | http://localhost:8000 | POST `/research` to submit queries |
| Retrieval service | http://localhost:8001/docs | GDELT ingestion + Qdrant search |
| Retrieval MCP | http://localhost:8010 | MCP server (`search_news` / `fetch_article` tools) |
| Agent service | http://localhost:8002/docs | Synthesize / extract claims / fact-check |
| Evaluation service | http://localhost:8003/docs | RAG metrics + CI gate |
| Qdrant UI | http://localhost:6333/dashboard | Vector store browser |
| Prometheus | http://localhost:9090 | Metrics explorer |
| OTEL Collector | http://localhost:4318 | OTLP trace receiver (HTTP) |

---

### Phase 3 — Build & Push Images

```bash
# Authenticate to your container registry (GitHub Container Registry shown)
echo $CR_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Build and push each service image
docker build -t ghcr.io/your-org/news-research-agent/orchestrator:latest services/orchestrator/
docker build -t ghcr.io/your-org/news-research-agent/retrieval:latest    services/retrieval/
docker build -t ghcr.io/your-org/news-research-agent/agent:latest        services/agent/
docker build -t ghcr.io/your-org/news-research-agent/evaluation:latest   services/evaluation/

docker push ghcr.io/your-org/news-research-agent/orchestrator:latest
docker push ghcr.io/your-org/news-research-agent/retrieval:latest
docker push ghcr.io/your-org/news-research-agent/agent:latest
docker push ghcr.io/your-org/news-research-agent/evaluation:latest
```

---

### Phase 4 — Kubernetes Cluster Setup

```bash
# Create a local kind cluster
kind create cluster --name news-research

# Verify the node is Ready
kubectl get nodes

# Install Linkerd (service mesh + mTLS)
curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install | sh
export PATH=$PATH:$HOME/.linkerd2/bin
linkerd install --crds | kubectl apply -f -
linkerd install | kubectl apply -f -
linkerd check

# Install Flagger (progressive canary delivery)
helm repo add flagger https://flagger.app
helm upgrade --install flagger flagger/flagger \
  --namespace linkerd \
  --set meshProvider=linkerd \
  --set metricsServer=http://prometheus:9090

# Install prometheus-adapter (exposes inflight_requests as a custom HPA metric)
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter \
  -f deploy/k8s/prometheus-adapter-values.yaml
```

---

### Phase 5 — Deploy to Kubernetes

```bash
# Create the Anthropic API key secret
kubectl create secret generic anthropic-secret \
  --from-literal=api-key=$ANTHROPIC_API_KEY

# Lint the Helm chart
helm lint deploy/helm

# Deploy
helm upgrade --install news-research-agent deploy/helm \
  --set image.registry=ghcr.io/your-org/news-research-agent \
  --set image.tag=latest

# Verify pods (7 at rest)
kubectl get pods

# Verify HPA is wired up
kubectl get hpa
```

---

### Phase 6 — Validate & Load Test

Port-forward all services to access them locally from the cluster:

```bash
# Run each in a separate terminal (or background with &)
kubectl port-forward svc/orchestrator  8000:8000
kubectl port-forward svc/retrieval     8001:8000
kubectl port-forward svc/agent         8002:8000
kubectl port-forward svc/evaluation    8003:8000
kubectl port-forward svc/retrieval-mcp 8010:8010
kubectl port-forward svc/qdrant        6333:6333
kubectl port-forward svc/prometheus    9090:9090
kubectl port-forward svc/grafana       3000:3000
```

Once forwarded, the same URLs as the local stack apply:

| Service | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3000 | RAG News Agent dashboard |
| Orchestrator API | http://localhost:8000/docs | Interactive Swagger UI |
| Orchestrator (raw) | http://localhost:8000 | POST `/research` to submit queries |
| Retrieval service | http://localhost:8001/docs | GDELT ingestion + Qdrant search |
| Retrieval MCP | http://localhost:8010 | MCP server tools |
| Agent service | http://localhost:8002/docs | Synthesize / fact-check |
| Evaluation service | http://localhost:8003/docs | RAG metrics + CI gate |
| Qdrant UI | http://localhost:6333/dashboard | Vector store browser |
| Prometheus | http://localhost:9090 | Metrics explorer |

```bash
# Install Locust and run the load test
pip install locust
locust -f loadtest/locustfile.py --host http://localhost:8000 \
       --users 25 --spawn-rate 5 --run-time 5m --headless

# In a second terminal — watch agent pods scale from 2 → up to 10
./loadtest/hpa_watch.sh
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
- **Flagger Canary** on `agent`: 10%→50% traffic shift, auto-promoted only if success-rate ≥ 99% and latency ≤ 5000 ms, checked every 30 s; else auto-rollback.

---

## Agentic loop + MCP

The orchestrator hands Claude four tools (`search_news`, `synthesize`,
`fact_check`, `emit_final`) and lets the model decide the sequence: it
re-searches when retrieval is thin, re-synthesizes when claims fail
verification, and abstains when sources can't answer. This is a model-directed
agent, not a fixed pipeline. `search_news` / `fetch_article` are served by an
**MCP server** (`retrieval-mcp`), so tools are decoupled from orchestration and
any MCP client could reuse them. See `docs/ARCHITECTURE.md`.

---

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

---

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