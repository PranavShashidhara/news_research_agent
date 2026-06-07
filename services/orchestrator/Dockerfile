# Multi-stage build keeps the final image small.
FROM python:3.12-slim AS builder
WORKDIR /install
COPY services/orchestrator/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/deps -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /install/deps /usr/local
# shared/ is copied in at build context root (see compose/helm build args)
COPY shared/ /app/shared/
COPY services/orchestrator/app/ /app/app/
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8000/health').status_code==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
