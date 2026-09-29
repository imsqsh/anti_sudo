# OpenClaw gateway + Playwright Chromium + the Story monitor, in one container.
# The gateway (the image's default command) runs the 30-minute command automation,
# which calls `anti-sudo monitor` inside this same container.
FROM ghcr.io/openclaw/openclaw:latest

USER root

COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/anti-sudo/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    DATA_DIR=/data \
    BROWSER_CHANNEL=""

WORKDIR /opt/anti-sudo

# Dependencies first (cached layer), then Chromium + its system libraries.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
 && .venv/bin/python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY scripts ./scripts
COPY database ./database
COPY config.yaml ./config.yaml
COPY docker/anti-sudo /usr/local/bin/anti-sudo

RUN chmod 755 /usr/local/bin/anti-sudo \
 && mkdir -p /data \
 && chown -R node:node /data /opt/anti-sudo /opt/ms-playwright

USER node
WORKDIR /app
