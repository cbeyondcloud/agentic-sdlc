# The Linux image the local CI runs in (scripts/check.py): the pinned uv/Python image
# plus git and the sandbox prerequisites, so the live `srt` tests run (M1-32, D38), and
# OpenSSH's ssh-keygen (BSD) so the release signatures are checked against it (M3-21).
FROM ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        git bubblewrap socat ripgrep nodejs npm curl ca-certificates openssh-client >/dev/null \
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
# actionlint for the shipped GitHub Actions workflow templates (M2-44): the release binary
# (MIT, v1.7.12 of 2026-03-30), pinned by version and sha256 for both architectures. Its
# shellcheck integration stays off (shellcheck is GPL-3.0, not on the licence allow-list).
ARG ACTIONLINT_VERSION=1.7.12
RUN set -e; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 ;; \
      arm64) sum=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6 ;; \
      *) echo "no pinned actionlint for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /tmp/actionlint.tgz \
      "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_${arch}.tar.gz"; \
    echo "$sum  /tmp/actionlint.tgz" | sha256sum -c -; \
    tar -xzf /tmp/actionlint.tgz -C /usr/local/bin actionlint; rm /tmp/actionlint.tgz
# helm and kubeconform for the umbrella chart's tests (M3-02): the release binaries
# (Apache-2.0; helm 4.3.1 of 2026-10-15, kubeconform 0.8.0 of 2026-06-04), pinned by version
# and sha256 for both architectures. scripts/tests/helm_support.py names the same releases.
ARG HELM_VERSION=4.3.1
RUN set -e; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b ;; \
      arm64) sum=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88 ;; \
      *) echo "no pinned helm for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /tmp/helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz"; \
    echo "$sum  /tmp/helm.tgz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tgz -C /usr/local/bin --strip-components=1 "linux-${arch}/helm"; \
    rm /tmp/helm.tgz
ARG KUBECONFORM_VERSION=0.8.0
RUN set -e; arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883 ;; \
      arm64) sum=1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87 ;; \
      *) echo "no pinned kubeconform for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /tmp/kubeconform.tgz \
      "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/kubeconform-linux-${arch}.tar.gz"; \
    echo "$sum  /tmp/kubeconform.tgz" | sha256sum -c -; \
    tar -xzf /tmp/kubeconform.tgz -C /usr/local/bin kubeconform; rm /tmp/kubeconform.tgz
# pg_dump and pg_restore for the restore drill's test (M3-02): Debian trixie's PostgreSQL
# 17 client (PostgreSQL licence), the same major version as the test server.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends postgresql-client >/dev/null \
    && rm -rf /var/lib/apt/lists/*
