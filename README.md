# ytdown backend

Backend for the ytdown downloader (Render web service).

## Deploy on Render

Render → New → Web Service → this repo → Language: Docker → Free plan.
(Do NOT keep Procfile / runtime.txt in the repo.)

After deploy open `https://YOUR-URL/api/health`
```json
{"cookies":false,"proxy":false,"status":"ok","version":"4.4","warp":true|false}
```
- `version:"4.4"` proves this fixed code is running.
- `warp` turns true ~20-40s after boot only if WARP signed in.

## v4.5

- Fixed: impersonation was forced on every request (even fallback attempts); now optional with automatic retry without it.
- curl-cffi pinned to a yt-dlp-compatible range.
- /api/health now shows yt-dlp version + impersonate flag.

## What changed (v4.4 fix)

1. **Original / best-quality no longer shrinks to a tiny mp4.**
   The video job previously forced every download to MP4. YouTube's best
   quality is VP9/AV1 (webm) + Opus audio, which can't go inside mp4, so
   yt-dlp silently fell back to a small combined stream (a 50 MB "Original"
   became ~5 MB). Now:
   - "Original (best quality)" downloads the true best stream and saves as
     `.mkv` (full quality, no re-encode, no quality loss).
   - The resolution buttons (1080p / 720p / …) still give MP4 as before.
2. **Bot / "confirm you're not a bot" errors reduced.**
   Backend now impersonates a real Chrome via `curl_cffi` (browser TLS
   fingerprint) and tries more YouTube player-clients, so the datacenter IP
   on Render is far less likely to be blocked. No cookies/proxy required.
3. **yt-dlp pinned** to a current release + `curl-cffi` added (requirements).

## Optional env vars (only if impersonation still gets blocked)

- `YT_COOKIES` — Netscape-format cookies.txt from a throw-away Google account
  (most reliable unlock for bot errors).
- `YT_PROXY` — your own proxy, e.g. `socks5://user:pass@host:port`.
- `WARP_CONF` — full text of a wgcf-profile.conf (if Cloudflare refuses
  registration from Render), see start.sh.

## Notes

- Free Render instances spin down after 15 min idle; the first request after
  idle takes ~1 min to wake up (the frontend shows "starting up").
- Free instances have an ephemeral filesystem — finished downloads are
  deleted on restart/spin-down (jobs expire in 15 min).

## v4.5 proxies & cookies (env vars on Render)

- `YT_PROXIES` - several proxies, comma or newline separated, e.g.
  `http://user:pass@host:10001,http://user:pass@host:10002`
  (a password containing `+` must be written `%2B`). Up to 3 random ones are tried per request.
- `cookies.txt` in this folder is loaded automatically (YouTube-only cookies). KEEP THE REPO PRIVATE. Or use env `YT_COOKIES` / a Render Secret File instead.
  Use a throw-away Google account and only youtube.com cookies. Never commit it to git.
- `/api/health` shows `proxies` (count) and `cookies`.
