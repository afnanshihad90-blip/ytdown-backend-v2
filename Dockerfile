FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# JavaScript runtime that recent yt-dlp versions need for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Free Cloudflare WARP (userspace, no root/TUN needed). Failures here never break the build.
ARG WGCF_VERSION=2.2.22
ARG WIREPROXY_VERSION=1.0.9
RUN set -eux; \
    case "$(uname -m)" in x86_64) A=amd64;; aarch64) A=arm64;; *) A=amd64;; esac; \
    ( curl -fsSL -o /usr/local/bin/wgcf "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${A}" \
      && chmod +x /usr/local/bin/wgcf ) || echo "wgcf download failed"; \
    ( curl -fsSL "https://github.com/pufferffish/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${A}.tar.gz" \
      | tar -xz -C /usr/local/bin wireproxy ) || echo "wireproxy download failed"; \
    ls -la /usr/local/bin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x start.sh

CMD ["sh", "start.sh"]
