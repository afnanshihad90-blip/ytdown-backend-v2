"""Admin-panel link: DISABLED.

Kept only as an inert stub so the rest of the code keeps working. It never contacts any server
and ignores the ADMIN_URL / ADMIN_SECRET environment variables.
"""
import hashlib

ENABLED = False


def config(force=False, ttl=45):
    return {}


def status():
    return {'enabled': False}


def hash_ip(ip):
    return hashlib.sha256(str(ip).encode()).hexdigest()[:12]


def event(**kw):
    return None
