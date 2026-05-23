import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger("freeapi")

DEFAULT_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "serveo_key"
)


def _ensure_key(key_path):
    if os.path.exists(key_path):
        logger.info("[Serveo] Using existing key: %s", key_path)
        return True

    os.makedirs(os.path.dirname(os.path.abspath(key_path)) or ".", exist_ok=True)

    # Try ssh-keygen first
    try:
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q"],
            capture_output=True,
        )
        if r.returncode == 0:
            logger.info("[Serveo] SSH key generated via ssh-keygen")
            return True
    except FileNotFoundError:
        pass

    # Fallback: generate via Python cryptography (always installed with paramiko)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption
        )
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
        with open(key_path, "wb") as f:
            f.write(pem)
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
        logger.info("[Serveo] SSH key generated via Python cryptography")
        return True
    except Exception as e:
        logger.error("[Serveo] Failed to generate key: %s", e)
        return False


def _ssh_available():
    try:
        r = subprocess.run(["ssh", "-V"], capture_output=True)
        return True
    except FileNotFoundError:
        return False


def _ssh_connect(key_path, port, subdomain):
    if subdomain:
        remote = "{}:80:localhost:{}".format(subdomain, port)
    else:
        remote = "80:localhost:{}".format(port)
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "IdentityFile=" + key_path,
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=QUIET",
        "-R", remote,
        "serveo.net",
    ]
    logger.info("[Serveo] ssh -R %s serveo.net", remote)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _paramiko_connect(key_path, port, subdomain):
    import paramiko
    import socket
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.Ed25519Key(filename=key_path)
    client.connect("serveo.net", port=22, username="serveo", pkey=pkey)
    transport = client.get_transport()
    name = subdomain if subdomain else ""
    transport.request_port_forward(name, 80)
    return client, transport


class ServeoManager:
    def __init__(self, port, name="", on_url=None):
        self._port = port
        self._on_url = on_url
        self._stop_event = threading.Event()
        self._url = None
        self._lock = threading.Lock()
        key_path = os.environ.get("SERVEO_KEY_PATH", DEFAULT_KEY_PATH)
        self._key_path = os.path.realpath(key_path)
        self._primary_name = name or os.environ.get("SERVEO_NAME", "favapi")
        self._use_subprocess = None

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="serveo-mgr")
        t.start()

    def stop(self):
        self._stop_event.set()

    @property
    def url(self):
        return self._url

    def _set_url(self, url):
        with self._lock:
            self._url = url
        logger.info("[Serveo] URL active: %s", url)
        if self._on_url:
            try:
                self._on_url(url)
            except Exception as exc:
                logger.error("[Serveo] on_url callback error: %s", exc)

    def _run(self):
        if not _ensure_key(self._key_path):
            logger.error("[Serveo] No SSH key, tunnel disabled")
            return

        self._use_subprocess = _ssh_available()
        mode = "subprocess ssh" if self._use_subprocess else "paramiko"
        logger.info("[Serveo] Using %s for tunnel", mode)

        attempts = [self._primary_name, None]
        attempt_idx = 0
        backoff = 5

        while not self._stop_event.is_set():
            subdomain = attempts[attempt_idx % len(attempts)]
            label = "{}.serveo.net".format(subdomain) if subdomain else "serveo.net (random)"
            logger.info("[Serveo] Connecting: %s", label)

            result = self._connect(subdomain)

            if result == "taken" and subdomain is not None:
                logger.warning("[Serveo] %s taken, switching to random", subdomain)
                attempt_idx = 1
                time.sleep(2)
                continue

            if result == "ok":
                backoff = 5

            if not self._stop_event.is_set():
                logger.info("[Serveo] Reconnecting in %ss...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _connect(self, subdomain):
        if self._use_subprocess:
            return self._connect_subprocess(subdomain)
        return self._connect_paramiko(subdomain)

    def _connect_subprocess(self, subdomain):
        proc = _ssh_connect(self._key_path, self._port, subdomain)
        url_found = False
        taken = False
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    logger.debug("[Serveo] >> %s", line)
                low = line.lower()
                if any(x in low for x in ("taken", "in use", "permission denied", "refused")) and subdomain:
                    taken = True
                    break
                m = re.search(r"(https://[\w\-]+\.serveo\.net)", line)
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
            return "taken"
        return "ok" if url_found else "error"

    def _connect_paramiko(self, subdomain):
        try:
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pkey = paramiko.Ed25519Key(filename=self._key_path)
            client.connect("serveo.net", port=22, username="serveo", pkey=pkey)
            transport = client.get_transport()
            name = subdomain if subdomain else ""
            remote_port = transport.request_port_forward(name, 80)
            url = "https://{}.serveo.net".format(name) if name else "https://serveo.net:{}" .format(remote_port)
            self._set_url(url)
            while not self._stop_event.is_set() and transport.is_active():
                time.sleep(5)
            client.close()
            return "ok"
        except paramiko.AuthenticationException:
            logger.warning("[Serveo] paramiko auth failed for %s", subdomain)
            return "taken"
        except Exception as e:
            logger.error("[Serveo] paramiko error: %s", e)
            return "error"


TunnelManager = ServeoManager
