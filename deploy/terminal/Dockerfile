# syntax=docker/dockerfile:1.7
# The terminal's diagnostic image. The base is pinned by digest, AWS CLI and kubectl by version and SHA-256 per
# architecture, and Debian packages come from the current archive (or a snapshot, see DEBIAN_SNAPSHOT).
# See docs/TERMINAL.md for updating it and for asking for another package.
# Build context: deploy/terminal. Works on arm64 (mk4) and amd64 through BuildKit's TARGETARCH.

FROM debian:trixie-20261027-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4 AS base
# Debian packages come from deb.debian.org by default, so each build gets current security fixes; the image digest
# that CI reports is what pins a release. DEBIAN_SNAPSHOT=<YYYYMMDDTHHMMSSZ> rebuilds against a fixed archive
# snapshot instead, for a reproducible build. snapshot.debian.org was answering 504 for hours on 2026-11-05, which is
# why it is no longer the default.
ARG DEBIAN_SNAPSHOT=""
# Plain http is only for the base image, which has no CA certificates yet: apt verifies every package's signature.
# Either mirror can throttle or stall, so apt retries with backoff, uses one connection at a time, and gives up on a
# stalled download sooner so the retry gets its turn.
RUN set -eu; \
    if [ -n "$DEBIAN_SNAPSHOT" ]; then \
      main="http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}"; security="http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}"; opts="[check-valid-until=no] "; \
    else \
      main="http://deb.debian.org/debian"; security="http://deb.debian.org/debian-security"; opts=""; \
    fi; \
    printf '%s\n' "deb ${opts}${main} trixie main" "deb ${opts}${main} trixie-updates main" "deb ${opts}${security} trixie-security main" > /etc/apt/sources.list; \
    rm -f /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      'Acquire::Retries "8";' \
      'Acquire::Retries::Delay::Maximum "30";' \
      'Acquire::http::Timeout "30";' \
      'Acquire::Queue-Mode "access";' \
      'Acquire::http::Pipeline-Depth "0";' \
      > /etc/apt/apt.conf.d/80-retries

FROM base AS tools
ARG TARGETARCH
ARG AWS_CLI_VERSION=2.39.3
ARG KUBECTL_VERSION=v1.37.2
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip
RUN set -eu; \
    case "$TARGETARCH" in \
      arm64) aws_arch=aarch64; aws_sha=f6eb845fbacb11bee87de3f5fbe8043773cbe89d0414607b44b71e216eb9226b; kube_sha=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509 ;; \
      amd64) aws_arch=x86_64; aws_sha=14cb35809ad7547dd3c69fa9899a3975fbd850b6a6791f3fe264856490cd9a63; kube_sha=c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4 ;; \
      *) echo "unsupported architecture $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}-${AWS_CLI_VERSION}.zip"; \
    echo "$aws_sha  /tmp/awscli.zip" | sha256sum -c -; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    echo "$kube_sha  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl; \
    unzip -q /tmp/awscli.zip -d /tmp; \
    /tmp/aws/install --install-dir /opt/aws-cli --bin-dir /usr/local/bin

FROM base
ARG GIT_COMMIT=dev
LABEL org.opencontainers.image.revision=$GIT_COMMIT org.opencontainers.image.title=moks-terminal
# upgrade first: the pinned base image lags its own security fixes (apt 3.0.3, CVE-2026-109518, fixed in 3.0.4)
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends bash bind9-dnsutils ca-certificates curl groff-base jq less netcat-openbsd openssl procps tmux \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*
COPY --from=tools /opt/aws-cli /opt/aws-cli
COPY --from=tools /usr/local/bin/kubectl /usr/local/bin/kubectl
RUN ln -s /opt/aws-cli/v2/current/bin/aws /usr/local/bin/aws
COPY tmux.conf /etc/tmux.conf
COPY bashrc /etc/moks-bashrc
COPY verify-isolation.sh /usr/local/bin/moks-verify-isolation
# No package manager for the session: it could not run as this user anyway, and this says so plainly.
RUN chmod 0755 /usr/local/bin/moks-verify-isolation \
    && rm -f /usr/bin/apt /usr/bin/apt-get /usr/bin/dpkg \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true
RUN groupadd --gid 10002 engineer && useradd --uid 10002 --gid 10002 --no-create-home --home-dir /tmp/home --shell /bin/bash engineer
ENV TERM=xterm-256color
USER 10002:10002
WORKDIR /tmp
CMD ["sleep", "infinity"]
