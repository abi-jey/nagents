FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install nagents + server dependencies (this directory is the build context from the repo)
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install hatchling && pip install --no-build-isolation . && pip uninstall -y hatchling
RUN pip install fastapi uvicorn python-dotenv

RUN useradd -m -u 1000 agent
USER agent

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
