#!/bin/sh
# Optional: free Cloudflare WARP (WireGuard) as a SOCKS5 proxy so YouTube does not see the datacenter IP.
if [ "${USE_WARP:-1}" = "1" ]; then
  mkdir -p /tmp/warp && cd /tmp/warp
  for i in 1 2 3; do
    [ -f wgcf-account.toml ] || wgcf register --accept-tos >/dev/null 2>&1
    wgcf generate >/dev/null 2>&1 && break
    sleep 2
  done
  if [ -f wgcf-profile.conf ]; then
    cp wgcf-profile.conf wireproxy.conf
    printf '\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' >> wireproxy.conf
    wireproxy -c wireproxy.conf >/tmp/warp/wireproxy.log 2>&1 &
    echo "WARP proxy started"
  else
    echo "WARP setup failed - continuing without it"
  fi
fi
cd /app
exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 600
