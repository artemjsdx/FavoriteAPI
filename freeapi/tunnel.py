"""
ServeoManager — SSH tunnel via serveo.net using subprocess.

Strategy:
  1. Try custom subdomain (SERVEO_NAME, default: favapi)
  2. If taken/rejected — connect without subdomain (Serveo assigns random URL)

SSH key is stored in SERVEO_KEY_PATH. Same key = same random URL across restarts.

Env vars:
  SERVEO_KEY_PATH  -- path to SSH private key (default: ./serveo_key)
  SERVEO_NAME      -- preferred subdomain (default: favapi)
"""
import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger('freeapi')

DEFAULT_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'serveo_key'
)


def _ensure_key(key_path: str) -> bool:
    """Generate SSH key if not present."""
    if os.path.exists(key_path):
        logger.info('[Serveo] Using existing key: %s', key_path)
        return True
    try:
        os.makedirs(os.path.dirname(os.path.abspath(key_path)) or '.', exist_ok=True)
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-f', key_path, '-N', '', '-q'],
            check=True, capture_output=True,
        )
        logger.info('[Serveo] SSH key generated: %s', key_path)
        return True
    except Exception as e:
        logger.error('[Serveo] Failed to generate key: %s', e)
        return False


def _ssh_connect(key_path: str, port: int, subdomain: Optional[str]) -> subprocess.Popen:
    """Start SSH process to serveo.net."""
    if subdomain:
        remote = f'{subdomain}:80:localhost:{port}'
    else:
        remote = f'80:localhost:{port}'
    cmd = [
        'ssh',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', f'IdentityFile={key_path}',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'BatchMode=yes',
        '-o', 'LogLevel=QUIET',
        '-R', remote,
        'serveo.net',
    ]
    logger.info('[Serveo] ssh -R %s serveo.net', remote)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


class ServeoManager:
    """
    Serveo tunnel via subprocess SSH.
    Tries preferred subdomain; falls back to Serveo-assigned random URL.
    """

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
            logger.error('[Serveo] No SSH key available, tunnel disabled')
            return

        # Try: preferred subdomain first, then random (None)
        attempts = [self._primary_name, None]
        attempt_idx = 0
        backoff = 5

        while not self._stop_event.is_set():
            subdomain = attempts[attempt_idx % len(attempts)]
            label = f'{subdomain}.serveo.net' if subdomain else 'serveo.net (random URL)'
            logger.info('[Serveo] Connecting: %s', label)

            result = self._connect(subdomain)

            if result == 'taken' and subdomain is not None:
                logger.warning('[Serveo] Subdomain %s taken, switching to random URL', subdomain)
                attempt_idx = 1  # go to random
                time.sleep(2)
                continue

            if result == 'ok':
                backoff = 5  # reset on clean reconnect

            if not self._stop_event.is_set():
                logger.info('[Serveo] Reconnecting in %ss...', backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _connect(self, subdomain: Optional[str]) -> str:
        """
        Run SSH connection. Returns:
          'ok'    -- connected and got URL, then dropped
          'taken' -- subdomain rejected by Serveo
          'error' -- SSH error or no URL found
        """
        proc = _ssh_connect(self._key_path, self._port, subdomain)
        url_found = False
        taken = False

        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    logger.debug('[Serveo] >> %s', line)
                low = line.lower()
                # Serveo signals subdomain conflict
                if any(x in low for x in ('taken', 'in use', 'permission denied', 'refused'))  and subdomain:
                    taken = True
                    break
                # Parse URL from Serveo output
                m = re.search(r'(https://[\w\-]+\.serveo\.net)', line)
                if m:
                    self._set_url(m.group(1))
                    url_found = True
                if self._stop_event.is_set():
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        if taken:
            return 'taken'
        if url_found:
            return 'ok'
        return 'error'


# Backward compatibility aliases
TunnelManager = ServeoManager
