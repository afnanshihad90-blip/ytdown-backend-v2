#!/bin/sh
# Starts a free Cloudflare WARP socks5 proxy (127.0.0.1:40000) in the background,
# then ALWAYS starts the web app, even if WARP fails.

rm -f /tmp/warp_ok
WORK=/tmp/warp
mkdir -p "$WORK"

warp_setup() {
  command -v wgcf >/dev/null 2>&1 || { echo "[warp] wgcf binary missing (download failed at build)"; return 1; }
  command -v wireproxy >/dev/null 2>&1 || { echo "[warp] wireproxy binary missing (download failed at build)"; return 1; }
  cd "$WORK" || return 1

  if [ -n "$WARP_CONF" ]; then
    echo "[warp] using WARP_CONF from environment"
    printf '%s\n' "$WARP_CONF" > wgcf-profile.conf
  else
    i=1
    while [ $i -le 4 ]; do
      echo "[warp] registering new WARP account (try $i/4)"
      rm -f wgcf-account.toml wgcf-profile.conf
      if wgcf register --accept-tos 2>&1 && wgcf generate 2>&1 && [ -s wgcf-profile.conf ]; then
        break
      fi
      echo "[warp] wgcf attempt $i failed"
      i=$((i+1)); sleep 5
    done
  fi
  [ -s wgcf-profile.conf ] || { echo "[warp] no profile created (Cloudflare refused registration from this IP)"; return 1; }

  # wireproxy config = wg profile + a local socks5 listener
  # (strip IPv6 so it works on hosts without IPv6)
  sed -e 's/, *[0-9a-fA-F:]*:[0-9a-fA-F:]*\/[0-9]*//g' wgcf-profile.conf > wireproxy.conf
  printf '\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' >> wireproxy.conf

  wireproxy -c wireproxy.conf > "$WORK/wireproxy.log" 2>&1 &
  sleep 4

  j=1
  while [ $j -le 5 ]; do
    T=$(curl -s -m 12 --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    if echo "$T" | grep -q "warp=on\|warp=plus"; then
      echo "[warp] WARP proxy is working: $(echo "$T" | grep '^ip=')"
      touch /tmp/warp_ok
      return 0
    fi
    j=$((j+1)); sleep 3
  done
  echo "[warp] proxy started but test failed. wireproxy log:"
  tail -n 15 "$WORK/wireproxy.log"
  return 1
}

( warp_setup || echo "[warp] SETUP FAILED - continuing without WARP" ) &

cd /app
exec gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 600
