"""Link between the Render backend and the admin panel (PHP, hosted on InfinityFree).

- pulls settings (API keys, proxies, cookies, mode) from  ADMIN_URL/api/config.php
- pushes events (lookups, failures, which key was used) to ADMIN_URL/api/log.php
Env on Render:  ADMIN_URL=https://your-admin-site.example   ADMIN_SECRET=<secret shown in admin Settings>
Nothing here is required: without these two variables the backend works as before.
"""
import hashlib
import json
import os
import queue
import threading
import time

import requests

ADMIN_URL = (os.environ.get('ADMIN_URL') or '').strip().rstrip('/')
ADMIN_SECRET = (os.environ.get('ADMIN_SECRET') or '').strip()
ENABLED = bool(ADMIN_URL and ADMIN_SECRET)
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

_state = {'data': {}, 'at': 0.0, 'ok': False, 'last_ok': 0.0,
          'error': 'ADMIN_URL / ADMIN_SECRET not set' if not ENABLED else 'not fetched yet',
          'log_error': ''}
_lock = threading.Lock()
_q = queue.Queue(maxsize=500)


def _url(path):
    # "?i=1" lets server-to-server calls pass InfinityFree's browser check
    # "s" duplicates the X-Secret header in case the host strips custom headers
    return f"{ADMIN_URL}/{path}{'&' if '?' in path else '?'}i=1&s={ADMIN_SECRET}"


def _headers():
    return {'X-Secret': ADMIN_SECRET, 'User-Agent': UA, 'Accept': 'application/json',
            'Content-Type': 'application/json'}


def config(force=False, ttl=45):
    """Settings from the admin panel (cached; last good copy is kept if the panel is down)."""
    if not ENABLED:
        return {}
    with _lock:
        if not force and time.time() - _state['at'] < ttl:
            return _state['data']
        _state['at'] = time.time()          # throttle retries even after a failure
    try:
        r = requests.get(_url('api/config.php'), headers=_headers(), timeout=8)
        try:
            data = r.json()
        except ValueError:
            raise ValueError(f'not JSON (HTTP {r.status_code}): {r.text[:80]!r}')
        if not r.ok or not data.get('ok'):
            raise ValueError(f"HTTP {r.status_code}: {str(data.get('error') or data)[:100]}")
        with _lock:
            _state.update(data=data, ok=True, error='', last_ok=time.time())
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update(ok=False, error=str(e)[:200])
    return _state['data']


def status():
    with _lock:
        return {'enabled': ENABLED, 'connected': _state['ok'], 'error': _state['error'],
                'log_error': _state['log_error'],
                'last_ok_seconds_ago': int(time.time() - _state['last_ok']) if _state['last_ok'] else None}


def hash_ip(ip):
    return hashlib.sha256(f'{ip}|{ADMIN_SECRET}'.encode()).hexdigest()[:12]


def event(**kw):
    """Queue one event for the admin panel. Never raises, never blocks."""
    if not ENABLED:
        return
    kw['ts'] = int(time.time())
    for k in ('detail', 'user_msg', 'url'):
        if k in kw and kw[k] is not None:
            kw[k] = str(kw[k])[:400]
    try:
        _q.put_nowait(kw)
    except queue.Full:
        pass


def _worker():
    while True:
        batch = [_q.get()]
        time.sleep(1.5)                      # collect a few events into one request
        while len(batch) < 50:
            try:
                batch.append(_q.get_nowait())
            except queue.Empty:
                break
        try:
            r = requests.post(_url('api/log.php'), headers=_headers(),
                              data=json.dumps({'events': batch}), timeout=10)
            err = '' if r.ok else f'log HTTP {r.status_code}'
        except Exception as e:  # noqa: BLE001
            err = str(e)[:150]
        with _lock:
            _state['log_error'] = err


if ENABLED:
    threading.Thread(target=_worker, daemon=True).start()
