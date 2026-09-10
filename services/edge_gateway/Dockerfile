FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install uv for fast dependency installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project manifests and install runtime dependencies
COPY pyproject.toml uv.lock ./
RUN uv export --only-group services --no-hashes -o requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ /app/src/
COPY services/ /app/services/
ENV PYTHONPATH="/app/src:/app"

EXPOSE 8000

CMD ["uvicorn", "services.edge_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
