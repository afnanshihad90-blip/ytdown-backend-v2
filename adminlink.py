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
import re
import threading
import time

import requests

try:                                   # pycryptodome (in requirements.txt)
    from Crypto.Cipher import AES as _AES

    def _aes_cbc_dec(key, iv, data):
        return _AES.new(key, _AES.MODE_CBC, iv).decrypt(data)
except Exception:  # noqa: BLE001
    try:                               # or the "cryptography" package
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def _aes_cbc_dec(key, iv, data):
            d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            return d.update(data) + d.finalize()
    except Exception:  # noqa: BLE001
        _aes_cbc_dec = None

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


_cookies = {}


def _solve_challenge(html):
    """Free hosts like InfinityFree answer scripts with a tiny "aes.js" page instead of the real
    response. The page holds an AES key, iv and ciphertext; decrypting gives the __test cookie
    a browser would set. We do the same so the backend can talk to our own admin panel."""
    if _aes_cbc_dec is None:
        return False
    m = re.findall(r'toNumbers\("([0-9a-fA-F]+)"\)', html)
    if len(m) < 3:
        return False
    try:
        key, iv, ct = (bytes.fromhex(x) for x in m[:3])
        _cookies['__test'] = _aes_cbc_dec(key, iv, ct).hex()
        return True
    except Exception:  # noqa: BLE001
        return False


def _request(method, path, **kw):
    r = None
    for attempt in (1, 2):
        r = requests.request(method, _url(path), headers=_headers(),
                             cookies=dict(_cookies) or None, **kw)
        if attempt == 1 and '/aes.js' in r.text[:400] and _solve_challenge(r.text):
            continue                       # retry once with the cookie
        break
    return r


def config(force=False, ttl=45):
    """Settings from the admin panel (cached; last good copy is kept if the panel is down)."""
    if not ENABLED:
        return {}
    with _lock:
        if not force and time.time() - _state['at'] < ttl:
            return _state['data']
        _state['at'] = time.time()          # throttle retries even after a failure
    try:
        r = _request('GET', 'api/config.php', timeout=8)
        try:
            data = r.json()
        except ValueError:
            hint = ' (browser-check not solved; is pycryptodome installed?)' if '/aes.js' in r.text[:400] else ''
            raise ValueError(f'not JSON (HTTP {r.status_code}){hint}: {r.text[:80]!r}')
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
            r = _request('POST', 'api/log.php', data=json.dumps({'events': batch}), timeout=10)
            err = '' if r.ok else f'log HTTP {r.status_code}'
        except Exception as e:  # noqa: BLE001
            err = str(e)[:150]
        with _lock:
            _state['log_error'] = err


if ENABLED:
    threading.Thread(target=_worker, daemon=True).start()
