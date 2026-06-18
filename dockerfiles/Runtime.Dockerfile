FROM python:3.12-slim

ARG DOCKER_GID=999

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-cli \
    curl \
    nodejs \
    npm \
    libnss3 libnspr4 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Playwright + Chromium globally (matching MCP server expectations)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN npm install -g playwright @playwright/mcp && \
    npx -y playwright install chromium && \
    npx -y playwright install-deps chromium 2>/dev/null || true

# Install nagents + server dependencies (this directory will be the build context from the repo)
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install hatchling && pip install --no-build-isolation ".[server]" && pip uninstall -y hatchling

RUN groupadd -g ${DOCKER_GID} docker-host && \
    useradd -m -u 1000 -G docker-host agent && \
    chown -R agent:agent /opt/playwright-browsers
USER agent

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

EXPOSE 8080
CMD ["python", "-m", "nagents.server"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
