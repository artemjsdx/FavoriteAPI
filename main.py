import os
import sys
import subprocess
import shutil
import textwrap

base = "/home/container"

# --- 1. Restore freeapi/ from GitHub if missing ---
if not os.path.isdir(os.path.join(base, "freeapi")):
    print("[setup] freeapi/ missing -- restoring from GitHub...", flush=True)
    subprocess.run(
        ["git", "clone", "--depth=1", "https://github.com/artemjsdx/FavoriteAPI.git", "/tmp/_fapi"],
        check=True
    )
    shutil.copytree("/tmp/_fapi/freeapi", os.path.join(base, "freeapi"))
    if not os.path.isdir(os.path.join(base, "static")) and os.path.isdir("/tmp/_fapi/static"):
        shutil.copytree("/tmp/_fapi/static", os.path.join(base, "static"))
    print("[setup] freeapi/ restored!", flush=True)

# --- 2. Always patch tunnel.py (Serveo-only, subprocess SSH) ---
_TUNNEL_PY = os.path.join(base, "freeapi", "tunnel.py")
_TUNNEL_CONTENT = r"""
\"\"\"
ServeoManager -- Serveo SSH tunnel via subprocess.

Runs: ssh -R <subdomain>:80:localhost:<port> serveo.net
Parses stdout to detect active URL.

Env vars:
  SERVEO_KEY_PATH  -- path to SSH key (default: ./serveo_key)
  SERVEO_NAME      -- desired subdomain (default: favapi)
\"\"\"
import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger('freeapi')

DEFAULT_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'serveo_key'
)


def _serveo_candidates(primary: str) -> List[str]:
    \"\"\"Subdomain candidates: primary first, then fallbacks.\"\"\"
    seen = set()
    result = []
    for a in [primary, f'{primary}2', f'{primary}3', f'{primary}x', f'{primary}app']:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result


def _ensure_key(key_path: str) -> bool:
    \"\"\"Generate SSH key if missing. Returns True on success.\"\"\"
    if os.path.exists(key_path):
        return True
    try:
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
        subprocess.run(
            ['ssh-keygen', '-t', 'rsa', '-b', '2048', '-f', key_path, '-N', '', '-q'],
            check=True, capture_output=True,
        )
        logger.info('[Serveo] SSH key generated: %s', key_path)
        return True
    except Exception as e:
        logger.error('[Serveo] Failed to generate key: %s', e)
        return False


class ServeoManager:
    \"\"\"
    Serveo tunnel via subprocess SSH.
    Tries primary subdomain first, then fallbacks if taken.
    \"\"\"

    def __init__(self, port: int, name: str = '',
                 on_url: Optional[Callable[[str], None]] = None):
        self._port = port
        self._on_url = on_url
        self._stop_event = threading.Event()
        self._url: Optional[str] = None
        self._lock = threading.Lock()
        key_path = os.environ.get('SERVEO_KEY_PATH', DEFAULT_KEY_PATH)
        self._key_path = os.path.realpath(key_path)
        self._primary_name = name or os.environ.get('SERVEO_NAME', 'favapi')

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name='serveo-mgr')
        t.start()

    def stop(self):
        self._stop_event.set()

    @property
    def url(self) -> Optional[str]:
        return self._url

    def _set_url(self, url: str):
        with self._lock:
            self._url = url
        logger.info('[Serveo] ✅ URL active: %s', url)
        if self._on_url:
            try:
                self._on_url(url)
            except Exception as exc:
                logger.error('[Serveo] on_url callback error: %s', exc)

    def _run(self):
        if not _ensure_key(self._key_path):
            logger.error('[Serveo] No SSH key, tunnel disabled')
            return

        candidates = _serveo_candidates(self._primary_name)
        idx = 0
        backoff = 5

        while not self._stop_event.is_set():
            name = candidates[idx % len(candidates)]
            logger.info('[Serveo] Trying subdomain: %s.serveo.net', name)
            result = self._connect(name)
            if result == 'taken':
                logger.warning('[Serveo] %s taken, trying next', name)
                idx += 1
                if idx >= len(candidates):
                    logger.warning('[Serveo] All candidates tried, waiting %ss', backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    idx = 0
                continue
            # dropped or error — reconnect with same name after backoff
            if not self._stop_event.is_set():
                logger.info('[Serveo] Reconnecting %s in %ss...', name, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _connect(self, name: str) -> str:
        \"\"\"
        Run SSH tunnel to serveo.net. Returns:
          'ok'    -- connected, then dropped
          'taken' -- subdomain refused
          'error' -- could not connect
        \"\"\"
        cmd = [
            'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', f'IdentityFile={self._key_path}',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'BatchMode=yes',
            '-o', 'LogLevel=QUIET',
            '-R', f'{name}:80:localhost:{self._port}',
            'serveo.net',
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        url_found = False
        status = 'error'
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    logger.debug('[Serveo] %s', line)
                low = line.lower()
                if 'taken' in low or 'in use' in low or 'permission denied' in low:
                    status = 'taken'
                    break
                m = re.search(r'(https://[\\w\\-]+\\.serveo\\.net)', line)
                if m:
                    url = m.group(1)
                    self._set_url(url)
                    url_found = True
                    status = 'ok'
                if self._stop_event.is_set():
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        if url_found:
            return 'ok'
        return status


# Backward compatibility aliases
TunnelManager = ServeoManager

""".strip()
with open(_TUNNEL_PY, "w", encoding="utf-8") as _f:
    _f.write(_TUNNEL_CONTENT + "\n")
print("[setup] tunnel.py patched (Serveo subprocess)", flush=True)

# --- 3. Ensure paramiko installed (fallback, may be needed elsewhere) ---
try:
    import paramiko  # noqa: F401
except ImportError:
    print("[setup] Installing paramiko...", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "paramiko"], check=True)

sys.path.insert(0, base)
os.chdir(base)

import runpy
runpy.run_path(os.path.join(base, "api.py"), run_name="__main__")
