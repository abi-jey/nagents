FROM python:3.12-slim

ARG DOCKER_GID=999

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-cli \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install nagents + server dependencies (this directory will be the build context from the repo)
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install hatchling && pip install --no-build-isolation ".[server]" && pip uninstall -y hatchling

RUN groupadd -g ${DOCKER_GID} docker-host && \
    useradd -m -u 1000 -G docker-host agent
USER agent

ENV PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python", "-m", "nagents.server"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
