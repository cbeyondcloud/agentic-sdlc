# Throwaway S6 probe image: srt 0.0.78 on Debian bookworm, run as a non-root user.
FROM node:22.11.0-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap socat ripgrep python3 curl git \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/sandbox-runtime@0.0.78
COPY probe.py /probe/probe.py
USER node
WORKDIR /home/node
CMD ["python3", "/probe/probe.py"]
