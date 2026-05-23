import logging
import re
import threading
import time

logger = logging.getLogger("freeapi")

_HOST = "localhost.run"
_PORT = 22
_USER = "nokey"


class ServeoManager:
    """Tunnel via localhost.run (no key needed, pure paramiko)."""

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
        backoff = 5
        while not self._stop_event.is_set():
            logger.info("[Tunnel] Connecting to localhost.run...")
            result = self._connect()
            if not self._stop_event.is_set():
                logger.info("[Tunnel] Reconnecting in %ss...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                if result == "ok":
                    backoff = 5

    def _connect(self):
        import paramiko
        transport = None
        try:
            transport = paramiko.Transport((_HOST, _PORT))
            transport.connect()
            # localhost.run accepts auth_none
            try:
                transport.auth_none(_USER)
            except paramiko.AuthenticationException:
                pass
            if not transport.is_authenticated():
                logger.error("[Tunnel] localhost.run auth failed")
                return "error"
            logger.info("[Tunnel] Connected, requesting port forward...")
            # stdout channel to read the URL banner
            chan = transport.open_session()
            # Request remote forward: 0 = let server pick port
            transport.request_port_forward("", 80)
            # Read the welcome message / URL from interactive channel
            chan2 = transport.open_session()
            chan2.exec_command("")
            # Actually, localhost.run sends URL as SSH banner
            # We need to read from the transport directly
            # Use a simpler approach: exec a shell-less session
            # The URL appears in the connection banner
            url_found = False
            # Read banner from transport
            banner = transport.get_banner()
            if banner:
                logger.info("[Tunnel] Banner: %s", banner.decode(errors="replace"))
                m = re.search(r"https://[\w\-]+\.lhr\.life", banner.decode(errors="replace"))
                if m:
                    self._set_url(m.group(0))
                    url_found = True
            # Keep alive while connected
            wait_count = 0
            while not self._stop_event.is_set() and transport.is_active():
                time.sleep(5)
                wait_count += 1
                if wait_count % 12 == 0:
                    logger.debug("[Tunnel] still alive")
            return "ok" if url_found else "error"
        except Exception as e:
            logger.error("[Tunnel] error: %s", e)
            return "error"
        finally:
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass


TunnelManager = ServeoManager
