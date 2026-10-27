# ytdown backend v4.3

Render: New -> Web Service -> this repo -> Language: Docker -> Free.
(Do NOT keep Procfile / runtime.txt in the repo.)

After deploy open  https://YOUR-URL/api/health
  {"status":"ok","version":"4.3","warp":true|false,"cookies":...,"proxy":...}
"version":"4.3" proves this code is running. warp turns true ~20-40s after boot.
Render Logs: search "[warp]" to see what WARP did.

Optional env vars
  WARP_CONF  full text of wgcf-profile.conf (if Cloudflare refuses registration from Render)
  YT_PROXY   your own proxy, e.g. socks5://user:pass@host:port (best stability)
  YT_COOKIES Netscape cookies.txt from a throw-away Google account
