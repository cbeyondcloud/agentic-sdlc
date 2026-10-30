# The kind smoke's tool container (M3-02): helm (from the pinned alpine/helm image), kind and
# kubectl (release binaries pinned by version and sha256 for both architectures) and the
# docker CLI from Alpine's packages. It drives the host's engine through its socket, so no
# native Kubernetes tool ever runs on a developer laptop. All Apache-2.0.
FROM alpine/helm:4.3.1@sha256:e3f8d5c462d75470fbb1fce79ad5c20063657096d1f98c0fcea869e2600e2e5e
RUN apk add --no-cache docker-cli curl ca-certificates >/dev/null
# kind v0.33.0 (2026-08-26)
ARG KIND_VERSION=0.33.0
RUN set -e; arch="$(uname -m)"; \
    case "$arch" in \
      x86_64) a=amd64; sum=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d ;; \
      aarch64) a=arm64; sum=20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2 ;; \
      *) echo "no pinned kind for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /usr/local/bin/kind \
      "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-${a}"; \
    echo "$sum  /usr/local/bin/kind" | sha256sum -c -; chmod 0755 /usr/local/bin/kind
# kubectl v1.34.11, the version of the kind node image in inside.sh
ARG KUBECTL_VERSION=1.34.11
RUN set -e; arch="$(uname -m)"; \
    case "$arch" in \
      x86_64) a=amd64; sum=8efbb9435132a190920eb65a47a8c1ecf755ad85ab57a600c9bedbab460bb7a8 ;; \
      aarch64) a=arm64; sum=5b045a4712674c88a56fd98eef4285689738b7fbe8735e1b9ee3509521af5cb4 ;; \
      *) echo "no pinned kubectl for $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSfL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${a}/kubectl"; \
    echo "$sum  /usr/local/bin/kubectl" | sha256sum -c -; chmod 0755 /usr/local/bin/kubectl
ENTRYPOINT []
