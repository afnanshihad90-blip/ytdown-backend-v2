#!/bin/sh
# Free Cloudflare WARP as a local SOCKS5 proxy (so YouTube does not see the datacenter IP).
# Config source: env WARP_CONF (recommended) or auto-register with wgcf.
if [ "${USE_WARP:-1}" = "1" ]; then
  mkdir -p /tmp/warp && cd /tmp/warp
  echo "[warp] setting up..."
  if [ -n "$WARP_CONF" ]; then
    printf '%s\n' "$WARP_CONF" > wgcf-profile.conf
    echo "[warp] using WARP_CONF from environment"
  else
    for i in 1 2 3; do
      [ -f wgcf-account.toml ] || wgcf register --accept-tos
      wgcf generate && break
      echo "[warp] wgcf attempt $i failed"
      sleep 3
    done
  fi
  if [ -s wgcf-profile.conf ]; then
    cp wgcf-profile.conf wireproxy.conf
    printf '\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' >> wireproxy.conf
    wireproxy -c wireproxy.conf > /tmp/warp/wireproxy.log 2>&1 &
    sleep 3
    echo "[warp] wireproxy log:"
    cat /tmp/warp/wireproxy.log
    echo "[warp] started"
  else
    echo "[warp] SETUP FAILED - continuing without WARP"
  fi
fi
cd /app
exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 600
