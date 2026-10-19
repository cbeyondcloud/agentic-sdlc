# The Linux image the local CI runs in (scripts/check.py): the pinned uv/Python image
# plus git and the sandbox prerequisites, so the live `srt` tests run (M1-32, D38).
FROM ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        git bubblewrap socat ripgrep nodejs npm curl ca-certificates >/dev/null \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g --silent @anthropic-ai/sandbox-runtime@0.0.78
