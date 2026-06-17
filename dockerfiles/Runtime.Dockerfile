FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install nagents (this directory will be the build context from the repo)
COPY pyproject.toml ./
COPY src/ src/
RUN pip install hatchling && pip install -e . && pip uninstall -y hatchling

RUN useradd -m -u 1000 agent
USER agent

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
