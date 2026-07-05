# The OKF operator image: MCP server, validator, and ingester.
#
# Knowledge is NOT baked into the image — mount it and point the operator at
# it (see docs/usage.md, "Deployment"):
#
#   docker build -t okf-operator .
#   docker run -i --rm \
#     -v /srv/acme-knowledge:/knowledge \
#     -e OKF_KNOWLEDGE_ROOT=/knowledge \
#     -e OKF_TOKEN=... \
#     okf-operator
#
# Without a mount/env the bundled demo fixtures are served, so the image also
# works standalone for evaluation.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# git is needed by the ingester's git connector
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --no-dev

# Demo fixtures + demo configs (fallback when no knowledge root is mounted)
COPY bundles ./bundles
COPY config ./config

CMD ["uv", "run", "okf-mcp"]
