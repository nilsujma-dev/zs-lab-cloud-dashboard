# syntax=docker/dockerfile:1
# Switchboard — cloud-lab control plane. One image: FastAPI app + the tools the
# use-case engine shells out to (OpenTofu, AWS CLI v2, git, python3).
#
# The lab host (10.1.200.10) is x86_64 Ubuntu, so the image is pinned to
# linux/amd64. Building on an Apple-silicon Mac works through Docker Desktop's
# emulation; the result is the image the host will actually run. Override with
# --build-arg TARGET_PLATFORM=linux/arm64 if you ever need a native image.
ARG TARGET_PLATFORM=linux/amd64
FROM --platform=${TARGET_PLATFORM} python:3.12-slim

ARG TOFU_VERSION=1.12.6
ARG AWSCLI_VERSION=2.36.40

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# --- OS packages ------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# --- OpenTofu (pinned, checksum-verified against the release SHA256SUMS) ----
RUN set -eux; \
    cd /tmp; \
    base="https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}"; \
    curl -fsSLO "${base}/tofu_${TOFU_VERSION}_amd64.deb"; \
    curl -fsSLO "${base}/tofu_${TOFU_VERSION}_SHA256SUMS"; \
    grep " tofu_${TOFU_VERSION}_amd64.deb\$" "tofu_${TOFU_VERSION}_SHA256SUMS" | sha256sum -c -; \
    dpkg -i "tofu_${TOFU_VERSION}_amd64.deb"; \
    rm -f "tofu_${TOFU_VERSION}_amd64.deb" "tofu_${TOFU_VERSION}_SHA256SUMS"; \
    tofu version

# --- AWS CLI v2 (pinned; official x86_64 zip) -------------------------------
# AWS publishes a GPG signature rather than a checksum for this artifact; the
# pin plus TLS to awscli.amazonaws.com is what is verified here.
RUN set -eux; \
    cd /tmp; \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWSCLI_VERSION}.zip" -o awscliv2.zip; \
    unzip -q awscliv2.zip; \
    ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli; \
    rm -rf aws awscliv2.zip; \
    aws --version

# --- Non-root runtime user ---------------------------------------------------
# uid/gid 1000 on purpose: the host's ~/.zscaler_api_key (mode 0600, owned by
# the deploying user, uid 1000 on a stock Ubuntu install) is bind-mounted
# read-only, and this is the uid that can read it.
RUN groupadd --gid 1000 switchboard \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/switchboard \
      --shell /bin/bash switchboard \
 && mkdir -p /data \
 && chown switchboard:switchboard /data

WORKDIR /app

# Requirements first so source changes do not reinstall dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY usecases/ usecases/

ENV HOME=/home/switchboard \
    SWITCHBOARD_DATA=/data

VOLUME ["/data"]
USER switchboard
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health >/dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
