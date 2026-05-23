import logging
import os
import threading
import time

logger = logging.getLogger("freeapi")


class ServeoManager:
    """Permanent tunnel via ngrok Python SDK.

    Required env vars:
      NGROK_TOKEN  - authtoken from dashboard.ngrok.com
      NGROK_DOMAIN - static domain, e.g. myapp.ngrok-free.app
    """

    def __init__(self, port, name="", on_url=None):
        self._port = port
        self._on_url = on_url
        self._stop_event = threading.Event()
        self._url = None
        self._lock = threading.Lock()

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="tunnel-mgr")
        t.start()

    def stop(self):
        self._stop_event.set()

    @property
    def url(self):
        return self._url

    def _set_url(self, url):
        with self._lock:
            self._url = url
        logger.info("[Tunnel] URL active: %s", url)
        if self._on_url:
            try:
                self._on_url(url)
            except Exception as exc:
                logger.error("[Tunnel] on_url callback error: %s", exc)

    def _run(self):
        token = os.environ.get("NGROK_TOKEN", "").strip()
        domain = os.environ.get("NGROK_DOMAIN", "").strip()

        if not token:
            logger.error("[Tunnel] NGROK_TOKEN not set — tunnel disabled.")
            logger.error("[Tunnel] Get free token at https://dashboard.ngrok.com")
            return

        backoff = 5
        while not self._stop_event.is_set():
            logger.info("[Tunnel] Starting ngrok (domain=%s)...", domain or "random")
            result = self._connect(token, domain)
            if not self._stop_event.is_set():
                logger.info("[Tunnel] Reconnecting in %ss...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                if result == "ok":
                    backoff = 5

    def _connect(self, token, domain):
        try:
            import ngrok
            kwargs = dict(authtoken=token)
            if domain:
                kwargs["domain"] = domain
            listener = ngrok.forward(self._port, **kwargs)
            url = listener.url()
            self._set_url(url)
            logger.info("[Tunnel] ngrok connected: %s", url)
            while not self._stop_event.is_set():
                time.sleep(5)
            try:
                ngrok.disconnect(url)
            except Exception:
                pass
            return "ok"
        except Exception as e:
            logger.error("[Tunnel] ngrok error: %s", e)
            return "error"


TunnelManager = ServeoManager
