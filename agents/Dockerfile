# AgentCore Runtime requires linux/arm64 containers. These version tags publish
# multi-architecture manifests; agents/build.sh selects the linux/arm64 variant
# explicitly. Do not pin an architecture-specific child digest here.
FROM ghcr.io/astral-sh/uv:0.12.8 AS uv
FROM python:3.12-slim

ARG AGENT_KIND=assessment
ENV AGENT_KIND=${AGENT_KIND}
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY common/ ./common/
COPY intake/ ./intake/
COPY assessment/ ./assessment/
COPY entrypoint.py .

EXPOSE 8080
CMD ["python", "entrypoint.py"]
