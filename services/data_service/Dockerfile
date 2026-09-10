ARG VARIANT=good

FROM python:3.12-slim

ARG VARIANT
ENV DATA_SERVICE_VARIANT=${VARIANT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install uv for fast dependency installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project manifests and install runtime dependencies
COPY pyproject.toml uv.lock ./
RUN uv export --only-group services --no-hashes -o requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY services/ /app/services/
ENV PYTHONPATH="/app"

EXPOSE 8000

CMD ["uvicorn", "services.data_service.app:app", "--host", "0.0.0.0", "--port", "8000"]
