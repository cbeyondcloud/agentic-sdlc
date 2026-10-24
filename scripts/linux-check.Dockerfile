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
# promtool for the control plane's alert rules (M2-12): only the promtool binary from the
# Prometheus release (Apache-2.0), pinned by version and sha256 for both architectures.
ARG PROMETHEUS_VERSION=3.15.0
RUN set -e; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum=2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0 ;; \
      arm64) sum=f1f90ec08e849d494ca66c611470afc50192f0355f1a61c33f2cbde02d067823 ;; \
      *) echo "no pinned prometheus for $arch" >&2; exit 1 ;; \
    esac; \
    name="prometheus-${PROMETHEUS_VERSION}.linux-${arch}"; \
    curl -sSfL -o /tmp/prometheus.tgz \
      "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/${name}.tar.gz"; \
    echo "$sum  /tmp/prometheus.tgz" | sha256sum -c -; \
    tar -xzf /tmp/prometheus.tgz -C /usr/local/bin --strip-components=1 "${name}/promtool"; \
    rm /tmp/prometheus.tgz
# The pinned Codex CLI for the codex backend's contract tests (M2-40, spike S3): the Linux
# musl binary from the GitHub release, checked against the release's sha256 (Apache-2.0).
# Developer laptops never get it from here: on Windows it runs inside WSL2 (D38).
ARG CODEX_TAG=rust-v0.160.1
RUN set -e; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) file=x86_64; sum=9226581be592d18f7e7f740a352fdb63aa61e45e39f7eb9b09d3888c84bba33f ;; \
      arm64) file=aarch64; sum=f54dc5852042445bf41da3aa31156f3cb02f52c5a1a04074de73dc5598f7e1f7 ;; \
      *) echo "no pinned codex for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /tmp/codex.tgz \
      "https://github.com/openai/codex/releases/download/${CODEX_TAG}/codex-${file}-unknown-linux-musl.tar.gz"; \
    echo "$sum  /tmp/codex.tgz" | sha256sum -c -; \
    tar -xzf /tmp/codex.tgz -C /usr/local/bin; \
    mv "/usr/local/bin/codex-${file}-unknown-linux-musl" /usr/local/bin/codex; \
    chmod 0755 /usr/local/bin/codex; rm /tmp/codex.tgz
