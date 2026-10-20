# Throwaway S7 probe image (never shipped). Every agent is pinned to its newest release
# at least two weeks old on 2026-10-20:
#   @google/gemini-cli 0.63.0 (2026-10-06)   opencode-ai 1.18.35 (2026-10-06)
#   @github/copilot 1.0.92 (2026-10-05)      goose v1.53.0 (2026-10-02)
#   agy (Antigravity CLI) 1.3.0 (2026-10-06) agy ACP server 1.3.0 (registry 2026-10-02)
#   agent-client-protocol (Python ACP SDK) 0.12.1 (2026-08-16)
# Run with `--network none`: the stub model endpoint and the refusing proxy are on 127.0.0.1.
FROM node:22-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git unzip bzip2 \
       python3 python3-venv procps \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir agent-client-protocol==0.12.1
RUN npm install -g --no-fund --no-audit @google/gemini-cli@0.63.0 opencode-ai@1.18.35 @github/copilot@1.0.92
RUN mkdir -p /opt/goose && curl -fsSL -o /tmp/goose.tar.bz2 \
        https://github.com/aaif-goose/goose/releases/download/v1.53.0/goose-x86_64-unknown-linux-gnu.tar.bz2 \
    && tar -xjf /tmp/goose.tar.bz2 -C /opt/goose && sha256sum /tmp/goose.tar.bz2 && rm /tmp/goose.tar.bz2 \
    && ln -s "$(find /opt/goose -type f -name goose | head -1)" /usr/local/bin/goose
RUN mkdir -p /opt/agy && curl -fsSL -o /tmp/agy.tar.gz \
        https://github.com/google-antigravity/antigravity-cli/releases/download/1.3.0/agy_cli_linux_x64.tar.gz \
    && tar -xzf /tmp/agy.tar.gz -C /opt/agy && sha256sum /tmp/agy.tar.gz && rm /tmp/agy.tar.gz \
    && ls -laR /opt/agy | head -40
RUN mkdir -p /opt/agy-acp && curl -fsSL -o /tmp/agyacp.zip \
        https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-1.3.0-linux-x86_64.zip \
    && unzip -q /tmp/agyacp.zip -d /opt/agy-acp && sha256sum /tmp/agyacp.zip && rm /tmp/agyacp.zip \
    && ls -la /opt/agy-acp
RUN useradd -m probe
COPY stub.py probe.py /probe/
USER probe
WORKDIR /home/probe
CMD ["/venv/bin/python", "/probe/probe.py"]
