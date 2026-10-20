# Throwaway S3 probe image: the pinned Codex CLI (rust-v0.160.1, released 2026-10-05, the
# newest release at least two weeks old on 2026-10-20) on Debian bookworm. The binary is
# fetched from the GitHub release and checked against the release's published sha256.
# Run the probe with `--network none`: the stub and the refusing proxy are on 127.0.0.1.
FROM python:3.12.7-slim-bookworm
ARG CODEX_TAG=rust-v0.160.1
ARG CODEX_SHA256=9226581be592d18f7e7f740a352fdb63aa61e45e39f7eb9b09d3888c84bba33f
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git bubblewrap \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/codex.tar.gz \
        "https://github.com/openai/codex/releases/download/${CODEX_TAG}/codex-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /usr/local/bin \
    && mv /usr/local/bin/codex-x86_64-unknown-linux-musl /usr/local/bin/codex \
    && chmod 0755 /usr/local/bin/codex \
    && rm /tmp/codex.tar.gz
RUN useradd -m probe
COPY stub.py probe.py probe2.py probe3.py /probe/
USER probe
WORKDIR /home/probe
CMD ["python3", "/probe/probe.py"]
