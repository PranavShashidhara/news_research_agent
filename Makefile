.PHONY: up down ingest research eval logs ps helm-lint helm-template

# Use a .env file (copy .env.example -> .env and add your key) so the API key
# is injected into containers automatically. Falls back to the exported shell
# var if no .env is present.

up:            ## Build & start the full local stack
	docker compose up --build -d

down:          ## Stop and remove volumes
	docker compose down -v

ps:            ## Show container status
	docker compose ps

logs:          ## Tail logs for all services (S=service to filter)
	docker compose logs -f $(S)

ingest:        ## Pull a news corpus into Qdrant (Q="..." optional)
	python3 scripts/call.py ingest "$(or $(Q),artificial intelligence)"

research:      ## Ask a question (Q="...")
	python3 scripts/call.py research "$(Q)"

eval:          ## Run the offline eval gate
	python3 eval/run_eval.py

helm-lint:
	helm lint deploy/helm

helm-template:
	helm template news deploy/helm
