# The control plane's image (M2-39): `python -m agentic_cp <command>` with the workspace's
# locked dependencies, no dev tools. Build from the repository root:
#   docker build -f control-plane/Dockerfile -t agentic-control-plane:local .
# The base is the uv/Python image the local CI uses, pinned by tag and digest
# (0.12.1, 2026-07-31). git is needed to install the locked git dependency and by the
# worker's policy sync.
FROM ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim@sha256:fce651fe385e83c691c15c33b2574486581fbc58e307f2db3a9c3987919f8b08 AS build
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends git ca-certificates >/dev/null \
    && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/agentic
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY core core
COPY commons commons
COPY cli/pyproject.toml cli/pyproject.toml
COPY cli/src cli/src
COPY control-plane control-plane
RUN uv sync --frozen --no-dev --no-editable --package agentic-control-plane

FROM ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim@sha256:fce651fe385e83c691c15c33b2574486581fbc58e307f2db3a9c3987919f8b08
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends git ca-certificates >/dev/null \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 agentic
COPY --from=build /opt/agentic /opt/agentic
ENV PATH=/opt/agentic/bin:$PATH PYTHONUNBUFFERED=1
USER agentic
WORKDIR /home/agentic
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "agentic_cp"]
CMD ["serve"]
