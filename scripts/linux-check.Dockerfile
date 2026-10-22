# The Linux image the local CI runs in (scripts/check.py): the pinned uv/Python image
# plus git and the sandbox prerequisites, so the live `srt` tests run (M1-32, D38).
FROM ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        git bubblewrap socat ripgrep nodejs npm curl ca-certificates >/dev/null \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g --silent @anthropic-ai/sandbox-runtime@0.0.78
# The opencode ACP profile's pinned agent (M2-57, spike S7): its contract tests run the real
# binary here only - developer laptops run no unsigned native binaries.
RUN npm install -g --silent opencode-ai@1.18.35
ENV AGENTIC_CHECK_IMAGE=1
# gitleaks for the repo secret scan (P0-03), pinned by version and sha256 for both
# architectures (amd64 on Windows/Intel hosts, arm64 on Apple Silicon Macs).
ARG GITLEAKS_VERSION=8.30.1
RUN set -e; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) file=linux_x64; sum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;; \
      arm64) file=linux_arm64; sum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;; \
      *) echo "no pinned gitleaks for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /tmp/gitleaks.tgz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_${file}.tar.gz"; \
    echo "$sum  /tmp/gitleaks.tgz" | sha256sum -c -; \
    tar -xzf /tmp/gitleaks.tgz -C /usr/local/bin gitleaks; rm /tmp/gitleaks.tgz
