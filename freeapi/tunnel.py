import logging
import os
import re
import threading
import time

logger = logging.getLogger("freeapi")

DEFAULT_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "serveo_key"
)


def _ensure_key(key_path):
    pub_path = key_path + ".pub"
    if os.path.exists(key_path) and os.path.exists(pub_path):
        logger.info("[Serveo] Using existing key: %s", key_path)
        return True
    os.makedirs(os.path.dirname(os.path.abspath(key_path)) or ".", exist_ok=True)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, PublicFormat, NoEncryption
        )
        priv_key = Ed25519PrivateKey.generate()
        priv_pem = priv_key.private_bytes(
            Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()
        )
        with open(key_path, "wb") as f:
            f.write(priv_pem)
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
        pub_bytes = priv_key.public_key().public_bytes(
            Encoding.OpenSSH, PublicFormat.OpenSSH
        )
        with open(pub_path, "wb") as f:
            f.write(pub_bytes + b"\n")
        logger.info("[Serveo] SSH key pair generated via Python cryptography")
        return True
    except Exception as e:
        logger.error("[Serveo] Failed to generate key: %s", e)
        return False


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

        attempts = [self._primary_name, None]
        attempt_idx = 0
        backoff = 5

        while not self._stop_event.is_set():
            subdomain = attempts[attempt_idx % len(attempts)]
            label = "{}.serveo.net".format(subdomain) if subdomain else "serveo.net (random)"
            logger.info("[Serveo] Connecting via paramiko: %s", label)
            result = self._connect_paramiko(subdomain)

            if result == "taken" and subdomain is not None:
                logger.warning("[Serveo] %s taken, switching to random", subdomain)
                attempt_idx = 1
                time.sleep(2)
                continue

            if not self._stop_event.is_set():
                logger.info("[Serveo] Reconnecting in %ss...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                if result == "ok":
                    backoff = 5

    def _connect_paramiko(self, subdomain):
        try:
            import paramiko
            import socket
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pkey = paramiko.Ed25519Key(filename=self._key_path)
            logger.info("[Serveo] paramiko connecting to serveo.net:22")
            client.connect(
                "serveo.net",
                port=22,
                username="serveo",
                pkey=pkey,
                look_for_keys=False,
                allow_agent=False,
                timeout=30,
            )
            transport = client.get_transport()
            name = subdomain if subdomain else ""
            logger.info("[Serveo] Requesting port forward for %r", name)
            remote_port = transport.request_port_forward(name, 80)
            if name:
                url = "https://{}.serveo.net".format(name)
            else:
                url = "https://serveo.net:{}".format(remote_port)
            self._set_url(url)
            while not self._stop_event.is_set() and transport.is_active():
                time.sleep(5)
            client.close()
            return "ok"
        except paramiko.AuthenticationException as e:
            logger.warning("[Serveo] Auth failed for %r: %s", subdomain, e)
            return "taken"
        except Exception as e:
            logger.error("[Serveo] paramiko error: %s", e)
            return "error"


TunnelManager = ServeoManager
